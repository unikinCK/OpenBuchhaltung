"""Firmenwissen je Gesellschaft (ADR-004): Quellen, Auszüge, Wissenseinträge, Paketimport.

Firmenwissen ist zitierbar, nicht durchsetzbar: Obi findet es über ``search_knowledge``
(mit ``company_id``) und gewichtet freigegebene Einträge höher als Entwürfe. Importierte
und vorgeschlagene Einträge sind immer Entwürfe; freigeben (``approved``) kann nur ein
Mensch mit Schreibrolle (im KI-Chat gesperrt). Nichts wird gelöscht: Einträge werden
ausgemustert, Quellen und Auszüge deaktiviert, jede Änderung geht in die Audit-Kette.

Wissenspakete (``format: openbuchhaltung-firmenwissen``, ``version: 1``) werden je Paket
über stabile Schlüssel abgeglichen: neu → anlegen, geändert → aktualisieren (geänderter
Inhalt eines freigegebenen Eintrags → zurück auf Entwurf), entfallen → ausmustern bzw.
deaktivieren. ``dry_run`` liefert nur den Bericht.
"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from app.services.knowledge import (
    DEFAULT_SEARCH_LIMIT,
    MAX_SEARCH_LIMIT,
    KnowledgeBase,
    KnowledgeDocument,
    KnowledgeSection,
    search_knowledge,
)
from domain.models import (
    AuditLog,
    Company,
    CompanyKnowledgeEntry,
    CompanyKnowledgeExcerpt,
    CompanyKnowledgeSource,
)

PACKAGE_FORMAT = "openbuchhaltung-firmenwissen"
PACKAGE_VERSION = 1

STATUSES = ("draft", "approved", "retired")
STATUS_LABELS = {
    "draft": "Entwurf – nicht freigegeben",
    "approved": "freigegeben",
    "retired": "ausgemustert",
}
KINDS = ("knowledge", "open_question")
KIND_LABELS = {"knowledge": "Wissen", "open_question": "Offene Frage"}
AUTHORITIES = ("binding", "reference", "historical", "unverified")
AUTHORITY_LABELS = {
    "binding": "maßgeblich",
    "reference": "Orientierung",
    "historical": "historisch",
    "unverified": "ungeprüft",
}

MAX_SOURCES = 5_000
MAX_EXCERPTS = 50_000
MAX_ENTRIES = 10_000
MAX_TEXT_CHARS = 20_000
MAX_NOTE_CHARS = 4_000
MAX_LIST_LIMIT = 500
DEFAULT_LIST_LIMIT = 100
MAX_REPORTED_CHANGES = 500

# Prüfgründe, die der Import setzt (``review_reason``).
REASON_CONTENT_CHANGED = "Inhalt durch Import geändert"
REASON_SOURCE_CHANGED = "Quelle geändert"
REASON_SOURCE_GONE = "Quelle entfallen"
REASON_ENTRY_GONE = "Im Paket entfallen"
REASON_SOURCE_DEACTIVATED = "Quelle deaktiviert"
# Grund an der Quelle (``inactive_reason``): nur im Paket entfallene Quellen reaktiviert
# ein späterer Import, von Hand deaktivierte bleiben aus.
REASON_SOURCE_MANUAL = "Manuell deaktiviert"

# Freigegebene Einträge ranken in der Suche vor gleich guten Entwürfen.
APPROVED_SCORE_FACTOR = 1.5
SEARCH_CANDIDATES = 50
RESULT_TEXT_LIMIT = 3_000

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]*$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class CompanyKnowledgeError(ValueError):
    """Ungültige Eingabe oder unzulässiger Statuswechsel im Firmenwissen."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(*parts: str | None) -> str:
    joined = "\x1f".join(part or "" for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _entry_hash(title: str, content: str, locator: str | None) -> str:
    return _sha256(title, content, locator)


# ---------------------------------------------------------------------------
# Eingaben prüfen
# ---------------------------------------------------------------------------


def _text(
    value: Any, field_name: str, *, max_len: int, required: bool = False
) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise CompanyKnowledgeError(f"{field_name}: Angabe fehlt.")
        return None
    if not isinstance(value, str):
        raise CompanyKnowledgeError(f"{field_name}: Text erwartet.")
    cleaned = value.strip()
    if len(cleaned) > max_len:
        raise CompanyKnowledgeError(f"{field_name}: höchstens {max_len} Zeichen.")
    return cleaned


def _key(value: Any, field_name: str, *, max_len: int) -> str:
    cleaned = _text(value, field_name, max_len=max_len, required=True)
    assert cleaned is not None
    if not _KEY_RE.match(cleaned):
        raise CompanyKnowledgeError(
            f"{field_name}: nur Buchstaben, Ziffern und . _ : / - erlaubt."
        )
    return cleaned


def _date(value: Any, field_name: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise CompanyKnowledgeError(f"{field_name}: Datum im Format JJJJ-MM-TT erwartet.")
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError as exc:
        raise CompanyKnowledgeError(
            f"{field_name}: Datum im Format JJJJ-MM-TT erwartet."
        ) from exc


def _choice(value: Any, field_name: str, choices: tuple[str, ...], default: str) -> str:
    if value is None or value == "":
        return default
    if value not in choices:
        raise CompanyKnowledgeError(f"{field_name}: erlaubt sind {', '.join(choices)}.")
    return str(value)


def parse_date_arg(value: Any, field_name: str) -> date | None:
    """Datum aus Formular/JSON (leer = keins); für UI und API."""
    return _date(value, field_name)


# ---------------------------------------------------------------------------
# Darstellung
# ---------------------------------------------------------------------------


def entry_needs_review(entry: CompanyKnowledgeEntry, *, today: date | None = None) -> bool:
    """Zu prüfen: nicht ausgemustert und Entwurf, Prüfgrund oder Wiedervorlage erreicht."""
    if entry.status == "retired":
        return False
    today = today or date.today()
    return (
        entry.status == "draft"
        or bool(entry.review_reason)
        or (entry.review_due is not None and entry.review_due <= today)
    )


def entry_dict(entry: CompanyKnowledgeEntry, *, with_content: bool = True) -> dict[str, Any]:
    source = entry.source
    data: dict[str, Any] = {
        "id": entry.id,
        "company_id": entry.company_id,
        "entry_key": entry.entry_key,
        "kind": entry.kind,
        "kind_label": KIND_LABELS.get(entry.kind, entry.kind),
        "topic": entry.topic,
        "title": entry.title,
        "status": entry.status,
        "status_label": STATUS_LABELS.get(entry.status, entry.status),
        "source_id": entry.source_id,
        "source_title": source.title if source else None,
        "source_active": source.is_active if source else None,
        "locator": entry.locator,
        "review_due": entry.review_due.isoformat() if entry.review_due else None,
        "review_note": entry.review_note,
        "review_reason": entry.review_reason,
        "needs_review": entry_needs_review(entry),
        "package_key": entry.package_key,
        "proposed_by": entry.proposed_by,
        "approved_by": entry.approved_by,
        "approved_at": entry.approved_at.isoformat() if entry.approved_at else None,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
    }
    if with_content:
        data["content"] = entry.content
    return data


def excerpt_dict(excerpt: CompanyKnowledgeExcerpt) -> dict[str, Any]:
    return {
        "id": excerpt.id,
        "excerpt_key": excerpt.excerpt_key,
        "source_id": excerpt.source_id,
        "locator": excerpt.locator,
        "content": excerpt.content,
        "is_active": excerpt.is_active,
    }


def source_dict(
    source: CompanyKnowledgeSource,
    *,
    excerpt_count: int | None = None,
    entry_count: int | None = None,
    with_excerpts: bool = False,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": source.id,
        "company_id": source.company_id,
        "source_key": source.source_key,
        "title": source.title,
        "origin": source.origin,
        "content_sha256": source.content_sha256,
        "document_kind": source.document_kind,
        "source_version": source.source_version,
        "source_date": source.source_date.isoformat() if source.source_date else None,
        "valid_from": source.valid_from.isoformat() if source.valid_from else None,
        "valid_to": source.valid_to.isoformat() if source.valid_to else None,
        "authority": source.authority,
        "authority_label": AUTHORITY_LABELS.get(source.authority, source.authority),
        "note": source.note,
        "is_active": source.is_active,
        "inactive_reason": source.inactive_reason,
        "package_key": source.package_key,
        "package_revision": source.package_revision,
        "updated_at": source.updated_at.isoformat() if source.updated_at else None,
    }
    if excerpt_count is not None:
        data["excerpt_count"] = excerpt_count
    if entry_count is not None:
        data["entry_count"] = entry_count
    if with_excerpts:
        data["excerpts"] = [excerpt_dict(excerpt) for excerpt in source.excerpts]
    return data


# ---------------------------------------------------------------------------
# Lesen
# ---------------------------------------------------------------------------


def list_entries(
    *,
    session: Session,
    company_id: int,
    status: str | None = None,
    kind: str | None = None,
    due_only: bool = False,
    query: str | None = None,
    source_id: int | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> tuple[list[CompanyKnowledgeEntry], int]:
    """Einträge einer Gesellschaft; ``status`` None = alle außer ausgemusterten."""
    stmt = select(CompanyKnowledgeEntry).where(CompanyKnowledgeEntry.company_id == company_id)
    if status in (None, "", "active"):
        stmt = stmt.where(CompanyKnowledgeEntry.status != "retired")
    elif status == "all":
        pass
    elif status in STATUSES:
        stmt = stmt.where(CompanyKnowledgeEntry.status == status)
    else:
        raise CompanyKnowledgeError("status: erlaubt sind active, all, draft, approved, retired.")
    if kind:
        stmt = stmt.where(CompanyKnowledgeEntry.kind == _choice(kind, "kind", KINDS, "knowledge"))
    if source_id is not None:
        stmt = stmt.where(CompanyKnowledgeEntry.source_id == source_id)
    if query and query.strip():
        pattern = f"%{query.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(CompanyKnowledgeEntry.title).like(pattern),
                func.lower(CompanyKnowledgeEntry.content).like(pattern),
                func.lower(CompanyKnowledgeEntry.topic).like(pattern),
                func.lower(CompanyKnowledgeEntry.entry_key).like(pattern),
            )
        )
    if due_only:
        stmt = stmt.where(
            CompanyKnowledgeEntry.status != "retired",
            or_(
                CompanyKnowledgeEntry.status == "draft",
                CompanyKnowledgeEntry.review_reason.is_not(None),
                CompanyKnowledgeEntry.review_due <= date.today(),
            ),
        )
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    limit = max(1, min(int(limit or DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT))
    rows = (
        session.execute(
            stmt.order_by(
                func.coalesce(CompanyKnowledgeEntry.topic, ""),
                CompanyKnowledgeEntry.title,
                CompanyKnowledgeEntry.id,
            )
            .limit(limit)
            .offset(max(0, int(offset or 0)))
        )
        .scalars()
        .all()
    )
    return list(rows), total


def get_entry(
    *, session: Session, entry_id: int, company_id: int | None = None
) -> CompanyKnowledgeEntry | None:
    entry = session.get(CompanyKnowledgeEntry, entry_id)
    if entry is None or (company_id is not None and entry.company_id != company_id):
        return None
    return entry


def list_sources(
    *,
    session: Session,
    company_id: int,
    include_inactive: bool = True,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Quellen mit Anzahl aktiver Auszüge und nicht ausgemusterter Einträge."""
    stmt = select(CompanyKnowledgeSource).where(
        CompanyKnowledgeSource.company_id == company_id
    )
    if not include_inactive:
        stmt = stmt.where(CompanyKnowledgeSource.is_active.is_(True))
    if query and query.strip():
        pattern = f"%{query.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(CompanyKnowledgeSource.title).like(pattern),
                func.lower(CompanyKnowledgeSource.source_key).like(pattern),
            )
        )
    sources = session.execute(
        stmt.order_by(CompanyKnowledgeSource.title, CompanyKnowledgeSource.id)
    ).scalars().all()
    excerpt_counts = dict(
        session.execute(
            select(CompanyKnowledgeExcerpt.source_id, func.count())
            .where(
                CompanyKnowledgeExcerpt.company_id == company_id,
                CompanyKnowledgeExcerpt.is_active.is_(True),
            )
            .group_by(CompanyKnowledgeExcerpt.source_id)
        ).all()
    )
    entry_counts = dict(
        session.execute(
            select(CompanyKnowledgeEntry.source_id, func.count())
            .where(
                CompanyKnowledgeEntry.company_id == company_id,
                CompanyKnowledgeEntry.status != "retired",
                CompanyKnowledgeEntry.source_id.is_not(None),
            )
            .group_by(CompanyKnowledgeEntry.source_id)
        ).all()
    )
    return [
        source_dict(
            source,
            excerpt_count=excerpt_counts.get(source.id, 0),
            entry_count=entry_counts.get(source.id, 0),
        )
        for source in sources
    ]


def get_source(
    *, session: Session, source_id: int, company_id: int | None = None
) -> CompanyKnowledgeSource | None:
    source = session.get(CompanyKnowledgeSource, source_id)
    if source is None or (company_id is not None and source.company_id != company_id):
        return None
    return source


def company_knowledge_summary(*, session: Session, company_id: int) -> dict[str, int]:
    """Kennzahlen für Systemprompt und Seitenkopf."""
    counts = dict(
        session.execute(
            select(CompanyKnowledgeEntry.status, func.count())
            .where(CompanyKnowledgeEntry.company_id == company_id)
            .group_by(CompanyKnowledgeEntry.status)
        ).all()
    )
    _entries, due = list_entries(session=session, company_id=company_id, due_only=True, limit=1)
    sources = session.execute(
        select(func.count()).where(
            CompanyKnowledgeSource.company_id == company_id,
            CompanyKnowledgeSource.is_active.is_(True),
        )
    ).scalar_one()
    return {
        "approved": int(counts.get("approved", 0)),
        "drafts": int(counts.get("draft", 0)),
        "retired": int(counts.get("retired", 0)),
        "due": int(due),
        "sources": int(sources),
    }


# ---------------------------------------------------------------------------
# Einträge anlegen, ändern, Status
# ---------------------------------------------------------------------------


def _resolve_source(
    session: Session, company_id: int, source_id: int | None
) -> CompanyKnowledgeSource | None:
    if source_id is None:
        return None
    source = get_source(session=session, source_id=source_id, company_id=company_id)
    if source is None:
        raise CompanyKnowledgeError("Quelle nicht gefunden.")
    return source


def _audit_entry(
    session: Session,
    entry: CompanyKnowledgeEntry,
    *,
    action: str,
    changed_by: str,
    payload: dict[str, Any],
) -> None:
    log_audit_event(
        session=session,
        tenant_id=entry.tenant_id,
        company_id=entry.company_id,
        entity_type="company_knowledge_entry",
        entity_id=str(entry.id),
        action=action,
        changed_by=changed_by,
        payload={"entry_key": entry.entry_key, **payload},
    )


def _audit_snapshot(entry: CompanyKnowledgeEntry) -> dict[str, Any]:
    return {
        "kind": entry.kind,
        "topic": entry.topic,
        "title": entry.title,
        "content_sha256": entry.content_sha256,
        "source_id": entry.source_id,
        "locator": entry.locator,
        "status": entry.status,
        "review_due": entry.review_due.isoformat() if entry.review_due else None,
        "review_reason": entry.review_reason,
    }


def create_entry(
    *,
    session: Session,
    company: Company,
    title: Any,
    content: Any,
    changed_by: str,
    kind: Any = None,
    topic: Any = None,
    source_id: int | None = None,
    locator: Any = None,
    review_due: date | None = None,
    review_note: Any = None,
    entry_key: Any = None,
) -> CompanyKnowledgeEntry:
    """Legt einen Eintrag als Entwurf an (auch wenn Obi ihn vorschlägt)."""
    cleaned_title = _text(title, "title", max_len=255, required=True)
    cleaned_content = _text(content, "content", max_len=MAX_TEXT_CHARS, required=True)
    assert cleaned_title is not None and cleaned_content is not None
    cleaned_locator = _text(locator, "locator", max_len=255)
    key = (
        _key(entry_key, "entry_key", max_len=120)
        if entry_key not in (None, "")
        else f"manuell-{uuid.uuid4().hex[:12]}"
    )
    duplicate = session.execute(
        select(CompanyKnowledgeEntry.id).where(
            CompanyKnowledgeEntry.company_id == company.id,
            CompanyKnowledgeEntry.entry_key == key,
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise CompanyKnowledgeError(f"Der Schlüssel „{key}“ ist bereits vergeben.")
    source = _resolve_source(session, company.id, source_id)
    entry = CompanyKnowledgeEntry(
        tenant_id=company.tenant_id,
        company_id=company.id,
        entry_key=key,
        kind=_choice(kind, "kind", KINDS, "knowledge"),
        topic=_text(topic, "topic", max_len=80),
        title=cleaned_title,
        content=cleaned_content,
        source_id=source.id if source else None,
        locator=cleaned_locator,
        status="draft",
        review_due=review_due,
        review_note=_text(review_note, "review_note", max_len=MAX_NOTE_CHARS),
        content_sha256=_entry_hash(cleaned_title, cleaned_content, cleaned_locator),
        proposed_by=changed_by,
    )
    session.add(entry)
    session.flush()
    _audit_entry(
        session, entry, action="created", changed_by=changed_by, payload=_audit_snapshot(entry)
    )
    return entry


_UNSET: Any = object()


def update_entry(
    *,
    session: Session,
    entry: CompanyKnowledgeEntry,
    changed_by: str,
    title: Any = _UNSET,
    content: Any = _UNSET,
    kind: Any = _UNSET,
    topic: Any = _UNSET,
    source_id: Any = _UNSET,
    locator: Any = _UNSET,
    review_due: Any = _UNSET,
    review_note: Any = _UNSET,
) -> bool:
    """Ändert einen Eintrag. Neuer Titel/Inhalt/Fundstelle/Quelle → wieder Entwurf.

    Gibt zurück, ob sich etwas geändert hat.
    """
    if entry.status == "retired":
        raise CompanyKnowledgeError(
            "Ausgemusterte Einträge zuerst wieder als Entwurf aktivieren."
        )
    before = _audit_snapshot(entry)
    before_note = entry.review_note
    new_title = entry.title
    new_content = entry.content
    new_locator = entry.locator
    if title is not _UNSET:
        new_title = _text(title, "title", max_len=255, required=True) or entry.title
    if content is not _UNSET:
        new_content = _text(content, "content", max_len=MAX_TEXT_CHARS, required=True) or (
            entry.content
        )
    if locator is not _UNSET:
        new_locator = _text(locator, "locator", max_len=255)
    new_source_id = entry.source_id
    if source_id is not _UNSET:
        source = _resolve_source(session, entry.company_id, source_id)
        new_source_id = source.id if source else None

    new_hash = _entry_hash(new_title, new_content, new_locator)
    substantive = new_hash != entry.content_sha256 or new_source_id != entry.source_id
    entry.title = new_title
    entry.content = new_content
    entry.locator = new_locator
    entry.source_id = new_source_id
    entry.content_sha256 = new_hash
    if kind is not _UNSET:
        entry.kind = _choice(kind, "kind", KINDS, entry.kind)
    if topic is not _UNSET:
        entry.topic = _text(topic, "topic", max_len=80)
    if review_due is not _UNSET:
        entry.review_due = review_due
    if review_note is not _UNSET:
        entry.review_note = _text(review_note, "review_note", max_len=MAX_NOTE_CHARS)

    after = _audit_snapshot(entry)
    if after == before and entry.review_note == before_note:
        return False
    reset = substantive and entry.status == "approved"
    if reset:
        entry.status = "draft"
        entry.approved_by = None
        entry.approved_at = None
        after = _audit_snapshot(entry)
    entry.updated_at = _now()
    _audit_entry(
        session,
        entry,
        action="updated",
        changed_by=changed_by,
        payload={"before": before, "after": after, "approval_reset": reset},
    )
    return True


def set_entry_status(
    *, session: Session, entry: CompanyKnowledgeEntry, status: str, changed_by: str
) -> CompanyKnowledgeEntry:
    """Freigeben, zurück auf Entwurf oder ausmustern.

    Ein freigegebener Eintrag mit Prüfgrund lässt sich erneut freigeben; das bestätigt
    ihn nach der Prüfung und löscht den Prüfgrund.
    """
    if status not in STATUSES:
        raise CompanyKnowledgeError(f"status: erlaubt sind {', '.join(STATUSES)}.")
    previous = entry.status
    if status == previous and not (status == "approved" and entry.review_reason):
        return entry
    if status == "approved" and previous == "retired":
        raise CompanyKnowledgeError(
            "Ausgemusterte Einträge zuerst wieder als Entwurf aktivieren."
        )
    entry.status = status
    if status == "approved":
        entry.approved_by = changed_by
        entry.approved_at = _now()
        entry.review_reason = None
    elif status == "draft":
        entry.approved_by = None
        entry.approved_at = None
        if entry.review_reason == REASON_ENTRY_GONE:
            entry.review_reason = None
    entry.updated_at = _now()
    action = {"approved": "approved", "draft": "set_draft", "retired": "retired"}[status]
    _audit_entry(
        session,
        entry,
        action=action,
        changed_by=changed_by,
        payload={"previous_status": previous, "status": status},
    )
    return entry


def set_knowledge_mcp(
    *, session: Session, company: Company, enabled: bool, changed_by: str
) -> bool:
    """Gibt das Firmenwissen für MCP-Clients frei oder sperrt es (Audit-Log).

    Gilt nur für Zugriffe über den MCP-Server; Obi im KI-Chat, Oberfläche und REST-API
    bleiben unberührt. Liefert ``True``, wenn sich etwas geändert hat; Aufrufer committet.
    """
    if company.knowledge_mcp_enabled == enabled:
        return False
    company.knowledge_mcp_enabled = enabled
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="company",
        entity_id=str(company.id),
        action="knowledge_mcp_enabled" if enabled else "knowledge_mcp_disabled",
        changed_by=changed_by,
        payload={"knowledge_mcp_enabled": enabled},
    )
    return True


def set_source_active(
    *, session: Session, source: CompanyKnowledgeSource, is_active: bool, changed_by: str
) -> CompanyKnowledgeSource:
    """(De-)aktiviert eine Quelle; freigegebene Einträge daraus erhalten einen Prüfgrund."""
    if not isinstance(is_active, bool):
        raise CompanyKnowledgeError("is_active muss true oder false sein.")
    if source.is_active == is_active:
        return source
    source.is_active = is_active
    source.inactive_reason = None if is_active else REASON_SOURCE_MANUAL
    source.updated_at = _now()
    flagged = 0
    if not is_active:
        for entry in session.execute(
            select(CompanyKnowledgeEntry).where(
                CompanyKnowledgeEntry.source_id == source.id,
                CompanyKnowledgeEntry.status == "approved",
            )
        ).scalars():
            entry.review_reason = REASON_SOURCE_DEACTIVATED
            entry.updated_at = _now()
            flagged += 1
    log_audit_event(
        session=session,
        tenant_id=source.tenant_id,
        company_id=source.company_id,
        entity_type="company_knowledge_source",
        entity_id=str(source.id),
        action="activated" if is_active else "deactivated",
        changed_by=changed_by,
        payload={"source_key": source.source_key, "flagged_entries": flagged},
    )
    return source


# ---------------------------------------------------------------------------
# Wissenspaket: prüfen, abgleichen, einspielen
# ---------------------------------------------------------------------------


@dataclass
class _PackageSource:
    key: str
    title: str
    origin: str | None
    sha256: str | None
    kind: str | None
    version: str | None
    source_date: date | None
    valid_from: date | None
    valid_to: date | None
    authority: str
    note: str | None


@dataclass
class _PackageExcerpt:
    key: str
    source: str
    locator: str
    content: str
    sha256: str


@dataclass
class _PackageEntry:
    key: str
    kind: str
    topic: str | None
    title: str
    content: str
    source: str | None
    locator: str | None
    review_due: date | None
    review_note: str | None
    sha256: str


@dataclass
class ParsedPackage:
    key: str
    revision: str | None
    title: str | None
    sources: list[_PackageSource] = field(default_factory=list)
    excerpts: list[_PackageExcerpt] = field(default_factory=list)
    entries: list[_PackageEntry] = field(default_factory=list)


def _items(data: dict[str, Any], name: str, limit: int, errors: list[str]) -> list[Any]:
    items = data.get(name) or []
    if not isinstance(items, list):
        errors.append(f"{name}: Liste erwartet.")
        return []
    if len(items) > limit:
        errors.append(f"{name}: höchstens {limit} Einträge je Paket.")
        return []
    return items


def parse_package(data: Any) -> ParsedPackage:
    """Prüft ein Wissenspaket vollständig, bevor etwas geschrieben wird."""
    if not isinstance(data, dict):
        raise CompanyKnowledgeError("Das Wissenspaket muss ein JSON-Objekt sein.")
    if data.get("format") != PACKAGE_FORMAT:
        raise CompanyKnowledgeError(f"format muss „{PACKAGE_FORMAT}“ sein.")
    if data.get("version") != PACKAGE_VERSION:
        raise CompanyKnowledgeError(f"version {PACKAGE_VERSION} wird unterstützt.")
    header = data.get("package")
    if not isinstance(header, dict):
        raise CompanyKnowledgeError("package: Objekt mit key erwartet.")
    package = ParsedPackage(
        key=_key(header.get("key"), "package.key", max_len=80),
        revision=_text(header.get("revision"), "package.revision", max_len=80),
        title=_text(header.get("title"), "package.title", max_len=255),
    )
    errors: list[str] = []

    source_keys: set[str] = set()
    for index, raw in enumerate(_items(data, "sources", MAX_SOURCES, errors)):
        label = f"sources[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{label}: Objekt erwartet.")
            continue
        try:
            sha = _text(raw.get("sha256"), "sha256", max_len=64)
            if sha is not None:
                sha = sha.lower()
                if not _SHA_RE.match(sha):
                    raise CompanyKnowledgeError("sha256: 64 Hex-Zeichen erwartet.")
            item = _PackageSource(
                key=_key(raw.get("key"), "key", max_len=120),
                title=_text(raw.get("title"), "title", max_len=255, required=True) or "",
                origin=_text(raw.get("origin"), "origin", max_len=500),
                sha256=sha,
                kind=_text(raw.get("kind"), "kind", max_len=80),
                version=_text(raw.get("version"), "version", max_len=80),
                source_date=_date(raw.get("date"), "date"),
                valid_from=_date(raw.get("valid_from"), "valid_from"),
                valid_to=_date(raw.get("valid_to"), "valid_to"),
                authority=_choice(raw.get("authority"), "authority", AUTHORITIES, "unverified"),
                note=_text(raw.get("note"), "note", max_len=MAX_NOTE_CHARS),
            )
        except CompanyKnowledgeError as exc:
            errors.append(f"{label}.{exc}")
            continue
        if item.key in source_keys:
            errors.append(f"{label}.key: „{item.key}“ ist doppelt.")
            continue
        source_keys.add(item.key)
        package.sources.append(item)

    excerpt_keys: set[str] = set()
    for index, raw in enumerate(_items(data, "excerpts", MAX_EXCERPTS, errors)):
        label = f"excerpts[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{label}: Objekt erwartet.")
            continue
        try:
            content = _text(raw.get("content"), "content", max_len=MAX_TEXT_CHARS, required=True)
            excerpt = _PackageExcerpt(
                key=_key(raw.get("key"), "key", max_len=160),
                source=_key(raw.get("source"), "source", max_len=120),
                locator=_text(raw.get("locator"), "locator", max_len=255) or "",
                content=content or "",
                sha256=_sha256(content),
            )
        except CompanyKnowledgeError as exc:
            errors.append(f"{label}.{exc}")
            continue
        if excerpt.source not in source_keys:
            errors.append(f"{label}.source: Quelle „{excerpt.source}“ fehlt im Paket.")
            continue
        if excerpt.key in excerpt_keys:
            errors.append(f"{label}.key: „{excerpt.key}“ ist doppelt.")
            continue
        excerpt_keys.add(excerpt.key)
        package.excerpts.append(excerpt)

    entry_keys: set[str] = set()
    for index, raw in enumerate(_items(data, "entries", MAX_ENTRIES, errors)):
        label = f"entries[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{label}: Objekt erwartet.")
            continue
        try:
            title = _text(raw.get("title"), "title", max_len=255, required=True) or ""
            content = (
                _text(raw.get("content"), "content", max_len=MAX_TEXT_CHARS, required=True) or ""
            )
            locator = _text(raw.get("locator"), "locator", max_len=255)
            source_key = raw.get("source")
            entry = _PackageEntry(
                key=_key(raw.get("key"), "key", max_len=120),
                kind=_choice(raw.get("kind"), "kind", KINDS, "knowledge"),
                topic=_text(raw.get("topic"), "topic", max_len=80),
                title=title,
                content=content,
                source=(
                    _key(source_key, "source", max_len=120)
                    if source_key not in (None, "")
                    else None
                ),
                locator=locator,
                review_due=_date(raw.get("review_due"), "review_due"),
                review_note=_text(raw.get("review_note"), "review_note", max_len=MAX_NOTE_CHARS),
                sha256=_entry_hash(title, content, locator),
            )
        except CompanyKnowledgeError as exc:
            errors.append(f"{label}.{exc}")
            continue
        if entry.source is not None and entry.source not in source_keys:
            errors.append(f"{label}.source: Quelle „{entry.source}“ fehlt im Paket.")
            continue
        if entry.key in entry_keys:
            errors.append(f"{label}.key: „{entry.key}“ ist doppelt.")
            continue
        entry_keys.add(entry.key)
        package.entries.append(entry)

    if errors:
        shown = "; ".join(errors[:10])
        more = f" (und {len(errors) - 10} weitere)" if len(errors) > 10 else ""
        raise CompanyKnowledgeError(f"Wissenspaket fehlerhaft: {shown}{more}")
    return package


def _empty_counts() -> dict[str, int]:
    return {"created": 0, "updated": 0, "unchanged": 0, "removed": 0}


def _source_values(item: _PackageSource) -> dict[str, Any]:
    return {
        "title": item.title,
        "origin": item.origin,
        "content_sha256": item.sha256,
        "document_kind": item.kind,
        "source_version": item.version,
        "source_date": item.source_date,
        "valid_from": item.valid_from,
        "valid_to": item.valid_to,
        "authority": item.authority,
        "note": item.note,
    }


def _check_ownership(
    rows: dict[str, Any], package_key: str, label: str, keys: set[str]
) -> list[str]:
    """Schlüssel, die schon zu einem anderen Paket oder zu manuellen Daten gehören."""
    conflicts = []
    for key in sorted(keys):
        row = rows.get(key)
        if row is not None and row.package_key != package_key:
            owner = f"Paket „{row.package_key}“" if row.package_key else "manuell angelegt"
            conflicts.append(f"{label} „{key}“ ({owner})")
    return conflicts


def import_package(
    *,
    session: Session,
    company: Company,
    data: Any,
    changed_by: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Spielt ein Wissenspaket ein (oder berechnet bei ``dry_run`` nur den Bericht)."""
    package = parse_package(data)
    now = _now()

    existing_sources = {
        row.source_key: row
        for row in session.execute(
            select(CompanyKnowledgeSource).where(CompanyKnowledgeSource.company_id == company.id)
        ).scalars()
    }
    existing_excerpts = {
        row.excerpt_key: row
        for row in session.execute(
            select(CompanyKnowledgeExcerpt).where(
                CompanyKnowledgeExcerpt.company_id == company.id
            )
        ).scalars()
    }
    existing_entries = {
        row.entry_key: row
        for row in session.execute(
            select(CompanyKnowledgeEntry).where(CompanyKnowledgeEntry.company_id == company.id)
        ).scalars()
    }

    conflicts = (
        _check_ownership(
            existing_sources, package.key, "Quelle", {item.key for item in package.sources}
        )
        + _check_ownership(
            existing_excerpts, package.key, "Auszug", {item.key for item in package.excerpts}
        )
        + _check_ownership(
            existing_entries, package.key, "Eintrag", {item.key for item in package.entries}
        )
    )
    if conflicts:
        shown = ", ".join(conflicts[:10])
        raise CompanyKnowledgeError(
            f"Schlüssel gehören nicht zu diesem Paket: {shown}. Schlüssel umbenennen oder "
            "das Paket mit dem ursprünglichen package.key einspielen."
        )

    report: dict[str, Any] = {
        "package": {"key": package.key, "revision": package.revision, "title": package.title},
        "dry_run": dry_run,
        "sources": _empty_counts(),
        "excerpts": _empty_counts(),
        "entries": {**_empty_counts(), "back_to_draft": 0, "flagged": 0, "revived": 0},
        "changes": [],
    }

    def note(kind: str, key: str, action: str, title: str | None = None) -> None:
        if len(report["changes"]) < MAX_REPORTED_CHANGES:
            report["changes"].append(
                {"type": kind, "key": key, "action": action, "title": title}
            )

    # -- Quellen -----------------------------------------------------------
    source_rows: dict[str, CompanyKnowledgeSource] = {}
    changed_sources: set[str] = set()
    gone_sources: set[str] = set()
    for item in package.sources:
        row = existing_sources.get(item.key)
        values = _source_values(item)
        if row is None:
            report["sources"]["created"] += 1
            note("source", item.key, "created", item.title)
            if not dry_run:
                row = CompanyKnowledgeSource(
                    tenant_id=company.tenant_id,
                    company_id=company.id,
                    source_key=item.key,
                    is_active=True,
                    package_key=package.key,
                    package_revision=package.revision,
                    created_by=changed_by,
                    created_at=now,
                    updated_at=now,
                    **values,
                )
                session.add(row)
                source_rows[item.key] = row
            continue
        differs = any(getattr(row, name) != value for name, value in values.items())
        if row.content_sha256 and item.sha256 and row.content_sha256 != item.sha256:
            changed_sources.add(item.key)
        reactivate = not row.is_active and row.inactive_reason == REASON_ENTRY_GONE
        if differs or reactivate:
            report["sources"]["updated"] += 1
            note("source", item.key, "reactivated" if reactivate else "updated", item.title)
            if not dry_run:
                for name, value in values.items():
                    setattr(row, name, value)
                if reactivate:
                    row.is_active = True
                    row.inactive_reason = None
                row.updated_at = now
        else:
            report["sources"]["unchanged"] += 1
        if not dry_run:
            row.package_revision = package.revision
        source_rows[item.key] = row
    package_source_keys = {item.key for item in package.sources}
    for key, row in existing_sources.items():
        if row.package_key == package.key and key not in package_source_keys and row.is_active:
            gone_sources.add(key)
            report["sources"]["removed"] += 1
            note("source", key, "deactivated", row.title)
            if not dry_run:
                row.is_active = False
                row.inactive_reason = REASON_ENTRY_GONE
                row.updated_at = now
    if not dry_run:
        session.flush()

    # -- Auszüge -----------------------------------------------------------
    package_excerpt_keys = set()
    for item in package.excerpts:
        package_excerpt_keys.add(item.key)
        row = existing_excerpts.get(item.key)
        if row is None:
            report["excerpts"]["created"] += 1
            if not dry_run:
                session.add(
                    CompanyKnowledgeExcerpt(
                        tenant_id=company.tenant_id,
                        company_id=company.id,
                        source_id=source_rows[item.source].id,
                        excerpt_key=item.key,
                        locator=item.locator,
                        content=item.content,
                        content_sha256=item.sha256,
                        is_active=True,
                        package_key=package.key,
                        created_at=now,
                        updated_at=now,
                    )
                )
            continue
        previous_source = existing_sources.get(item.source)
        differs = (
            row.content_sha256 != item.sha256
            or row.locator != item.locator
            or not row.is_active
            or previous_source is None
            or row.source_id != previous_source.id
        )
        if not differs:
            report["excerpts"]["unchanged"] += 1
            continue
        report["excerpts"]["updated"] += 1
        if not dry_run:
            row.locator = item.locator
            row.content = item.content
            row.content_sha256 = item.sha256
            row.source_id = source_rows[item.source].id
            row.is_active = True
            row.updated_at = now
    for key, row in existing_excerpts.items():
        if row.package_key == package.key and key not in package_excerpt_keys and row.is_active:
            report["excerpts"]["removed"] += 1
            if not dry_run:
                row.is_active = False
                row.updated_at = now

    # -- Einträge ----------------------------------------------------------
    # Entscheidungen fallen nur auf dem Stand vor dem Import, damit Vorschau und
    # Einspielen denselben Bericht liefern.
    events: list[tuple[CompanyKnowledgeEntry, str, dict[str, Any]]] = []
    package_entry_keys = {item.key for item in package.entries}
    leaves_approved: set[str] = set()
    for item in package.entries:
        row = existing_entries.get(item.key)
        source_row = source_rows.get(item.source) if item.source else None
        if row is None:
            report["entries"]["created"] += 1
            note("entry", item.key, "created", item.title)
            if not dry_run:
                row = CompanyKnowledgeEntry(
                    tenant_id=company.tenant_id,
                    company_id=company.id,
                    entry_key=item.key,
                    kind=item.kind,
                    topic=item.topic,
                    title=item.title,
                    content=item.content,
                    source_id=source_row.id if source_row else None,
                    locator=item.locator,
                    status="draft",
                    review_due=item.review_due,
                    review_note=item.review_note,
                    content_sha256=item.sha256,
                    package_key=package.key,
                    proposed_by=changed_by,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                events.append((row, "imported", {"status": "draft"}))
            continue

        previous_source_key = row.source.source_key if row.source is not None else None
        content_changed = row.content_sha256 != item.sha256 or previous_source_key != item.source
        meta_changed = (
            row.kind != item.kind
            or row.topic != item.topic
            or row.review_note != item.review_note
            or (row.review_due is None and item.review_due is not None)
        )
        revive = row.status == "retired" and row.review_reason == REASON_ENTRY_GONE
        if row.status == "retired" and not revive:
            # Von Hand ausgemustert: bleibt ausgemustert.
            report["entries"]["unchanged"] += 1
            continue
        back_to_draft = content_changed and row.status == "approved"
        flag = (
            not content_changed
            and row.status == "approved"
            and item.source in changed_sources
            and row.review_reason != REASON_SOURCE_CHANGED
        )
        if not (content_changed or meta_changed or revive or flag):
            report["entries"]["unchanged"] += 1
            continue

        if back_to_draft:
            leaves_approved.add(item.key)
            report["entries"]["back_to_draft"] += 1
            action = "back_to_draft"
        elif revive:
            report["entries"]["revived"] += 1
            action = "revived"
        elif content_changed or meta_changed:
            action = "updated"
        else:
            action = "flagged"
        if content_changed or meta_changed or revive:
            report["entries"]["updated"] += 1
        if flag:
            report["entries"]["flagged"] += 1
        note("entry", item.key, action, item.title)
        if dry_run:
            continue

        before = _audit_snapshot(row)
        if content_changed:
            row.title = item.title
            row.content = item.content
            row.locator = item.locator
            row.content_sha256 = item.sha256
            row.source_id = source_row.id if source_row else None
        row.kind = item.kind
        row.topic = item.topic
        row.review_note = item.review_note
        if row.review_due is None and item.review_due is not None:
            row.review_due = item.review_due
        if revive:
            row.status = "draft"
            row.review_reason = None
        if back_to_draft:
            row.status = "draft"
            row.approved_by = None
            row.approved_at = None
            row.review_reason = REASON_CONTENT_CHANGED
        if flag:
            row.review_reason = REASON_SOURCE_CHANGED
        row.updated_at = now
        events.append(
            (row, f"import_{action}", {"before": before, "after": _audit_snapshot(row)})
        )

    # Im Paket entfallene Einträge dieses Pakets ausmustern.
    for key, row in existing_entries.items():
        if row.package_key != package.key or key in package_entry_keys:
            continue
        if row.status == "retired":
            continue
        if row.status == "approved":
            leaves_approved.add(key)
        report["entries"]["removed"] += 1
        note("entry", key, "retired", row.title)
        if not dry_run:
            previous = row.status
            row.status = "retired"
            row.review_reason = REASON_ENTRY_GONE
            row.updated_at = now
            events.append((row, "import_retired", {"previous_status": previous}))

    # Freigegebene Einträge (auch manuelle oder anderer Pakete), deren Quelle entfallen ist.
    if gone_sources:
        for key, row in existing_entries.items():
            if (
                key in leaves_approved
                or row.status != "approved"
                or row.source is None
                or row.source.source_key not in gone_sources
                or row.review_reason == REASON_SOURCE_GONE
            ):
                continue
            report["entries"]["flagged"] += 1
            note("entry", key, "flagged", row.title)
            if not dry_run:
                row.review_reason = REASON_SOURCE_GONE
                row.updated_at = now
                events.append((row, "import_flagged", {"review_reason": REASON_SOURCE_GONE}))

    if dry_run:
        return report

    session.flush()
    for row, action, payload in events:
        _audit_entry(session, row, action=action, changed_by=changed_by, payload=payload)
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="company_knowledge_package",
        entity_id=package.key,
        action="imported",
        changed_by=changed_by,
        payload={
            "revision": package.revision,
            "sources": report["sources"],
            "excerpts": report["excerpts"],
            "entries": report["entries"],
        },
    )
    invalidate_company_index(company.id)
    return report


# ---------------------------------------------------------------------------
# Suche (BM25 wie die allgemeine Wissensbasis, Index je Gesellschaft)
# ---------------------------------------------------------------------------

_INDEX_LOCK = threading.Lock()
_INDEX_CACHE: dict[int, tuple[tuple[Any, ...], KnowledgeBase, dict[str, dict[str, Any]]]] = {}


def invalidate_company_index(company_id: int | None = None) -> None:
    with _INDEX_LOCK:
        if company_id is None:
            _INDEX_CACHE.clear()
        else:
            _INDEX_CACHE.pop(company_id, None)


def _fingerprint(session: Session, company_id: int) -> tuple[Any, ...]:
    """Ändert sich bei jeder Änderung an Einträgen, Auszügen oder Quellen."""
    parts: list[Any] = []
    for model in (CompanyKnowledgeEntry, CompanyKnowledgeExcerpt, CompanyKnowledgeSource):
        count, latest = session.execute(
            select(func.count(), func.max(model.updated_at)).where(
                model.company_id == company_id
            )
        ).one()
        parts.extend([count, str(latest)])
    return tuple(parts)


def _clip(text: str) -> str:
    if len(text) <= RESULT_TEXT_LIMIT:
        return text
    return text[:RESULT_TEXT_LIMIT].rstrip() + " … (gekürzt)"


def _build_index(
    session: Session, company_id: int
) -> tuple[KnowledgeBase, dict[str, dict[str, Any]]]:
    sections: list[KnowledgeSection] = []
    meta: dict[str, dict[str, Any]] = {}
    entries = session.execute(
        select(CompanyKnowledgeEntry).where(
            CompanyKnowledgeEntry.company_id == company_id,
            CompanyKnowledgeEntry.status != "retired",
        )
    ).scalars()
    for entry in entries:
        section_id = f"entry:{entry.id}"
        source = entry.source
        sections.append(
            KnowledgeSection(
                id=section_id,
                file="firmenwissen",
                source_title=source.title if source else "Firmenwissen",
                title=entry.title,
                rechtsstand=None,
                keywords=tuple(part for part in (entry.topic, entry.entry_key) if part),
                text=entry.content,
            )
        )
        meta[section_id] = {
            "type": "entry",
            "id": entry.id,
            "entry_key": entry.entry_key,
            "kind": entry.kind,
            "kind_label": KIND_LABELS.get(entry.kind, entry.kind),
            "topic": entry.topic,
            "title": entry.title,
            "text": _clip(entry.content),
            "status": entry.status,
            "status_label": STATUS_LABELS[entry.status],
            "review_reason": entry.review_reason,
            "source_id": entry.source_id,
            "source_title": source.title if source else None,
            "source_active": source.is_active if source else None,
            "authority": source.authority if source else None,
            "valid_from": source.valid_from.isoformat() if source and source.valid_from else None,
            "valid_to": source.valid_to.isoformat() if source and source.valid_to else None,
            "locator": entry.locator,
        }
    rows = session.execute(
        select(CompanyKnowledgeExcerpt, CompanyKnowledgeSource)
        .join(
            CompanyKnowledgeSource,
            CompanyKnowledgeExcerpt.source_id == CompanyKnowledgeSource.id,
        )
        .where(
            CompanyKnowledgeExcerpt.company_id == company_id,
            CompanyKnowledgeExcerpt.is_active.is_(True),
            CompanyKnowledgeSource.is_active.is_(True),
        )
    ).all()
    for excerpt, source in rows:
        section_id = f"excerpt:{excerpt.id}"
        title = f"{source.title} – {excerpt.locator}" if excerpt.locator else source.title
        sections.append(
            KnowledgeSection(
                id=section_id,
                file="firmenwissen",
                source_title=source.title,
                title=title,
                rechtsstand=None,
                keywords=tuple(part for part in (source.document_kind,) if part),
                text=excerpt.content,
            )
        )
        meta[section_id] = {
            "type": "excerpt",
            "id": excerpt.id,
            "title": title,
            "text": _clip(excerpt.content),
            "status": "excerpt",
            "status_label": (
                "Quellenauszug, Verbindlichkeit: "
                f"{AUTHORITY_LABELS.get(source.authority, source.authority)}"
            ),
            "source_id": source.id,
            "source_title": source.title,
            "source_active": True,
            "authority": source.authority,
            "valid_from": source.valid_from.isoformat() if source.valid_from else None,
            "valid_to": source.valid_to.isoformat() if source.valid_to else None,
            "locator": excerpt.locator,
        }
    document = KnowledgeDocument(
        file="firmenwissen",
        title="Firmenwissen",
        rechtsstand=None,
        meta={},
        sections=tuple(sections),
        body="",
    )
    return KnowledgeBase([document]), meta


def _company_index(
    session: Session, company_id: int
) -> tuple[KnowledgeBase, dict[str, dict[str, Any]]]:
    fingerprint = _fingerprint(session, company_id)
    with _INDEX_LOCK:
        cached = _INDEX_CACHE.get(company_id)
        if cached is not None and cached[0] == fingerprint:
            return cached[1], cached[2]
    base, meta = _build_index(session, company_id)
    with _INDEX_LOCK:
        _INDEX_CACHE[company_id] = (fingerprint, base, meta)
    return base, meta


def search_company_knowledge(
    *, session: Session, company_id: int, query: str, limit: int
) -> list[dict[str, Any]]:
    """Treffer im Firmenwissen; freigegebene Einträge werden höher gewichtet."""
    query = (query or "").strip()
    if not query:
        return []
    base, meta = _company_index(session, company_id)
    hits = base.search(query, limit=SEARCH_CANDIDATES, cap=SEARCH_CANDIDATES)
    ranked = []
    for section, score in hits:
        info = meta[section.id]
        weighted = score * APPROVED_SCORE_FACTOR if info["status"] == "approved" else score
        ranked.append((round(weighted, 3), section.id, info))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [{**info, "score": score} for score, _section_id, info in ranked[:limit]]


COMPANY_RESULTS_NOTE = (
    "Firmenwissen der Gesellschaft: freigegebene Einträge sind Unternehmensregeln, Entwürfe "
    "und Quellenauszüge sind nicht freigegeben und so zu kennzeichnen. Gesetz und Rechtsstand "
    "gehen vor; ein Eintrag erlaubt keine Aktion. Bei Widersprüchen nachfragen."
)


def search_with_company_knowledge(
    *, session: Session, company_id: int, query: str | None, limit: int
) -> dict[str, Any]:
    """Allgemeine Wissensbasis plus Firmenwissen einer Gesellschaft (API, MCP, UI)."""
    result = search_knowledge(query, limit=limit)
    limit = max(1, min(int(limit or DEFAULT_SEARCH_LIMIT), MAX_SEARCH_LIMIT))
    result["company"] = {
        "company_id": company_id,
        **company_knowledge_summary(session=session, company_id=company_id),
        "note": COMPANY_RESULTS_NOTE,
    }
    result["company_results"] = search_company_knowledge(
        session=session, company_id=company_id, query=query or "", limit=limit
    )
    return result


def knowledge_history(
    *, session: Session, company_id: int, entity_type: str, entity_id: int, limit: int = 50
) -> list[AuditLog]:
    """Audit-Ereignisse eines Eintrags oder einer Quelle, neueste zuerst."""
    return list(
        session.execute(
            select(AuditLog)
            .where(
                AuditLog.company_id == company_id,
                AuditLog.entity_type == entity_type,
                AuditLog.entity_id == str(entity_id),
            )
            .order_by(AuditLog.id.desc())
            .limit(limit)
        ).scalars()
    )
