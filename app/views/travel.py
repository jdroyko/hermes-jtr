"""Flights, hotels and rental cars: a Concur-style search tied to the
document's TDY locations. Picks feed the cost estimate and, after approval,
the travel agency's booking queue."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import inventory, jtr
from ..db import audit, get_db
from ._util import Form, can_edit, get_doc, load_children, me, recalc
from .documents import STEPS

bp = Blueprint("travel", __name__, url_prefix="/documents")

TABS = [("AIR", "Flights"), ("LODGING", "Hotels"), ("RENTAL_CAR", "Rental cars")]
SORTS = {"AIR": [("price", "Lowest price"), ("other", "Earliest departure"), ("policy", "In policy first")],
         "LODGING": [("price", "Lowest price"), ("other", "Closest"), ("policy", "In policy first")],
         "RENTAL_CAR": [("price", "Lowest price"), ("other", "Smallest car"), ("policy", "In policy first")]}


def lodging_max_for(stop):
    return float(jtr.rate_for(stop["rate_location"], stop["rate_state"], stop["arrive_date"])["lodging"])


def place_of(stop):
    return "%s, %s" % (stop["location"], stop["state"])


def _search_params(kind, doc, stops, src):
    """Validate the search fields in ``src`` against this document's TDY
    locations. Returns (stop, inventory params, values, errors)."""
    values = {"stop_id": src.get("stop_id", ""), "origin": src.get("origin", "").strip(),
              "depart": src.get("depart", "") or doc["start_date"], "return": src.get("return", "") or doc["end_date"]}
    by_id = {str(s["id"]): s for s in stops}
    stop = by_id.get(values["stop_id"]) or stops[0]
    values["stop_id"] = str(stop["id"])
    if kind == "AIR":
        if not values["origin"]:
            values["origin"] = (g.user["duty_station"] or "").strip()
        f = Form(values)
        if not values["origin"] and not src.get("searched"):
            return stop, None, values, {}
        f.required("origin", "Departing from")
        a = f.date("depart", "Departure date")
        b = f.date("return", "Return date")
        if a and not doc["start_date"] <= a <= doc["end_date"]:
            f.errors["depart"] = "Departure date must fall within the trip dates."
        if a and b and not a <= b <= doc["end_date"]:
            f.errors["return"] = "Return date must be on or after departure and within the trip dates."
        params = dict(origin=values["origin"], destination=place_of(stop), depart=values["depart"], ret=values["return"])
        return stop, params, values, f.errors
    elif kind == "LODGING":
        params = dict(location=stop["location"], state=stop["state"], check_in=stop["arrive_date"],
                      check_out=stop["depart_date"], lodging_max=lodging_max_for(stop))
    else:
        params = dict(location=place_of(stop), pick_up=stop["arrive_date"], drop_off=stop["depart_date"],
                      lodging_max=lodging_max_for(stop))
    return stop, params, values, {}


def _selections(doc_id):
    return get_db().execute("SELECT * FROM trip_selections WHERE document_id=? ORDER BY "
                            "CASE kind WHEN 'AIR' THEN 0 WHEN 'LODGING' THEN 1 ELSE 2 END, start_date, id",
                            (doc_id,)).fetchall()


def remove_selection(db, sel):
    if sel["expense_id"]:
        db.execute("DELETE FROM expenses WHERE id=?", (sel["expense_id"],))
    db.execute("DELETE FROM trip_selections WHERE id=?", (sel["id"],))


def save_selection(db, doc, kind, stop, params, opt, justification):
    old = db.execute("SELECT * FROM trip_selections WHERE document_id=? AND kind=? AND "
                     "(kind='AIR' OR stop_id=?)", (doc["id"], kind, stop["id"])).fetchall()
    for sel in old:
        remove_selection(db, sel)
    if kind == "AIR":
        start, end = params["depart"], params["ret"]
    else:
        start, end = stop["arrive_date"], stop["depart_date"]
    expense_id = None
    if kind in ("AIR", "RENTAL_CAR"):
        category, method = ("AIRFARE", "CBA") if kind == "AIR" else ("RENTAL_CAR", "GTCC")
        expense_id = db.execute(
            "INSERT INTO expenses(document_id, expense_date, category, description, amount, payment_method)"
            " VALUES (?,?,?,?,?,?)", (doc["id"], start, category, "Selected: %s" % opt["title"], opt["total"],
                                      method)).lastrowid
    db.execute("INSERT INTO trip_selections(document_id, kind, stop_id, vendor, title, details, start_date, end_date,"
               " unit_price, units, amount, in_policy, policy_reason, justification, expense_id)"
               " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (doc["id"], kind, None if kind == "AIR" else stop["id"], opt["vendor"], opt["title"],
                "; ".join(opt["details"]), start, end, opt["unit_price"], opt["units"], opt["total"],
                1 if opt["in_policy"] else 0, opt["policy_reason"], justification, expense_id))
    recalc(doc["id"])
    audit(me(), "TRAVEL_SELECT", "document:%d" % doc["id"], "%s %s %.2f" % (kind, opt["title"], opt["total"]))


@bp.route("/<int:doc_id>/travel", methods=["GET", "POST"])
def search(doc_id):
    doc = get_doc(doc_id)
    if doc["doc_type"] != "AUTH":
        abort(404)
    if not can_edit(doc):
        abort(403)
    db = get_db()
    stops, _ = load_children(doc_id)
    src = request.form if request.method == "POST" else request.args
    kind = src.get("kind", "AIR")
    if kind not in inventory.KINDS:
        abort(400)
    confirm, errors = None, {}

    if request.method == "POST" and src.get("action") == "remove":
        sel = db.execute("SELECT * FROM trip_selections WHERE id=? AND document_id=?",
                         (src.get("selection_id"), doc_id)).fetchone()
        if sel:
            remove_selection(db, sel)
            recalc(doc_id)
            audit(me(), "TRAVEL_REMOVE", "document:%d" % doc_id, sel["title"])
            db.commit()
            flash("Removed %s." % sel["title"], "success")
        return redirect(url_for(".search", doc_id=doc_id, kind=kind))

    results, values, stop, sort, only = [], {}, None, src.get("sort", "price"), bool(src.get("in_policy_only"))
    if sort not in dict(SORTS[kind]):
        sort = "price"
    if stops:
        stop, params, values, errors = _search_params(kind, doc, stops, src)
        if request.method == "POST":
            if errors or params is None:
                abort(400)
            opt = inventory.find(kind, src.get("option_id", ""), **params)
            if opt is None:
                abort(400)
            justification = (src.get("justification") or "").strip()
            if not opt["in_policy"] and not justification:
                confirm = opt
                if src.get("confirming"):
                    errors = {"justification": "Explain why this out-of-policy option is needed."}
            else:
                save_selection(db, doc, kind, stop, params, opt, justification)
                db.commit()
                flash("Added %s (${:,.2f}) to your plan.".format(opt["total"]) % opt["title"], "success")
                return redirect(url_for(".search", doc_id=doc_id, kind=kind, stop_id=values["stop_id"],
                                        origin=values.get("origin") if kind == "AIR" else None))
        elif not errors and params is not None:
            results = inventory.arrange(inventory.search(kind, **params), sort, only)
    selected = {(s["kind"], s["stop_id"]): s for s in _selections(doc_id)}
    return render_template("documents/travel.html", doc=doc, stops=stops, kind=kind, tabs=TABS, values=values,
                           errors=errors, results=results, stop=stop, sort=sort, sorts=SORTS[kind],
                           in_policy_only=only, confirm=confirm, selections=_selections(doc_id), selected=selected,
                           lodging_max=lodging_max_for(stop) if stop else None, place=place_of,
                           steps=STEPS, current="travel")

