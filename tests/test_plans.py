"""Travel plans: a traveler's plan routes like an authorization and, once
approved, goes to the travel agency. An AO's group plan is issued as one
authorization per traveler; each still needs funds verification, after
which the AO's approval is applied automatically."""
import datetime as dt
import re

from app.db import get_db


def future(days):
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()


def _doc_id(resp):
    return int(re.search(r"/(?:documents|plans)/(\d+)", resp.headers["Location"]).group(1))


def _user_id(app, username):
    with app.app_context():
        return get_db().execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]


def _row(app, sql, *params):
    with app.app_context():
        return get_db().execute(sql, params).fetchone()


def _traveler_plan(as_user, doc_type="PLAN"):
    t = as_user("traveler")
    s, e = future(10), future(13)
    r = t.post("/documents/new", data={"type": doc_type, "traveler_id": "2", "trip_purpose": "TDY",
                                       "trip_description": "Site survey", "start_date": s, "end_date": e,
                                       "loa_id": "2"})
    assert r.status_code == 302
    doc = _doc_id(r)
    t.post("/documents/%d/itinerary" % doc,
           data={"action": "add", "location_text": "San Diego, CA", "arrive_date": s, "depart_date": e})
    t.post("/documents/%d/expenses" % doc, data={"action": "add", "expense_date": s, "category": "AIRFARE",
                                                 "amount": "450", "payment_method": "CBA"})
    r = t.post("/documents/%d/sign" % doc, data={"certify": "1"})
    assert r.status_code == 302
    return t, doc


def test_traveler_plan_approval_sends_to_agency(app, as_user):
    t, doc = _traveler_plan(as_user)
    assert _row(app, "SELECT is_plan FROM documents WHERE id=?", doc)["is_plan"] == 1
    assert "Travel plan" in t.get("/documents/%d" % doc).get_data(as_text=True)

    as_user("budget").post("/documents/%d/action" % doc, data={"action": "stamp"})
    assert _row(app, "SELECT 1 FROM bookings WHERE document_id=?", doc) is None
    as_user("ao").post("/documents/%d/action" % doc, data={"action": "stamp"})

    assert _row(app, "SELECT status FROM documents WHERE id=?", doc)["status"] == "APPROVED"
    assert _row(app, "SELECT status FROM bookings WHERE document_id=?", doc)["status"] == "PENDING"
    page = t.get("/documents/%d" % doc).get_data(as_text=True)
    assert "Authorization (from plan)" in page and "Waiting for travel agency" in page


def test_plain_authorization_creates_no_booking(app, as_user):
    _t, doc = _traveler_plan(as_user, doc_type="AUTH")
    as_user("budget").post("/documents/%d/action" % doc, data={"action": "stamp"})
    as_user("ao").post("/documents/%d/action" % doc, data={"action": "stamp"})
    assert _row(app, "SELECT status FROM documents WHERE id=?", doc)["status"] == "APPROVED"
    assert _row(app, "SELECT 1 FROM bookings WHERE document_id=?", doc) is None


def test_override_to_final_also_creates_booking(app, as_user):
    _t, doc = _traveler_plan(as_user)
    as_user("budget").post("/documents/%d/action" % doc, data={"action": "stamp"})
    as_user("admin").post("/documents/%d/action" % doc, data={"action": "override", "remarks": "AO on leave"})
    assert _row(app, "SELECT status FROM documents WHERE id=?", doc)["status"] == "APPROVED"
    assert _row(app, "SELECT status FROM bookings WHERE document_id=?", doc)["status"] == "PENDING"


# ------------------------------------------------------------- group plans
def _group_plan(as_user):
    ao = as_user("ao")
    s, e = future(20), future(23)
    r = ao.post("/plans/new", data={"trip_purpose": "TRAINING", "trip_description": "Team exercise",
                                    "start_date": s, "end_date": e, "loa_id": "1"})
    assert r.status_code == 302
    plan = _doc_id(r)
    ao.post("/documents/%d/itinerary" % plan,
            data={"action": "add", "location_text": "Colorado Springs, CO", "arrive_date": s, "depart_date": e})
    ao.post("/documents/%d/expenses" % plan, data={"action": "add", "expense_date": s, "category": "AIRFARE",
                                                   "amount": "380", "payment_method": "CBA"})
    return ao, plan


def test_save_as_draft_creates_no_documents(app, as_user):
    ao, plan = _group_plan(as_user)
    t1, t2 = _user_id(app, "traveler"), _user_id(app, "traveler2")
    before = _row(app, "SELECT COUNT(*) c FROM documents")["c"]
    r = ao.post("/plans/%d" % plan, data={"action": "draft", "traveler_id": [str(t1), str(t2)]})
    assert r.status_code == 302
    assert _row(app, "SELECT COUNT(*) c FROM documents")["c"] == before
    assert _row(app, "SELECT COUNT(*) c FROM plan_assignees WHERE template_id=?", plan)["c"] == 2
    assert _row(app, "SELECT status FROM documents WHERE id=?", plan)["status"] == "CREATED"


