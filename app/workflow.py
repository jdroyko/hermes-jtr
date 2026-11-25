"""Document routing (DTS-style stamps).

Status lifecycle::

    CREATED --sign--> ROUTING (step 1 .. n) --final stamp--> APPROVED (AUTH)
       ^                 |                                   PAID     (VOUCHER/LOCAL)
       |              return
       +---- RETURNED <--+
    any non-final --cancel--> CANCELLED
    APPROVED AUTH --amend--> CREATED (amendment + 1)
"""
from __future__ import annotations

from . import rbac
from .db import get_db

EDITABLE = ("CREATED", "RETURNED")
FINAL = ("APPROVED", "PAID", "CANCELLED")

STATUS_LABELS = {
    "CREATED": "Created – not signed",
    "RETURNED": "Returned for correction",
    "ROUTING": "In routing",
    "APPROVED": "Approved",
    "PAID": "Certified – sent for payment",
    "CANCELLED": "Cancelled",
    "ISSUED": "Issued to travelers",
}

def doc_label(doc):
    if doc["is_template"]:
        return "Group travel plan"
    if doc["is_plan"]:
        return "Authorization (from plan)" if doc["status"] in ("APPROVED", "PAID") else "Travel plan"
    return DOC_TYPE_LABELS.get(doc["doc_type"], doc["doc_type"])


BOOKING_KINDS = {"AIR": "Air", "LODGING": "Lodging", "RENTAL_CAR": "Rental car", "RAIL": "Rail", "OTHER": "Other"}

BOOKING_LABELS = {"PENDING": "Waiting for travel agency", "BOOKED": "Booked", "CANCELLED": "Booking cancelled"}

DOC_TYPE_LABELS = {"AUTH": "Authorization", "VOUCHER": "Voucher", "LOCAL": "Local voucher"}


def resolve_routing_list(traveler, org_id):
    db = get_db()
    if traveler["default_routing_list_id"]:
        return traveler["default_routing_list_id"]
    for oid in rbac.org_ancestors(org_id):
        row = db.execute("SELECT id FROM routing_lists WHERE org_id=? ORDER BY id LIMIT 1", (oid,)).fetchone()
        if row:
            return row["id"]
    return None


def steps_for(doc):
    if not doc["routing_list_id"]:
        return []
    rows = get_db().execute("SELECT * FROM routing_steps WHERE routing_list_id=? ORDER BY seq",
                            (doc["routing_list_id"],)).fetchall()
    return [r for r in rows if doc["doc_type"] in r["doc_types"].split(",")]


def current_step(doc):
    if doc["status"] != "ROUTING":
        return None
    for s in steps_for(doc):
        if s["seq"] == doc["current_step_seq"]:
            return s
    return None


def _stamped_by(doc_id, user_id, stamp):
    return get_db().execute(
        "SELECT 1 FROM document_history WHERE document_id=? AND user_id=? AND stamp=? "
        "AND id > COALESCE((SELECT MAX(id) FROM document_history WHERE document_id=? AND action IN ('RETURN','AMEND')),0)",
        (doc_id, user_id, stamp, doc_id)).fetchone() is not None


def sod_violation(user_id, doc, step):
    if rbac.sod_enabled("no_self_approve") and user_id == doc["traveler_id"]:
        return "Separation of duties: you cannot act on your own travel document."
    if rbac.sod_enabled("no_preparer_approve") and user_id == doc["created_by"]:
        return "Separation of duties: you prepared this document and cannot apply routing stamps to it."
    if rbac.sod_enabled("distinct_approver_certifier"):
        if step["required_permission"] == "workflow.certify" and _stamped_by(doc["id"], user_id, "APPROVED"):
            return "Separation of duties: you approved this document and cannot also certify it."
        if step["required_permission"] == "workflow.approve" and _stamped_by(doc["id"], user_id, "CERTIFIED"):
            return "Separation of duties: you certified this document and cannot also approve it."
    return None


def can_act(user_id, doc, preapproval=False):
    """(ok, reason, step). ``preapproval`` is the AO stamp carried by a group
    plan: the AO necessarily prepared the document, so only
    no_preparer_approve is waived -- every other check still applies."""
    step = current_step(doc)
    if step is None:
        return False, "Document is not awaiting action.", None
    if step["assignee_user_id"] and step["assignee_user_id"] != user_id:
        return False, "This step is assigned to a specific official.", step
    if not rbac.has_perm(user_id, step["required_permission"], doc["org_id"]):
        return False, "You lack permission %s for this organization." % step["required_permission"], step
    v = sod_violation(user_id, doc, step)
    if v and preapproval and rbac.sod_enabled("no_preparer_approve") and user_id == doc["created_by"]:
        v = sod_violation(user_id, dict(doc, created_by=None), step)
    if v:
        return False, v, step
    return True, "", step


def can_return(user_id, doc):
    ok, reason, step = can_act(user_id, doc)
    if not ok:
        return False
    return rbac.has_perm(user_id, "workflow.return", doc["org_id"])


