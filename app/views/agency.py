"""Travel agency (TMC) booking queue for approved travel plans."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .. import jtr, rbac, workflow
from ..db import audit, get_db
from ._util import Form, load_children, me

bp = Blueprint("agency", __name__, url_prefix="/agency")


@bp.before_request
def _gate():
    if not rbac.has_perm(me(), "booking.view"):
        abort(403)


def _rows(status):
    scope = rbac.scoped_org_ids(me(), "booking.view")
    sql = ("SELECT b.*, d.doc_number, d.trip_description, d.start_date, d.end_date, d.total_estimate, "
           "u.first_name, u.last_name, o.code org_code FROM bookings b JOIN documents d ON d.id=b.document_id "
           "JOIN users u ON u.id=d.traveler_id JOIN orgs o ON o.id=d.org_id WHERE b.status=?")
    params = [status]
    if scope is not None:
        if not scope:
            return []
        sql += " AND d.org_id IN (%s)" % ",".join("?" * len(scope))
        params += list(scope)
    return get_db().execute(sql + " ORDER BY d.start_date", params).fetchall()


@bp.route("/")
def index():
    return render_template("agency/index.html", pending=_rows("PENDING"), booked=_rows("BOOKED"),
                           cancelled=_rows("CANCELLED"))


@bp.route("/<int:booking_id>", methods=["GET", "POST"])
def detail(booking_id):
    db = get_db()
    booking = db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if booking is None:
        abort(404)
    doc = db.execute("SELECT * FROM documents WHERE id=?", (booking["document_id"],)).fetchone()
    if not rbac.has_perm(me(), "booking.view", doc["org_id"]):
        abort(403)
    manage = rbac.has_perm(me(), "booking.manage", doc["org_id"])
    values = {"kind": "", "vendor": "", "confirmation": "", "amount": "", "details": ""}
    errors = {}
    if request.method == "POST":
        if not manage:
            abort(403)
        act = request.form.get("action")
        if booking["status"] == "CANCELLED":
            abort(400, "This booking was cancelled because the travel plan was cancelled or amended.")
        if act in ("add_item", "delete_item", "mark_booked") and booking["status"] != "PENDING":
            abort(400, "Reopen this booking before changing it.")
        if act == "add_item":
            f = Form(request.form)
            values.update({k: f.get(k) for k in values})
            f.choice("kind", "Reservation type", list(workflow.BOOKING_KINDS))
            f.required("vendor", "Vendor")
            f.required("confirmation", "Confirmation number")
            amount = f.number("amount", "Amount")
            errors = f.errors
            if f.ok:
                db.execute("INSERT INTO booking_items(booking_id, kind, vendor, confirmation, amount, details)"
                           " VALUES (?,?,?,?,?,?)", (booking_id, values["kind"], values["vendor"],
                                                     values["confirmation"], amount, values["details"]))
                audit(me(), "BOOKING_ITEM_ADD", "booking:%d" % booking_id, values["confirmation"])
                db.commit()
                flash("Reservation added.", "success")
                return redirect(request.path)
        elif act == "delete_item":
            db.execute("DELETE FROM booking_items WHERE id=? AND booking_id=?",
                       (request.form.get("item_id"), booking_id))
            audit(me(), "BOOKING_ITEM_DELETE", "booking:%d" % booking_id)
            db.commit()
            flash("Reservation removed.", "success")
            return redirect(request.path)
        elif act == "mark_booked":
            items = db.execute("SELECT * FROM booking_items WHERE booking_id=?", (booking_id,)).fetchall()
            if not items:
                flash("Add at least one reservation before marking this trip booked.", "error")
                return redirect(request.path)
            db.execute("UPDATE bookings SET status='BOOKED', agent_id=?, booked_at=datetime('now') WHERE id=?",
                       (me(), booking_id))
            summary = "; ".join("%s %s %s" % (workflow.BOOKING_KINDS[i["kind"]], i["vendor"], i["confirmation"])
                                for i in items)
            workflow.log(doc["id"], me(), "BOOKING", "BOOKED", summary)
            audit(me(), "BOOKING_BOOKED", "booking:%d" % booking_id)
            db.commit()
            flash("Trip marked booked. The traveler can see the reservations on their authorization.", "success")
            return redirect(request.path)
        elif act == "reopen":
            db.execute("UPDATE bookings SET status='PENDING', booked_at=NULL WHERE id=?", (booking_id,))
            workflow.log(doc["id"], me(), "BOOKING", "REOPENED", request.form.get("remarks", ""))
            audit(me(), "BOOKING_REOPEN", "booking:%d" % booking_id)
            db.commit()
            flash("Booking reopened.", "success")
            return redirect(request.path)
        else:
            abort(400)
    items = db.execute("SELECT * FROM booking_items WHERE booking_id=? ORDER BY id", (booking_id,)).fetchall()
    traveler = db.execute("SELECT * FROM users WHERE id=?", (doc["traveler_id"],)).fetchone()
    stops, exps = load_children(doc["id"])
    selections = db.execute("SELECT * FROM trip_selections WHERE document_id=? ORDER BY start_date, id",
                            (doc["id"],)).fetchall()
    return render_template("agency/detail.html", booking=booking, doc=doc, items=items, traveler=traveler,
                           selections=selections,
                           stops=stops, expenses=exps, manage=manage, values=values, errors=errors,
                           kinds=list(workflow.BOOKING_KINDS.items()),
                           amount_of=jtr.expense_amount)
