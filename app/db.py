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


def migrate(db):
    """Idempotent, additive schema fixes for databases created before a
    given column/table existed. Safe to call on every startup."""
    cols = {r["name"] for r in db.execute("PRAGMA table_info(document_stops)")}
    if "sort_order" not in cols:
        db.execute("ALTER TABLE document_stops ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
        db.execute(
            "UPDATE document_stops SET sort_order = ("
            "  SELECT rn - 1 FROM ("
            "    SELECT id, ROW_NUMBER() OVER (PARTITION BY document_id ORDER BY arrive_date) rn"
            "    FROM document_stops"
            "  ) t WHERE t.id = document_stops.id"
            ")"
        )
    db.execute(
        "CREATE TABLE IF NOT EXISTS attachments ("
        " id INTEGER PRIMARY KEY,"
        " expense_id INTEGER NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,"
        " document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,"
        " filename TEXT NOT NULL,"
        " stored_name TEXT NOT NULL UNIQUE,"
        " content_type TEXT NOT NULL DEFAULT '',"
        " size_bytes INTEGER NOT NULL DEFAULT 0,"
        " uploaded_by INTEGER REFERENCES users(id),"
        " created_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_attach_expense ON attachments(expense_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_attach_doc ON attachments(document_id)")

    rate_cols = {r["name"] for r in db.execute("PRAGMA table_info(per_diem_rates)")}
    if "status" not in rate_cols:
        db.execute("ALTER TABLE per_diem_rates ADD COLUMN status TEXT NOT NULL DEFAULT 'active' "
                  "CHECK (status IN ('active', 'pending'))")
    if "requested_by" not in rate_cols:
        db.execute("ALTER TABLE per_diem_rates ADD COLUMN requested_by INTEGER REFERENCES users(id)")
    if "document_id" not in rate_cols:
        db.execute("ALTER TABLE per_diem_rates ADD COLUMN document_id INTEGER REFERENCES documents(id)")
    if "created_at" not in rate_cols:
        # SQLite rejects a non-constant DEFAULT on ALTER TABLE ADD COLUMN, so add it
        # plain and backfill. Fresh databases get the default from schema.sql; on a
        # migrated one, later inserts leave it NULL (nothing reads this column yet).
        db.execute("ALTER TABLE per_diem_rates ADD COLUMN created_at TEXT")
        db.execute("UPDATE per_diem_rates SET created_at = datetime('now') WHERE created_at IS NULL")
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
