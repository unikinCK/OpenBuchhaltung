"""Verwaltung: Mandanten-, Gesellschafts- und Benutzeranlage."""

from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.auth import (
    ROLE_ADMIN,
    ROLE_BUCHHALTER,
    ROLE_PRUEFER,
    ROLE_SUPPORT,
    current_tenant_id,
    current_user,
    generate_api_token,
    hash_api_token,
    hash_password,
    password_policy_error,
    unlock_user_login,
)
from app.services.scoping import scoped_select
from app.services.security_events import record_security_event
from app.web.blueprint import main_bp
from app.web.helpers import company_context, get_session_factory
from domain.models import Account, Company, Tenant, User

ROLES = (ROLE_ADMIN, ROLE_BUCHHALTER, ROLE_PRUEFER, ROLE_SUPPORT)


def _is_admin() -> bool:
    user = current_user()
    return user is not None and user["role"] == ROLE_ADMIN


def _actor() -> str:
    user = current_user()
    return user["username"] if user else "web-form"


def _render_admin_page(**extra):
    tenant_scope = current_tenant_id()
    session_factory = get_session_factory()
    with session_factory() as session:
        tenant_query = scoped_select(Tenant).order_by(Tenant.name)
        if tenant_scope is not None:
            tenant_query = tenant_query.where(Tenant.id == tenant_scope)
        tenants = session.execute(tenant_query).scalars().all()
        companies, selected_company_id = company_context(session)
        account_count = (
            len(
                session.execute(scoped_select(Account, company_id=selected_company_id))
                .scalars()
                .all()
            )
            if selected_company_id
            else 0
        )
        user_query = select(User).options(selectinload(User.tenant)).order_by(User.username)
        if tenant_scope is not None:
            user_query = user_query.where(User.tenant_id == tenant_scope)
        users = session.execute(user_query).scalars().all() if _is_admin() else []

    return render_template(
        "verwaltung.html",
        tenants=tenants,
        companies=companies,
        users=users,
        roles=ROLES,
        selected_company_id=selected_company_id,
        account_count=account_count,
        is_global_admin=tenant_scope is None and _is_admin(),
        is_admin=_is_admin(),
        **extra,
    )


@main_bp.get("/verwaltung")
def admin_page():
    return _render_admin_page()


@main_bp.post("/tenants")
def create_tenant_and_company():
    # Wie die API (api_can_create_tenant): nur globale Administratoren.
    if not _is_admin() or current_tenant_id() is not None:
        flash("Neue Mandanten kann nur ein globaler Administrator anlegen.", "error")
        return redirect(url_for("main.admin_page"))

    tenant_name = request.form.get("tenant_name", "").strip()
    company_name = request.form.get("company_name", "").strip()

    if not tenant_name or not company_name:
        flash("Mandant und Gesellschaft sind Pflichtfelder.", "error")
        return redirect(url_for("main.admin_page"))

    session_factory = get_session_factory()
    with session_factory() as session:
        tenant = Tenant(name=tenant_name)
        company = Company(name=company_name, currency_code="EUR", tenant=tenant)
        session.add_all([tenant, company])

        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            flash("Mandant oder Gesellschaft existiert bereits.", "error")
            return redirect(url_for("main.admin_page"))

    flash("Mandant und Gesellschaft wurden angelegt.", "success")
    return redirect(url_for("main.admin_page"))


@main_bp.post("/users")
def create_user_action():
    if not _is_admin():
        flash("Benutzer verwalten kann nur ein Administrator.", "error")
        return redirect(url_for("main.admin_page"))

    tenant_scope = current_tenant_id()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", ROLE_BUCHHALTER).strip()
    tenant_id = request.form.get("tenant_id", type=int)
    if tenant_scope is not None:
        tenant_id = tenant_scope

    if not username or not password or role not in ROLES:
        flash("Benutzername, Passwort und gültige Rolle sind Pflichtfelder.", "error")
        return redirect(url_for("main.admin_page"))
    policy_error = password_policy_error(password)
    if policy_error:
        flash(policy_error, "error")
        return redirect(url_for("main.admin_page"))

    session_factory = get_session_factory()
    with session_factory() as session:
        if tenant_id is not None and session.get(Tenant, tenant_id) is None:
            flash("Mandant wurde nicht gefunden.", "error")
            return redirect(url_for("main.admin_page"))
        user = User(
            username=username,
            password_hash=hash_password(password),
            role=role,
            tenant_id=tenant_id,
        )
        session.add(user)
        try:
            session.flush()
            record_security_event(
                session,
                user=user,
                action="created",
                actor=_actor(),
                payload={"username": username, "role": role, "tenant_id": tenant_id},
            )
            session.commit()
        except IntegrityError:
            session.rollback()
            flash("Benutzer existiert bereits.", "error")
            return redirect(url_for("main.admin_page"))

    flash("Benutzer wurde angelegt.", "success")
    return redirect(url_for("main.admin_page"))


