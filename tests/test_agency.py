"""Travel agency booking queue for approved travel plans."""
import datetime as dt
import re

from app.db import get_db


def future(days):
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()


def _approved_plan(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(12)
    r = t.post("/documents/new", data={"type": "PLAN", "traveler_id": "2", "trip_purpose": "TDY",
                                       "trip_description": "Conference", "start_date": s, "end_date": e,
                                       "loa_id": "2"})
    doc = int(re.search(r"/documents/(\d+)/", r.headers["Location"]).group(1))
    t.post("/documents/%d/itinerary" % doc,
           data={"action": "add", "location_text": "Standard CONUS, US", "arrive_date": s, "depart_date": e})
    t.post("/documents/%d/sign" % doc, data={"certify": "1"})
    as_user("budget").post("/documents/%d/action" % doc, data={"action": "stamp"})
    as_user("ao").post("/documents/%d/action" % doc, data={"action": "stamp"})
    with app.app_context():
        booking = get_db().execute("SELECT id FROM bookings WHERE document_id=?", (doc,)).fetchone()["id"]
    return t, doc, booking


def _status(app, booking):
    with app.app_context():
        return get_db().execute("SELECT status FROM bookings WHERE id=?", (booking,)).fetchone()["status"]


def test_agent_books_trip_and_traveler_sees_it(app, as_user):
    t, doc, booking = _approved_plan(app, as_user)
    agent = as_user("agent")
    assert "Conference" in agent.get("/agency/").get_data(as_text=True)

    r = agent.post("/agency/%d" % booking, data={"action": "add_item", "kind": "AIR", "vendor": "United",
                                                  "confirmation": "QX7P2L", "amount": "412.30",
                                                  "details": "UA 1234"})
    assert r.status_code == 302
    r = agent.post("/agency/%d" % booking, data={"action": "mark_booked"})
    assert r.status_code == 302
    assert _status(app, booking) == "BOOKED"

    page = t.get("/documents/%d" % doc).get_data(as_text=True)
    assert "QX7P2L" in page and "BOOKED" in page

    agent.post("/agency/%d" % booking, data={"action": "reopen"})
    assert _status(app, booking) == "PENDING"


def test_mark_booked_requires_a_reservation(app, as_user):
    _t, _doc, booking = _approved_plan(app, as_user)
    as_user("agent").post("/agency/%d" % booking, data={"action": "mark_booked"})
    assert _status(app, booking) == "PENDING"


def test_add_item_validates(app, as_user):
    _t, _doc, booking = _approved_plan(app, as_user)
    r = as_user("agent").post("/agency/%d" % booking, data={"action": "add_item", "kind": "AIR", "vendor": "",
                                                            "confirmation": "", "amount": "abc"})
    assert r.status_code == 200 and "error-summary" in r.get_data(as_text=True)


def test_non_agency_users_are_denied(app, as_user):
    _t, _doc, booking = _approved_plan(app, as_user)
    for user in ("traveler", "ao"):
        c = as_user(user)
        assert c.get("/agency/").status_code == 403
        assert c.post("/agency/%d" % booking, data={"action": "mark_booked"}).status_code == 403


def test_amending_plan_cancels_booking_and_reapproval_reopens(app, as_user):
    t, doc, booking = _approved_plan(app, as_user)
    t.post("/documents/%d/action" % doc, data={"action": "amend", "remarks": "Extra day"})
    assert _status(app, booking) == "CANCELLED"
    assert as_user("agent").post("/agency/%d" % booking, data={"action": "reopen"}).status_code == 400

    t.post("/documents/%d/sign" % doc, data={"certify": "1"})
    as_user("budget").post("/documents/%d/action" % doc, data={"action": "stamp"})
    as_user("ao").post("/documents/%d/action" % doc, data={"action": "stamp"})
    assert _status(app, booking) == "PENDING"
