from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import jtr, rbac, workflow
from ..db import audit, get_db
from ._util import (Form, can_amend, can_cancel, can_edit, can_sign, get_doc, is_own, load_children, me,
                    next_doc_number, recalc, travelers_for_preparer)

bp = Blueprint("documents", __name__, url_prefix="/documents")

STEPS = [("trip", "Trip details"), ("itinerary", "Itinerary & lodging"),
         ("expenses", "Expenses"), ("review", "Review & sign")]


def _loas_for(org_id):
    ids = rbac.org_ancestors(org_id)
    return get_db().execute("SELECT * FROM loas WHERE active=1 AND org_id IN (%s) ORDER BY label"
                            % ",".join("?" * len(ids)), ids).fetchall()


def _locations():
    return get_db().execute("SELECT MIN(id) id, location, state FROM per_diem_rates "
                            "GROUP BY location, state ORDER BY location").fetchall()


# --------------------------------------------------------------------- list
@bp.route("/")
def index():
    db = get_db()
    status = request.args.get("status", "")
    doc_type = request.args.get("type", "")
    scope = request.args.get("scope", "mine")
    where, params = [], []
    if scope == "org" and rbac.has_perm(me(), "document.view.org"):
        orgs = rbac.scoped_org_ids(me(), "document.view.org")
        if orgs is not None:
            where.append("d.org_id IN (%s)" % (",".join("?" * len(orgs)) or "NULL"))
            params += list(orgs)
    else:
        scope = "mine"
        where.append("(d.traveler_id=? OR d.created_by=?)")
        params += [me(), me()]
    if status:
        where.append("d.status=?")
        params.append(status)
    if doc_type:
        where.append("d.doc_type=?")
        params.append(doc_type)
    sql = ("SELECT d.*, u.first_name, u.last_name, o.code org_code FROM documents d "
           "JOIN users u ON u.id=d.traveler_id JOIN orgs o ON o.id=d.org_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    docs = db.execute(sql + " ORDER BY d.updated_at DESC LIMIT 500", params).fetchall()
    return render_template("documents/index.html", docs=docs, status=status, doc_type=doc_type, scope=scope)


@bp.route("/queue")
def queue():
    items = workflow.queue_for(me())
    db = get_db()
    rows = []
    for d, step in items:
        u = db.execute("SELECT first_name, last_name FROM users WHERE id=?", (d["traveler_id"],)).fetchone()
        rows.append((d, step, u))
    return render_template("documents/queue.html", rows=rows)


# ------------------------------------------------------------------- create
@bp.route("/new", methods=["GET", "POST"])
def new():
    doc_type = request.values.get("type", "AUTH")
    if doc_type not in ("AUTH", "LOCAL"):
        abort(404)
    travelers = travelers_for_preparer()
    if not travelers:
        abort(403)
    values = {"traveler_id": str(me()), "trip_purpose": "TDY", "start_date": "", "end_date": "",
              "trip_description": "", "loa_id": ""}
    errors = {}
    if request.method == "POST":
        f = Form(request.form)
        values.update({k: f.get(k) for k in values})
        tid = f.get("traveler_id")
        traveler = next((t for t in travelers if str(t["id"]) == tid), None)
        if traveler is None:
            f.errors["traveler_id"] = "Select a traveler you are authorized to prepare documents for."
        f.choice("trip_purpose", "Trip purpose", [k for k, _ in jtr.TRIP_PURPOSES])
        f.required("trip_description", "Trip description")
        s = f.date("start_date", "Start date")
        e = f.date("end_date", "End date")
        if s and e and e < s:
            f.errors["end_date"] = "End date must be the same as or after the start date."
        errors = f.errors
        if f.ok:
            db = get_db()
            loa = f.get("loa_id") or None
            rl = workflow.resolve_routing_list(traveler, traveler["org_id"])
            doc_id = db.execute(
                "INSERT INTO documents(doc_number, doc_type, traveler_id, created_by, org_id, trip_purpose,"
                " trip_description, start_date, end_date, loa_id, routing_list_id, trip_type)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (next_doc_number(doc_type), doc_type, traveler["id"], me(), traveler["org_id"],
                 values["trip_purpose"], values["trip_description"], s, e, loa, rl,
                 "LOCAL" if doc_type == "LOCAL" else "TDY")).lastrowid
            workflow.log(doc_id, me(), "CREATE", "CREATED")
            audit(me(), "DOC_CREATE", "document:%d" % doc_id)
            db.commit()
            flash("%s created. Add your itinerary next." % workflow.DOC_TYPE_LABELS[doc_type], "success")
            nxt = "expenses" if doc_type == "LOCAL" else "itinerary"
            return redirect(url_for("documents." + nxt, doc_id=doc_id))
    org_id = g.user["org_id"]
    return render_template("documents/trip.html", doc=None, doc_type=doc_type, values=values, errors=errors,
                           travelers=travelers, loas=_loas_for(org_id), purposes=jtr.TRIP_PURPOSES,
                           steps=STEPS, current="trip")


