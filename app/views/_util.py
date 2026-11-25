import datetime as dt

from flask import abort, g

from .. import jtr, rbac, workflow
from ..db import get_db


def me():
    return g.user["id"]


def get_doc(doc_id):
    d = get_db().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if d is None:
        abort(404)
    if not can_view(d):
        abort(403)
    return d


def is_own(doc):
    return doc["traveler_id"] == me()


def can_view(doc):
    if is_own(doc) and rbac.has_perm(me(), "document.view.self"):
        return True
    if rbac.has_perm(me(), "document.view.org", doc["org_id"]):
        return True
    if workflow.can_act(me(), doc)[0]:
        return True
    return get_db().execute("SELECT 1 FROM document_history WHERE document_id=? AND user_id=?",
                            (doc["id"], me())).fetchone() is not None


def can_edit(doc):
    if doc["status"] not in workflow.EDITABLE:
        return False
    return (is_own(doc) and rbac.has_perm(me(), "document.edit.self")) or \
        rbac.has_perm(me(), "document.edit.others", doc["org_id"])


def can_sign(doc):
    if doc["status"] not in workflow.EDITABLE:
        return False
    return (is_own(doc) and rbac.has_perm(me(), "document.sign.self")) or \
        (not is_own(doc) and rbac.has_perm(me(), "document.sign.others", doc["org_id"]))


def can_cancel(doc):
    if doc["status"] in workflow.FINAL:
        return False
    return rbac.has_perm(me(), "document.cancel", doc["org_id"] if not is_own(doc) else None) and \
        (is_own(doc) or rbac.has_perm(me(), "document.edit.others", doc["org_id"]))


def can_amend(doc):
    return doc["doc_type"] == "AUTH" and doc["status"] == "APPROVED" and \
        rbac.has_perm(me(), "document.amend") and \
        (is_own(doc) or rbac.has_perm(me(), "document.edit.others", doc["org_id"]))


def load_children(doc_id):
    db = get_db()
    stops = [dict(r) for r in db.execute(
        "SELECT s.*, r.location, r.state FROM document_stops s JOIN per_diem_rates r ON r.id=s.rate_id "
        "WHERE s.document_id=? ORDER BY s.arrive_date", (doc_id,))]
    for s in stops:
        s["nights"] = (dt.date.fromisoformat(s["depart_date"]) - dt.date.fromisoformat(s["arrive_date"])).days
    expenses = [dict(r) for r in db.execute(
        "SELECT * FROM expenses WHERE document_id=? ORDER BY expense_date, id", (doc_id,))]
    return stops, expenses


def recalc(doc_id):
    db = get_db()
    doc = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    stops, expenses = load_children(doc_id)
    _, _, total = jtr.document_totals(doc, stops, expenses)
    db.execute("UPDATE documents SET total_estimate=?, updated_at=datetime('now') WHERE id=?", (total, doc_id))


def travelers_for_preparer():
    """Users the current user may create documents for (incl. self)."""
    db = get_db()
    users = []
    if rbac.has_perm(me(), "document.create.self"):
        users.append(g.user)
    scope = rbac.scoped_org_ids(me(), "document.create.others")
    if scope is None:
        rows = db.execute("SELECT * FROM users WHERE active=1 AND id<>? ORDER BY last_name", (me(),)).fetchall()
    elif scope:
        q = ",".join("?" * len(scope))
        rows = db.execute("SELECT * FROM users WHERE active=1 AND id<>? AND org_id IN (%s) ORDER BY last_name" % q,
                          (me(), *scope)).fetchall()
    else:
        rows = []
    return users + list(rows)


def next_doc_number(doc_type):
    prefix = {"AUTH": "TA", "VOUCHER": "TV", "LOCAL": "LV"}[doc_type]
    n = get_db().execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] + 1
    return "%s-%d-%06d" % (prefix, dt.date.today().year, n)


class Form:
    """Tiny validation helper producing an ordered error dict for the
    accessible error summary (field id -> message)."""

    def __init__(self, data):
        self.data = data
        self.errors = {}

    def get(self, name, default=""):
        return (self.data.get(name) or default).strip() if isinstance(self.data.get(name, default), str) \
            else self.data.get(name, default)

    def required(self, name, label):
        v = self.get(name)
        if not v:
            self.errors[name] = "Enter %s." % label.lower()
        return v

    def date(self, name, label, required=True):
        v = self.get(name)
        if not v:
            if required:
                self.errors[name] = "Enter %s." % label.lower()
            return None
        try:
            return dt.date.fromisoformat(v).isoformat()
        except ValueError:
            self.errors[name] = "%s must be a real date in the format YYYY-MM-DD." % label
            return None

    def number(self, name, label, required=True, minimum=0.0):
        v = self.get(name)
        if not v:
            if required:
                self.errors[name] = "Enter %s." % label.lower()
            return None
        try:
            n = float(v.replace(",", "").replace("$", ""))
        except ValueError:
            self.errors[name] = "%s must be a number, like 125.50." % label
            return None
        if minimum is not None and n < minimum:
            self.errors[name] = "%s must be %s or more." % (label, minimum)
        return n

    def choice(self, name, label, options):
        v = self.get(name)
        if v not in options:
            self.errors[name] = "Select %s." % label.lower()
        return v

    @property
    def ok(self):
        return not self.errors
