"""Belegabgleich im Hintergrund: Auftrag anlegen, im Thread ausführen, Stand abfragen.

OCR und KI-Abgleich dauern mit lokalen Modellen leicht länger, als nginx auf eine
Antwort wartet (504). Der Start legt deshalb nur einen :class:`ReceiptMatchJob` an
und kehrt sofort zurück; :func:`create_match_suggestion` läuft in einem Thread des
Workers. Den Stand liest jeder Worker aus der Datenbank (UI-Benachrichtigung,
REST-API, MCP).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.receipt_matching import (
    ReceiptMatchError,
    check_matchable_document,
    create_match_suggestion,
)
from domain.models import ReceiptMatchJob

logger = logging.getLogger(__name__)

JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"

# So lange läuft kein Abgleich (wenige KI-Aufrufe mit je LLM_TIMEOUT_SECONDS). Ein
# älterer laufender Auftrag ist mit seinem Worker untergegangen (Redeploy, Absturz).
JOB_STALE_AFTER = timedelta(minutes=20)
STALE_ERROR = "Der Abgleich wurde abgebrochen (Server neu gestartet). Bitte erneut starten."


def _utc(value: datetime) -> datetime:
    # SQLite liefert DateTime(timezone=True) ohne Zeitzone zurück.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _start_thread(target: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=target, name="receipt-match", daemon=True)
    thread.start()
    return thread


def expire_stale_jobs(session: Session, jobs: Iterable[ReceiptMatchJob]) -> None:
    """Hängengebliebene laufende Aufträge als fehlgeschlagen abschließen."""
    now = datetime.now(timezone.utc)
    changed = False
    for job in jobs:
        if job.status == JOB_RUNNING and now - _utc(job.created_at) > JOB_STALE_AFTER:
            job.status = JOB_FAILED
            job.error = STALE_ERROR
            job.finished_at = now
            changed = True
    if changed:
        session.commit()


def latest_jobs(session: Session, document_ids: Iterable[int]) -> dict[int, ReceiptMatchJob]:
    """Jüngster Auftrag je Beleg (hängengebliebene bereits als fehlgeschlagen)."""
    ids = list(document_ids)
    if not ids:
        return {}
    jobs = (
        session.execute(
            select(ReceiptMatchJob)
            .where(ReceiptMatchJob.document_id.in_(ids))
            .order_by(ReceiptMatchJob.id)
        )
        .scalars()
        .all()
    )
    latest = {job.document_id: job for job in jobs}
    expire_stale_jobs(session, latest.values())
    return latest


def get_job(session: Session, job_id: int) -> ReceiptMatchJob | None:
    job = session.get(ReceiptMatchJob, job_id)
    if job is not None:
        expire_stale_jobs(session, [job])
    return job


def start_match_job(
    *,
    session: Session,
    company_id: int,
    document_id: int,
    changed_by: str,
    analysis_options: dict[str, object],
) -> ReceiptMatchJob:
    """Prüft den Beleg, legt den Auftrag an und startet den Abgleich im Hintergrund.

    Hindernisse, die schon vorher feststehen (Beleg verknüpft, offener Vorschlag,
    laufender Abgleich), meldet die Funktion sofort als :class:`ReceiptMatchError`;
    Fehler des Abgleichs selbst stehen danach im Auftrag. ``analysis_options`` kommt
    aus ``receipt_analysis_options`` und muss im Request ermittelt werden (KI-Zugang
    des angemeldeten Benutzers).
    """
    company, document = check_matchable_document(session, company_id, document_id)
    current = latest_jobs(session, [document.id]).get(document.id)
    if current is not None and current.status == JOB_RUNNING:
        raise ReceiptMatchError("Für diesen Beleg läuft bereits ein Abgleich.")

    job = ReceiptMatchJob(
        tenant_id=company.tenant_id,
        company_id=company.id,
        document_id=document.id,
        status=JOB_RUNNING,
        created_by=changed_by,
    )
    session.add(job)
    session.commit()

    app = current_app._get_current_object()
    session_factory = app.extensions["db_session_factory"]
    job_id = job.id

    def worker() -> None:
        with app.app_context():
            _run_job(session_factory, job_id, changed_by, analysis_options)

    _start_thread(worker)
    return job


def _run_job(
    session_factory, job_id: int, changed_by: str, analysis_options: dict[str, object]
) -> None:
    with session_factory() as session:
        job = session.get(ReceiptMatchJob, job_id)
        if job is None:
            return
        try:
            suggestion = create_match_suggestion(
                session=session,
                company_id=job.company_id,
                document_id=job.document_id,
                changed_by=changed_by,
                **analysis_options,
            )
        except ReceiptMatchError as exc:
            session.rollback()
            _finish(session, job_id, JOB_FAILED, error=str(exc))
        except Exception:  # noqa: BLE001 – der Fehler gehört in den Auftrag, nicht ins Nichts
            logger.exception("Belegabgleich: Fehler im Hintergrundauftrag %s", job_id)
            session.rollback()
            _finish(session, job_id, JOB_FAILED, error="Interner Fehler beim Abgleich.")
        else:
            _finish(session, job_id, JOB_DONE, suggestion_id=suggestion.id)


def _finish(
    session: Session,
    job_id: int,
    status: str,
    *,
    error: str | None = None,
    suggestion_id: int | None = None,
) -> None:
    job = session.get(ReceiptMatchJob, job_id)
    if job is None:
        return
    job.status = status
    job.error = error
    job.suggestion_id = suggestion_id
    job.finished_at = datetime.now(timezone.utc)
    session.commit()


def job_dict(job: ReceiptMatchJob) -> dict[str, object]:
    """Stand eines Auftrags für UI-Abfrage, REST-API und MCP."""
    return {
        "id": job.id,
        "company_id": job.company_id,
        "document_id": job.document_id,
        "file_name": job.document.file_name if job.document is not None else None,
        "status": job.status,
        "suggestion_id": job.suggestion_id,
        "suggestion_type": (
            job.suggestion.suggestion_type if job.suggestion is not None else None
        ),
        "error": job.error,
        "created_by": job.created_by,
        "created_at": _utc(job.created_at).isoformat(),
        "finished_at": _utc(job.finished_at).isoformat() if job.finished_at else None,
    }