@bp.route("/<int:doc_id>/voucher", methods=["POST"])
def create_voucher(doc_id):
    auth = get_doc(doc_id)
    if auth["doc_type"] != "AUTH" or auth["status"] != "APPROVED":
        abort(400, "Vouchers can only be created from approved authorizations.")
    if not (is_own(auth) or rbac.has_perm(me(), "document.create.others", auth["org_id"])):
        abort(403)
    db = get_db()
    existing = db.execute("SELECT id FROM documents WHERE parent_auth_id=? AND status<>'CANCELLED'",
                          (doc_id,)).fetchone()
    if existing:
        flash("A voucher already exists for this authorization.", "info")
        return redirect(url_for("documents.view", doc_id=existing["id"]))
    vid = db.execute(
        "INSERT INTO documents(doc_number, doc_type, traveler_id, created_by, org_id, trip_purpose, trip_description,"
        " start_date, end_date, loa_id, routing_list_id, parent_auth_id)"
        " VALUES (?, 'VOUCHER', ?,?,?,?,?,?,?,?,?,?)",
        (next_doc_number("VOUCHER"), auth["traveler_id"], me(), auth["org_id"], auth["trip_purpose"],
         auth["trip_description"], auth["start_date"], auth["end_date"], auth["loa_id"],
         auth["routing_list_id"], doc_id)).lastrowid
    db.execute("INSERT INTO document_stops(document_id, rate_id, arrive_date, depart_date, gov_quarters) "
               "SELECT ?, rate_id, arrive_date, depart_date, gov_quarters FROM document_stops WHERE document_id=?",
               (vid, doc_id))
    db.execute("INSERT INTO expenses(document_id, expense_date, category, description, amount, miles, payment_method)"
               " SELECT ?, expense_date, category, description, amount, miles, payment_method FROM expenses "
               "WHERE document_id=?", (vid, doc_id))
    workflow.log(vid, me(), "CREATE", "CREATED", "Created from authorization %s" % auth["doc_number"])
    recalc(vid)
    db.commit()
    flash("Voucher created from %s. Enter actual lodging costs and confirm receipts." % auth["doc_number"], "success")
    return redirect(url_for("documents.itinerary", doc_id=vid))


# --------------------------------------------------------------- wizard pages
def _require_edit(doc):
    if not can_edit(doc):
        abort(403)


