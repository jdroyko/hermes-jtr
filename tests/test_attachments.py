import datetime as dt
import io
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


def _add_expense(client, doc_id, amount="80"):
    r = client.post("/documents/%d/expenses" % doc_id,
                    data={"action": "add", "expense_date": future(10), "category": "RENTAL_CAR",
                          "description": "Needed for site visits", "amount": amount, "payment_method": "GTCC"})
    assert r.status_code == 302
    with_app = client.application
    with with_app.app_context():
        return get_db().execute("SELECT id FROM expenses WHERE document_id=? ORDER BY id DESC LIMIT 1",
                                (doc_id,)).fetchone()["id"]


def _pdf_bytes():
    return b"%PDF-1.4\n%fake receipt for test\n%%EOF"


def test_upload_sets_receipt_attached(app, as_user):
    t = as_user("traveler")
    doc = _new_auth(t, future(10), future(16))
    exp = _add_expense(t, doc)
    r = t.post("/documents/%d/expenses/%d/attachments" % (doc, exp),
              data={"receipt": (io.BytesIO(_pdf_bytes()), "receipt.pdf")},
              content_type="multipart/form-data")
    assert r.status_code == 302
    with app.app_context():
        row = get_db().execute("SELECT receipt_attached FROM expenses WHERE id=?", (exp,)).fetchone()
        assert row["receipt_attached"] == 1
        count = get_db().execute("SELECT COUNT(*) c FROM attachments WHERE expense_id=?", (exp,)).fetchone()["c"]
        assert count == 1


def test_upload_rejects_bad_extension(app, as_user):
    t = as_user("traveler")
    doc = _new_auth(t, future(10), future(16))
    exp = _add_expense(t, doc)
    r = t.post("/documents/%d/expenses/%d/attachments" % (doc, exp),
              data={"receipt": (io.BytesIO(b"not a real file"), "virus.exe")},
              content_type="multipart/form-data")
    assert r.status_code == 302  # redirected back with a flashed error, not saved
    with app.app_context():
        count = get_db().execute("SELECT COUNT(*) c FROM attachments WHERE expense_id=?", (exp,)).fetchone()["c"]
        assert count == 0


def test_upload_rejects_mismatched_magic_bytes(app, as_user):
    t = as_user("traveler")
    doc = _new_auth(t, future(10), future(16))
    exp = _add_expense(t, doc)
    r = t.post("/documents/%d/expenses/%d/attachments" % (doc, exp),
              data={"receipt": (io.BytesIO(b"this is not actually a pdf"), "fake.pdf")},
              content_type="multipart/form-data")
    assert r.status_code == 302
    with app.app_context():
        count = get_db().execute("SELECT COUNT(*) c FROM attachments WHERE expense_id=?", (exp,)).fetchone()["c"]
        assert count == 0


def test_download_requires_view_access(app, as_user):
    t = as_user("traveler")
    doc = _new_auth(t, future(10), future(16))
    exp = _add_expense(t, doc)
    t.post("/documents/%d/expenses/%d/attachments" % (doc, exp),
          data={"receipt": (io.BytesIO(_pdf_bytes()), "receipt.pdf")}, content_type="multipart/form-data")
    with app.app_context():
        att_id = get_db().execute("SELECT id FROM attachments WHERE expense_id=?", (exp,)).fetchone()["id"]

    ok = t.get("/documents/%d/attachments/%d" % (doc, att_id))
    assert ok.status_code == 200

    other = as_user("traveler2")  # a different traveler with only document.view.self
    denied = other.get("/documents/%d/attachments/%d" % (doc, att_id))
    assert denied.status_code == 403


def test_delete_clears_receipt_attached_when_last_one_removed(app, as_user):
    t = as_user("traveler")
    doc = _new_auth(t, future(10), future(16))
    exp = _add_expense(t, doc)
    t.post("/documents/%d/expenses/%d/attachments" % (doc, exp),
          data={"receipt": (io.BytesIO(_pdf_bytes()), "receipt.pdf")}, content_type="multipart/form-data")
    with app.app_context():
        att_id = get_db().execute("SELECT id FROM attachments WHERE expense_id=?", (exp,)).fetchone()["id"]

    r = t.post("/documents/%d/attachments/%d/delete" % (doc, att_id))
    assert r.status_code == 302
    with app.app_context():
        row = get_db().execute("SELECT receipt_attached FROM expenses WHERE id=?", (exp,)).fetchone()
        assert row["receipt_attached"] == 0
        count = get_db().execute("SELECT COUNT(*) c FROM attachments WHERE expense_id=?", (exp,)).fetchone()["c"]
        assert count == 0


def test_upload_requires_edit_access(app, as_user):
    t = as_user("traveler")
    doc = _new_auth(t, future(10), future(16))
    exp = _add_expense(t, doc)

    other = as_user("ao")
    r = other.post("/documents/%d/expenses/%d/attachments" % (doc, exp),
                   data={"receipt": (io.BytesIO(_pdf_bytes()), "receipt.pdf")}, content_type="multipart/form-data")
    assert r.status_code == 403
