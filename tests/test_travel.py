"""Flights, hotels and cars search tied to the TDY locations, policy
justification, estimate feed, agency visibility and the display theme."""
import datetime as dt
import re

from app import inventory
from app.db import get_db


def future(days):
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()


def _plan(as_user, location="San Diego, CA"):
    t = as_user("traveler")
    s, e = future(10), future(13)
    r = t.post("/documents/new", data={"type": "PLAN", "traveler_id": "2", "trip_purpose": "TDY",
                                       "trip_description": "Exercise", "start_date": s, "end_date": e,
                                       "loa_id": "2"})
    doc = int(re.search(r"/documents/(\d+)/", r.headers["Location"]).group(1))
    t.post("/documents/%d/itinerary" % doc,
           data={"action": "add", "location_text": location, "arrive_date": s, "depart_date": e})
    return t, doc, s, e


def _stop_id(app, doc):
    with app.app_context():
        return get_db().execute("SELECT id FROM document_stops WHERE document_id=?", (doc,)).fetchone()["id"]


def _q(app, sql, *p):
    with app.app_context():
        return get_db().execute(sql, p).fetchall()


def test_inventory_is_deterministic_and_policy_flagged():
    a = inventory.hotels("San Diego", "CA", "2027-03-01", "2027-03-04", 215)
    assert a == inventory.hotels("San Diego", "CA", "2027-03-01", "2027-03-04", 215)
    assert all(h["in_policy"] == (h["unit_price"] <= 215) for h in a)
    cars = inventory.cars("San Diego, CA", "2027-03-01", "2027-03-04", 215)
    assert {c["in_policy"] for c in cars if "Minivan" in c["title"]} == {False}
    air = inventory.flights("Dayton, OH", "San Diego, CA", "2027-03-01", "2027-03-04")
    assert any(not f["in_policy"] for f in air) and any("GSA City Pair" in f["badges"] for f in air)


def test_hotel_results_use_tdy_location_and_selection_saved(app, as_user):
    t, doc, s, e = _plan(as_user)
    page = t.get("/documents/%d/travel?kind=LODGING" % doc).get_data(as_text=True)
    assert "San Diego" in page and "lodging maximum" in page.lower()
    stop = _stop_id(app, doc)
    opt = next(o for o in inventory.hotels("San Diego", "CA", s, e, 215) if o["in_policy"])
    r = t.post("/documents/%d/travel" % doc, data={"action": "select", "kind": "LODGING", "stop_id": stop,
                                                   "option_id": opt["id"]})
    assert r.status_code == 302
    rows = _q(app, "SELECT * FROM trip_selections WHERE document_id=?", doc)
    assert len(rows) == 1 and rows[0]["amount"] == opt["total"] and rows[0]["expense_id"] is None


def test_flight_selection_adds_airfare_expense_and_replaces(app, as_user):
    t, doc, s, e = _plan(as_user)
    stop = _stop_id(app, doc)
    opts = [o for o in inventory.flights("Dayton, OH", "San Diego, CA", s, e) if o["in_policy"]]
    base = {"action": "select", "kind": "AIR", "stop_id": stop, "origin": "Dayton, OH", "depart": s, "return": e}
    t.post("/documents/%d/travel" % doc, data=dict(base, option_id=opts[0]["id"]))
    t.post("/documents/%d/travel" % doc, data=dict(base, option_id=opts[1]["id"]))
    sels = _q(app, "SELECT * FROM trip_selections WHERE document_id=?", doc)
    exps = _q(app, "SELECT * FROM expenses WHERE document_id=? AND category='AIRFARE'", doc)
    assert len(sels) == 1 and len(exps) == 1 and exps[0]["amount"] == opts[1]["total"]
    total = _q(app, "SELECT total_estimate FROM documents WHERE id=?", doc)[0]["total_estimate"]
    assert total > opts[1]["total"]

    t.post("/documents/%d/travel" % doc, data={"action": "remove", "kind": "AIR", "selection_id": sels[0]["id"]})
    assert not _q(app, "SELECT 1 FROM expenses WHERE document_id=? AND category='AIRFARE'", doc)