@bp.route("/<int:doc_id>/trip", methods=["GET", "POST"])
def trip(doc_id):
    doc = get_doc(doc_id)
    _require_edit(doc)
    values = {k: doc[k] if doc[k] is not None else "" for k in
              ("trip_purpose", "trip_description", "start_date", "end_date", "loa_id")}
    errors = {}
    if request.method == "POST":
        f = Form(request.form)
        values.update({k: f.get(k) for k in values})
        f.choice("trip_purpose", "Trip purpose", [k for k, _ in jtr.TRIP_PURPOSES])
        f.required("trip_description", "Trip description")
        s = f.date("start_date", "Start date")
        e = f.date("end_date", "End date")
        if s and e and e < s:
            f.errors["end_date"] = "End date must be the same as or after the start date."
        errors = f.errors
        if f.ok:
            db = get_db()
            db.execute("UPDATE documents SET trip_purpose=?, trip_description=?, start_date=?, end_date=?, loa_id=? "
                       "WHERE id=?", (values["trip_purpose"], values["trip_description"], s, e,
                                      values["loa_id"] or None, doc_id))
            recalc(doc_id)
            db.commit()
            flash("Trip details saved.", "success")
            return redirect(url_for("documents.expenses" if doc["doc_type"] == "LOCAL" else "documents.itinerary",
                                    doc_id=doc_id))
    return render_template("documents/trip.html", doc=doc, doc_type=doc["doc_type"], values=values, errors=errors,
                           travelers=None, loas=_loas_for(doc["org_id"]), purposes=jtr.TRIP_PURPOSES,
                           steps=STEPS, current="trip")


@bp.route("/<int:doc_id>/itinerary", methods=["GET", "POST"])
def itinerary(doc_id):
    doc = get_doc(doc_id)
    _require_edit(doc)
    db = get_db()
    errors, values = {}, {"rate_id": "", "arrive_date": doc["start_date"], "depart_date": doc["end_date"],
                          "lodging_nightly": "", "gov_quarters": ""}
    if request.method == "POST":
        action = request.form.get("action", "add")
        if action == "delete":
            db.execute("DELETE FROM document_stops WHERE id=? AND document_id=?",
                       (request.form.get("stop_id"), doc_id))
            recalc(doc_id)
            db.commit()
            flash("Location removed.", "success")
            return redirect(request.path)
        if action == "lodging":
            f = Form(request.form)
            n = f.number("lodging_nightly_%s" % request.form.get("stop_id"), "Nightly lodging cost", required=False)
            if f.ok:
                db.execute("UPDATE document_stops SET lodging_nightly=? WHERE id=? AND document_id=?",
                           (n, request.form.get("stop_id"), doc_id))
                recalc(doc_id)
                db.commit()
                flash("Lodging cost saved.", "success")
                return redirect(request.path)
            errors = f.errors
        else:
            f = Form(request.form)
            values.update({k: f.get(k) for k in values})
            rid = f.required("rate_id", "TDY location")
            a = f.date("arrive_date", "Arrival date")
            d = f.date("depart_date", "Departure date")
            if a and d:
                if d <= a:
                    f.errors["depart_date"] = "Departure date must be after the arrival date."
                elif a < doc["start_date"] or d > doc["end_date"]:
                    f.errors["arrive_date"] = "Dates must fall within the trip dates (%s to %s)." % (
                        doc["start_date"], doc["end_date"])
            nightly = f.number("lodging_nightly", "Nightly lodging cost", required=False)
            errors = f.errors
            if f.ok:
                db.execute("INSERT INTO document_stops(document_id, rate_id, arrive_date, depart_date, "
                           "lodging_nightly, gov_quarters) VALUES (?,?,?,?,?,?)",
                           (doc_id, rid, a, d, nightly, 1 if values["gov_quarters"] else 0))
                recalc(doc_id)
                db.commit()
                flash("Location added.", "success")
                return redirect(request.path)
    stops, expenses = load_children(doc_id)
    pd, _, _ = jtr.document_totals(doc, stops, expenses)
    return render_template("documents/itinerary.html", doc=doc, stops=stops, per_diem=pd, values=values,
                           errors=errors, locations=_locations(), steps=STEPS, current="itinerary")