def test_save_as_authorization_funds_then_auto_ao_approval(app, as_user):
    ao, plan = _group_plan(as_user)
    t1, t2 = _user_id(app, "traveler"), _user_id(app, "traveler2")
    r = ao.post("/plans/%d" % plan, data={"action": "issue", "traveler_id": [str(t1), str(t2)]})
    assert r.status_code == 302
    assert _row(app, "SELECT status FROM documents WHERE id=?", plan)["status"] == "ISSUED"
    with app.app_context():
        docs = get_db().execute("SELECT * FROM documents WHERE template_id=? ORDER BY traveler_id",
                                (plan,)).fetchall()
        assert [d["traveler_id"] for d in docs] == sorted([t1, t2])
        for d in docs:
            assert d["status"] == "ROUTING" and d["current_step_seq"] == 1  # funds verification first
            assert d["is_plan"] == 1 and d["created_by"] == _user_id(app, "ao")
            assert get_db().execute("SELECT COUNT(*) c FROM document_stops WHERE document_id=?",
                                    (d["id"],)).fetchone()["c"] == 1
            assert d["total_estimate"] > 0

    budget = as_user("budget")
    for d in docs:
        r = budget.post("/documents/%d/action" % d["id"], data={"action": "stamp"})
        assert r.status_code == 302
    for d in docs:
        assert _row(app, "SELECT status FROM documents WHERE id=?", d["id"])["status"] == "APPROVED"
        assert _row(app, "SELECT status FROM bookings WHERE document_id=?", d["id"])["status"] == "PENDING"
        stamp = _row(app, "SELECT user_id, remarks FROM document_history WHERE document_id=? AND stamp='APPROVED'",
                     d["id"])
        assert stamp["user_id"] == _user_id(app, "ao") and "Pre-approved" in stamp["remarks"]


def test_issue_rejects_out_of_scope_self_and_empty(app, as_user):
    ao, plan = _group_plan(as_user)
    r = ao.post("/plans/%d" % plan, data={"action": "issue", "traveler_id": [str(_user_id(app, "auditor"))]})
    assert r.status_code == 400  # auditor's org (HQ) is outside the AO's scope (HQ-W1)
    r = ao.post("/plans/%d" % plan, data={"action": "issue", "traveler_id": [str(_user_id(app, "ao"))]})
    assert r.status_code == 400
    r = ao.post("/plans/%d" % plan, data={"action": "issue"})
    assert r.status_code == 200 and "at least one traveler" in r.get_data(as_text=True)
    assert _row(app, "SELECT COUNT(*) c FROM documents WHERE template_id=?", plan)["c"] == 0


def test_plans_require_plan_assign(as_user):
    t = as_user("traveler")
    assert t.get("/plans/").status_code == 403
    assert t.post("/plans/new", data={}).status_code == 403


def test_funds_return_clears_preapproval(app, as_user):
    ao, plan = _group_plan(as_user)
    t1 = _user_id(app, "traveler")
    ao.post("/plans/%d" % plan, data={"action": "issue", "traveler_id": [str(t1)]})
    doc = _row(app, "SELECT id FROM documents WHERE template_id=?", plan)["id"]

    as_user("budget").post("/documents/%d/action" % doc, data={"action": "return", "remarks": "Wrong LOA"})
    assert _row(app, "SELECT preapproved_by FROM documents WHERE id=?", doc)["preapproved_by"] is None
    t = as_user("traveler")
    t.post("/documents/%d/sign" % doc, data={"certify": "1"})
    as_user("budget").post("/documents/%d/action" % doc, data={"action": "stamp"})
    row = _row(app, "SELECT status FROM documents WHERE id=?", doc)
    assert row["status"] == "ROUTING"  # waiting on an approver now, not auto-approved
    # The issuing AO prepared the document, so no_preparer_approve now applies
    # to them; a different approver must review the changed version.
    ao.post("/documents/%d/action" % doc, data={"action": "stamp"})
    assert _row(app, "SELECT status FROM documents WHERE id=?", doc)["status"] == "ROUTING"
    as_user("admin").post("/documents/%d/action" % doc, data={"action": "stamp"})
    assert _row(app, "SELECT status FROM documents WHERE id=?", doc)["status"] == "APPROVED"


def test_templates_hidden_from_listings(app, as_user):
    ao, plan = _group_plan(as_user)
    number = _row(app, "SELECT doc_number FROM documents WHERE id=?", plan)["doc_number"]
    for url in ("/", "/documents/", "/documents/?scope=org", "/documents/board", "/reports/?r=travelers"):
        assert number not in ao.get(url).get_data(as_text=True), url
    assert number in ao.get("/plans/").get_data(as_text=True)
    # The document page redirects to the plan review page; signing is impossible.
    assert "/plans/%d" % plan in ao.get("/documents/%d" % plan).headers["Location"]
    assert ao.post("/documents/%d/sign" % plan, data={"certify": "1"}).status_code == 403
