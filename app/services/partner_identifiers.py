"""Kennungen von Geschäftspartnern in Fremdtexten: IBAN und USt-IdNr.

Belege, Kontoauszüge und E-Rechnungen liefern diese Angaben ungeprüft. Eine ungültige
Angabe zählt hier als „nicht vorhanden“, sie darf weder einen Import abbrechen noch einen
Partner treffen. Die strengen Prüfungen beim Anlegen bleiben in ``partners`` und ``sepa_export``.
"""

from __future__ import annotations

import re

# Länge je Land: Ohne sie wäre in „DE89…3000BIC+COBADEFF“ oder „DE89…3000Muster GmbH“ nicht
# erkennbar, wo die IBAN endet.
IBAN_LENGTHS = {
    "AD": 24,
    "AT": 20,
    "BE": 16,
    "BG": 22,
    "CH": 21,
    "CY": 28,
    "CZ": 24,
    "DE": 22,
    "DK": 18,
    "EE": 20,
    "ES": 24,
    "FI": 18,
    "FR": 27,
    "GB": 22,
    "GI": 23,
    "GR": 27,
    "HR": 21,
    "HU": 28,
    "IE": 22,
    "IS": 26,
    "IT": 27,
    "LI": 21,
    "LT": 20,
    "LU": 20,
    "LV": 21,
    "MC": 27,
    "MT": 31,
    "NL": 18,
    "NO": 15,
    "PL": 28,
    "PT": 25,
    "RO": 24,
    "SE": 24,
    "SI": 19,
    "SK": 24,
    "SM": 27,
    "VA": 22,
}

_IBAN_START_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{2}) ?(\d{2})(?=[ A-Z0-9])")
_DE_VAT_RE = re.compile(r"(?<![A-Z0-9])DE[ .]?(\d{3})[ .]?(\d{3})[ .]?(\d{3})(?![0-9])")
_LABELLED_VAT_RE = re.compile(
    r"(?<![A-Za-zÄÖÜäöüß])"
    r"(?:USt[-. ]?Id(?:ent)?[-. ]?(?:Nr|Nummer)?|UID(?:[-. ]?Nr)?|VAT(?:[- ]?(?:ID|No|Nr|Reg))?"
    r"|TVA|BTW)[.:# ]*(?:Nr\.?:? ?)?([A-Z]{2})[ .]?([0-9A-Z+*]{8,12})(?![0-9A-Za-z])",
    re.IGNORECASE,
)
_LABELLED_TAX_NUMBER_RE = re.compile(
    # Ohne die Grenze zählte „Best.-Nr. 4500123456“ als Steuernummer.
    r"(?<![A-Za-zÄÖÜäöüß])(?:U(?:msatz)?)?(?:St\.?[- ]?Nr\.?|Steuer[- ]?(?:nummer|nr\.?))"
    r"[.: ]*(\d[\d/ ]{7,16}\d)",
    re.IGNORECASE,
)


def _iban_checksum_ok(iban: str) -> bool:
    try:
        numeric = int("".join(str(int(char, 36)) for char in iban[4:] + iban[:4]))
    except ValueError:
        return False
    return numeric % 97 == 1


def clean_iban(value: str | None) -> str | None:
    """Normalisierte IBAN, sonst ``None`` (Länge je Land und Prüfsumme müssen stimmen)."""
    iban = re.sub(r"[\s.-]", "", value or "").upper()
    if len(iban) < 15 or len(iban) > 34 or not iban[:2].isalpha() or not iban[2:4].isdigit():
        return None
    expected = IBAN_LENGTHS.get(iban[:2])
    if expected is not None and len(iban) != expected:
        return None
    return iban if _iban_checksum_ok(iban) else None


def leading_iban(value: str | None) -> str | None:
    """IBAN am Anfang eines Textes, an den weitere Angaben angehängt sein können."""
    compact = re.sub(r"\s", "", value or "").upper()
    length = IBAN_LENGTHS.get(compact[:2])
    return clean_iban(compact[:length]) if length else None


def find_ibans(text: str | None) -> list[str]:
    """Alle gültigen IBANs eines Textes, auch in Vierergruppen, in Reihenfolge des Auftretens."""
    found: list[str] = []
    text = text or ""
    for match in _IBAN_START_RE.finditer(text):
        length = IBAN_LENGTHS.get(match.group(1))
        if length is None:
            continue
        compact = re.sub(r" ", "", text[match.start() : match.start() + length + length // 2])
        iban = clean_iban(compact[:length])
        if iban and iban not in found:
            found.append(iban)
    return found


def clean_vat_id(value: str | None) -> str | None:
    """USt-IdNr. ohne Trennzeichen in Großbuchstaben, ohne Formatprüfung je Land."""
    vat_id = re.sub(r"[\s.-]", "", value or "").upper()
    return vat_id if len(vat_id) >= 4 and vat_id[:2].isalpha() else None


def find_vat_ids(text: str | None) -> list[str]:
    """USt-IdNrn. eines Textes: deutsche am Muster, ausländische nur hinter ihrer Bezeichnung."""
    found: list[str] = []
    text = text or ""
    for match in _DE_VAT_RE.finditer(text):
        vat_id = "DE" + "".join(match.groups())
        if vat_id not in found:
            found.append(vat_id)
    for match in _LABELLED_VAT_RE.finditer(text):
        vat_id = clean_vat_id(match.group(1) + match.group(2))
        # Ein Wort hinter der Bezeichnung ist keine Kennung, eine Ziffer muss vorkommen.
        if vat_id and any(char.isdigit() for char in vat_id) and vat_id not in found:
            found.append(vat_id)
    return found


def clean_tax_number(value: str | None) -> str | None:
    """Steuernummer nur aus Ziffern: Schreibweisen mit Schrägstrich bleiben vergleichbar."""
    digits = re.sub(r"\D", "", value or "")
    return digits if len(digits) >= 8 else None


def find_tax_numbers(text: str | None) -> list[str]:
    """Steuernummern hinter ihrer Bezeichnung, z. B. ``St.-Nr. 123/456/78901``."""
    numbers = (clean_tax_number(value) for value in _LABELLED_TAX_NUMBER_RE.findall(text or ""))
    return list(dict.fromkeys(number for number in numbers if number))


def clean_bic(value: str | None) -> str | None:
    """BIC in Großbuchstaben, sonst ``None``."""
    bic = re.sub(r"\s", "", value or "").upper()
    return bic if len(bic) in (8, 11) and bic.isalnum() else None
