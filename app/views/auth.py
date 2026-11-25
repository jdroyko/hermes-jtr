from flask import Blueprint, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from ..db import audit, get_db

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    errors = {}
    username = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username:
            errors["username"] = "Enter your username."
        if not password:
            errors["password"] = "Enter your password."
        if not errors:
            row = get_db().execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
            if row and check_password_hash(row["password_hash"], password):
                csrf = session.get("csrf")
                session.clear()
                session["csrf"] = csrf
                session["uid"] = row["id"]
                import time
                session["last_seen"] = time.time()
                audit(row["id"], "LOGIN", "user:%d" % row["id"])
                get_db().commit()
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("main.home"))
            errors["password"] = "The username or password is incorrect."
            audit(None, "LOGIN_FAILED", username)
            get_db().commit()
    return render_template("login.html", errors=errors, username=username,
                           timed_out=request.args.get("timeout")), (400 if errors else 200)


@bp.route("/logout", methods=["POST"])
def logout():
    if g.user:
        audit(g.user["id"], "LOGOUT", "user:%d" % g.user["id"])
        get_db().commit()
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/session/extend", methods=["POST"])
def extend():
    return jsonify(ok=True)


@bp.route("/session/status")
def ping_status():
    return jsonify(ok=True)


@bp.route("/accessibility")
def accessibility():
    return render_template("accessibility.html")
