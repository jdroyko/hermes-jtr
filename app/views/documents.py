from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

from .. import geo, jtr, rbac, workflow
from ..db import audit, get_db
from ._util import (Form, can_amend, can_cancel, can_edit, can_sign, get_doc, is_own, load_children, me,
                    next_doc_number, next_sort_order, recalc, travelers_for_preparer)

bp = Blueprint("documents", __name__, url_prefix="/documents")

STEPS = [("trip", "Trip details"), ("itinerary", "Itinerary & lodging"), ("travel", "Flights, hotels & cars"),
         ("expenses", "Expenses"), ("review", "Review & sign")]


def loas_for(org_id):
    ids = rbac.org_ancestors(org_id)
    return get_db().execute("SELECT * FROM loas WHERE active=1 AND org_id IN (%s) ORDER BY label"
                            % ",".join("?" * len(ids)), ids).fetchall()


def _locations():
    return get_db().execute("SELECT MIN(id) id, location, state FROM per_diem_rates WHERE status='active' "
                            "GROUP BY location, state ORDER BY location").fetchall()


def tidy_place(text):
    """'SAn francisco' -> 'San Francisco'; short all-caps words (AFB, JB, NAS) stay."""
    return " ".join(w if (w.isupper() and len(w) <= 4) or not w.isalpha() else w[:1].upper() + w[1:].lower()
                    for w in text.split())


def resolve_location(db, location_text, new_state):
    """Match a typed TDY location against the per diem rate list
    (case-insensitive). Anything else is accepted as typed and paid at the
    Standard CONUS rate (lower 48 states and DC) or Standard OCONUS rate.
    With no state given, the state is looked up from known cities and bases;
    a place that can't be placed is assumed to be in CONUS (state None).
    Returns (rate_id, custom, error); custom is (place, state) or None."""
    location_text = (location_text or "").strip()
    loc, state = location_text, (new_state or "").strip()
    if not state and "," in location_text:
        loc, state = (p.strip() for p in location_text.rsplit(",", 1))
    if not loc:
        return None, None, None  # caller already requires location_text
    if not state:
        rows = db.execute("SELECT MIN(id) id FROM per_diem_rates WHERE location=? COLLATE NOCASE "
                          "GROUP BY state", (loc,)).fetchall()
        if len(rows) == 1:
            return rows[0]["id"], None, None
        found = geo.locate(loc)
        state = found[3] if found else ""
    if state:
        row = db.execute("SELECT id FROM per_diem_rates WHERE location=? COLLATE NOCASE AND state=? COLLATE NOCASE",
                         (loc, state)).fetchone()
        if row:
            return row["id"], None, None
    code = jtr.conus_code(state) if state else None
    standard = jtr.STANDARD_OCONUS if state and not code else jtr.STANDARD_CONUS
    rate = db.execute("SELECT MIN(id) id FROM per_diem_rates WHERE location=?", (standard,)).fetchone()
    if rate["id"] is None:
        return None, None, "No %s per diem rate is set up. Ask your administrator to add one." % standard
    place = tidy_place(loc)
    if code and place.lower() == jtr.CONUS_STATES[code].lower():
        place = jtr.CONUS_STATES[code]
    return rate["id"], (place, code or state or None), None


def place_notice(custom):
    """Flash text for a place with no locality rate of its own."""
    name, state = custom
    if not state:
        return ("%s has no locality per diem rate, so the Standard CONUS rate applies. If it is outside the "
                "continental US, remove it and add it again with its country, like %s, Germany." % (name, name))
    std = "CONUS" if jtr.conus_code(state) else "OCONUS"
    return "%s, %s has no locality per diem rate, so the Standard %s rate applies." % (name, state, std)


def validate_trip(f):
    f.choice("trip_purpose", "Trip purpose", [k for k, _ in jtr.TRIP_PURPOSES])
    f.required("trip_description", "Trip description")
    s = f.date("start_date", "Start date")
    e = f.date("end_date", "End date")
    if s and e and e < s:
        f.errors["end_date"] = "End date must be the same as or after the start date."
    return s, e


