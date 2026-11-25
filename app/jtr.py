"""Joint Travel Regulations (JTR) calculation engine.

Implements a deliberately transparent subset of JTR Chapter 2 per diem rules
so the logic can be reviewed and extended:

* Lodging is allowed for each night spent at a TDY location, up to the
  locality maximum (actual cost on vouchers, max on authorization estimates).
  Government quarters -> no lodging allowance.  (JTR 0203)
* M&IE is paid for every travel day at the rate of the location where
  lodging is obtained; on the last day, the rate of the last TDY location.
* First and last day of travel M&IE = ``first_last_day_pct`` (75%). (JTR 020302)
* Same-day travel (start == end) earns no per diem (12-hour rule is outside
  this prototype's scope – flagged for review).
* Long-term TDY flat rate: if one stop lasts 31–180 days, lodging cap and M&IE
  for the days at that stop are reduced to ``ltdy_pct_31_180`` (75%); more than
  180 days -> ``ltdy_pct_181`` (55%).  (JTR 020306)
* Rates are seasonal: the rate row is chosen per *date*.

Every rule here is parameterised through the ``settings`` table so policy
owners can tune it without code changes.
"""
from __future__ import annotations

import datetime as dt

from .db import get_db, setting

EXPENSE_CATEGORIES = [
    ("AIRFARE", "Commercial airfare"),
    ("RAIL", "Rail"),
    ("RENTAL_CAR", "Rental car"),
    ("POV_MILEAGE", "Privately owned vehicle (mileage)"),
    ("TAXI", "Taxi / rideshare / TNC"),
    ("PARKING", "Parking"),
    ("TOLLS", "Tolls"),
    ("BAGGAGE", "Baggage fees"),
    ("REGISTRATION", "Conference registration"),
    ("GOV_QUARTERS", "Government quarters fee"),
    ("LAUNDRY", "Laundry (TDY over 7 nights)"),
    ("OTHER", "Other reimbursable"),
]
CATEGORY_LABELS = dict(EXPENSE_CATEGORIES)

PAYMENT_METHODS = [
    ("GTCC", "Government Travel Charge Card (individually billed)"),
    ("CBA", "Centrally billed account"),
    ("PERSONAL", "Personal funds"),
]

TRIP_PURPOSES = [
    ("TDY", "TDY travel"),
    ("TRAINING", "Training attendance"),
    ("CONFERENCE", "Conference attendance"),
    ("SITE_VISIT", "Site visit"),
    ("INVITATIONAL", "Invitational travel"),
    ("OTHER", "Other"),
]


def parse(d):
    return d if isinstance(d, dt.date) else dt.date.fromisoformat(d)


def daterange(a, b):
    a, b = parse(a), parse(b)
    for n in range((b - a).days + 1):
        yield a + dt.timedelta(days=n)


def _in_season(mmdd, start, end):
    if start <= end:
        return start <= mmdd <= end
    return mmdd >= start or mmdd <= end  # season wraps year end


def rate_for(location, state, day):
    """Seasonal rate row for location on a date; falls back to Standard CONUS."""
    db = get_db()
    mmdd = parse(day).strftime("%m-%d")
    for r in db.execute("SELECT * FROM per_diem_rates WHERE location=? AND state=? ORDER BY fiscal_year DESC",
                        (location, state)):
        if _in_season(mmdd, r["season_start"], r["season_end"]):
            return r
    return db.execute("SELECT * FROM per_diem_rates WHERE location='Standard CONUS' LIMIT 1").fetchone()


def compute_per_diem(start, end, stops, actual=False, cfg=None):
    """Return dict(lines=[...], lodging=, mie=, total=, notes=[...]).

    ``stops``: iterable of mappings with location, state, arrive_date,
    depart_date, lodging_nightly, gov_quarters.
    ``cfg``: optional settings override (used by unit tests).
    """
    cfg = cfg or {
        "first_last_day_pct": setting("first_last_day_pct", 75, float),
        "ltdy_enabled": setting("ltdy_enabled", 1, int),
        "ltdy_pct_31_180": setting("ltdy_pct_31_180", 75, float),
        "ltdy_pct_181": setting("ltdy_pct_181", 55, float),
    }
    start, end = parse(start), parse(end)
    # Display order follows the traveler's custom itinerary order when known
    # (falls back to arrival date for callers/tests that don't set it); the
    # per-night lookup below scans all stops regardless of order, so this
    # only affects the order `lines` is reported in, not the totals.
    stops = sorted(stops, key=lambda s: (s.get("sort_order", 0), s["arrive_date"]))
    lines, notes = [], []
    if not stops:
        return {"lines": [], "lodging": 0.0, "mie": 0.0, "total": 0.0, "notes": ["No TDY locations entered."]}
    if start == end:
        notes.append("Same-day travel: no per diem computed. Travel of more than 12 hours may "
                     "qualify for 75% M&IE – route to AO for determination.")
        return {"lines": [], "lodging": 0.0, "mie": 0.0, "total": 0.0, "notes": notes}

    def stop_for_night(d):
        for s in stops:
            if parse(s["arrive_date"]) <= d < parse(s["depart_date"]):
                return s
        return None

    def ltdy_pct(s):
        if not cfg["ltdy_enabled"]:
            return 100.0
        nights = (parse(s["depart_date"]) - parse(s["arrive_date"])).days
        if nights > 180:
            return cfg["ltdy_pct_181"]
        if nights > 30:
            return cfg["ltdy_pct_31_180"]
        return 100.0

    flagged_ltdy = set()
    last_seen = stops[0]
    for d in daterange(start, end):
        s = stop_for_night(d) if d != end else None
        mie_stop = s or (stops[-1] if d == end else last_seen)
        if s:
            last_seen = s
        rate = rate_for(mie_stop["location"], mie_stop["state"], d)
        pct_flat = ltdy_pct(mie_stop)
        if pct_flat < 100 and id(mie_stop) not in flagged_ltdy:
            flagged_ltdy.add(id(mie_stop))
            notes.append("Long-term TDY flat rate of %d%% applied at %s." % (pct_flat, mie_stop["location"]))

        lodging_max = round(rate["lodging"] * pct_flat / 100, 2) if s else 0.0
        if s and s.get("gov_quarters"):
            lodging = 0.0
        elif s:
            if actual and s.get("lodging_nightly") is not None:
                lodging = min(float(s["lodging_nightly"]), lodging_max)
            else:
                lodging = lodging_max
        else:
            lodging = 0.0

        travel_day = d in (start, end)
        mie_pct = cfg["first_last_day_pct"] if travel_day else pct_flat
        mie = round(rate["mie"] * mie_pct / 100, 2)
        lines.append({
            "date": d.isoformat(), "location": "%s, %s" % (rate["location"], rate["state"]),
            "lodging_max": lodging_max,
            "lodging_claimed": (float(s["lodging_nightly"]) if s and s.get("lodging_nightly") is not None else None),
            "lodging": round(lodging, 2), "mie_rate": rate["mie"], "mie_pct": mie_pct, "mie": mie,
            "total": round(lodging + mie, 2),
        })
    lodging_t = round(sum(x["lodging"] for x in lines), 2)
    mie_t = round(sum(x["mie"] for x in lines), 2)
    return {"lines": lines, "lodging": lodging_t, "mie": mie_t, "total": round(lodging_t + mie_t, 2), "notes": notes}


def expense_amount(e, mileage_rate=None):
    if e["category"] == "POV_MILEAGE":
        rate = mileage_rate if mileage_rate is not None else setting("pov_mileage_rate", 0.70, float)
        return round(float(e["miles"] or 0) * rate, 2)
    return round(float(e["amount"] or 0), 2)


def document_totals(doc, stops, expenses):
    actual = doc["doc_type"] != "AUTH"
    pd = compute_per_diem(doc["start_date"], doc["end_date"], stops, actual=actual)
    rate = setting("pov_mileage_rate", 0.70, float)
    exp_total = round(sum(expense_amount(e, rate) for e in expenses), 2)
    if doc["doc_type"] == "LOCAL":
        pd = {"lines": [], "lodging": 0.0, "mie": 0.0, "total": 0.0,
              "notes": ["Local travel: no per diem (JTR 0305)."]}
    return pd, exp_total, round(pd["total"] + exp_total, 2)


def preaudit(doc, stops, expenses, per_diem):
    """Return list of findings: dict(code, message, ref). Findings require a
    traveler justification before the document can be signed."""
    out = []
    threshold = setting("receipt_threshold", 75, float)
    today = dt.date.today()
    start, end = parse(doc["start_date"]), parse(doc["end_date"])

    if end < start:
        out.append(dict(code="DATES", message="Trip end date is before start date.", ref="—"))
    if (end - start).days > 180:
        out.append(dict(code="LTDY180", message="TDY exceeds 180 days; requires Secretarial Process approval.",
                        ref="JTR 032301"))
    if doc["doc_type"] == "AUTH" and start < today and doc["status"] in ("CREATED", "RETURNED"):
        out.append(dict(code="POSTTRIP", message="Authorization is being prepared after travel began.",
                        ref="JTR 010206"))
    if not doc["loa_id"]:
        out.append(dict(code="NOLOA", message="No line of accounting selected.", ref="DoD FMR Vol 9"))

    for line in per_diem.get("lines", []):
        if line["lodging_claimed"] is not None and line["lodging_claimed"] > line["lodging_max"] > 0:
            out.append(dict(code="LODGEMAX",
                            message="Lodging on %s ($%.2f) exceeds the maximum ($%.2f). Excess is not "
                                    "reimbursed unless actual expense allowance (AEA) is authorized."
                                    % (line["date"], line["lodging_claimed"], line["lodging_max"]),
                            ref="JTR 020303"))
            break
    if doc["doc_type"] != "AUTH":
        for s in stops:
            if not s.get("gov_quarters") and s.get("lodging_nightly") is None:
                out.append(dict(code="LODGERCPT", message="Lodging cost not entered for %s; a lodging "
                                "receipt is required." % s["location"], ref="JTR 010206"))

    for e in expenses:
        amt = expense_amount(e)
        label = CATEGORY_LABELS.get(e["category"], e["category"])
        if doc["doc_type"] != "AUTH" and amt >= threshold and not e["receipt_attached"] \
                and e["category"] != "POV_MILEAGE":
            out.append(dict(code="RECEIPT", message="%s on %s ($%.2f) requires a receipt."
                            % (label, e["expense_date"], amt), ref="JTR 010206"))
        if e["category"] == "AIRFARE" and e["payment_method"] == "PERSONAL":
            out.append(dict(code="GTCC", message="Airfare paid with personal funds; the GTCC or CBA must be "
                            "used for official travel unless exempt.", ref="DoD FMR Vol 9 Ch 3"))
        if e["category"] == "RENTAL_CAR" and not (e["description"] or "").strip():
            out.append(dict(code="RENTAL", message="Rental car requires a justification in its description.",
                            ref="JTR 020209"))
        if e["category"] == "LAUNDRY" and (end - start).days < 7:
            out.append(dict(code="LAUNDRY", message="Laundry is reimbursable only for TDY of 7 or more "
                            "consecutive nights.", ref="JTR 020304"))
    for n in per_diem.get("notes", []):
        if n.startswith("Same-day"):
            out.append(dict(code="SAMEDAY", message=n, ref="JTR 020302"))
    return out