def _load_managed_user(session, user_id: int) -> User | None:
    tenant_scope = current_tenant_id()
    user = session.get(User, user_id)
    if user is None or (tenant_scope is not None and user.tenant_id != tenant_scope):
        return None
    return user


@main_bp.post("/users/<int:user_id>/api-token")
def rotate_user_api_token_action(user_id: int):
    if not _is_admin():
        flash("API-Token verwalten kann nur ein Administrator.", "error")
        return redirect(url_for("main.admin_page"))

    token = generate_api_token()
    session_factory = get_session_factory()
    with session_factory() as session:
        user = _load_managed_user(session, user_id)
        if user is None:
            flash("Benutzer wurde nicht gefunden.", "error")
            return redirect(url_for("main.admin_page"))
        username = user.username
        user.api_token_hash = hash_api_token(token)
        user.api_token_last4 = token[-4:]
        record_security_event(
            session,
            user=user,
            action="api_token_rotated",
            actor=_actor(),
            payload={"api_token_last4": token[-4:]},
        )
        session.commit()

    # Das Token wird einmalig direkt in der Seite angezeigt — nicht per flash(),
    # das würde es im (signierten, aber lesbaren) Session-Cookie ablegen.
    return _render_admin_page(new_api_token={"username": username, "token": token})


@main_bp.post("/users/<int:user_id>/active")
def set_user_active_action(user_id: int):
    if not _is_admin():
        flash("Benutzer verwalten kann nur ein Administrator.", "error")
        return redirect(url_for("main.admin_page"))

    is_active = request.form.get("is_active") == "true"
    session_factory = get_session_factory()
    with session_factory() as session:
        user = _load_managed_user(session, user_id)
        if user is None:
            flash("Benutzer wurde nicht gefunden.", "error")
            return redirect(url_for("main.admin_page"))
        user.is_active = is_active
        record_security_event(
            session,
            user=user,
            action="activated" if is_active else "deactivated",
            actor=_actor(),
        )
        session.commit()

    flash("Benutzerstatus wurde aktualisiert.", "success")
    return redirect(url_for("main.admin_page"))


@main_bp.post("/users/<int:user_id>/unlock")
def unlock_user_action(user_id: int):
    """Hebt die Login-Sperre (Rate-Limit nach Fehlversuchen) eines Benutzers auf."""
    if not _is_admin():
        flash("Benutzer verwalten kann nur ein Administrator.", "error")
        return redirect(url_for("main.admin_page"))

    session_factory = get_session_factory()
    with session_factory() as session:
        user = _load_managed_user(session, user_id)
        if user is None:
            flash("Benutzer wurde nicht gefunden.", "error")
            return redirect(url_for("main.admin_page"))
        removed = unlock_user_login(session, username=user.username)
        record_security_event(
            session,
            user=user,
            action="login_unlocked",
            actor=_actor(),
            payload={"removed_attempts": removed},
        )
        session.commit()

    flash("Login-Sperre wurde aufgehoben.", "success")
    return redirect(url_for("main.admin_page"))


@main_bp.post("/users/<int:user_id>/password")
def set_user_password_action(user_id: int):
    """Administrator setzt das Passwort eines Benutzers neu."""
    if not _is_admin():
        flash("Benutzer verwalten kann nur ein Administrator.", "error")
        return redirect(url_for("main.admin_page"))

    new_password = request.form.get("new_password", "")
    policy_error = password_policy_error(new_password)
    if policy_error:
        flash(policy_error, "error")
        return redirect(url_for("main.admin_page"))

    session_factory = get_session_factory()
    with session_factory() as session:
        user = _load_managed_user(session, user_id)
        if user is None:
            flash("Benutzer wurde nicht gefunden.", "error")
            return redirect(url_for("main.admin_page"))
        user.password_hash = hash_password(new_password)
        record_security_event(session, user=user, action="password_reset", actor=_actor())
        session.commit()

    flash("Passwort wurde gesetzt.", "success")
    return redirect(url_for("main.admin_page"))