# --------------------------------------------------------------------- list
# Whitelisted ORDER BY expressions for the Documents list (never user text).
_DOC_SORTS = {"doc": "d.doc_number", "type": "d.doc_type", "traveler": "u.last_name COLLATE NOCASE, u.first_name COLLATE NOCASE",
              "org": "o.code", "trip": "d.trip_description COLLATE NOCASE", "dates": "d.start_date",
              "status": "d.status", "total": "d.total_estimate"}


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
    else:
        # Cancelled documents stay out of the default list; pick the
        # Cancelled status filter to see them.
        where.append("d.status<>'CANCELLED'")
    if doc_type:
        where.append("d.doc_type=?")
        params.append(doc_type)
    where.append("d.is_template=0")
    sort = request.args.get("sort", "")
    if sort not in _DOC_SORTS:
        sort = ""
    direction = "desc" if request.args.get("dir") == "desc" else "asc"
    order = ("%s %s, d.id" % (_DOC_SORTS[sort], direction.upper())) if sort else "d.updated_at DESC"
    sql = ("SELECT d.*, u.first_name, u.last_name, o.code org_code FROM documents d "
           "JOIN users u ON u.id=d.traveler_id JOIN orgs o ON o.id=d.org_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    docs = db.execute(sql + " ORDER BY " + order + " LIMIT 500", params).fetchall()
    # Travelers looking only at their own documents don't need a Traveler column.
    show_traveler = any(d["traveler_id"] != me() for d in docs)
    return render_template("documents/index.html", docs=docs, status=status, doc_type=doc_type, scope=scope,
                           sort=sort, direction=direction, show_traveler=show_traveler)


@bp.route("/queue")
def queue():
    items = workflow.queue_for(me())
    db = get_db()
    rows = []
    for d, step in items:
        u = db.execute("SELECT first_name, last_name FROM users WHERE id=?", (d["traveler_id"],)).fetchone()
        rows.append((d, step, u))
    return render_template("documents/queue.html", rows=rows)


def _visible_docs(status_list):
    """Documents in the given statuses that the current user may view,
    mirroring index()'s own-or-org-scoped visibility rule."""
    db = get_db()
    where = ["d.status IN (%s)" % ",".join("?" * len(status_list)), "d.is_template=0"]
    params = list(status_list)
    org_clause = "(d.traveler_id=? OR d.created_by=?)"
    org_params = [me(), me()]
    if rbac.has_perm(me(), "document.view.org"):
        orgs = rbac.scoped_org_ids(me(), "document.view.org")
        if orgs is None:
            org_clause = "1"
            org_params = []
        elif orgs:
            org_clause = "(d.traveler_id=? OR d.created_by=? OR d.org_id IN (%s))" % (",".join("?" * len(orgs)))
            org_params = [me(), me()] + list(orgs)
    where.append(org_clause)
    params += org_params
    sql = ("SELECT d.*, u.first_name, u.last_name FROM documents d JOIN users u ON u.id=d.traveler_id "
          "WHERE " + " AND ".join(where) + " ORDER BY d.updated_at DESC LIMIT 100")
    return db.execute(sql, params).fetchall()


def _card_context(doc):
    stops, exps = load_children(doc["id"])
    pd, _, _ = jtr.document_totals(doc, stops, exps)
    findings = jtr.preaudit(doc, stops, exps, pd)
    act_ok, act_reason, cur = workflow.can_act(me(), doc)
    return dict(
        doc=doc, findings=findings, current_step=cur, can_act=act_ok, act_reason=act_reason,
        editable=can_edit(doc), signable=can_sign(doc), cancellable=can_cancel(doc),
        amendable=can_amend(doc), returnable=workflow.can_return(me(), doc),
        overridable=doc["status"] == "ROUTING" and rbac.has_perm(me(), "workflow.override", doc["org_id"]),
        own=is_own(doc),
    )


@bp.route("/board")
def board():
    needs_action = [d for d, _step in workflow.queue_for(me())]
    returned = _visible_docs(["RETURNED"])
    approved = _visible_docs(["APPROVED", "PAID"])
    columns = [
        ("needs-action", "Needs my action", needs_action),
        ("returned", "Returned", returned),
        ("approved", "Approved / Paid", approved),
    ]
    card_context = {}
    for _key, _label, docs in columns:
        for d in docs:
            card_context[d["id"]] = _card_context(d)
    return render_template("documents/board.html", columns=columns, card_context=card_context)


# ------------------------------------------------------------------- create
@bp.route("/new", methods=["GET", "POST"])
def new():
    requested = request.values.get("type", "AUTH")
    if requested not in ("AUTH", "LOCAL", "PLAN"):
        abort(404)
    is_plan = requested == "PLAN"
    doc_type = "AUTH" if is_plan else requested
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
        s, e = validate_trip(f)
        errors = f.errors
        if f.ok:
            db = get_db()
            loa = f.get("loa_id") or None
            rl = workflow.resolve_routing_list(traveler, traveler["org_id"])
            doc_id = db.execute(
                "INSERT INTO documents(doc_number, doc_type, traveler_id, created_by, org_id, trip_purpose,"
                " trip_description, start_date, end_date, loa_id, routing_list_id, trip_type, is_plan)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (next_doc_number(doc_type), doc_type, traveler["id"], me(), traveler["org_id"],
                 values["trip_purpose"], values["trip_description"], s, e, loa, rl,
                 "LOCAL" if doc_type == "LOCAL" else "TDY", int(is_plan))).lastrowid
            workflow.log(doc_id, me(), "CREATE", "CREATED")
            audit(me(), "DOC_CREATE", "document:%d" % doc_id)
            db.commit()
            label = "Travel plan" if is_plan else workflow.DOC_TYPE_LABELS[doc_type]
            flash("%s created. Add your itinerary next." % label, "success")
            nxt = "expenses" if doc_type == "LOCAL" else "itinerary"
            return redirect(url_for("documents." + nxt, doc_id=doc_id))
    org_id = g.user["org_id"]
    return render_template("documents/trip.html", doc=None, doc_type=requested, values=values, errors=errors,
                           travelers=travelers, loas=loas_for(org_id), purposes=jtr.TRIP_PURPOSES,
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
    db.execute("INSERT INTO document_stops(document_id, rate_id, arrive_date, depart_date, gov_quarters, sort_order, "
               "location_text, location_state) SELECT ?, rate_id, arrive_date, depart_date, gov_quarters, sort_order, "
               "location_text, location_state FROM document_stops "
               "WHERE document_id=?", (vid, doc_id))
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
        s, e = validate_trip(f)
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
                           travelers=None, loas=loas_for(doc["org_id"]), purposes=jtr.TRIP_PURPOSES,
                           steps=STEPS, current="trip")