def test_out_of_policy_requires_justification(app, as_user):
    t, doc, s, e = _plan(as_user)
    stop = _stop_id(app, doc)
    opt = next(o for o in inventory.cars("San Diego, CA", s, e, 215) if not o["in_policy"])
    data = {"action": "select", "kind": "RENTAL_CAR", "stop_id": stop, "option_id": opt["id"]}
    r = t.post("/documents/%d/travel" % doc, data=data)
    assert r.status_code == 200 and "Justify an out-of-policy choice" in r.get_data(as_text=True)
    r = t.post("/documents/%d/travel" % doc, data=dict(data, confirming="1"))
    assert "error-summary" in r.get_data(as_text=True)
    assert not _q(app, "SELECT 1 FROM trip_selections WHERE document_id=?", doc)
    r = t.post("/documents/%d/travel" % doc, data=dict(data, justification="Carrying equipment"))
    assert r.status_code == 302
    sel = _q(app, "SELECT * FROM trip_selections WHERE document_id=?", doc)[0]
    assert sel["in_policy"] == 0 and sel["justification"] == "Carrying equipment"
    assert "Carrying equipment" in t.get("/documents/%d" % doc).get_data(as_text=True)


def test_tampered_option_is_rejected(app, as_user):
    t, doc, _s, _e = _plan(as_user)
    r = t.post("/documents/%d/travel" % doc, data={"action": "select", "kind": "LODGING",
                                                   "stop_id": _stop_id(app, doc), "option_id": "H999"})
    assert r.status_code == 400


def test_other_users_cannot_search_or_select(app, as_user):
    _t, doc, _s, _e = _plan(as_user)
    assert as_user("traveler2").get("/documents/%d/travel" % doc).status_code == 403


def test_removing_stop_removes_its_car_expense(app, as_user):
    t, doc, s, e = _plan(as_user)
    stop = _stop_id(app, doc)
    opt = next(o for o in inventory.cars("San Diego, CA", s, e, 215) if o["in_policy"])
    t.post("/documents/%d/travel" % doc, data={"action": "select", "kind": "RENTAL_CAR", "stop_id": stop,
                                               "option_id": opt["id"]})
    t.post("/documents/%d/itinerary" % doc, data={"action": "delete", "stop_id": stop})
    assert not _q(app, "SELECT 1 FROM expenses WHERE document_id=?", doc)
    assert not _q(app, "SELECT 1 FROM trip_selections WHERE document_id=?", doc)


def test_agency_sees_selections(app, as_user):
    t, doc, s, e = _plan(as_user)
    stop = _stop_id(app, doc)
    opt = next(o for o in inventory.hotels("San Diego", "CA", s, e, 215) if o["in_policy"])
    t.post("/documents/%d/travel" % doc, data={"action": "select", "kind": "LODGING", "stop_id": stop,
                                               "option_id": opt["id"]})
    t.post("/documents/%d/sign" % doc, data={"certify": "1"})
    as_user("budget").post("/documents/%d/action" % doc, data={"action": "stamp"})
    as_user("ao").post("/documents/%d/action" % doc, data={"action": "stamp"})
    booking = _q(app, "SELECT id FROM bookings WHERE document_id=?", doc)[0]["id"]
    page = as_user("agent").get("/agency/%d" % booking).get_data(as_text=True)
    assert "selections to book" in page and opt["title"] in page


def test_theme_switch(app, as_user):
    t = as_user("traveler")
    assert 'class="theme-cirrus"' in t.get("/").get_data(as_text=True)
    assert t.post("/profile/theme", data={"theme": "emburse"}).status_code == 302
    assert 'class="theme-emburse"' in t.get("/").get_data(as_text=True)
    assert t.post("/profile/theme", data={"theme": "concur"}).status_code == 302
    assert 'class="theme-concur"' in t.get("/").get_data(as_text=True)
    assert t.post("/profile/theme", data={"theme": "cirrus"}).status_code == 302
    assert 'class="theme-cirrus"' in t.get("/").get_data(as_text=True)
    assert t.post("/profile/theme", data={"theme": "evil"}).status_code == 400


