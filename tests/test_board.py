"""The kanban board must only ever move a document through the same
workflow.stamp()/return_doc() gate the ordinary Approve/Return buttons use
-- these tests hit the JSON-returning path (`X-Requested-With: fetch`) a
drag fires, and confirm it enforces the exact same rules."""
import datetime as dt
import re

from app.db import get_db


def _doc_id(resp):
    m = re.search(r"/documents/(\d+)/", resp.headers["Location"])
    return int(m.group(1))


def future(days):
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()


def _fetch_headers():
    return {"X-Requested-With": "fetch"}


def _routing_doc(as_user):
    t = as_user("traveler")
    s, e = future(7), future(9)
    r = t.post("/documents/new", data={"type": "AUTH", "traveler_id": "2", "trip_purpose": "TDY",
                                       "trip_description": "Board test trip", "start_date": s, "end_date": e,
                                       "loa_id": "2"})
    doc = _doc_id(r)
    t.post("/documents/%d/itinerary" % doc,
          data={"action": "add", "location_text": "Standard CONUS, US", "arrive_date": s, "depart_date": e})
    t.post("/documents/%d/sign" % doc, data={"certify": "1"})
    return t, doc


def test_json_stamp_success(app, as_user):
    t, doc = _routing_doc(as_user)
    budget = as_user("budget")
    r = budget.post("/documents/%d/action" % doc, data={"action": "stamp"}, headers=_fetch_headers())
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    with app.app_context():
        row = get_db().execute("SELECT status FROM documents WHERE id=?", (doc,)).fetchone()
        assert row["status"] == "ROUTING"  # advanced to the next step, not blindly "approved"


def test_json_sod_violation_does_not_mutate_status(app, as_user):
    t, doc = _routing_doc(as_user)
    with app.app_context():
        before = get_db().execute("SELECT status, current_step_seq FROM documents WHERE id=?", (doc,)).fetchone()
        before = (before["status"], before["current_step_seq"])

    # traveler cannot stamp their own document (no_self_approve)
    r = t.post("/documents/%d/action" % doc, data={"action": "stamp"}, headers=_fetch_headers())
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is False and data["error"]

    with app.app_context():
        after = get_db().execute("SELECT status, current_step_seq FROM documents WHERE id=?", (doc,)).fetchone()
        after = (after["status"], after["current_step_seq"])
    assert before == after


def test_json_return_without_remarks_rejected(app, as_user):
    t, doc = _routing_doc(as_user)
    budget = as_user("budget")
    r = budget.post("/documents/%d/action" % doc, data={"action": "return", "remarks": ""}, headers=_fetch_headers())
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is False
    with app.app_context():
        row = get_db().execute("SELECT status FROM documents WHERE id=?", (doc,)).fetchone()
        assert row["status"] == "ROUTING"


def test_json_return_with_remarks_succeeds(app, as_user):
    t, doc = _routing_doc(as_user)
    budget = as_user("budget")
    r = budget.post("/documents/%d/action" % doc, data={"action": "return", "remarks": "Fix the dates"},
                    headers=_fetch_headers())
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    with app.app_context():
        row = get_db().execute("SELECT status FROM documents WHERE id=?", (doc,)).fetchone()
        assert row["status"] == "RETURNED"


def test_board_keyboard_actions_match_direct_action(app, as_user):
    """The board's per-card <details> disclosure includes the exact same
    _actions.html partial as the document page -- confirm the stamp button
    it renders posts to the same endpoint with the same effect."""
    t, doc = _routing_doc(as_user)
    budget = as_user("budget")
    board_html = budget.get("/documents/board").get_data(as_text=True)
    assert "/documents/%d/action" % doc in board_html
    assert "Apply" in board_html and "stamp" in board_html

    r = budget.post("/documents/%d/action" % doc, data={"action": "stamp"})
    assert r.status_code == 302
    with app.app_context():
        row = get_db().execute("SELECT status FROM documents WHERE id=?", (doc,)).fetchone()
        assert row["status"] == "ROUTING"
