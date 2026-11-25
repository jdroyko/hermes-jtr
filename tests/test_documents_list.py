"""Documents list: cancelled hidden by default, sortable columns, status
filter, Traveler column only when it adds information; plus the header
switch between the 508 accessible design and the modern design."""
import datetime as dt
import re

from app.db import get_db


def _new(t, desc, days):
    s, e = (dt.date.today() + dt.timedelta(days=days)).isoformat(), (dt.date.today() + dt.timedelta(days=days + 2)).isoformat()
    r = t.post("/documents/new", data={"type": "AUTH", "traveler_id": "2", "trip_purpose": "TDY",
                                       "trip_description": desc, "start_date": s, "end_date": e, "loa_id": "2"})
    return int(re.search(r"/documents/(\d+)/", r.headers["Location"]).group(1))


def _order(html, names):
    return sorted(names, key=lambda n: html.index(n))


def test_cancelled_hidden_by_default_and_filterable(app, as_user):
    t = as_user("traveler")
    keep, gone = _new(t, "Keepme trip", 10), _new(t, "Cancelme trip", 20)
    with app.app_context():
        get_db().execute("UPDATE documents SET status='CANCELLED' WHERE id=?", (gone,))
        get_db().commit()
    page = t.get("/documents/").get_data(as_text=True)
    assert "Keepme trip" in page and "Cancelme trip" not in page
    page = t.get("/documents/?status=CANCELLED").get_data(as_text=True)
    assert "Cancelme trip" in page and "Keepme trip" not in page


def test_columns_sort_both_directions(app, as_user):
    t = as_user("traveler")
    _new(t, "Aaa trip", 30)
    _new(t, "Zzz trip", 10)
    asc = t.get("/documents/?sort=trip&dir=asc").get_data(as_text=True)
    desc = t.get("/documents/?sort=trip&dir=desc").get_data(as_text=True)
    assert asc.index("Aaa trip") < asc.index("Zzz trip")
    assert desc.index("Zzz trip") < desc.index("Aaa trip")
    assert 'aria-sort="ascending"' in asc and 'aria-sort="descending"' in desc
    # Unknown sort keys are ignored rather than reaching SQL.
    assert t.get("/documents/?sort=1;drop%20table%20users&dir=desc").status_code == 200


def test_traveler_column_hidden_for_own_documents(app, as_user):
    t = as_user("traveler")
    _new(t, "Mine trip", 12)
    assert "sort=traveler" not in t.get("/documents/").get_data(as_text=True)  # no Traveler column
    assert "sort=traveler" in as_user("admin").get("/documents/?scope=org").get_data(as_text=True)


def test_design_toggle(app, as_user):
    t = as_user("traveler")
    assert "508 accessible design: On" in t.get("/").get_data(as_text=True)
    r = t.post("/profile/theme/toggle", data={"next": "/documents"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/documents")
    page = t.get("/").get_data(as_text=True)
    assert 'class="theme-modern"' in page and "508 accessible design: Off" in page
    r = t.post("/profile/theme/toggle", data={"next": "//evil.example"})
    assert "evil.example" not in r.headers["Location"]
    assert 'class="theme-cirrus"' in t.get("/").get_data(as_text=True)