def test_memberships_show_badges_on_matching_vendors(app, as_user):
    from test_accessibility import check
    t, doc, s, e = _plan(as_user)
    assert t.post("/profile/memberships", data={"program": "UA", "member_last4": "12ab"}).status_code == 302
    t.post("/profile/memberships", data={"program": "HILTON"})
    t.post("/profile/memberships", data={"program": "HERTZ"})
    t.post("/profile/memberships", data={"program": "BOGUS"})
    t.post("/profile/memberships", data={"program": "DL", "member_last4": "123456"})
    rows = _q(app, "SELECT program, member_last4 FROM user_memberships ORDER BY program")
    assert [(r["program"], r["member_last4"]) for r in rows] == [("HERTZ", ""), ("HILTON", ""), ("UA", "12AB")]
    profile = t.get("/profile").get_data(as_text=True)
    assert "United MileagePlus" in profile and "ending 12AB" in profile
    check(profile, "/profile (memberships)")

    q = "kind=AIR&searched=1&origin=DAY&stop_id=%d&depart=%s&return=%s" % (_stop_id(app, doc), s, e)
    page = t.get("/documents/%d/travel?%s" % (doc, q)).get_data(as_text=True)
    check(page, "flights with badges")
    flights = inventory.flights("DAY", "San Diego, CA", s, e)
    if any(f["vendor"] == "United Airlines" for f in flights):
        assert "United MileagePlus member" in page
    assert 'class="vendor-logo' in page and any(f["vendor"] in page for f in flights)
    assert "Delta SkyMiles member" not in page
    hotels = t.get("/documents/%d/travel?kind=LODGING" % doc).get_data(as_text=True)
    assert "Hilton Honors member" in hotels and "Marriott Bonvoy member" not in hotels
    cars = t.get("/documents/%d/travel?kind=RENTAL_CAR" % doc).get_data(as_text=True)
    if any(c["vendor"] == "Hertz" for c in inventory.cars("San Diego, CA", s, e, 215)):
        assert "Hertz Gold Plus Rewards member" in cars

    mid = _q(app, "SELECT id FROM user_memberships WHERE program='HILTON'")[0]["id"]
    t.post("/profile/memberships", data={"action": "remove", "membership_id": mid})
    assert "Hilton Honors member" not in t.get("/documents/%d/travel?kind=LODGING" % doc).get_data(as_text=True)


def test_memberships_are_private_per_user(app, as_user):
    as_user("traveler").post("/profile/memberships", data={"program": "HILTON"})
    mid = _q(app, "SELECT id FROM user_memberships")[0]["id"]
    as_user("traveler2").post("/profile/memberships", data={"action": "remove", "membership_id": mid})
    assert _q(app, "SELECT 1 FROM user_memberships WHERE id=?", mid)


def test_brand_info():
    assert inventory.brand("Hampton Inn & Suites")["program"] == "HILTON"
    assert inventory.brand("Hampton Inn & Suites")["slug"] == "hampton-inn-suites"
    assert inventory.brand("Delta Air Lines")["mark"] == "DL"
    assert inventory.brand("Government lodging")["program"] is None


def test_result_cards_show_real_logos(app, as_user):
    t, doc, s, e = _plan(as_user)
    q = "kind=AIR&searched=1&origin=DAY&stop_id=%d&depart=%s&return=%s" % (_stop_id(app, doc), s, e)
    page = t.get("/documents/%d/travel?%s" % (doc, q)).get_data(as_text=True)
    codes = {inventory.brand(f["vendor"])["logo"] for f in inventory.flights("DAY", "San Diego, CA", s, e)}
    for code in codes:
        assert "/static/img/logos/%s.png" % code in page
    hotels = t.get("/documents/%d/travel?kind=LODGING" % doc).get_data(as_text=True)
    assert "/static/img/logos/hilton.png" in hotels and "/static/img/logos/marriott.png" in hotels
    assert t.get("/static/img/logos/aa.png").status_code == 200
