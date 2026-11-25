import datetime as dt
import os
import secrets
import time

from flask import Flask, abort, g, redirect, render_template, request, session, url_for

from . import db as dbmod
from . import inventory, rbac, workflow, jtr

THEMES = {"cirrus": ("Cirrus style (508 accessible)", "Rounded pill buttons and Inter type, with full Section 508 contrast, focus and target sizes."),
          "modern": ("Modern style (not 508 compliant)", "The Cirrus look as published: light borders, soft focus glow, compact type. Does not meet 508 contrast or focus rules."),
          "emburse": ("Emburse style", "Side navigation, rounded cards, roomy layout."),
          "concur": ("Concur style", "Top navigation bar, squared corners, compact layout.")}
ACCESSIBLE_THEME, MODERN_THEME = "cirrus", "modern"


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        DATABASE=os.path.join(app.instance_path, "travel.sqlite3"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SECURE_COOKIES") == "1",
        UPLOAD_FOLDER=os.path.join(app.instance_path, "uploads"),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        ALLOWED_UPLOAD_EXTENSIONS={".pdf", ".jpg", ".jpeg", ".png"},
    )
    if test_config:
        app.config.update(test_config)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    app.teardown_appcontext(dbmod.close_db)

    @app.cli.command("init-db")
    def init_db_cmd():
        """Drop and recreate the database with demo data."""
        dbmod.init_db()
        print("Initialized database at", app.config["DATABASE"])

    with app.app_context():
        if not os.path.exists(app.config["DATABASE"]):
            dbmod.init_db()
        else:
            dbmod.migrate(dbmod.get_db())
            rbac.sync_catalog(dbmod.get_db())

    # ------------------------------------------------------------------ auth
    @app.before_request
    def load_user():
        g.user = None
        uid = session.get("uid")
        if uid:
            timeout = dbmod.setting("session_timeout_minutes", 15, int) * 60
            if time.time() - session.get("last_seen", 0) > timeout:
                session.clear()
                if request.endpoint not in ("static", "auth.login"):
                    return redirect(url_for("auth.login", timeout=1))
            else:
                row = dbmod.get_db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
                if row:
                    g.user = row
                    if request.endpoint != "auth.ping_status":
                        session["last_seen"] = time.time()
                else:
                    session.clear()
        public = ("static", "auth.login", "auth.accessibility")
        if g.user is None and request.endpoint not in public and request.endpoint is not None:
            return redirect(url_for("auth.login", next=request.path))

    # ------------------------------------------------------------------ CSRF
    @app.before_request
    def csrf_protect():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        if request.method == "POST" and not app.config.get("TESTING_NO_CSRF"):
            token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not token or not secrets.compare_digest(token, session["csrf"]):
                abort(400, "Your form session expired. Reload the page and try again.")

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; "
                                                   "style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------- templates
    logo_dir = os.path.join(app.static_folder, "img", "logos")
    logos = {os.path.splitext(f)[0]: f for f in os.listdir(logo_dir)
             if f.endswith((".svg", ".png"))} if os.path.isdir(logo_dir) else {}

    def my_programs(uid):
        if not uid:
            return set()
        return {r["program"] for r in dbmod.get_db().execute(
            "SELECT program FROM user_memberships WHERE user_id=?", (uid,))}

    @app.context_processor
    def inject():
        uid = g.user["id"] if g.get("user") else None
        return dict(
            can=lambda key, org_id=None: rbac.has_perm(uid, key, org_id),
            csrf_token=session.get("csrf", ""),
            STATUS_LABELS=workflow.STATUS_LABELS,
            DOC_TYPE_LABELS=workflow.DOC_TYPE_LABELS,
            doc_label=workflow.doc_label,
            BOOKING_LABELS=workflow.BOOKING_LABELS,
            BOOKING_KINDS=workflow.BOOKING_KINDS,
            CATEGORY_LABELS=jtr.CATEGORY_LABELS,
            TRAVEL_KINDS=inventory.KINDS,
            THEMES=THEMES,
            PROGRAMS=inventory.PROGRAMS,
            LOGOS=logos,
            my_programs=my_programs(uid),
            org_name=dbmod.setting("org_name", "Travel"),
            session_timeout=dbmod.setting("session_timeout_minutes", 15, int),
            today=dt.date.today().isoformat(),
        )

    @app.template_filter("money")
    def money(v):
        return "${:,.2f}".format(float(v or 0))

    @app.template_filter("datefmt")
    def datefmt(v):
        try:
            return dt.date.fromisoformat(str(v)[:10]).strftime("%d %b %Y")
        except ValueError:
            return v

    # ---------------------------------------------------------------- errors
    def err(code, title, msg):
        def handler(e):
            detail = getattr(e, "description", None) if code == 400 else None
            return render_template("error.html", code=code, title=title, message=detail or msg), code
        return handler

    app.register_error_handler(400, err(400, "Bad request", "The request could not be processed."))
    app.register_error_handler(401, err(401, "Sign in required", "Please sign in to continue."))
    app.register_error_handler(403, err(403, "Access denied",
                                        "Your roles do not grant access to this page. Contact your "
                                        "Defense Travel Administrator if you believe this is an error."))
    app.register_error_handler(404, err(404, "Page not found", "The page you requested does not exist."))
    app.register_error_handler(413, err(413, "File too large",
                                        "That file is larger than the 10 MB limit. Reduce the file size and "
                                        "try again."))

    from .views import auth, main, documents, admin, reports, attachments, plans, agency, travel, planner
    for bp in (auth.bp, main.bp, documents.bp, admin.bp, reports.bp, attachments.bp, plans.bp, agency.bp,
               travel.bp, planner.bp):
        app.register_blueprint(bp)
    return app
