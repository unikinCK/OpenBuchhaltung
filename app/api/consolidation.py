"""Konsolidierte Auswertung über die REST-API (Gruppen, Bericht, manuelle Buchungen)."""

from __future__ import annotations

from datetime import date

from flask import Response, jsonify, request

from app.api.blueprint import api_bp
from app.api.downloads import linkable_download
from app.api.helpers import (
    DateArgError,
    api_can_write,
    api_scoped_company,
    bool_arg,
    date_arg,
    forbidden,
    get_session_factory,
    validation_error,
)
from app.auth import current_api_tenant_id, current_api_user
from app.services.consolidation import (
    ConsolidationError,
    consolidation_report,
    consolidation_report_csv,
    create_adjustment,
    create_group,
    delete_adjustment,
    json_ready,
    list_groups,
    serialize_adjustment,
    serialize_group,
    update_group,
)
from domain.models import ConsolidationAdjustment, ConsolidationGroup

CREATE_FIELDS = {"name", "parent_company_id", "members", "is_active"}
UPDATE_FIELDS = {"name", "parent_company_id", "members", "is_active"}
ADJUSTMENT_FIELDS = {"adjustment_date", "kind", "description", "lines"}


def _changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _scoped_group(session, group_id: int) -> ConsolidationGroup | None:
    """Gruppe im Mandanten des Benutzers; der globale Admin sieht alle."""
    group = session.get(ConsolidationGroup, group_id)
    if group is None:
        return None
    tenant_id = current_api_tenant_id()
    if tenant_id is not None and group.tenant_id != tenant_id:
        return None
    return group


def _group_not_found():
    return jsonify({"error": "Consolidation group not found."}), 404


def _report_args() -> tuple[date | None, date | None, bool]:
    return date_arg("date_from"), date_arg("date_to"), bool_arg("include_closing_entries")


@api_bp.get("/consolidation-groups")
def list_consolidation_groups():
    include_inactive = bool_arg("include_inactive", default=True)
    session_factory = get_session_factory()
    with session_factory() as session:
        groups = list_groups(
            session=session,
            tenant_id=current_api_tenant_id(),
            include_inactive=include_inactive,
        )
        return jsonify({"groups": [serialize_group(group) for group in groups]})


@api_bp.post("/consolidation-groups")
def create_consolidation_group():
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    unknown = set(payload) - CREATE_FIELDS
    if unknown:
        return jsonify({"error": f"Unknown fields: {', '.join(sorted(unknown))}."}), 400
    try:
        parent_company_id = int(payload.get("parent_company_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "parent_company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        parent_company = api_scoped_company(session, parent_company_id)
        if parent_company is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            group = create_group(
                session=session,
                parent_company=parent_company,
                changed_by=_changed_by(),
                name=payload.get("name"),
                members=payload.get("members"),
                is_active=payload.get("is_active", True),
            )
        except ConsolidationError as exc:
            session.rollback()
            return validation_error(str(exc))
        session.commit()
        return jsonify(serialize_group(group)), 201


@api_bp.get("/consolidation-groups/<int:group_id>")
def get_consolidation_group(group_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        group = _scoped_group(session, group_id)
        if group is None:
            return _group_not_found()
        return jsonify(
            {
                **serialize_group(group),
                "adjustments": [serialize_adjustment(item) for item in group.adjustments],
            }
        )


@api_bp.patch("/consolidation-groups/<int:group_id>")
def update_consolidation_group(group_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    unknown = set(payload) - UPDATE_FIELDS
    if unknown:
        message = f"Unknown or immutable fields: {', '.join(sorted(unknown))}."
        return jsonify({"error": message}), 400
    if not payload:
        return jsonify({"error": "At least one editable field is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        group = _scoped_group(session, group_id)
        if group is None:
            return _group_not_found()
        try:
            changed = update_group(
                session=session, group=group, changed_by=_changed_by(), **payload
            )
        except ConsolidationError as exc:
            session.rollback()
            return validation_error(str(exc))
        session.commit()
        return jsonify({**serialize_group(group), "changed": changed})


@api_bp.get("/consolidation-groups/<int:group_id>/report")
def get_consolidation_report(group_id: int):
    try:
        date_from, date_to, include_closing_entries = _report_args()
    except DateArgError as exc:
        return jsonify({"error": str(exc)}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        group = _scoped_group(session, group_id)
        if group is None:
            return _group_not_found()
        try:
            report = consolidation_report(
                session=session,
                group=group,
                date_from=date_from,
                date_to=date_to,
                include_closing_entries=include_closing_entries,
            )
        except ConsolidationError as exc:
            return validation_error(str(exc))
        return jsonify(json_ready(report))


@api_bp.get("/consolidation-groups/<int:group_id>/adjustments")
def list_consolidation_adjustments(group_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        group = _scoped_group(session, group_id)
        if group is None:
            return _group_not_found()
        return jsonify(
            {
                "group_id": group.id,
                "adjustments": [serialize_adjustment(item) for item in group.adjustments],
            }
        )


@api_bp.post("/consolidation-groups/<int:group_id>/adjustments")
def create_consolidation_adjustment(group_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    unknown = set(payload) - ADJUSTMENT_FIELDS
    if unknown:
        return jsonify({"error": f"Unknown fields: {', '.join(sorted(unknown))}."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        group = _scoped_group(session, group_id)
        if group is None:
            return _group_not_found()
        try:
            adjustment = create_adjustment(
                session=session, group=group, changed_by=_changed_by(), **payload
            )
        except ConsolidationError as exc:
            session.rollback()
            return validation_error(str(exc))
        session.commit()
        return jsonify(serialize_adjustment(adjustment)), 201


@api_bp.post("/consolidation-groups/<int:group_id>/adjustments/<int:adjustment_id>/delete")
def delete_consolidation_adjustment(group_id: int, adjustment_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    session_factory = get_session_factory()
    with session_factory() as session:
        group = _scoped_group(session, group_id)
        if group is None:
            return _group_not_found()
        adjustment = session.get(ConsolidationAdjustment, adjustment_id)
        if adjustment is None or adjustment.group_id != group.id:
            return jsonify({"error": "Consolidation adjustment not found."}), 404
        delete_adjustment(
            session=session,
            adjustment=adjustment,
            changed_by=_changed_by(),
            reason=payload.get("reason"),
        )
        session.commit()
    return jsonify({"deleted": True, "adjustment_id": adjustment_id})


@api_bp.get("/exports/consolidation-report.csv")
@linkable_download
def export_consolidation_report_csv():
    group_id = request.args.get("group_id", type=int)
    if not group_id:
        return jsonify({"error": "group_id is required."}), 400
    try:
        date_from, date_to, include_closing_entries = _report_args()
    except DateArgError as exc:
        return jsonify({"error": str(exc)}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        group = _scoped_group(session, group_id)
        if group is None:
            return _group_not_found()
        try:
            report = consolidation_report(
                session=session,
                group=group,
                date_from=date_from,
                date_to=date_to,
                include_closing_entries=include_closing_entries,
            )
        except ConsolidationError as exc:
            return validation_error(str(exc))
        content = consolidation_report_csv(report)

    file_name = f"konsolidierung-gruppe-{group_id}-{date.today().isoformat()}.csv"
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )
