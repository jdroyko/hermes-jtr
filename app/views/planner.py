"""Plan a trip: a simple, one-question-per-screen flow.

Where -> When -> How (drive / fly / other) -> drive comparison or flight
search -> where to stay -> review. The destination becomes the document's
TDY location, which drives both the per diem rate and the travel search.
"Other" hands off to the full authorization form."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for

from .. import geo, inventory, jtr, workflow
from ..db import audit, get_db, setting
from ._util import (Form, can_edit, get_doc, load_children, me, next_doc_number, next_sort_order, recalc,
                    travelers_for_preparer)
from .documents import place_notice, resolve_location
from .travel import lodging_max_for, place_of, remove_selection, save_selection

bp = Blueprint("planner", __name__, url_prefix="/plan")

SCREENS = [("where", "Where"), ("when", "When"), ("how", "How"), ("travel", "Travel"), ("stay", "Stay"),
           ("done", "Review")]
MODES = [("POV", "Personal vehicle", "Drive your own car. We compare the mileage cost with flying."),
         ("AIR", "Airfare", "Fly commercially. Pick airports and see flights."),
         ("OTHER", "Other", "Rail, government vehicle, or something else. Continue in the full authorization form.")]
TRIP_TYPES = [("ROUND", "Round trip"), ("ONE", "One way"), ("MULTI", "Multi-city")]


@bp.before_request
def _gate():
    if not any(t["id"] == me() for t in travelers_for_preparer()):
        abort(403)


def _render(screen, **ctx):
    key = "travel" if screen in ("drive", "fly") else screen
    idx = [k for k, _ in SCREENS].index(key) + 1
    return render_template("planner/%s.html" % screen, screen=screen, step=idx, step_total=len(SCREENS),
                           screens=SCREENS, **ctx)


def _doc(doc_id):
    doc = get_doc(doc_id)
    if not (doc["is_plan"] and doc["doc_type"] == "AUTH" and not doc["is_template"] and can_edit(doc)):
        abort(403)
    stops, _ = load_children(doc_id)
    if not stops:
        flash("Add where you are going first.", "error")
        abort(redirect(url_for("documents.itinerary", doc_id=doc_id)))
    return doc, stops[0]


# ------------------------------------------------------------- where / when
@bp.route("/", methods=["GET", "POST"])
def where():
    draft = session.get("planner", {})
    values = {"destination": draft.get("destination", ""), "new_state": draft.get("new_state", "")}
    errors = {}
    if request.method == "POST":
        f = Form(request.form)
        values = {k: f.get(k) for k in values}
        if f.required("destination", "Where you are going"):
            _rid, _custom, err = resolve_location(get_db(), values["destination"], values["new_state"])
            if err:
                f.errors["new_state"] = err
        errors = f.errors
        if f.ok:
            session["planner"] = values
            return redirect(url_for(".when"))
    locations = get_db().execute("SELECT DISTINCT location, state FROM per_diem_rates WHERE status='active' "
                                 "AND location NOT LIKE 'Standard %' ORDER BY location").fetchall()
    return _render("where", values=values, errors=errors, locations=locations,
                   need_state="new_state" in errors or values["new_state"])


@bp.route("/when", methods=["GET", "POST"])
def when():
    draft = session.get("planner")
    if not draft:
        return redirect(url_for(".where"))
    values = {"start_date": "", "end_date": ""}
    errors = {}
    if request.method == "POST":
        f = Form(request.form)
        values = {k: f.get(k) for k in values}
        s = f.date("start_date", "Leave date")
        e = f.date("end_date", "Return date")
        if s and e and e < s:
            f.errors["end_date"] = "Return date must be the same as or after the leave date."
        errors = f.errors
        if f.ok:
            db = get_db()
            rid, custom, err = resolve_location(db, draft["destination"], draft["new_state"])
            if err:
                return redirect(url_for(".where"))
            user = g.user
            name = (", ".join(x for x in custom if x)) if custom else draft["destination"]
            doc_id = db.execute(
                "INSERT INTO documents(doc_number, doc_type, traveler_id, created_by, org_id, trip_purpose,"
                " trip_description, start_date, end_date, routing_list_id, trip_type, is_plan)"
                " VALUES (?, 'AUTH', ?,?,?, 'TDY', ?,?,?,?, 'TDY', 1)",
                (next_doc_number("AUTH"), me(), me(), user["org_id"], "Trip to %s" % name, s, e,
                 workflow.resolve_routing_list(user, user["org_id"]))).lastrowid
            db.execute("INSERT INTO document_stops(document_id, rate_id, arrive_date, depart_date, sort_order,"
                       " location_text, location_state) VALUES (?,?,?,?,?,?,?)",
                       (doc_id, rid, s, e, next_sort_order(doc_id), custom[0] if custom else None,
                        custom[1] if custom else None))
            recalc(doc_id)
            workflow.log(doc_id, me(), "CREATE", "CREATED", "Started in the trip planner")
            audit(me(), "DOC_CREATE", "document:%d" % doc_id)
            db.commit()
            session.pop("planner", None)
            if custom:
                flash(place_notice(custom), "info")
            return redirect(url_for(".how", doc_id=doc_id))
    return _render("when", values=values, errors=errors, destination=draft["destination"])


# ---------------------------------------------------------------------- how
@bp.route("/<int:doc_id>/how", methods=["GET", "POST"])
def how(doc_id):
    doc, stop = _doc(doc_id)
    errors = {}
    if request.method == "POST":
        mode = request.form.get("mode")
        if mode == "POV":
            return redirect(url_for(".drive", doc_id=doc_id))
        if mode == "AIR":
            return redirect(url_for(".fly", doc_id=doc_id))
        if mode == "OTHER":
            flash("Your trip to %s is saved. Finish it in the full authorization form." % place_of(stop), "success")
            return redirect(url_for("documents.trip", doc_id=doc_id))
        errors = {"mode-POV": "Choose how you want to get there."}
    return _render("how", doc=doc, stop=stop, modes=MODES, errors=errors)


# -------------------------------------------------------------------- drive
def _compare(doc, stop, values):
    """Mileage, reimbursement, gas and an airfare estimate for comparison."""
    f = Form(values)
    start = f.required("start", "Starting city or address")
    dest = f.required("destination", "Destination city or address")
    mpg = f.number("mpg", "Miles per gallon") if values.get("mpg") else geo.DEFAULT_MPG
    entered = f.number("miles", "One-way miles", required=False)
    if not f.ok:
        return None, f.errors
    if entered:
        one_way, method = entered, "entered"
    else:
        one_way, method = geo.driving_miles(start, dest)
        if one_way is None:
            return None, {"miles": method}
    rate = setting("pov_mileage_rate", 0.70, float)
    round_trip = round(one_way * 2, 1)
    gas = geo.gas_price(dest)
    out = {"start": start, "destination": dest, "one_way": one_way, "round_trip": round_trip, "method": method,
           "rate": rate, "reimbursement": round(round_trip * rate, 2), "mpg": mpg, "gas_price": gas,
           "gas_cost": round(round_trip / (mpg or geo.DEFAULT_MPG) * gas, 2)}
    a, b = geo.nearest_airport(start), geo.nearest_airport(dest)
    out["origin_airport"], out["dest_airport"], out["airfare"], out["flights"] = None, None, None, []
    if a and b and a[0] != b[0]:
        fares = inventory.arrange(inventory.flights(a[0], b[0], doc["start_date"], doc["end_date"]), "price", True)
        out.update(origin_airport=geo.airport_label(a), dest_airport=geo.airport_label(b),
                   airfare=fares[0]["total"], flights=fares[:3])
    return out, {}


@bp.route("/<int:doc_id>/drive", methods=["GET", "POST"])
def drive(doc_id):
    doc, stop = _doc(doc_id)
    db = get_db()
    saved = db.execute("SELECT * FROM trip_comparisons WHERE document_id=?", (doc_id,)).fetchone()
    values = {"start": saved["start_place"] if saved else (g.user["duty_station"] or ""),
              "destination": saved["destination"] if saved else place_of(stop),
              "mpg": str(int(saved["mpg"])) if saved else str(geo.DEFAULT_MPG),
              "miles": str(saved["one_way_miles"]) if saved and saved["distance_method"] == "entered" else ""}
    result, errors = None, {}
    if request.method == "POST":
        values.update({k: (request.form.get(k) or "").strip() for k in values})
        result, errors = _compare(doc, stop, values)
        if result and request.form.get("action") == "save":
            db.execute("DELETE FROM expenses WHERE document_id=? AND category='POV_MILEAGE' "
                       "AND description LIKE 'Trip planner:%'", (doc_id,))
            db.execute("INSERT INTO expenses(document_id, expense_date, category, description, amount, miles,"
                       " payment_method) VALUES (?,?,?,?,0,?,'PERSONAL')",
                       (doc_id, doc["start_date"], "POV_MILEAGE", "Trip planner: %s to %s and back"
                        % (result["start"], result["destination"]), result["round_trip"]))
            db.execute("INSERT OR REPLACE INTO trip_comparisons(document_id, start_place, destination, one_way_miles,"
                       " round_trip_miles, distance_method, mileage_rate, reimbursement, mpg, gas_price, gas_cost,"
                       " origin_airport, dest_airport, airfare_estimate) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (doc_id, result["start"], result["destination"], result["one_way"], result["round_trip"],
                        result["method"], result["rate"], result["reimbursement"], result["mpg"],
                        result["gas_price"], result["gas_cost"], result["origin_airport"], result["dest_airport"],
                        result["airfare"]))
            for sel in db.execute("SELECT * FROM trip_selections WHERE document_id=? AND kind='AIR'",
                                  (doc_id,)).fetchall():
                remove_selection(db, sel)
            recalc(doc_id)
            audit(me(), "POV_COMPARISON", "document:%d" % doc_id, "%s mi" % result["round_trip"])
            db.commit()
            flash("Driving estimate saved: %s round-trip miles." % result["round_trip"], "success")
            return redirect(url_for(".stay", doc_id=doc_id))
    elif saved:
        result, errors = _compare(doc, stop, values)
    return _render("drive", doc=doc, stop=stop, values=values, errors=errors, result=result)


# ---------------------------------------------------------------------- fly
def _legs(doc, src):
    """Parse the flight search into one-way legs, or errors."""
    trip = src.get("trip_type", "ROUND")
    if trip not in dict(TRIP_TYPES):
        trip = "ROUND"
    errors, legs, values = {}, [], {"trip_type": trip}
    count = 3 if trip == "MULTI" else 1
    for i in range(count):
        o, d, day = (src.get("from_%d" % i, "").strip(), src.get("to_%d" % i, "").strip(),
                     src.get("date_%d" % i, "").strip())
        values.update({"from_%d" % i: o, "to_%d" % i: d, "date_%d" % i: day})
        if trip == "MULTI" and i == 2 and not (o or d):
            continue
        if not o:
            errors["from_%d" % i] = "Enter the departure airport%s." % (" for flight %d" % (i + 1) if count > 1 else "")
        if not d:
            errors["to_%d" % i] = "Enter the arrival airport%s." % (" for flight %d" % (i + 1) if count > 1 else "")
        if not day or not doc["start_date"] <= day <= doc["end_date"]:
            errors["date_%d" % i] = "Enter a date within your trip (%s to %s)." % (doc["start_date"], doc["end_date"])
        elif legs and day < legs[-1][2]:
            errors["date_%d" % i] = "Each flight must be on or after the one before it."
        legs.append((geo.airport_code(o) or o, geo.airport_code(d) or d, day))
    ret = src.get("return_date", "").strip()
    values["return_date"] = ret
    if trip == "ROUND" and (not ret or not (legs and legs[0][2] <= ret <= doc["end_date"])):
        errors["return_date"] = "Enter a return date on or after the departure date, within your trip."
    return trip, legs, ret, values, errors


def _flight_options(trip, legs, ret):
    if trip == "MULTI":
        return inventory.multi_city(legs)
    o, d, day = legs[0]
    opts = inventory.flights(o, d, day, ret if trip == "ROUND" else day)
    return inventory.arrange(opts, "price", in_policy_only=True)


@bp.route("/<int:doc_id>/fly", methods=["GET", "POST"])
def fly(doc_id):
    doc, stop = _doc(doc_id)
    src = request.form if request.method == "POST" else request.args
    a = geo.nearest_airport(g.user["duty_station"] or "")
    b = geo.nearest_airport(place_of(stop))
    values = {"trip_type": "ROUND", "from_0": geo.airport_label(a) if a else "", "to_0": geo.airport_label(b) if b else "",
              "date_0": doc["start_date"], "return_date": doc["end_date"]}
    for i in (1, 2):
        values.update({"from_%d" % i: "", "to_%d" % i: "", "date_%d" % i: ""})
    if src.get("trip_type") and not src.get("searched"):  # just switching the trip type
        values["trip_type"] = src.get("trip_type") if src.get("trip_type") in dict(TRIP_TYPES) else "ROUND"
        if values["trip_type"] == "MULTI":
            values.update({"from_1": values["to_0"], "date_1": doc["end_date"], "to_1": values["from_0"]})
    results, errors = [], {}
    if src.get("searched"):
        trip, legs, ret, parsed, errors = _legs(doc, src)
        values.update(parsed)
        if not errors:
            results = _flight_options(trip, legs, ret)
            if request.method == "POST":
                opt = next((o for o in results if o["id"] == src.get("option_id")), None)
                if opt is None:
                    abort(400)
                end = ret if trip == "ROUND" else legs[-1][2]
                get_db().execute("DELETE FROM expenses WHERE document_id=? AND category='POV_MILEAGE' "
                                 "AND description LIKE 'Trip planner:%'", (doc_id,))
                save_selection(get_db(), doc, "AIR", stop, {"depart": legs[0][2], "ret": end}, opt, "")
                get_db().commit()
                flash("Flight added: %s." % opt["title"], "success")
                return redirect(url_for(".stay", doc_id=doc_id))
    elif request.method == "POST":
        abort(400)
    airports = [geo.airport_label(x) for x in geo.AIRPORTS]
    return _render("fly", doc=doc, stop=stop, values=values, errors=errors, results=results,
                   trip_types=TRIP_TYPES, airports=airports)


# --------------------------------------------------------------------- stay
@bp.route("/<int:doc_id>/stay", methods=["GET", "POST"])
def stay(doc_id):
    doc, stop = _doc(doc_id)
    db = get_db()
    cap = lodging_max_for(stop)
    params = dict(location=stop["location"], state=stop["state"], check_in=stop["arrive_date"],
                  check_out=stop["depart_date"], lodging_max=cap)
    if request.method == "POST":
        if request.form.get("action") == "skip":
            return redirect(url_for(".done", doc_id=doc_id))
        if request.form.get("action") == "gov_quarters":
            db.execute("UPDATE document_stops SET gov_quarters=1 WHERE id=?", (stop["id"],))
            recalc(doc_id)
            db.commit()
            return redirect(url_for(".done", doc_id=doc_id))
        opt = inventory.find("LODGING", request.form.get("option_id", ""), **params)
        if opt is None or not opt["in_policy"]:
            abort(400)
        save_selection(db, doc, "LODGING", stop, params, opt, "")
        db.commit()
        flash("Hotel added: %s." % opt["title"], "success")
        return redirect(url_for(".done", doc_id=doc_id))
    hotels = inventory.arrange(inventory.search("LODGING", **params), "price", in_policy_only=True)
    return _render("stay", doc=doc, stop=stop, hotels=hotels, lodging_max=cap)


# --------------------------------------------------------------------- done
@bp.route("/<int:doc_id>/done")
def done(doc_id):
    doc, stop = _doc(doc_id)
    db = get_db()
    doc = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    stops, exps = load_children(doc_id)
    pd, exp_total, total = jtr.document_totals(doc, stops, exps)
    selections = db.execute("SELECT * FROM trip_selections WHERE document_id=? ORDER BY start_date, id",
                            (doc_id,)).fetchall()
    comparison = db.execute("SELECT * FROM trip_comparisons WHERE document_id=?", (doc_id,)).fetchone()
    return _render("done", doc=doc, stop=stop, per_diem=pd, exp_total=exp_total, total=total,
                   selections=selections, comparison=comparison)
