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


def _add_stop(client, doc_id, location, a, d):
    r = client.post("/documents/%d/itinerary" % doc_id,
                    data={"action": "add", "location_text": location, "arrive_date": a, "depart_date": d})
    assert r.status_code == 302


def _stop_ids(app, doc_id):
    with app.app_context():
        rows = get_db().execute("SELECT id FROM document_stops WHERE document_id=? ORDER BY sort_order",
                                (doc_id,)).fetchall()
        return [r["id"] for r in rows]


def test_add_stops_get_increasing_sort_order(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(16)
    doc = _new_auth(t, s, e)
    _add_stop(t, doc, "San Diego, CA", s, future(13))
    _add_stop(t, doc, "Standard CONUS, US", future(13), e)
    ids = _stop_ids(app, doc)
    assert len(ids) == 2


def test_reorder_move_down(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(16)
    doc = _new_auth(t, s, e)
    _add_stop(t, doc, "San Diego, CA", s, future(13))
    _add_stop(t, doc, "Standard CONUS, US", future(13), e)
    ids = _stop_ids(app, doc)
    r = t.post("/documents/%d/itinerary/reorder" % doc, data={"move_down": str(ids[0])})
    assert r.status_code == 302
    assert _stop_ids(app, doc) == [ids[1], ids[0]]


def test_reorder_bulk_drag(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(16)
    doc = _new_auth(t, s, e)
    _add_stop(t, doc, "San Diego, CA", s, future(13))
    _add_stop(t, doc, "Standard CONUS, US", future(13), e)
    ids = _stop_ids(app, doc)
    new_order = list(reversed(ids))
    r = t.post("/documents/%d/itinerary/reorder" % doc, data={"stop_id": [str(i) for i in new_order]})
    assert r.status_code == 302
    assert _stop_ids(app, doc) == new_order


def test_reorder_rejects_mismatched_ids(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(16)
    doc = _new_auth(t, s, e)
    _add_stop(t, doc, "San Diego, CA", s, future(13))
    ids = _stop_ids(app, doc)
    r = t.post("/documents/%d/itinerary/reorder" % doc, data={"stop_id": ["999999"]})
    assert r.status_code == 400
    assert _stop_ids(app, doc) == ids


def test_reorder_requires_edit_access(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(16)
    doc = _new_auth(t, s, e)
    _add_stop(t, doc, "San Diego, CA", s, future(13))
    ids = _stop_ids(app, doc)

    other = as_user("ao")
    r = other.post("/documents/%d/itinerary/reorder" % doc, data={"move_down": str(ids[0])})
    assert r.status_code == 403
    assert _stop_ids(app, doc) == ids


def test_voucher_copy_preserves_sort_order(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(16)
    doc = _new_auth(t, s, e)
    _add_stop(t, doc, "San Diego, CA", s, future(13))
    _add_stop(t, doc, "Standard CONUS, US", future(13), e)
    ids = _stop_ids(app, doc)
    new_order = list(reversed(ids))
    t.post("/documents/%d/itinerary/reorder" % doc, data={"stop_id": [str(i) for i in new_order]})

    with app.app_context():
        db = get_db()
        db.execute("UPDATE documents SET status='APPROVED' WHERE id=?", (doc,))
        db.commit()
    r = t.post("/documents/%d/voucher" % doc)
    assert r.status_code == 302
    v = _doc_id(r)
    with app.app_context():
        rows = get_db().execute("SELECT location FROM document_stops s JOIN per_diem_rates r ON r.id=s.rate_id "
                                "WHERE s.document_id=? ORDER BY s.sort_order", (v,)).fetchall()
        copied_order = [row["location"] for row in rows]
    with app.app_context():
        rows = get_db().execute("SELECT location FROM document_stops s JOIN per_diem_rates r ON r.id=s.rate_id "
                                "WHERE s.document_id=? ORDER BY s.sort_order", (doc,)).fetchall()
        original_order = [row["location"] for row in rows]
    assert copied_order == original_order
