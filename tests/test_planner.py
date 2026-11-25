"""Simple 'Plan a trip' flow: where -> when -> how -> drive/fly -> stay -> review."""
import datetime as dt
import re

from app import geo, inventory
from app.db import get_db
from test_accessibility import check


def future(days):
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()


def _q(app, sql, *p):
    with app.app_context():
        return get_db().execute(sql, p).fetchall()


def _start(t, destination="San Diego, CA", **extra):
    r = t.post("/plan/", data=dict({"destination": destination}, **extra))
    assert r.status_code == 302 and r.headers["Location"].endswith("/plan/when")
    s, e = future(10), future(13)
    r = t.post("/plan/when", data={"start_date": s, "end_date": e})
    assert r.status_code == 302
    doc = int(re.search(r"/plan/(\d+)/how", r.headers["Location"]).group(1))
    return doc, s, e


def test_where_saves_location_for_per_diem_and_booking(app, as_user):
    t = as_user("traveler")
    doc, s, e = _start(t)
    d = _q(app, "SELECT * FROM documents WHERE id=?", doc)[0]
    assert d["is_plan"] == 1 and d["doc_type"] == "AUTH" and d["trip_description"] == "Trip to San Diego, CA"
    stop = _q(app, "SELECT r.location FROM document_stops s JOIN per_diem_rates r ON r.id=s.rate_id "
                   "WHERE s.document_id=?", doc)[0]
    assert stop["location"] == "San Diego"  # per diem
    assert "San Diego" in t.get("/plan/%d/stay" % doc).get_data(as_text=True)  # booking


def test_any_place_is_accepted(app, as_user):
    t = as_user("traveler")
    doc, _s, _e = _start(t, "SAn Francisco")
    d = _q(app, "SELECT trip_description FROM documents WHERE id=?", doc)[0]
    assert d["trip_description"] == "Trip to San Francisco, CA"
    doc, _s, _e = _start(t, "Nowhere Base", new_state="KS")
    row = _q(app, "SELECT s.location_text, r.location FROM document_stops s JOIN per_diem_rates r "
                  "ON r.id=s.rate_id WHERE s.document_id=?", doc)[0]
    assert row["location_text"] == "Nowhere Base" and row["location"] == "Standard CONUS"


def test_other_hands_off_to_full_authorization(app, as_user):
    t = as_user("traveler")
    doc, _s, _e = _start(t)
    r = t.post("/plan/%d/how" % doc, data={"mode": "OTHER"})
    assert r.headers["Location"].endswith("/documents/%d/trip" % doc)
    assert t.post("/plan/%d/how" % doc, data={}).status_code == 200


def test_personal_vehicle_comparison_saved(app, as_user):
    t = as_user("traveler")
    doc, s, e = _start(t)
    assert t.post("/plan/%d/how" % doc, data={"mode": "POV"}).headers["Location"].endswith("/drive")
    form = {"start": "Dayton, OH", "destination": "San Diego, CA", "mpg": "25", "miles": ""}
    page = t.post("/plan/%d/drive" % doc, data=dict(form, action="calculate")).get_data(as_text=True)
    assert "Mileage reimbursement" in page and "Flying instead" in page
    assert "Three lowest in-policy flights" in page and page.count('<li class="result-card">') == 3
    r = t.post("/plan/%d/drive" % doc, data=dict(form, action="save"))
    assert r.headers["Location"].endswith("/plan/%d/stay" % doc)
    cmp_ = _q(app, "SELECT * FROM trip_comparisons WHERE document_id=?", doc)[0]
    assert 1700 < cmp_["one_way_miles"] < 2400 and cmp_["airfare_estimate"] > 0
    assert cmp_["gas_price"] == geo.GAS["CA"] and cmp_["distance_method"] == "city"
    exp = _q(app, "SELECT * FROM expenses WHERE document_id=? AND category='POV_MILEAGE'", doc)
    assert len(exp) == 1 and exp[0]["miles"] == cmp_["round_trip_miles"]
    # Saving again replaces rather than duplicates; entered miles win.
    t.post("/plan/%d/drive" % doc, data=dict(form, miles="2100", action="save"))
    exp = _q(app, "SELECT miles FROM expenses WHERE document_id=? AND category='POV_MILEAGE'", doc)
    assert [x["miles"] for x in exp] == [4200]
    assert "Personal vehicle compared with flying" in t.get("/documents/%d" % doc).get_data(as_text=True)


def test_unknown_places_ask_for_miles(app, as_user):
    t = as_user("traveler")
    doc, _s, _e = _start(t)
    page = t.post("/plan/%d/drive" % doc, data={"start": "Atlantis", "destination": "San Diego, CA",
                                                 "action": "calculate"}).get_data(as_text=True)
    assert "Enter the miles yourself" in page