@bp.route("/<int:doc_id>/itinerary", methods=["GET", "POST"])
def itinerary(doc_id):
    doc = get_doc(doc_id)
    _require_edit(doc)
    db = get_db()
    errors, values = {}, {"location_text": "", "new_state": "", "arrive_date": doc["start_date"],
                          "depart_date": doc["end_date"], "lodging_nightly": "", "gov_quarters": ""}
    if request.method == "POST":
        action = request.form.get("action", "add")
        if action == "delete":
            db.execute("DELETE FROM expenses WHERE id IN (SELECT expense_id FROM trip_selections "
                       "WHERE stop_id=? AND document_id=?)", (request.form.get("stop_id"), doc_id))
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
            location_text = f.required("location_text", "TDY location")
            a = f.date("arrive_date", "Arrival date")
            d = f.date("depart_date", "Departure date")
            if a and d:
                if d <= a:
                    f.errors["depart_date"] = "Departure date must be after the arrival date."
                elif a < doc["start_date"] or d > doc["end_date"]:
                    f.errors["arrive_date"] = "Dates must fall within the trip dates (%s to %s)." % (
                        doc["start_date"], doc["end_date"])
            nightly = f.number("lodging_nightly", "Nightly lodging cost", required=False)
            rid, custom = None, None
            if location_text:
                rid, custom, loc_error = resolve_location(db, location_text, values["new_state"])
                if loc_error:
                    f.errors["new_state"] = loc_error
            errors = f.errors
            if f.ok:
                db.execute("INSERT INTO document_stops(document_id, rate_id, arrive_date, depart_date, "
                           "lodging_nightly, gov_quarters, sort_order, location_text, location_state) "
                           "VALUES (?,?,?,?,?,?,?,?,?)",
                           (doc_id, rid, a, d, nightly, 1 if values["gov_quarters"] else 0,
                            next_sort_order(doc_id), custom[0] if custom else None, custom[1] if custom else None))
                recalc(doc_id)
                db.commit()
                if custom:
                    flash("Location added. " + place_notice(custom), "success")
                else:
                    flash("Location added.", "success")
                return redirect(request.path)
    stops, expenses = load_children(doc_id)
    pd, _, _ = jtr.document_totals(doc, stops, expenses)
    return render_template("documents/itinerary.html", doc=doc, stops=stops, per_diem=pd, values=values,
                           errors=errors, locations=_locations(), steps=STEPS, current="itinerary")


