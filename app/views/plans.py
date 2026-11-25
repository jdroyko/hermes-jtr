"""AO group travel plans: one trip estimate issued as an authorization to
each assigned traveler. Each authorization still routes through funds
verification; the AO's approval stamp is then applied automatically
(workflow._apply_preapproval)."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import jtr, rbac, workflow
from ..db import audit, get_db
from ._util import Form, get_doc, is_plan_author, load_children, me, next_doc_number, recalc
from .documents import STEPS, loas_for, validate_trip

bp = Blueprint("plans", __name__, url_prefix="/plans")


@bp.before_request
def _gate():
    if not rbac.has_perm(me(), "plan.assign"):
        abort(403)


def _candidates():
    """Active users the current AO may issue plans to (never themselves)."""
    db = get_db()
    scope = rbac.scoped_org_ids(me(), "plan.assign")
    sql = ("SELECT u.*, o.code org_code FROM users u JOIN orgs o ON o.id=u.org_id "
           "WHERE u.active=1 AND u.id<>?")
    params = [me()]
    if scope is not None:
        if not scope:
            return []
        sql += " AND u.org_id IN (%s)" % ",".join("?" * len(scope))
        params += list(scope)
    return db.execute(sql + " ORDER BY u.last_name, u.first_name", params).fetchall()


def _template(plan_id):
    doc = get_doc(plan_id)
    if not doc["is_template"]:
        abort(404)
    if not is_plan_author(doc):
        abort(403)
    return doc


@bp.route("/")
def index():
    plans = get_db().execute(
        "SELECT d.*, (SELECT COUNT(*) FROM plan_assignees a WHERE a.template_id=d.id) assignees "
        "FROM documents d WHERE d.is_template=1 AND d.created_by=? ORDER BY d.updated_at DESC", (me(),)).fetchall()
    return render_template("plans/index.html", plans=plans)


@bp.route("/new", methods=["GET", "POST"])
def new():
    values = {"trip_purpose": "TDY", "start_date": "", "end_date": "", "trip_description": "", "loa_id": ""}
    errors = {}
    if request.method == "POST":
        f = Form(request.form)
        values.update({k: f.get(k) for k in values})
        s, e = validate_trip(f)
        errors = f.errors
        if f.ok:
            db = get_db()
            doc_id = db.execute(
                "INSERT INTO documents(doc_number, doc_type, traveler_id, created_by, org_id, trip_purpose,"
                " trip_description, start_date, end_date, loa_id, is_plan, is_template)"
                " VALUES (?, 'AUTH', ?,?,?,?,?,?,?,?,1,1)",
                (next_doc_number("AUTH"), me(), me(), g.user["org_id"], values["trip_purpose"],
                 values["trip_description"], s, e, values["loa_id"] or None)).lastrowid
            workflow.log(doc_id, me(), "CREATE", "CREATED", "Group travel plan")
            audit(me(), "PLAN_CREATE", "document:%d" % doc_id)
            db.commit()
            flash("Group plan created. Add the itinerary next.", "success")
            return redirect(url_for("documents.itinerary", doc_id=doc_id))
    return render_template("documents/trip.html", doc=None, doc_type="GROUP", values=values, errors=errors,
                           travelers=None, loas=loas_for(g.user["org_id"]), purposes=jtr.TRIP_PURPOSES,
                           steps=STEPS, current="trip")


@bp.route("/<int:plan_id>", methods=["GET", "POST"])
def review(plan_id):
    doc = _template(plan_id)
    db = get_db()
    candidates = _candidates()
    by_id = {c["id"]: c for c in candidates}
    stops, exps = load_children(plan_id)
    pd, exp_total, total = jtr.document_totals(doc, stops, exps)
    findings = jtr.preaudit(doc, stops, exps, pd)
    selected = {r["user_id"] for r in db.execute("SELECT user_id FROM plan_assignees WHERE template_id=?",
                                                  (plan_id,))}
    errors = {}
    justification = doc["justification"]
    if request.method == "POST":
        if doc["status"] != "CREATED":
            abort(400, "This plan has already been issued.")
        chosen = set()
        for raw in request.form.getlist("traveler_id"):
            if not raw.isdigit() or int(raw) not in by_id:
                abort(400, "One of the selected travelers is outside your organizations.")
            chosen.add(int(raw))
        selected = chosen
        justification = request.form.get("justification", "").strip()
        issuing = request.form.get("action") == "issue"
        if issuing:
            if not chosen:
                errors["travelers"] = "Select at least one traveler to issue this plan to."
            if not stops:
                errors["travelers"] = "Add at least one TDY location to the itinerary before issuing."
            if findings and not justification:
                errors["justification"] = "Enter a justification for the pre-audit flags before issuing."
            for uid in sorted(chosen):
                person = by_id[uid]
                rl = workflow.resolve_routing_list(person, person["org_id"])
                if not workflow.steps_for({"routing_list_id": rl, "doc_type": "AUTH"}):
                    errors["travelers"] = ("%s %s has no routing list for authorizations. Ask your DTA to set one "
                                           "up, or remove them from this plan." % (person["first_name"],
                                                                                  person["last_name"]))
                    break
        if not errors:
            db.execute("DELETE FROM plan_assignees WHERE template_id=?", (plan_id,))
            for uid in sorted(chosen):
                db.execute("INSERT INTO plan_assignees(template_id, user_id) VALUES (?,?)", (plan_id, uid))
            db.execute("UPDATE documents SET justification=?, updated_at=datetime('now') WHERE id=?",
                       (justification, plan_id))
            if issuing:
                try:
                    count = _issue(doc, [by_id[u] for u in sorted(chosen)], justification)
                except (PermissionError, ValueError) as exc:
                    db.rollback()
                    flash(str(exc), "error")
                    return redirect(request.path)
                audit(me(), "PLAN_ISSUE", "document:%d" % plan_id, "%d traveler(s)" % count)
                db.commit()
                flash("Plan issued as %d authorization%s. Each goes to funds verification next; your approval "
                      "is applied automatically after that." % (count, "" if count == 1 else "s"), "success")
            else:
                audit(me(), "PLAN_SAVE_DRAFT", "document:%d" % plan_id)
                db.commit()
                flash("Draft saved. No authorizations were created.", "success")
            return redirect(request.path)
    issued = db.execute(
        "SELECT a.*, u.first_name, u.last_name, d.doc_number, d.status, d.current_step_seq, d.routing_list_id, "
        "d.doc_type FROM plan_assignees a JOIN users u ON u.id=a.user_id "
        "LEFT JOIN documents d ON d.id=a.document_id WHERE a.template_id=? ORDER BY u.last_name",
        (plan_id,)).fetchall()
    return render_template("plans/review.html", doc=doc, stops=stops, expenses=exps, per_diem=pd,
                           exp_total=exp_total, total=total, findings=findings, candidates=candidates,
                           selected=selected, issued=issued, errors=errors, justification=justification,
                           wizard_steps=STEPS, amount_of=jtr.expense_amount)


def _issue(template, people, justification):
    db = get_db()
    for person in people:
        rl = workflow.resolve_routing_list(person, person["org_id"])
        new_id = db.execute(
            "INSERT INTO documents(doc_number, doc_type, traveler_id, created_by, org_id, trip_purpose,"
            " trip_description, start_date, end_date, loa_id, routing_list_id, justification, is_plan,"
            " template_id, preapproved_by)"
            " VALUES (?, 'AUTH', ?,?,?,?,?,?,?,?,?,?,1,?,?)",
            (next_doc_number("AUTH"), person["id"], me(), person["org_id"], template["trip_purpose"],
             template["trip_description"], template["start_date"], template["end_date"], template["loa_id"],
             rl, justification, template["id"], me())).lastrowid
        db.execute("INSERT INTO document_stops(document_id, rate_id, arrive_date, depart_date, lodging_nightly,"
                   " gov_quarters, sort_order, location_text, location_state) SELECT ?, rate_id, arrive_date, depart_date,"
                   " lodging_nightly, gov_quarters, sort_order, location_text, location_state FROM document_stops WHERE document_id=?", (new_id, template["id"]))
        db.execute("INSERT INTO expenses(document_id, expense_date, category, description, amount, miles,"
                   " payment_method) SELECT ?, expense_date, category, description, amount, miles, payment_method"
                   " FROM expenses WHERE document_id=?", (new_id, template["id"]))
        db.execute(
            "INSERT INTO trip_selections(document_id, kind, stop_id, vendor, title, details, start_date, end_date,"
            " unit_price, units, amount, in_policy, policy_reason, justification, expense_id)"
            " SELECT ?, t.kind,"
            " (SELECT ns.id FROM document_stops ns JOIN document_stops os ON os.sort_order=ns.sort_order"
            "  AND os.arrive_date=ns.arrive_date WHERE ns.document_id=? AND os.id=t.stop_id LIMIT 1),"
            " t.vendor, t.title, t.details, t.start_date, t.end_date, t.unit_price, t.units, t.amount, t.in_policy,"
            " t.policy_reason, t.justification,"
            " (SELECT ne.id FROM expenses ne JOIN expenses oe ON oe.category=ne.category"
            "  AND oe.expense_date=ne.expense_date AND oe.description=ne.description AND oe.amount=ne.amount"
            "  WHERE ne.document_id=? AND oe.id=t.expense_id ORDER BY ne.id LIMIT 1)"
            " FROM trip_selections t WHERE t.document_id=?", (new_id, new_id, new_id, template["id"]))
        workflow.log(new_id, me(), "CREATE", "CREATED", "Issued from group plan %s" % template["doc_number"])
        recalc(new_id)
        doc = db.execute("SELECT * FROM documents WHERE id=?", (new_id,)).fetchone()
        workflow.sign(doc, me(), "Signed by the Approving Official when issuing group plan %s"
                      % template["doc_number"])
        db.execute("UPDATE plan_assignees SET document_id=? WHERE template_id=? AND user_id=?",
                   (new_id, template["id"], person["id"]))
    db.execute("UPDATE documents SET status='ISSUED', updated_at=datetime('now') WHERE id=?", (template["id"],))
    workflow.log(template["id"], me(), "ISSUE", "ISSUED", "%d traveler(s)" % len(people))
    return len(people)