@bp.route("/<int:doc_id>/expenses", methods=["GET", "POST"])
def expenses(doc_id):
    doc = get_doc(doc_id)
    _require_edit(doc)
    db = get_db()
    errors = {}
    values = {"expense_date": doc["start_date"], "category": "", "description": "", "amount": "",
              "miles": "", "payment_method": "GTCC", "receipt_attached": ""}
    if request.method == "POST":
        if request.form.get("action") == "delete":
            db.execute("DELETE FROM expenses WHERE id=? AND document_id=?", (request.form.get("expense_id"), doc_id))
            recalc(doc_id)
            db.commit()
            flash("Expense removed.", "success")
            return redirect(request.path)
        f = Form(request.form)
        values.update({k: f.get(k) for k in values})
        dte = f.date("expense_date", "Expense date")
        cat = f.choice("category", "Expense type", [k for k, _ in jtr.EXPENSE_CATEGORIES])
        f.choice("payment_method", "Payment method", [k for k, _ in jtr.PAYMENT_METHODS])
        miles = amount = None
        if cat == "POV_MILEAGE":
            miles = f.number("miles", "Miles")
            amount = 0
        else:
            amount = f.number("amount", "Amount")
        if dte and not (doc["start_date"] <= dte <= doc["end_date"]):
            f.errors["expense_date"] = "Expense date must fall within the trip dates."
        errors = f.errors
        if f.ok:
            db.execute("INSERT INTO expenses(document_id, expense_date, category, description, amount, miles,"
                       " payment_method, receipt_attached) VALUES (?,?,?,?,?,?,?,?)",
                       (doc_id, dte, cat, values["description"], amount, miles, values["payment_method"],
                        1 if values["receipt_attached"] else 0))
            recalc(doc_id)
            db.commit()
            flash("Expense added.", "success")
            return redirect(request.path)
    stops, exps = load_children(doc_id)
    rate = float(get_db().execute("SELECT value FROM settings WHERE key='pov_mileage_rate'").fetchone()["value"])
    return render_template("documents/expenses.html", doc=doc, expenses=exps, values=values, errors=errors,
                           categories=jtr.EXPENSE_CATEGORIES, methods=jtr.PAYMENT_METHODS, mileage_rate=rate,
                           amount_of=lambda e: jtr.expense_amount(e, rate), steps=STEPS, current="expenses")


# ---------------------------------------------------------------- review/view
@bp.route("/<int:doc_id>")
def view(doc_id):
    doc = get_doc(doc_id)
    db = get_db()
    stops, exps = load_children(doc_id)
    pd, exp_total, total = jtr.document_totals(doc, stops, exps)
    findings = jtr.preaudit(doc, stops, exps, pd)
    traveler = db.execute("SELECT * FROM users WHERE id=?", (doc["traveler_id"],)).fetchone()
    preparer = db.execute("SELECT * FROM users WHERE id=?", (doc["created_by"],)).fetchone()
    org = db.execute("SELECT * FROM orgs WHERE id=?", (doc["org_id"],)).fetchone()
    loa = db.execute("SELECT * FROM loas WHERE id=?", (doc["loa_id"],)).fetchone() if doc["loa_id"] else None
    history = db.execute("SELECT h.*, u.first_name, u.last_name FROM document_history h "
                         "LEFT JOIN users u ON u.id=h.user_id WHERE document_id=? ORDER BY h.id", (doc_id,)).fetchall()
    steps = workflow.steps_for(doc)
    act_ok, act_reason, cur = workflow.can_act(me(), doc)
    parent = db.execute("SELECT * FROM documents WHERE id=?", (doc["parent_auth_id"],)).fetchone() \
        if doc["parent_auth_id"] else None
    voucher = db.execute("SELECT * FROM documents WHERE parent_auth_id=? AND status<>'CANCELLED'",
                         (doc_id,)).fetchone()
    funds = None
    if loa:
        used = db.execute("SELECT COALESCE(SUM(total_estimate),0) s FROM documents WHERE loa_id=? AND doc_type IN "
                          "('AUTH','LOCAL') AND status IN ('ROUTING','APPROVED','PAID') AND id<>?",
                          (loa["id"], doc_id)).fetchone()["s"]
        funds = {"available": loa["budget_amount"] - used}
    return render_template(
        "documents/view.html", doc=doc, stops=stops, expenses=exps, per_diem=pd, exp_total=exp_total,
        total=total, findings=findings, traveler=traveler, preparer=preparer, org=org, loa=loa, history=history,
        steps=steps, current_step=cur, can_act=act_ok, act_reason=act_reason, parent=parent, voucher=voucher,
        funds=funds, editable=can_edit(doc), signable=can_sign(doc), cancellable=can_cancel(doc),
        amendable=can_amend(doc), returnable=workflow.can_return(me(), doc),
        overridable=doc["status"] == "ROUTING" and rbac.has_perm(me(), "workflow.override", doc["org_id"]),
        own=is_own(doc), wizard_steps=STEPS, errors=request.args.get("e"),
        amount_of=jtr.expense_amount, print_view=request.args.get("print") == "1")