@bp.route("/<int:doc_id>/itinerary/reorder", methods=["POST"])
def itinerary_reorder(doc_id):
    doc = get_doc(doc_id)
    _require_edit(doc)
    db = get_db()
    stops = db.execute("SELECT id, sort_order FROM document_stops WHERE document_id=? ORDER BY sort_order",
                       (doc_id,)).fetchall()
    ids_in_order = [s["id"] for s in stops]

    move_up, move_down = request.form.get("move_up"), request.form.get("move_down")
    if move_up or move_down:
        stop_id = int(move_up or move_down)
        if stop_id not in ids_in_order:
            abort(400)
        i = ids_in_order.index(stop_id)
        j = i - 1 if move_up else i + 1
        if 0 <= j < len(ids_in_order):
            ids_in_order[i], ids_in_order[j] = ids_in_order[j], ids_in_order[i]
    else:
        order = request.form.getlist("stop_id")
        if not order or {int(i) for i in order} != set(ids_in_order):
            abort(400)
        ids_in_order = [int(i) for i in order]

    for i, stop_id in enumerate(ids_in_order):
        db.execute("UPDATE document_stops SET sort_order=? WHERE id=? AND document_id=?", (i, stop_id, doc_id))
    recalc(doc_id)
    db.commit()
    flash("Itinerary order updated.", "success")
    return redirect(url_for("documents.itinerary", doc_id=doc_id))


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
    attachments_by_expense = {}
    if exps:
        ids = [e["id"] for e in exps]
        for a in db.execute("SELECT * FROM attachments WHERE expense_id IN (%s) ORDER BY created_at"
                            % ",".join("?" * len(ids)), ids):
            attachments_by_expense.setdefault(a["expense_id"], []).append(dict(a))
    for e in exps:
        e["attachments"] = attachments_by_expense.get(e["id"], [])
    rate = float(get_db().execute("SELECT value FROM settings WHERE key='pov_mileage_rate'").fetchone()["value"])
    return render_template("documents/expenses.html", doc=doc, expenses=exps, values=values, errors=errors,
                           categories=jtr.EXPENSE_CATEGORIES, methods=jtr.PAYMENT_METHODS, mileage_rate=rate,
                           amount_of=lambda e: jtr.expense_amount(e, rate), steps=STEPS, current="expenses")


# ---------------------------------------------------------------- review/view
@bp.route("/<int:doc_id>")
def view(doc_id):
    doc = get_doc(doc_id)
    if doc["is_template"]:
        return redirect(url_for("plans.review", plan_id=doc_id))
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
    booking = db.execute("SELECT * FROM bookings WHERE document_id=?", (doc_id,)).fetchone()
    booking_items = db.execute("SELECT * FROM booking_items WHERE booking_id=? ORDER BY id",
                               (booking["id"],)).fetchall() if booking else []
    selections = db.execute("SELECT * FROM trip_selections WHERE document_id=? ORDER BY start_date, id",
                            (doc_id,)).fetchall()
    comparison = db.execute("SELECT * FROM trip_comparisons WHERE document_id=?", (doc_id,)).fetchone()
    return render_template(
        "documents/view.html", doc=doc, stops=stops, expenses=exps, per_diem=pd, exp_total=exp_total,
        total=total, findings=findings, traveler=traveler, preparer=preparer, org=org, loa=loa, history=history,
        steps=steps, current_step=cur, can_act=act_ok, act_reason=act_reason, parent=parent, voucher=voucher,
        funds=funds, booking=booking, booking_items=booking_items, selections=selections, comparison=comparison, editable=can_edit(doc), signable=can_sign(doc), cancellable=can_cancel(doc),
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
        return redirect(url_for("documents.view", doc_id=doc_id) + "#sign-%d" % doc_id)
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
    wants_json = request.headers.get("X-Requested-With") == "fetch"
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
            db.execute("UPDATE bookings SET status='CANCELLED' WHERE document_id=?", (doc_id,))
            workflow.log(doc_id, me(), "CANCEL", "CANCELLED", remarks)
            msg = "Document cancelled."
        elif act == "amend":
            if not can_amend(doc):
                raise PermissionError("You cannot amend this document.")
            db.execute("UPDATE documents SET status='CREATED', amendment=amendment+1, preapproved_by=NULL "
                       "WHERE id=?", (doc_id,))
            db.execute("UPDATE bookings SET status='CANCELLED' WHERE document_id=?", (doc_id,))
            workflow.log(doc_id, me(), "AMEND", "AMENDED", remarks)
            msg = "Amendment started. Make your changes, then sign again."
        else:
            abort(400)
    except (PermissionError, ValueError) as exc:
        if wants_json:
            return jsonify(ok=False, error=str(exc)), 200
        flash(str(exc), "error")
        return redirect(url_for("documents.view", doc_id=doc_id) + "#actions")
    audit(me(), "DOC_" + act.upper(), "document:%d" % doc_id, remarks)
    db.commit()
    if wants_json:
        doc = get_doc(doc_id)
        return jsonify(ok=True, message=msg, status=doc["status"])
    flash(msg, "success")
    if act in ("stamp", "return") and request.form.get("from") == "queue":
        return redirect(url_for("documents.queue"))
    return redirect(url_for("documents.view", doc_id=doc_id))
