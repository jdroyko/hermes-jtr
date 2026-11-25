"""End-to-end: authorization -> funds -> AO -> voucher -> AO -> CO."""
import datetime as dt
import re


def _doc_id(resp):
    m = re.search(r"/documents/(\d+)/", resp.headers["Location"])
    return int(m.group(1))


def future(days):
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()


def test_full_lifecycle(as_user):
    t = as_user("traveler")
    s, e = future(10), future(13)
    r = t.post("/documents/new", data={"type": "AUTH", "traveler_id": "2", "trip_purpose": "TRAINING",
                                       "trip_description": "Logistics course", "start_date": s, "end_date": e,
                                       "loa_id": "2"})
    assert r.status_code == 302
    doc = _doc_id(r)
    assert t.post("/documents/%d/itinerary" % doc, data={"action": "add", "location_text": "San Diego, CA",
                                                         "arrive_date": s, "depart_date": e}).status_code == 302
    assert t.post("/documents/%d/expenses" % doc, data={"action": "add", "expense_date": s, "category": "AIRFARE",
                                                        "amount": "450", "payment_method": "CBA"}).status_code == 302
    # sign without certification -> error
    t.post("/documents/%d/sign" % doc, data={})
    assert "Created" in t.get("/documents/%d" % doc).get_data(as_text=True)
    assert t.post("/documents/%d/sign" % doc, data={"certify": "1"}).status_code == 302
    assert "In routing" in t.get("/documents/%d" % doc).get_data(as_text=True)

    # traveler cannot stamp own doc
    t.post("/documents/%d/action" % doc, data={"action": "stamp"})
    assert "Funds verification" in t.get("/documents/%d" % doc).get_data(as_text=True)

    # AO is not first: funds step
    ao = as_user("ao")
    assert "Awaiting" not in ao.get("/documents/queue").get_data(as_text=True) or \
        ("TA-" not in ao.get("/documents/queue").get_data(as_text=True))
    b = as_user("budget")
    assert "TA-" in b.get("/documents/queue").get_data(as_text=True)
    b.post("/documents/%d/action" % doc, data={"action": "stamp"})
    ao.post("/documents/%d/action" % doc, data={"action": "stamp"})
    assert "Approved" in t.get("/documents/%d" % doc).get_data(as_text=True)

    # voucher
    r = t.post("/documents/%d/voucher" % doc)
    v = _doc_id(r)
    page = t.get("/documents/%d" % v).get_data(as_text=True)
    assert "Voucher" in page and "LODGERCPT" in page and "RECEIPT" in page
    t.post("/documents/%d/sign" % v, data={"certify": "1", "justification": "Receipts uploaded separately."})
    ao.post("/documents/%d/action" % v, data={"action": "return", "remarks": ""})  # needs remarks
    assert "In routing" in t.get("/documents/%d" % v).get_data(as_text=True)
    ao.post("/documents/%d/action" % v, data={"action": "return", "remarks": "Attach hotel receipt"})
    assert "Returned" in t.get("/documents/%d" % v).get_data(as_text=True)
    t.post("/documents/%d/sign" % v, data={"certify": "1", "justification": "Hotel receipt attached."})
    ao.post("/documents/%d/action" % v, data={"action": "stamp"})
    co = as_user("co")
    co.post("/documents/%d/action" % v, data={"action": "stamp"})
    assert "sent for payment" in t.get("/documents/%d" % v).get_data(as_text=True)


def test_approver_cannot_certify_same_doc(app, as_user):
    from app.db import get_db
    with app.app_context():
        db = get_db()
        # give AO the certifying role too
        rid = db.execute("SELECT id FROM roles WHERE key='certifying_officer'").fetchone()["id"]
        db.execute("INSERT INTO role_assignments(user_id, role_id, org_id, include_children) VALUES (5, ?, 2, 1)", (rid,))
        db.commit()
    t = as_user("traveler")
    s, e = future(5), future(5)
    r = t.post("/documents/new", data={"type": "LOCAL", "traveler_id": "2", "trip_purpose": "SITE_VISIT",
                                       "trip_description": "Local site visit", "start_date": s, "end_date": e, "loa_id": "2"})
    doc = _doc_id(r)
    t.post("/documents/%d/expenses" % doc, data={"action": "add", "expense_date": s, "category": "POV_MILEAGE",
                                                 "miles": "40", "payment_method": "PERSONAL"})
    t.post("/documents/%d/sign" % doc, data={"certify": "1"})
    ao = as_user("ao")
    ao.post("/documents/%d/action" % doc, data={"action": "stamp"})
    page = ao.get("/documents/%d" % doc).get_data(as_text=True)
    assert "cannot also certify" in page


def test_preparer_signs_on_behalf(as_user):
    p = as_user("preparer")
    s, e = future(20), future(22)
    r = p.post("/documents/new", data={"type": "AUTH", "traveler_id": "2", "trip_purpose": "TDY",
                                       "trip_description": "Prepared trip", "start_date": s, "end_date": e, "loa_id": "2"})
    doc = _doc_id(r)
    page = p.get("/documents/%d" % doc).get_data(as_text=True)
    assert "signing on behalf" in page


def test_traveler_cannot_see_others_docs(as_user):
    t2 = as_user("traveler2")
    r = t2.post("/documents/new", data={"type": "AUTH", "traveler_id": "3", "trip_purpose": "TDY",
                                        "trip_description": "Private", "start_date": future(3), "end_date": future(4)})
    doc = _doc_id(r)
    assert as_user("traveler").get("/documents/%d" % doc).status_code == 403
    assert as_user("auditor").get("/documents/%d" % doc).status_code == 200


def test_admin_pages_forbidden_for_traveler(as_user):
    t = as_user("traveler")
    for url in ("/admin/", "/admin/roles", "/admin/users", "/admin/matrix", "/admin/policy", "/reports/"):
        assert t.get(url).status_code == 403, url
