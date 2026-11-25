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
}

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


def can_act(user_id, doc):
    """(ok, reason, step)."""
    step = current_step(doc)
    if step is None:
        return False, "Document is not awaiting action.", None
    if step["assignee_user_id"] and step["assignee_user_id"] != user_id:
        return False, "This step is assigned to a specific official.", step
    if not rbac.has_perm(user_id, step["required_permission"], doc["org_id"]):
        return False, "You lack permission %s for this organization." % step["required_permission"], step
    v = sod_violation(user_id, doc, step)
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


def sign(doc, user_id):
    db = get_db()
    steps = steps_for(doc)
    if not steps:
        raise ValueError("No routing list with steps applies to this document. Contact your DTA.")
    db.execute("UPDATE documents SET status='ROUTING', current_step_seq=?, updated_at=datetime('now') WHERE id=?",
               (steps[0]["seq"], doc["id"]))
    log(doc["id"], user_id, "SIGN", "SIGNED")


def stamp(doc, user_id, remarks=""):
    db = get_db()
    ok, reason, step = can_act(user_id, doc)
    if not ok:
        raise PermissionError(reason)
    log(doc["id"], user_id, "STAMP", step["stamp"], remarks)
    steps = steps_for(doc)
    later = [s for s in steps if s["seq"] > step["seq"]]
    if later:
        db.execute("UPDATE documents SET current_step_seq=?, updated_at=datetime('now') WHERE id=?",
                   (later[0]["seq"], doc["id"]))
        return "Stamped %s. Routed to: %s." % (step["stamp"], later[0]["name"])
    final = "APPROVED" if doc["doc_type"] == "AUTH" else "PAID"
    db.execute("UPDATE documents SET status=?, current_step_seq=NULL, updated_at=datetime('now') WHERE id=?",
               (final, doc["id"]))
    log(doc["id"], user_id, "FINAL", final)
    if final == "APPROVED":
        # Any TDY location the traveler proposed on this document is approved
        # along with the document itself -- no separate AO action needed.
        db.execute(
            "UPDATE per_diem_rates SET status='active' WHERE status='pending' AND id IN "
            "(SELECT rate_id FROM document_stops WHERE document_id=?)", (doc["id"],))
    return "Stamped %s. Document is now %s." % (step["stamp"], STATUS_LABELS[final].lower())


def return_doc(doc, user_id, remarks):
    if not remarks.strip():
        raise ValueError("Remarks are required when returning a document.")
    if not can_return(user_id, doc):
        raise PermissionError("You cannot return this document.")
    get_db().execute("UPDATE documents SET status='RETURNED', current_step_seq=NULL, updated_at=datetime('now') "
                     "WHERE id=?", (doc["id"],))
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
    db = get_db()
    later = [s for s in steps_for(doc) if s["seq"] > step["seq"]]
    if later:
        db.execute("UPDATE documents SET current_step_seq=? WHERE id=?", (later[0]["seq"], doc["id"]))
    else:
        final = "APPROVED" if doc["doc_type"] == "AUTH" else "PAID"
        db.execute("UPDATE documents SET status=?, current_step_seq=NULL WHERE id=?", (final, doc["id"]))
        log(doc["id"], user_id, "FINAL", final)


def queue_for(user_id):
    """Documents awaiting this user's action."""
    db = get_db()
    out = []
    for d in db.execute("SELECT * FROM documents WHERE status='ROUTING' ORDER BY updated_at"):
        ok, _, step = can_act(user_id, d)
        if ok:
            out.append((d, step))
    return out
