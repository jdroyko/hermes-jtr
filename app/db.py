import os
import sqlite3

from flask import current_app, g


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(seed=True):
    path = current_app.config["DATABASE"]
    if os.path.exists(path):
        os.remove(path)
    db = get_db()
    with current_app.open_resource("schema.sql") as f:
        db.executescript(f.read().decode("utf8"))
    from . import rbac
    rbac.sync_catalog(db, seed_roles=True)
    if seed:
        from .seed import seed_demo
        seed_demo(db)
    db.commit()


def setting(key, default=None, cast=str):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return cast(row["value"])
    except (TypeError, ValueError):
        return default


def audit(user_id, action, target="", details=""):
    get_db().execute("INSERT INTO audit_log(user_id, action, target, details) VALUES (?,?,?,?)",
                     (user_id, action, target, details))
