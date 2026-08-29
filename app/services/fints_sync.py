"""FinTS/HBCI-Direktabruf von Bankumsätzen über python-fints.

Sicherheitsmodell: Es werden ausschließlich Zugangs-Stammdaten gespeichert
(BLZ, Login, FinTS-URL). PIN und TAN werden nie persistiert — die PIN wird
bei jedem Abruf (und bei der TAN-Bestätigung erneut) eingegeben. Erfordert
eine Bank einen TAN-Schritt (PSD2-SCA), wird der FinTS-Dialog serialisiert
in ``fints_pending_dialog`` eingefroren und nach der TAN-Eingabe fortgesetzt;
das deckt sowohl TAN-Code-Verfahren als auch entkoppelte Verfahren (pushTAN-
Bestätigung in der Banking-App) ab.

Die abgerufenen Umsätze laufen durch dieselbe Import-Pipeline wie der
Dateiimport (Dedup über Hash, Audit-Log, Status "open").
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from app.services.bank_import import BankImportReport, import_bank_items
from app.services.bank_statement import row_from_mt940_data
from domain.models import Account, Company, FinTSConnection, FinTSPendingDialog

logger = logging.getLogger(__name__)

DIALOG_MAX_AGE = timedelta(minutes=15)
DEFAULT_SYNC_DAYS = 30


class FinTSSyncError(ValueError):
    """Raised when a FinTS operation is invalid or fails."""


@dataclass(slots=True)
class TanChallenge:
    dialog_id: str
    challenge: str
    decoupled: bool


@dataclass(slots=True)
class FinTSSyncResult:
    """Entweder ein fertiger Import-Report oder eine offene TAN-Anforderung."""

    report: BankImportReport | None = None
    challenge: TanChallenge | None = None


def _build_client(connection: FinTSConnection, pin: str, product_id: str, from_data=None):
    """Erzeugt den python-fints-Client; in Tests durch einen Fake ersetzt."""
    from fints.client import FinTS3PinTanClient

    return FinTS3PinTanClient(
        connection.blz,
        connection.login,
        pin,
        connection.fints_url,
        product_id=product_id,
        from_data=from_data,
    )


# ---------------------------------------------------------------------------
# Verwaltung der Bankzugänge


def serialize_connection(connection: FinTSConnection) -> dict[str, object]:
    return {
        "id": connection.id,
        "company_id": connection.company_id,
        "bank_account_id": connection.bank_account_id,
        "name": connection.name,
        "blz": connection.blz,
        "login": connection.login,
        "fints_url": connection.fints_url,
        "sepa_iban": connection.sepa_iban,
        "is_active": connection.is_active,
        "created_at": connection.created_at.isoformat(),
    }


def create_fints_connection(
    *,
    session: Session,
    company_id: int,
    bank_account_id: int,
    name: str,
    blz: str,
    login: str,
    fints_url: str,
    sepa_iban: str | None = None,
    changed_by: str,
) -> FinTSConnection:
    company = session.get(Company, company_id)
    if company is None:
        raise FinTSSyncError("Gesellschaft nicht gefunden.")

    bank_account = session.get(Account, bank_account_id)
    if bank_account is None or bank_account.company_id != company.id:
        raise FinTSSyncError("Bankkonto nicht gefunden.")

    name = name.strip()
    blz = blz.strip()
    login = login.strip()
    fints_url = fints_url.strip()
    sepa_iban = (sepa_iban or "").replace(" ", "").upper() or None

    if not name:
        raise FinTSSyncError("Name des Bankzugangs fehlt.")
    if not (blz.isdigit() and len(blz) == 8):
        raise FinTSSyncError("BLZ muss aus genau 8 Ziffern bestehen.")
    if not login:
        raise FinTSSyncError("FinTS-Login fehlt.")
    if not fints_url.startswith("https://"):
        raise FinTSSyncError("FinTS-URL muss mit https:// beginnen.")

    existing = session.execute(
        select(FinTSConnection).where(
            FinTSConnection.company_id == company.id, FinTSConnection.name == name
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise FinTSSyncError("Ein Bankzugang mit diesem Namen existiert bereits.")

    connection = FinTSConnection(
        tenant_id=company.tenant_id,
        company_id=company.id,
        bank_account_id=bank_account.id,
        name=name,
        blz=blz,
        login=login,
        fints_url=fints_url,
        sepa_iban=sepa_iban,
    )
    session.add(connection)
    session.flush()

    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="fints_connection",
        entity_id=str(connection.id),
        action="created",
        changed_by=changed_by,
        payload={"name": name, "blz": blz, "bank_account_id": bank_account.id},
    )
    session.commit()
    session.refresh(connection)
    return connection


def list_fints_connections(
    *, session: Session, company_id: int, include_inactive: bool = False
) -> list[FinTSConnection]:
    stmt = select(FinTSConnection).where(FinTSConnection.company_id == company_id)
    if not include_inactive:
        stmt = stmt.where(FinTSConnection.is_active.is_(True))
    return list(session.execute(stmt.order_by(FinTSConnection.name)).scalars())


def set_fints_connection_active(
    *, session: Session, connection_id: int, is_active: bool, changed_by: str
) -> FinTSConnection:
    connection = session.get(FinTSConnection, connection_id)
    if connection is None:
        raise FinTSSyncError("Bankzugang nicht gefunden.")

    connection.is_active = is_active
    log_audit_event(
        session=session,
        tenant_id=connection.tenant_id,
        company_id=connection.company_id,
        entity_type="fints_connection",
        entity_id=str(connection.id),
        action="activated" if is_active else "deactivated",
        changed_by=changed_by,
        payload={"name": connection.name},
    )
    session.commit()
    session.refresh(connection)
    return connection


# ---------------------------------------------------------------------------
# Umsatzabruf


def start_fints_sync(
    *,
    session: Session,
    connection_id: int,
    pin: str,
    product_id: str | None,
    from_date: date | None = None,
    to_date: date | None = None,
    changed_by: str,
) -> FinTSSyncResult:
    """Startet einen Umsatzabruf; Ergebnis ist ein Report oder eine TAN-Anforderung."""
    connection = _get_active_connection(session, connection_id)
    _require_pin(pin)
    product_id = _require_product_id(product_id)
    _cleanup_expired_dialogs(session)

    if to_date is None:
        to_date = date.today()
    if from_date is None:
        from_date = to_date - timedelta(days=DEFAULT_SYNC_DAYS)
    if from_date > to_date:
        raise FinTSSyncError("Der Von-Datum darf nicht nach dem Bis-Datum liegen.")

    client = _build_client(connection, pin, product_id)
    from fints.client import NeedTANResponse

    try:
        _ensure_tan_mechanism(client)
        tan_request = None
        step = None
        dialog_data = None
        transactions = None
        with client:
            if getattr(client, "init_tan_response", None):
                tan_request = client.init_tan_response
                step = "init"
            else:
                result = _fetch_transactions(client, connection, from_date, to_date)
                if isinstance(result, NeedTANResponse):
                    tan_request = result
                    step = "transactions"
                else:
                    transactions = result
            if step is not None:
                dialog_data = client.pause_dialog()
    except FinTSSyncError:
        raise
    except Exception as exc:  # fints wirft eigene Fehlerklassen + requests-Fehler
        raise FinTSSyncError(f"FinTS-Abruf fehlgeschlagen: {exc}") from exc

    if step is not None:
        return FinTSSyncResult(
            challenge=_freeze_dialog(
                session=session,
                connection=connection,
                client=client,
                tan_request=tan_request,
                dialog_data=dialog_data,
                step=step,
                from_date=from_date,
                to_date=to_date,
            )
        )

    report = _import_transactions(
        session=session, connection=connection, transactions=transactions, changed_by=changed_by
    )
    return FinTSSyncResult(report=report)


def submit_fints_tan(
    *,
    session: Session,
    dialog_id: str,
    pin: str,
    tan: str | None,
    product_id: str | None,
    changed_by: str,
) -> FinTSSyncResult:
    """Setzt einen eingefrorenen Dialog mit TAN (bzw. pushTAN-Bestätigung) fort."""
    pending = session.get(FinTSPendingDialog, dialog_id)
    if pending is None:
        raise FinTSSyncError("TAN-Dialog nicht gefunden oder bereits abgeschlossen.")
    if _dialog_expired(pending):
        session.delete(pending)
        session.commit()
        raise FinTSSyncError("TAN-Dialog ist abgelaufen — bitte Abruf neu starten.")

    connection = _get_active_connection(session, pending.connection_id)
    _require_pin(pin)
    product_id = _require_product_id(product_id)

    client = _build_client(connection, pin, product_id, from_data=pending.client_data)
    from fints.client import NeedTANResponse

    try:
        tan_request = _tan_request_from_data(pending.tan_request_data)
        next_request = None
        next_step = None
        dialog_data = None
        transactions = None
        with client.resume_dialog(pending.dialog_data):
            response = client.send_tan(tan_request, tan or "")
            if isinstance(response, NeedTANResponse):
                # Entkoppeltes Verfahren: Freigabe in der Banking-App steht noch aus.
                next_request = response
                next_step = pending.step
            elif pending.step == "init":
                result = _fetch_transactions(
                    client, connection, pending.from_date, pending.to_date
                )
                if isinstance(result, NeedTANResponse):
                    next_request = result
                    next_step = "transactions"
                else:
                    transactions = result
            else:
                transactions = response
            if next_step is not None:
                dialog_data = client.pause_dialog()
    except Exception as exc:
        # Der Bankdialog ist durch den Context-Manager-Exit beendet — der
        # eingefrorene Zustand ist nicht wiederverwendbar, also aufräumen.
        session.delete(pending)
        session.commit()
        if isinstance(exc, FinTSSyncError):
            raise
        raise FinTSSyncError(f"TAN-Bestätigung fehlgeschlagen: {exc}") from exc

    if next_step is not None:
        challenge = _freeze_dialog(
            session=session,
            connection=connection,
            client=client,
            tan_request=next_request,
            dialog_data=dialog_data,
            step=next_step,
            from_date=pending.from_date,
            to_date=pending.to_date,
            existing=pending,
        )
        return FinTSSyncResult(challenge=challenge)

    session.delete(pending)
    report = _import_transactions(
        session=session, connection=connection, transactions=transactions, changed_by=changed_by
    )
    return FinTSSyncResult(report=report)


def cancel_pending_dialog(*, session: Session, dialog_id: str, changed_by: str) -> bool:
    """Verwirft einen eingefrorenen TAN-Dialog samt gespeichertem Client-Zustand."""
    pending = session.get(FinTSPendingDialog, dialog_id)
    if pending is None:
        return False
    log_audit_event(
        session=session,
        tenant_id=pending.tenant_id,
        company_id=pending.company_id,
        entity_type="fints_pending_dialog",
        entity_id=pending.id,
        action="cancelled",
        changed_by=changed_by,
        payload={"connection_id": pending.connection_id, "step": pending.step},
    )
    session.delete(pending)
    session.commit()
    return True


# ---------------------------------------------------------------------------
# Interna


def _tan_request_from_data(data: bytes):
    """Deserialisiert eine gespeicherte TAN-Anforderung; in Tests ersetzt."""
    from fints.client import NeedTANResponse

    return NeedTANResponse.from_data(data)


def _require_pin(pin: str) -> None:
    if not pin or not pin.strip():
        raise FinTSSyncError("PIN ist erforderlich (wird nicht gespeichert).")


def _require_product_id(product_id: str | None) -> str:
    if not product_id:
        raise FinTSSyncError(
            "FINTS_PRODUCT_ID ist nicht konfiguriert. Für den FinTS-Zugang ist eine "
            "Produktregistrierung der Deutschen Kreditwirtschaft nötig "
            "(https://www.fints.org) — die Kennung als Umgebungsvariable "
            "FINTS_PRODUCT_ID setzen."
        )
    return product_id


def _get_active_connection(session: Session, connection_id: int) -> FinTSConnection:
    connection = session.get(FinTSConnection, connection_id)
    if connection is None:
        raise FinTSSyncError("Bankzugang nicht gefunden.")
    if not connection.is_active:
        raise FinTSSyncError("Der Bankzugang ist deaktiviert.")
    return connection


def _ensure_tan_mechanism(client) -> None:
    """Wählt nicht-interaktiv ein TAN-Verfahren und ggf. ein TAN-Medium."""
    if client.get_current_tan_mechanism():
        return
    client.fetch_tan_mechanisms()
    mechanisms = client.get_tan_mechanisms()
    if not mechanisms:
        raise FinTSSyncError("Die Bank meldet kein unterstütztes TAN-Verfahren.")
    if client.get_current_tan_mechanism() is None or len(mechanisms) > 1:
        client.set_tan_mechanism(next(iter(mechanisms.keys())))
    if client.selected_tan_medium is None and client.is_tan_media_required():
        _, media = client.get_tan_media()
        if media:
            client.set_tan_medium(media[0])
        else:
            # Workaround wie in fints.utils: manche Banken benötigen kein Medium.
            client.selected_tan_medium = ""


def _fetch_transactions(client, connection: FinTSConnection, from_date, to_date):
    accounts = client.get_sepa_accounts()
    from fints.client import NeedTANResponse

    if isinstance(accounts, NeedTANResponse):
        raise FinTSSyncError(
            "Die Bank verlangt eine TAN bereits für die Kontenliste — "
            "das wird derzeit nicht unterstützt."
        )
    if not accounts:
        raise FinTSSyncError("Der FinTS-Zugang meldet keine SEPA-Konten.")

    if connection.sepa_iban:
        matching = [
            account for account in accounts if (account.iban or "") == connection.sepa_iban
        ]
        if not matching:
            raise FinTSSyncError(
                f"Kein SEPA-Konto mit IBAN {connection.sepa_iban} im Zugang gefunden."
            )
        account = matching[0]
    elif len(accounts) == 1:
        account = accounts[0]
    else:
        ibans = ", ".join(str(account.iban) for account in accounts)
        raise FinTSSyncError(
            f"Der Zugang umfasst mehrere Konten ({ibans}) — bitte am Bankzugang "
            "eine IBAN hinterlegen."
        )

    return client.get_transactions(account, from_date, to_date)


def _import_transactions(
    *, session: Session, connection: FinTSConnection, transactions, changed_by: str
) -> BankImportReport:
    items = [
        row_from_mt940_data(position, transaction.data)
        for position, transaction in enumerate(transactions or [], start=1)
    ]
    return import_bank_items(
        session=session,
        company_id=connection.company_id,
        bank_account_id=connection.bank_account_id,
        items=items,
        changed_by=changed_by,
        source="fints",
    )


def _freeze_dialog(
    *,
    session: Session,
    connection: FinTSConnection,
    client,
    tan_request,
    dialog_data: bytes,
    step: str,
    from_date,
    to_date,
    existing: FinTSPendingDialog | None = None,
) -> TanChallenge:
    client_data = client.deconstruct(including_private=True)
    if existing is not None:
        pending = existing
        pending.step = step
        pending.client_data = client_data
        pending.dialog_data = dialog_data
        pending.tan_request_data = tan_request.get_data()
        pending.created_at = datetime.now(timezone.utc)
    else:
        pending = FinTSPendingDialog(
            id=str(uuid.uuid4()),
            tenant_id=connection.tenant_id,
            company_id=connection.company_id,
            connection_id=connection.id,
            step=step,
            client_data=client_data,
            dialog_data=dialog_data,
            tan_request_data=tan_request.get_data(),
            from_date=from_date,
            to_date=to_date,
        )
        session.add(pending)
    session.commit()

    challenge_text = getattr(tan_request, "challenge", None) or "TAN-Bestätigung erforderlich."
    decoupled = bool(getattr(tan_request, "decoupled", False))
    if decoupled:
        challenge_text += " (Freigabe in der Banking-App, danach hier fortsetzen.)"
    return TanChallenge(
        dialog_id=pending.id, challenge=challenge_text, decoupled=decoupled
    )


def _dialog_expired(pending: FinTSPendingDialog) -> bool:
    created_at = pending.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created_at > DIALOG_MAX_AGE


def _cleanup_expired_dialogs(session: Session) -> None:
    cutoff = datetime.now(timezone.utc) - DIALOG_MAX_AGE
    session.execute(delete(FinTSPendingDialog).where(FinTSPendingDialog.created_at < cutoff))
    session.commit()