def test_round_trip_flight_then_hotel_then_review(app, as_user):
    t = as_user("traveler")
    doc, s, e = _start(t)
    q = {"searched": "1", "trip_type": "ROUND", "from_0": "DAY - Dayton, OH", "to_0": "SAN", "date_0": s,
         "return_date": e}
    page = t.get("/plan/%d/fly" % doc, query_string=q).get_data(as_text=True)
    assert "in-policy flights" in page and "DAY to SAN" in page
    opt = inventory.arrange(inventory.flights("DAY", "SAN", s, e), "price", True)[0]
    r = t.post("/plan/%d/fly" % doc, data=dict(q, option_id=opt["id"]))
    assert r.headers["Location"].endswith("/plan/%d/stay" % doc)
    assert _q(app, "SELECT amount FROM expenses WHERE document_id=? AND category='AIRFARE'", doc)[0]["amount"] \
        == opt["total"]

    hotel = next(o for o in inventory.hotels("San Diego", "CA", s, e, 215) if o["in_policy"])
    out = next(o for o in inventory.hotels("San Diego", "CA", s, e, 215) if not o["in_policy"])
    assert t.post("/plan/%d/stay" % doc, data={"option_id": out["id"]}).status_code == 400
    assert t.post("/plan/%d/stay" % doc, data={"option_id": hotel["id"]}).status_code == 302
    page = t.get("/plan/%d/done" % doc).get_data(as_text=True)
    assert opt["title"] in page and hotel["title"] in page and "Review and sign" in page


def test_one_way_and_multi_city(app, as_user):
    t = as_user("traveler")
    doc, s, e = _start(t)
    page = t.get("/plan/%d/fly?trip_type=MULTI" % doc).get_data(as_text=True)
    assert "Flight 3" in page
    q = {"searched": "1", "trip_type": "MULTI", "from_0": "DAY", "to_0": "SAN", "date_0": s,
         "from_1": "SAN", "to_1": "DEN", "date_1": future(12), "from_2": "DEN", "to_2": "DAY", "date_2": e}
    page = t.get("/plan/%d/fly" % doc, query_string=q).get_data(as_text=True)
    assert "Multi-city DAY to SAN to DEN to DAY" in page
    r = t.post("/plan/%d/fly" % doc, data=dict(q, option_id="M0"))
    assert r.status_code == 302
    sel = _q(app, "SELECT * FROM trip_selections WHERE document_id=? AND kind='AIR'", doc)[0]
    assert sel["title"].startswith("Multi-city") and sel["end_date"] == e

    q = {"searched": "1", "trip_type": "ONE", "from_0": "DAY", "to_0": "SAN", "date_0": s}
    assert "one way" in t.get("/plan/%d/fly" % doc, query_string=q).get_data(as_text=True)
    bad = dict(q, date_0=future(40))
    assert "within your trip" in t.get("/plan/%d/fly" % doc, query_string=bad).get_data(as_text=True)


def test_other_travelers_cannot_open_my_plan(app, as_user):
    doc, _s, _e = _start(as_user("traveler"))
    assert as_user("traveler2").get("/plan/%d/how" % doc).status_code == 403


def test_planner_screens_a11y(app, as_user):
    t = as_user("traveler")
    for url in ("/plan/",):
        check(t.get(url).get_data(as_text=True), url)
    check(t.post("/plan/", data={"destination": ""}).get_data(as_text=True), "where (error)")
    t.post("/plan/", data={"destination": "San Diego, CA"})
    check(t.get("/plan/when").get_data(as_text=True), "when")
    check(t.post("/plan/when", data={}).get_data(as_text=True), "when (error)")
    doc, s, e = _start(t)
    check(t.post("/plan/%d/how" % doc, data={}).get_data(as_text=True), "how (error)")
    pages = ["/plan/%d/how" % doc, "/plan/%d/drive" % doc, "/plan/%d/fly" % doc, "/plan/%d/fly?trip_type=MULTI" % doc,
             "/plan/%d/fly?searched=1&trip_type=ROUND&from_0=DAY&to_0=SAN&date_0=%s&return_date=%s" % (doc, s, e),
             "/plan/%d/fly?searched=1&trip_type=ROUND" % doc, "/plan/%d/stay" % doc, "/plan/%d/done" % doc]
    for url in pages:
        resp = t.get(url)
        assert resp.status_code == 200, url
        check(resp.get_data(as_text=True), url)
    html = t.post("/plan/%d/drive" % doc, data={"start": "Dayton, OH", "destination": "San Diego, CA",
                                                 "action": "calculate"}).get_data(as_text=True)
    check(html, "drive (result)")
    check(t.post("/plan/%d/drive" % doc, data={"start": "", "action": "calculate"}).get_data(as_text=True),
          "drive (error)")