@bp.route("/<int:doc_id>/sign", methods=["POST"])
def sign(doc_id):
    doc = get_doc(doc_id)
    if not can_sign(doc):
        abort(403)
    db = get_db()
    stops, exps = load_children(doc_id)
    pd, _, _ = jtr.document_totals(doc, stops, exps)
    findings = jtr.preaudit(doc, stops, exps, pd)
    justification = request.form.get("justification", "").strip()
    errors = []
    if doc["doc_type"] != "LOCAL" and not stops:
        errors.append("Add at least one TDY location before signing.")
    if findings and not justification:
        errors.append("Enter a justification for the pre-audit flags before signing.")
    if not request.form.get("certify"):
        errors.append("Confirm the certification statement to sign.")
    if errors:
        flash(" ".join(errors), "error")
        return redirect(url_for("documents.view", doc_id=doc_id) + "#sign")
    db.execute("UPDATE documents SET justification=? WHERE id=?", (justification, doc_id))
    try:
        workflow.sign(doc, me())
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("documents.view", doc_id=doc_id))
    recalc(doc_id)
    audit(me(), "DOC_SIGN", "document:%d" % doc_id, "on behalf" if not is_own(doc) else "")
    db.commit()
    flash("Document signed and sent to routing.", "success")
    return redirect(url_for("documents.view", doc_id=doc_id))


@bp.route("/<int:doc_id>/action", methods=["POST"])
def action(doc_id):
    doc = get_doc(doc_id)
    db = get_db()
    act = request.form.get("action")
    remarks = request.form.get("remarks", "")
    try:
        if act == "stamp":
            msg = workflow.stamp(doc, me(), remarks)
        elif act == "return":
            workflow.return_doc(doc, me(), remarks)
            msg = "Document returned to the traveler."
        elif act == "override":
            workflow.override(doc, me(), remarks)
            msg = "Routing step overridden."
        elif act == "cancel":
            if not can_cancel(doc):
                raise PermissionError("You cannot cancel this document.")
            db.execute("UPDATE documents SET status='CANCELLED', current_step_seq=NULL WHERE id=?", (doc_id,))
            workflow.log(doc_id, me(), "CANCEL", "CANCELLED", remarks)
            msg = "Document cancelled."
        elif act == "amend":
            if not can_amend(doc):
                raise PermissionError("You cannot amend this document.")
            db.execute("UPDATE documents SET status='CREATED', amendment=amendment+1 WHERE id=?", (doc_id,))
            workflow.log(doc_id, me(), "AMEND", "AMENDED", remarks)
            msg = "Amendment started. Make your changes, then sign again."
        else:
            abort(400)
    except (PermissionError, ValueError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("documents.view", doc_id=doc_id) + "#actions")
    audit(me(), "DOC_" + act.upper(), "document:%d" % doc_id, remarks)
    db.commit()
    flash(msg, "success")
    if act in ("stamp", "return") and request.form.get("from") == "queue":
        return redirect(url_for("documents.queue"))
    return redirect(url_for("documents.view", doc_id=doc_id))
