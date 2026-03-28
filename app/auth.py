from flask import Blueprint, flash, redirect, render_template, request, session, url_for

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

# Phase-0 placeholder user store. Will be replaced by DB-backed user model.
USERS = {
    "admin": {"password": "admin", "role": "Admin"},
    "buchhalter": {"password": "buchhalter", "role": "Buchhalter"},
    "pruefer": {"password": "pruefer", "role": "Pruefer"},
}


@auth_bp.get("/login")
def login_form():
    return render_template("login.html")


@auth_bp.post("/login")
def login():
    username = request.form.get("username", "")
    password = request.form.get("password", "")

    user = USERS.get(username)
    if not user or user["password"] != password:
        flash("Ungültige Zugangsdaten", "error")
        return redirect(url_for("auth.login_form"))

    session["user"] = {"username": username, "role": user["role"]}
    flash("Login erfolgreich", "success")
    return redirect(url_for("main.index"))


@auth_bp.post("/logout")
def logout():
    session.pop("user", None)
    flash("Abgemeldet", "success")
    return redirect(url_for("main.index"))