def log(doc_id, user_id, action, stamp=None, remarks=""):
    get_db().execute("INSERT INTO document_history(document_id, user_id, action, stamp, remarks) VALUES (?,?,?,?,?)",
                     (doc_id, user_id, action, stamp, remarks))


def sign(doc, user_id, remarks=""):
    db = get_db()
    steps = steps_for(doc)
    if not steps:
        raise ValueError("No routing list with steps applies to this document. Contact your DTA.")
    db.execute("UPDATE documents SET status='ROUTING', current_step_seq=?, updated_at=datetime('now') WHERE id=?",
               (steps[0]["seq"], doc["id"]))
    log(doc["id"], user_id, "SIGN", "SIGNED", remarks)


def stamp(doc, user_id, remarks=""):
    db = get_db()
    ok, reason, step = can_act(user_id, doc)
    if not ok:
        raise PermissionError(reason)
    log(doc["id"], user_id, "STAMP", step["stamp"], remarks)
    msg = "Stamped %s." % step["stamp"]
    nxt = _advance(doc, step, user_id)
    while nxt is not None and _apply_preapproval(doc):
        msg += " Approving Official's group-plan approval applied."
        nxt = _advance(doc, nxt, doc["preapproved_by"])
    if nxt is not None:
        return msg + " Routed to: %s." % nxt["name"]
    final = "APPROVED" if doc["doc_type"] == "AUTH" else "PAID"
    return msg + " Document is now %s." % STATUS_LABELS[final].lower()


def _advance(doc, step, user_id):
    """Move past ``step``; return the next step, or None after finalizing."""
    db = get_db()
    later = [s for s in steps_for(doc) if s["seq"] > step["seq"]]
    if later:
        db.execute("UPDATE documents SET current_step_seq=?, updated_at=datetime('now') WHERE id=?",
                   (later[0]["seq"], doc["id"]))
        return later[0]
    final = "APPROVED" if doc["doc_type"] == "AUTH" else "PAID"
    db.execute("UPDATE documents SET status=?, current_step_seq=NULL, updated_at=datetime('now') WHERE id=?",
               (final, doc["id"]))
    log(doc["id"], user_id, "FINAL", final)
    if final == "APPROVED":
        _on_approved(doc, user_id)
    return None


def _apply_preapproval(doc):
    """Apply the issuing AO's APPROVED stamp if the document is now at an
    approval step that AO may act on. Returns True if a stamp was applied."""
    db = get_db()
    fresh = db.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone()
    ao = fresh["preapproved_by"]
    if not ao:
        return False
    step = current_step(fresh)
    if step is None or step["required_permission"] != "workflow.approve":
        return False
    ok, _reason, _step = can_act(ao, fresh, preapproval=True)
    if not ok:
        return False
    u = db.execute("SELECT first_name, last_name FROM users WHERE id=?", (ao,)).fetchone()
    log(doc["id"], ao, "STAMP", step["stamp"],
        "Pre-approved by %s %s when the group plan was issued." % (u["first_name"], u["last_name"]))
    return True


def _on_approved(doc, user_id):
    db = get_db()
    # Any TDY location the traveler proposed on this document is approved
    # along with the document itself -- no separate AO action needed.
    db.execute(
        "UPDATE per_diem_rates SET status='active' WHERE status='pending' AND id IN "
        "(SELECT rate_id FROM document_stops WHERE document_id=?)", (doc["id"],))
    if doc["is_plan"]:
        cur = db.execute("INSERT OR IGNORE INTO bookings(document_id) VALUES (?)", (doc["id"],))
        if not cur.rowcount:
            db.execute("UPDATE bookings SET status='PENDING', booked_at=NULL WHERE document_id=?", (doc["id"],))
        log(doc["id"], user_id, "BOOKING", "SENT TO TRAVEL AGENCY")


def return_doc(doc, user_id, remarks):
    if not remarks.strip():
        raise ValueError("Remarks are required when returning a document.")
    if not can_return(user_id, doc):
        raise PermissionError("You cannot return this document.")
    get_db().execute("UPDATE documents SET status='RETURNED', current_step_seq=NULL, preapproved_by=NULL, "
                     "updated_at=datetime('now') WHERE id=?", (doc["id"],))
    log(doc["id"], user_id, "RETURN", "RETURNED", remarks)


def override(doc, user_id, remarks):
    """Skip the current step (requires workflow.override)."""
    if not remarks.strip():
        raise ValueError("Remarks are required for a routing override.")
    if not rbac.has_perm(user_id, "workflow.override", doc["org_id"]):
        raise PermissionError("You cannot override routing for this organization.")
    step = current_step(doc)
    if not step:
        raise ValueError("Document is not in routing.")
    log(doc["id"], user_id, "OVERRIDE", "OVERRIDE: " + step["stamp"], remarks)
    _advance(doc, step, user_id)


def queue_for(user_id):
    """Documents awaiting this user's action."""
    db = get_db()
    out = []
    for d in db.execute("SELECT * FROM documents WHERE status='ROUTING' ORDER BY updated_at"):
        ok, _, step = can_act(user_id, d)
        if ok:
            out.append((d, step))
    return out
