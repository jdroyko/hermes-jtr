"""Traveler-proposed TDY locations: typed text that doesn't match an
existing rate becomes a 'pending' per_diem_rates row, usable right away
(at the Standard CONUS rate) but excluded from the autocomplete list until
the AO's final approval stamp activates it -- never a separate AO step."""
import datetime as dt
import re

from app.db import get_db


def future(days):
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()


def _doc_id(resp):
    m = re.search(r"/documents/(\d+)/", resp.headers["Location"])
    return int(m.group(1))


def _new_auth(client, start, end):
    r = client.post("/documents/new", data={"type": "AUTH", "traveler_id": "2", "trip_purpose": "TRAINING",
                                             "trip_description": "Course", "start_date": start, "end_date": end,
                                             "loa_id": "2"})
    assert r.status_code == 302
    return _doc_id(r)


def test_new_location_creates_pending_rate(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(13)
    doc = _new_auth(t, s, e)
    r = t.post("/documents/%d/itinerary" % doc,
              data={"action": "add", "location_text": "Diego Garcia", "new_state": "IO",
                    "arrive_date": s, "depart_date": e})
    assert r.status_code == 302
    with app.app_context():
        row = get_db().execute("SELECT * FROM per_diem_rates WHERE location='Diego Garcia'").fetchone()
        assert row is not None
        assert row["status"] == "pending"
        assert row["document_id"] == doc
        conus = get_db().execute("SELECT lodging, mie FROM per_diem_rates WHERE location='Standard CONUS'").fetchone()
        assert row["lodging"] == conus["lodging"] and row["mie"] == conus["mie"]
        stop = get_db().execute("SELECT rate_id FROM document_stops WHERE document_id=?", (doc,)).fetchone()
        assert stop["rate_id"] == row["id"]


def test_pending_location_excluded_from_autocomplete(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(13)
    doc = _new_auth(t, s, e)
    t.post("/documents/%d/itinerary" % doc,
          data={"action": "add", "location_text": "Diego Garcia", "new_state": "IO",
                "arrive_date": s, "depart_date": e})
    page = t.get("/documents/%d/itinerary" % doc).get_data(as_text=True)
    assert "Diego Garcia" not in page.split('id="location-options"')[1].split("</datalist>")[0]


def test_missing_state_for_unlisted_location_is_an_error(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(13)
    doc = _new_auth(t, s, e)
    r = t.post("/documents/%d/itinerary" % doc,
              data={"action": "add", "location_text": "Nowhere Base", "arrive_date": s, "depart_date": e})
    assert r.status_code == 200  # re-rendered with an error, not redirected
    assert "state or country" in r.get_data(as_text=True).lower()
    with app.app_context():
        assert get_db().execute("SELECT 1 FROM per_diem_rates WHERE location='Nowhere Base'").fetchone() is None


def test_comma_separated_location_parses_state(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(13)
    doc = _new_auth(t, s, e)
    r = t.post("/documents/%d/itinerary" % doc,
              data={"action": "add", "location_text": "Diego Garcia, IO", "arrive_date": s, "depart_date": e})
    assert r.status_code == 302
    with app.app_context():
        row = get_db().execute("SELECT * FROM per_diem_rates WHERE location='Diego Garcia'").fetchone()
        assert row is not None and row["state"] == "IO" and row["status"] == "pending"


def test_case_insensitive_match_reuses_existing_rate_no_duplicate(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(13)
    doc = _new_auth(t, s, e)
    r = t.post("/documents/%d/itinerary" % doc,
              data={"action": "add", "location_text": "san diego, ca", "arrive_date": s, "depart_date": e})
    assert r.status_code == 302
    with app.app_context():
        rows = get_db().execute("SELECT * FROM per_diem_rates WHERE location='San Diego'").fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "active"
        stop = get_db().execute("SELECT rate_id FROM document_stops WHERE document_id=?", (doc,)).fetchone()
        assert stop["rate_id"] == rows[0]["id"]


def test_ao_approval_activates_pending_location(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(13)
    doc = _new_auth(t, s, e)
    t.post("/documents/%d/itinerary" % doc,
          data={"action": "add", "location_text": "Diego Garcia", "new_state": "IO",
                "arrive_date": s, "depart_date": e})
    t.post("/documents/%d/sign" % doc, data={"certify": "1"})

    budget = as_user("budget")
    budget.post("/documents/%d/action" % doc, data={"action": "stamp"})
    with app.app_context():
        row = get_db().execute("SELECT status FROM per_diem_rates WHERE location='Diego Garcia'").fetchone()
        assert row["status"] == "pending"  # funds verification is not the final step; not approved yet

    ao = as_user("ao")
    r = ao.post("/documents/%d/action" % doc, data={"action": "stamp"})
    assert r.status_code == 302
    with app.app_context():
        doc_row = get_db().execute("SELECT status FROM documents WHERE id=?", (doc,)).fetchone()
        assert doc_row["status"] == "APPROVED"
        row = get_db().execute("SELECT status FROM per_diem_rates WHERE location='Diego Garcia'").fetchone()
        assert row["status"] == "active"
