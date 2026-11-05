"""Partnererkennung in automatischen Quellen (E-Rechnung, Beleg-OCR, Bankumsatz).

Das Ergebnis ist entweder eine eindeutige Zuordnung oder ein Vorschlag zum Bestätigen:

* ``unique``: Eine harte Kennung (USt-IdNr., Steuernummer, IBAN, ausdrücklich genannte
  Personenkontonummer) trifft genau einen aktiven Partner, ohne Widerspruch. Nur dann ist
  :attr:`PartnerMatch.partner` gesetzt und darf eine Buchung ihn ohne Rückfrage tragen.
* ``uncertain``: Genau ein Kandidat, aber nur über den Namen, über eine Nummer aus Freitext,
  mit falscher Rolle oder mit einer widersprechenden Kennung.
* ``ambiguous``: Mehrere Kandidaten, etwa zwei Partner mit derselben IBAN oder USt-IdNr. des
  einen und IBAN des anderen. Eine präparierte Rechnung soll hier nicht durchrutschen.
* ``none``: Kein Kandidat; die Oberflächen bieten „Lieferant anlegen“ an.

Gesucht wird immer nur in einer Gesellschaft (Mandantentrennung), inaktive Partner werden nie
zugeordnet. Eigene Kennungen der Gesellschaft zählen nicht: Auf einer Eingangsrechnung steht
oft die USt-IdNr. des Empfängers, und ein Partner mit dieser Nummer wäre nicht der Absender.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.partner_identifiers import clean_iban, clean_tax_number, clean_vat_id
from app.services.partners import NUMBER_FIELDS, ROLE_LABELS, partner_display_number
from domain.models import (
    SUBLEDGER_CREDITOR,
    SUBLEDGER_DEBTOR,
    BusinessPartner,
    Company,
    FinTSConnection,
    OpenBankingConnection,
)

STATUS_UNIQUE = "unique"
STATUS_UNCERTAIN = "uncertain"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_NONE = "none"

MATCH_VAT_ID = "vat_id"
MATCH_TAX_NUMBER = "tax_number"
MATCH_IBAN = "iban"
MATCH_PARTNER_NUMBER = "partner_number"
MATCH_NUMBER_HINT = "partner_number_hint"
MATCH_NAME = "name"

MATCH_LABELS = {
    MATCH_VAT_ID: "USt-IdNr.",
    MATCH_TAX_NUMBER: "Steuernummer",
    MATCH_IBAN: "IBAN",
    MATCH_PARTNER_NUMBER: "Partnernummer",
    MATCH_NUMBER_HINT: "Partnernummer im Text",
    MATCH_NAME: "Name",
}

MAX_CANDIDATES = 5

# Rechtsformzusätze und Füllwörter, die beim Namensvergleich nicht zählen.
NAME_NOISE = {
    "gmbh",
    "ag",
    "kg",
    "ug",
    "ohg",
    "gbr",
    "ek",
    "mbh",
    "co",
    "kgaa",
    "ltd",
    "llc",
    "inc",
    "plc",
    "pbc",
    "opco",
    "sarl",
    "sa",
    "se",
    "nv",
    "bv",
    "the",
    "und",
    "and",
    "international",
    "distribution",
    "deutschland",
    "germany",
    "europe",
    "emea",
}

# Partnernummer im Verwendungszweck, nur hinter einer Bezeichnung: fünfstellige Zahlen kommen in
# Freitexten auch sonst vor.
_NUMBER_HINT_RE = re.compile(
    r"(?:kunden|kd|debitor(?:en)?|kreditor(?:en)?|lieferanten|lief|partner)"
    r"[-. ]*(?:nr|nummer|no)?[.:# ]*(\d{5,6})(?!\d)",
    re.IGNORECASE,
)


def name_tokens(value: str | None) -> set[str]:
    """Namensbestandteile ohne Rechtsform; ``ü`` und ``ue`` gelten als gleich."""
    text = (value or "").casefold()
    for umlaut, replacement in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(umlaut, replacement)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text)
        if token not in NAME_NOISE and len(token) > 1
    }


def number_hints(text: str | None) -> tuple[str, ...]:
    """Partnernummern, die ein Freitext ausdrücklich als solche bezeichnet."""
    return tuple(dict.fromkeys(_NUMBER_HINT_RE.findall(text or "")))


@dataclass(frozen=True, slots=True)
class PartnerSignals:
    """Was eine Quelle über ihren Partner weiß. Mehrere Werte je Kennung sind möglich."""

    vat_ids: tuple[str, ...] = ()
    ibans: tuple[str, ...] = ()
    tax_numbers: tuple[str, ...] = ()
    # Ausdrücklich angegebene Personenkontonummer (REST/MCP); gilt als harte Kennung.
    partner_numbers: tuple[str, ...] = ()
    # Nummer aus Freitext oder dem Lieferantenfeld einer Rechnung; bleibt ein Vorschlag.
    number_hints: tuple[str, ...] = ()
    name: str | None = None


@dataclass(frozen=True, slots=True)
class PartnerCandidate:
    partner: BusinessPartner
    matched_on: tuple[str, ...]
    role_fits: bool = True

    def as_dict(self) -> dict[str, Any]:
        partner = self.partner
        return {
            "partner_id": partner.id,
            "partner_name": partner.name,
            "partner_number": partner_display_number(partner),
            "is_customer": partner.debtor_number is not None,
            "is_supplier": partner.creditor_number is not None,
            "matched_on": list(self.matched_on),
            "matched_on_labels": [MATCH_LABELS[kind] for kind in self.matched_on],
            "role_fits": self.role_fits,
        }


@dataclass(frozen=True, slots=True)
class PartnerMatch:
    status: str
    reason: str
    # Nur bei ``unique`` gesetzt: Alles andere ist ein Vorschlag und wird nie still zugeordnet.
    partner: BusinessPartner | None = None
    candidates: tuple[PartnerCandidate, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def needs_confirmation(self) -> bool:
        return self.status in {STATUS_UNCERTAIN, STATUS_AMBIGUOUS}

    @property
    def matched_on(self) -> tuple[str, ...]:
        return self.candidates[0].matched_on if self.candidates else ()

    @property
    def matched_on_labels(self) -> list[str]:
        return [MATCH_LABELS[kind] for kind in self.matched_on]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "needs_confirmation": self.needs_confirmation,
            "reason": self.reason,
            "partner_id": self.partner.id if self.partner else None,
            "partner_name": self.partner.name if self.partner else None,
            "partner_number": partner_display_number(self.partner) if self.partner else None,
            "matched_on": list(self.matched_on) if self.partner else [],
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "warnings": list(self.warnings),
        }


NO_MATCH = PartnerMatch(status=STATUS_NONE, reason="Kein passender Geschäftspartner gefunden.")


def _has_role(partner: BusinessPartner, role: str | None) -> bool:
    return role is None or getattr(partner, NUMBER_FIELDS[role]) is not None


def _label(partner: BusinessPartner) -> str:
    return f"{partner_display_number(partner)} {partner.name}".strip()


def _kinds_text(kinds: tuple[str, ...]) -> str:
    return ", ".join(MATCH_LABELS[kind] for kind in kinds)


class PartnerDirectory:
    """Partner einer Gesellschaft mit Suchindizes; ein Aufbau genügt für viele Umsätze."""

    def __init__(
        self,
        partners: list[BusinessPartner],
        own_vat_ids: set[str] | None = None,
        own_ibans: set[str] | None = None,
    ) -> None:
        self.own_vat_ids = own_vat_ids or set()
        self.own_ibans = own_ibans or set()
        self.active = [partner for partner in partners if partner.is_active]
        self.inactive = [partner for partner in partners if not partner.is_active]
        self.by_vat_id = self._index(self.active, lambda p: clean_vat_id(p.vat_id))
        self.by_tax_number = self._index(self.active, lambda p: clean_tax_number(p.tax_number))
        # Altwerte im Stamm ohne gültige Prüfsumme treffen nur sich selbst.
        self.by_iban = self._index(self.active, lambda p: _compact(p.iban))
        self.by_number: dict[str, list[BusinessPartner]] = {}
        for partner in self.active:
            for number in (partner.debtor_number, partner.creditor_number):
                if number:
                    self.by_number.setdefault(number, []).append(partner)
        self.name_index = [(partner, name_tokens(partner.name)) for partner in self.active]

    @staticmethod
    def _index(partners, key) -> dict[str, list[BusinessPartner]]:
        index: dict[str, list[BusinessPartner]] = {}
        for partner in partners:
            value = key(partner)
            if value:
                index.setdefault(value, []).append(partner)
        return index

    def _hard_hits(self, signals: PartnerSignals) -> dict[int, tuple[BusinessPartner, list[str]]]:
        hits: dict[int, tuple[BusinessPartner, list[str]]] = {}
        for kind, values, index in (
            (MATCH_VAT_ID, self.foreign_vat_ids(signals), self.by_vat_id),
            (
                MATCH_TAX_NUMBER,
                _clean_all(signals.tax_numbers, clean_tax_number),
                self.by_tax_number,
            ),
            (MATCH_IBAN, self.foreign_ibans(signals), self.by_iban),
            (MATCH_PARTNER_NUMBER, _clean_all(signals.partner_numbers, _stripped), self.by_number),
        ):
            for value in values:
                for partner in index.get(value, ()):
                    kinds = hits.setdefault(partner.id, (partner, []))[1]
                    if kind not in kinds:
                        kinds.append(kind)
        return hits

    def foreign_vat_ids(self, signals: PartnerSignals) -> list[str]:
        return [
            value
            for value in _clean_all(signals.vat_ids, clean_vat_id)
            if value not in self.own_vat_ids
        ]

    def foreign_ibans(self, signals: PartnerSignals) -> list[str]:
        return [
            value for value in _clean_all(signals.ibans, _compact) if value not in self.own_ibans
        ]

    def _weak_hits(self, signals: PartnerSignals) -> list[tuple[int, BusinessPartner, list[str]]]:
        """Kandidaten über Nummern aus Freitext und den Namen, stärkster zuerst."""
        scored: dict[int, tuple[int, BusinessPartner, list[str]]] = {}
        for number in _clean_all(signals.number_hints, _stripped):
            for partner in self.by_number.get(number, ()):
                scored[partner.id] = (100, partner, [MATCH_NUMBER_HINT])
        wanted = name_tokens(signals.name)
        if wanted:
            for partner, tokens in self.name_index:
                score = _name_score(wanted, tokens)
                if score == 0:
                    continue
                previous = scored.get(partner.id)
                if previous is None:
                    scored[partner.id] = (score, partner, [MATCH_NAME])
                else:
                    scored[partner.id] = (previous[0] + score, partner, [*previous[2], MATCH_NAME])
        return sorted(scored.values(), key=lambda item: (-item[0], item[1].name, item[1].id))

    def match(
        self,
        signals: PartnerSignals,
        *,
        role: str | None = None,
        preferred_role: str | None = None,
    ) -> PartnerMatch:
        """Ordnet Signale einem Partner zu.

        ``role`` ist durch die Quelle festgelegt: Eine Eingangsrechnung kommt von einem
        Lieferanten. ``preferred_role`` sortiert nur: Ein Zahlungseingang kommt meist vom
        Kunden, kann aber auch die Erstattung eines Lieferanten sein.
        """
        hits = self._hard_hits(signals)
        if hits:
            return self._match_hard(signals, hits, role=role, preferred_role=preferred_role)
        return self._match_weak(signals, role=role, preferred_role=preferred_role)

    def _match_hard(
        self,
        signals: PartnerSignals,
        hits: dict[int, tuple[BusinessPartner, list[str]]],
        *,
        role: str | None,
        preferred_role: str | None,
    ) -> PartnerMatch:
        fitting = {pid: hit for pid, hit in hits.items() if _has_role(hit[0], role)}
        pool = fitting or hits
        kinds_found = {kind for _partner, kinds in pool.values() for kind in kinds}
        # Wer alle getroffenen Kennungsarten erfüllt, schlägt Partner mit nur einem Teil davon:
        # USt-IdNr. und IBAN bei A, nur die IBAN bei B. USt-IdNr. bei A und IBAN bei B bleibt offen.
        complete = {pid: hit for pid, hit in pool.items() if set(hit[1]) == kinds_found}
        candidates = _sorted_candidates(hits, role=role, preferred_role=preferred_role)

        if len(complete) != 1:
            names = ", ".join(_label(candidate.partner) for candidate in candidates)
            return PartnerMatch(
                status=STATUS_AMBIGUOUS,
                reason=f"Mehrere Geschäftspartner passen ({names}). Bitte auswählen.",
                candidates=candidates,
            )

        partner, kinds = next(iter(complete.values()))
        winner = next(c for c in candidates if c.partner.id == partner.id)
        ordered = (winner, *(c for c in candidates if c.partner.id != partner.id))
        kinds_text = _kinds_text(winner.matched_on)
        warnings = self.identifier_warnings(signals, partner)
        if not fitting:
            return PartnerMatch(
                status=STATUS_UNCERTAIN,
                reason=(
                    f"{_label(partner)} passt über {kinds_text}, ist aber bisher kein "
                    f"{ROLE_LABELS[role]}. Bitte prüfen und die Rolle im Partnerstamm ergänzen."
                ),
                candidates=ordered,
                warnings=warnings,
            )
        contradiction = self._vat_contradiction(signals, partner, kinds)
        if contradiction:
            return PartnerMatch(
                status=STATUS_UNCERTAIN,
                reason=f"{_label(partner)} passt über {kinds_text}, aber: {contradiction}",
                candidates=ordered,
                warnings=warnings,
            )
        return PartnerMatch(
            status=STATUS_UNIQUE,
            reason=f"{_label(partner)} eindeutig erkannt über {kinds_text}.",
            partner=partner,
            candidates=ordered,
            warnings=warnings,
        )

    def _vat_contradiction(
        self, signals: PartnerSignals, partner: BusinessPartner, kinds: list[str]
    ) -> str | None:
        """Die USt-IdNr. identifiziert stärker als die IBAN: Passt sie nicht, ist es unsicher."""
        document_vat_ids = self.foreign_vat_ids(signals)
        partner_vat_id = clean_vat_id(partner.vat_id)
        if (
            MATCH_VAT_ID in kinds
            or not document_vat_ids
            or not partner_vat_id
            or partner_vat_id in document_vat_ids
        ):
            return None
        return (
            f"Die USt-IdNr. der Quelle ({document_vat_ids[0]}) weicht vom Partnerstamm "
            f"({partner_vat_id}) ab."
        )

    def identifier_warnings(
        self, signals: PartnerSignals, partner: BusinessPartner
    ) -> tuple[str, ...]:
        """Eine neue Bankverbindung kann echt sein oder Betrug: immer sichtbar machen."""
        document_ibans = self.foreign_ibans(signals)
        partner_iban = _compact(partner.iban)
        if document_ibans and partner_iban and partner_iban not in document_ibans:
            return (
                f"Die IBAN der Quelle ({document_ibans[0]}) weicht vom Partnerstamm "
                f"({partner_iban}) ab. Gezahlt wird auf die IBAN im Stamm; eine Änderung bitte "
                "beim Lieferanten prüfen.",
            )
        return ()

    def _match_weak(
        self,
        signals: PartnerSignals,
        *,
        role: str | None,
        preferred_role: str | None,
    ) -> PartnerMatch:
        weak = self._weak_hits(signals)
        if not weak:
            return self._no_match(signals)
        wanted_role = role or preferred_role
        # Stabil sortiert: Die passende Rolle steht vorn, sonst bleibt die Reihenfolge der Stärke.
        weak.sort(key=lambda item: not _has_role(item[1], wanted_role))
        candidates = tuple(
            PartnerCandidate(
                partner=partner,
                matched_on=tuple(kinds),
                role_fits=_has_role(partner, wanted_role),
            )
            for _score, partner, kinds in weak[:MAX_CANDIDATES]
        )
        if len(candidates) > 1:
            names = ", ".join(_label(candidate.partner) for candidate in candidates)
            return PartnerMatch(
                status=STATUS_AMBIGUOUS,
                reason=f"Mehrere Geschäftspartner kommen infrage ({names}). Bitte auswählen.",
                candidates=candidates,
            )
        candidate = candidates[0]
        return PartnerMatch(
            status=STATUS_UNCERTAIN,
            reason=(
                f"Vorschlag: {_label(candidate.partner)} (nur über "
                f"{_kinds_text(candidate.matched_on)}). Bitte bestätigen."
            ),
            candidates=candidates,
        )

    def _no_match(self, signals: PartnerSignals) -> PartnerMatch:
        vat_ids = self.foreign_vat_ids(signals)
        ibans = self.foreign_ibans(signals)
        for partner in self.inactive:
            if (partner.vat_id and clean_vat_id(partner.vat_id) in vat_ids) or (
                partner.iban and _compact(partner.iban) in ibans
            ):
                return PartnerMatch(
                    status=STATUS_NONE,
                    reason=NO_MATCH.reason,
                    warnings=(
                        f"{_label(partner)} passt, ist aber inaktiv: bitte reaktivieren statt "
                        "doppelt anlegen.",
                    ),
                )
        return NO_MATCH


def _stripped(value: str | None) -> str | None:
    return (value or "").strip() or None


def _compact(value: str | None) -> str | None:
    """IBAN für den Vergleich; die Gültigkeit prüft die Quelle (``clean_iban``)."""
    return re.sub(r"[\s.-]", "", value or "").upper() or None


def _clean_all(values: tuple[str, ...], clean) -> list[str]:
    return list(dict.fromkeys(value for value in map(clean, values) if value))


def _name_score(wanted: set[str], tokens: set[str]) -> int:
    """0 = passt nicht. Beide Richtungen, denn Bankauszüge kürzen Namen oft ab."""
    if not tokens:
        return 0
    overlap = wanted & tokens
    if tokens <= wanted:
        return 20 + 2 * len(overlap) + (10 if tokens == wanted else 0)
    if wanted <= tokens and (len(wanted) >= 2 or len(next(iter(wanted))) >= 4):
        return 10 + len(overlap)
    return 0


def _sorted_candidates(
    hits: dict[int, tuple[BusinessPartner, list[str]]],
    *,
    role: str | None,
    preferred_role: str | None,
) -> tuple[PartnerCandidate, ...]:
    wanted_role = role or preferred_role
    weight = {MATCH_VAT_ID: 8, MATCH_TAX_NUMBER: 4, MATCH_IBAN: 2, MATCH_PARTNER_NUMBER: 1}
    ranked = sorted(
        hits.values(),
        key=lambda hit: (
            not _has_role(hit[0], wanted_role),
            -sum(weight[kind] for kind in hit[1]),
            hit[0].name,
            hit[0].id,
        ),
    )
    return tuple(
        PartnerCandidate(
            partner=partner,
            matched_on=tuple(kinds),
            role_fits=_has_role(partner, wanted_role),
        )
        for partner, kinds in ranked[:MAX_CANDIDATES]
    )


def load_partner_directory(session: Session, company_id: int) -> PartnerDirectory:
    """Partner und eigene Kennungen einer Gesellschaft; nie gesellschaftsübergreifend."""
    partners = (
        session.execute(
            select(BusinessPartner)
            .where(BusinessPartner.company_id == company_id)
            .order_by(BusinessPartner.id)
        )
        .scalars()
        .all()
    )
    company = session.get(Company, company_id)
    own_ibans = {
        value
        for value in (
            company.iban if company else None,
            *session.execute(
                select(FinTSConnection.sepa_iban).where(FinTSConnection.company_id == company_id)
            ).scalars(),
            *session.execute(
                select(OpenBankingConnection.iban).where(
                    OpenBankingConnection.company_id == company_id
                )
            ).scalars(),
        )
        if value
    }
    own_vat_id = clean_vat_id(company.vat_id) if company else None
    return PartnerDirectory(
        list(partners),
        own_vat_ids={own_vat_id} if own_vat_id else set(),
        own_ibans={iban for iban in map(_compact, own_ibans) if iban},
    )


def recognize_partner(
    session: Session,
    company_id: int,
    signals: PartnerSignals,
    *,
    role: str | None = None,
    preferred_role: str | None = None,
) -> PartnerMatch:
    """Erkennt den Partner einer einzelnen Quelle; siehe :meth:`PartnerDirectory.match`."""
    directory = load_partner_directory(session, company_id)
    return directory.match(signals, role=role, preferred_role=preferred_role)


def role_for_amount(amount) -> str:
    """Erwartete Rolle eines Bankumsatzes: Eingang vom Kunden, Ausgang an den Lieferanten."""
    return SUBLEDGER_DEBTOR if amount > 0 else SUBLEDGER_CREDITOR


def transaction_signals(transaction) -> PartnerSignals:
    """Signale eines Bankumsatzes; eine IBAN vor dem Namen (MT940) zählt nicht zum Namen."""
    iban = transaction.counterparty_iban
    name = transaction.counterparty
    if iban and name and name.upper().startswith(iban):
        name = name[len(iban) :]
    return PartnerSignals(
        ibans=(iban,) if iban else (),
        number_hints=number_hints(transaction.purpose),
        name=name,
    )


def proposed_partner_data(
    *,
    name: str | None,
    vat_id: str | None = None,
    tax_number: str | None = None,
    street: str | None = None,
    postal_code: str | None = None,
    city: str | None = None,
    country_code: str | None = None,
    email: str | None = None,
    iban: str | None = None,
    bic: str | None = None,
) -> dict[str, Any]:
    """Vorbelegung für „Lieferant anlegen“; IBAN und BIC laufen über den Bankdaten-Weg."""
    return {
        "name": name or None,
        "is_supplier": True,
        "vat_id": clean_vat_id(vat_id),
        "tax_number": tax_number or None,
        "street": street or None,
        "postal_code": postal_code or None,
        "city": city or None,
        "country_code": (country_code or "DE").upper()[:2],
        "email": email or None,
        "iban": clean_iban(iban),
        "bic": bic or None,
    }
