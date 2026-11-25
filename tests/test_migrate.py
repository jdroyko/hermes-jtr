"""migrate() must upgrade a database created before newer columns existed.
The normal fixtures build fresh databases from schema.sql, so they never
exercise the ALTER TABLE paths -- this test does."""
import sqlite3

from app.db import migrate


def _old_database(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE documents (id INTEGER PRIMARY KEY);
        CREATE TABLE expenses (id INTEGER PRIMARY KEY);
        CREATE TABLE document_stops (
            id INTEGER PRIMARY KEY, document_id INTEGER, rate_id INTEGER,
            arrive_date TEXT, depart_date TEXT, lodging_nightly REAL, gov_quarters INTEGER DEFAULT 0);
        CREATE TABLE per_diem_rates (
            id INTEGER PRIMARY KEY, location TEXT NOT NULL, state TEXT NOT NULL,
            season_start TEXT NOT NULL DEFAULT '01-01', season_end TEXT NOT NULL DEFAULT '12-31',
            lodging REAL NOT NULL, mie REAL NOT NULL, fiscal_year INTEGER NOT NULL);
        INSERT INTO per_diem_rates(location, state, lodging, mie, fiscal_year)
            VALUES ('San Diego', 'CA', 215, 86, 2027);
        INSERT INTO document_stops(document_id, rate_id, arrive_date, depart_date)
            VALUES (1, 1, '2027-03-02', '2027-03-04'), (1, 1, '2027-03-01', '2027-03-02');
    """)
    conn.commit()
    return conn


def test_migrate_upgrades_old_database(tmp_path):
    conn = _old_database(str(tmp_path / "old.sqlite3"))
    migrate(conn)
    migrate(conn)  # idempotent

    rate_cols = {r["name"] for r in conn.execute("PRAGMA table_info(per_diem_rates)")}
    assert {"status", "requested_by", "document_id", "created_at"} <= rate_cols
    rate = conn.execute("SELECT status, created_at FROM per_diem_rates").fetchone()
    assert rate["status"] == "active" and rate["created_at"]

    orders = [r["sort_order"] for r in conn.execute(
        "SELECT sort_order FROM document_stops ORDER BY arrive_date")]
    assert orders == [0, 1]  # backfilled in arrival-date order
    stop_cols = {r["name"] for r in conn.execute("PRAGMA table_info(document_stops)")}
    assert {"location_text", "location_state"} <= stop_cols
    assert conn.execute("SELECT 1 FROM per_diem_rates WHERE location='Standard OCONUS'").fetchone()

    assert conn.execute("SELECT name FROM sqlite_master WHERE name='attachments'").fetchone()

    doc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    assert {"is_plan", "is_template", "template_id", "preapproved_by"} <= doc_cols
    for table in ("plan_assignees", "bookings", "booking_items"):
        assert conn.execute("SELECT name FROM sqlite_master WHERE name=?", (table,)).fetchone(), table
