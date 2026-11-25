"""Demo travel inventory: flights, hotels and rental cars for a TDY location.

There is no live GDS connection, so results are generated deterministically
from the search (same search, same results). That lets a selection be
re-derived on the server from its option id, so a posted price can never be
tampered with. Policy rules follow common DoD practice:

* Lodging: nightly rate at or below the locality lodging maximum.
* Airfare: coach / GSA City Pair fares; business and first class are out of policy.
* Rental car: intermediate or smaller.
"""
import datetime as dt
import hashlib
import random
import re

KINDS = {"AIR": "Flight", "LODGING": "Hotel", "RENTAL_CAR": "Rental car"}

CARRIERS = [("American Airlines", "AA"), ("Delta Air Lines", "DL"), ("United Airlines", "UA"),
            ("Southwest Airlines", "WN"), ("Alaska Airlines", "AS"), ("JetBlue", "B6")]
HOTELS = [("Hilton Garden Inn", 1.02), ("Courtyard by Marriott", 1.08), ("Hampton Inn & Suites", 0.9),
          ("Holiday Inn Express", 0.84), ("Residence Inn", 1.18), ("Hyatt Place", 0.97),
          ("Best Western Plus", 0.74), ("Embassy Suites", 1.32), ("Marriott", 1.48)]
AREAS = ["Downtown", "Airport", "Convention Center", "North", "Gateway", "Riverside"]
CAR_VENDORS = ["Enterprise", "National", "Hertz", "Avis", "Budget"]
CAR_CLASSES = [("Economy", 36, True), ("Compact", 41, True), ("Intermediate", 47, True),
               ("Standard", 55, False), ("Full-size", 61, False), ("Standard SUV", 79, False),
               ("Minivan", 88, False)]
# Loyalty programs a traveler can record on their profile: key -> (kind, name, vendors)
PROGRAMS = {
    "AA": ("AIR", "American AAdvantage", ["American Airlines"]),
    "DL": ("AIR", "Delta SkyMiles", ["Delta Air Lines"]),
    "UA": ("AIR", "United MileagePlus", ["United Airlines"]),
    "WN": ("AIR", "Southwest Rapid Rewards", ["Southwest Airlines"]),
    "AS": ("AIR", "Alaska Mileage Plan", ["Alaska Airlines"]),
    "B6": ("AIR", "JetBlue TrueBlue", ["JetBlue"]),
    "HILTON": ("LODGING", "Hilton Honors", ["Hilton Garden Inn", "Hampton Inn & Suites", "Embassy Suites"]),
    "MARRIOTT": ("LODGING", "Marriott Bonvoy", ["Courtyard by Marriott", "Residence Inn", "Marriott"]),
    "IHG": ("LODGING", "IHG One Rewards", ["Holiday Inn Express"]),
    "HYATT": ("LODGING", "World of Hyatt", ["Hyatt Place"]),
    "BW": ("LODGING", "Best Western Rewards", ["Best Western Plus"]),
    "ENTERPRISE": ("RENTAL_CAR", "Enterprise Plus", ["Enterprise"]),
    "NATIONAL": ("RENTAL_CAR", "National Emerald Club", ["National"]),
    "HERTZ": ("RENTAL_CAR", "Hertz Gold Plus Rewards", ["Hertz"]),
    "AVIS": ("RENTAL_CAR", "Avis Preferred", ["Avis"]),
    "BUDGET": ("RENTAL_CAR", "Budget Fastbreak", ["Budget"]),
}
_PROGRAM_OF = {v: k for k, (_kind, _name, vendors) in PROGRAMS.items() for v in vendors}
_AIR_CODES = {name: code for name, code in [
    ("American Airlines", "AA"), ("Delta Air Lines", "DL"), ("United Airlines", "UA"),
    ("Southwest Airlines", "WN"), ("Alaska Airlines", "AS"), ("JetBlue", "B6")]}


def brand(vendor):
    """Display info for a vendor: name, short mark for its logo tile, logo
    file slug, and loyalty program key (or None)."""
    words = [w for w in vendor.replace("&", "").split() if w[0].isalpha()]
    mark = _AIR_CODES.get(vendor) or "".join(w[0] for w in words[:2]).upper()
    slug = re.sub(r"[^a-z0-9]+", "-", vendor.lower()).strip("-")
    program = _PROGRAM_OF.get(vendor)
    return {"name": vendor, "mark": mark, "slug": slug, "program": program,
            "logo": (_AIR_CODES.get(vendor) or program or slug).lower(),
            "tone": sum(map(ord, vendor)) % 6}


BASE_WORDS = ("afb", "ab", "air base", "base", "fort ", "ft ", "naval", "nas ", "camp ", "jb ", "joint base",
              "army", "station", "barracks", "garrison")


def _rng(*parts):
    seed = hashlib.sha256("|".join(str(p).lower() for p in parts).encode()).hexdigest()
    return random.Random(seed)


def _days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def _md(day):
    d = dt.date.fromisoformat(day)
    return "%s %d" % (d.strftime("%b"), d.day)


def _clock(minutes):
    h, m = divmod(minutes % (24 * 60), 60)
    return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")


def _option(oid, kind, vendor, title, details, unit_price, units, in_policy, reason, badges=(), sort=0):
    return {"id": oid, "kind": kind, "vendor": vendor, "title": title, "details": details,
            "unit_price": round(unit_price, 2), "units": units, "total": round(unit_price * units, 2),
            "in_policy": in_policy, "policy_reason": "" if in_policy else reason, "badges": list(badges),
            "sort": sort, "brands": [brand(vendor)]}


def flights(origin, destination, depart, ret):
    rng = _rng("air", origin, destination, depart, ret)
    base = rng.randint(180, 420)
    round_trip = ret != depart
    out = []
    for n in range(9):
        name, code = CARRIERS[rng.randrange(len(CARRIERS))]
        stops = 0 if rng.random() < 0.55 else 1
        dep = rng.randrange(6 * 60, 21 * 60, 5)
        dur = rng.randint(95, 330) + stops * rng.randint(55, 120)
        ret_dep = rng.randrange(7 * 60, 21 * 60, 5)
        cabin = ["city_pair", "city_pair", "main", "main", "main", "business"][n % 6]
        if cabin == "city_pair":
            fare, label, badges = base * rng.uniform(0.95, 1.1), "GSA City Pair (YCA), refundable", ["GSA City Pair"]
        elif cabin == "main":
            fare, label, badges = base * rng.uniform(0.7, 1.15), "Main cabin, restricted", []
        else:
            fare, label, badges = base * rng.uniform(2.4, 3.2), "Business class", []
        fare = round(fare * (2 if round_trip else 1) * (0.92 if stops else 1))
        number = "%s %d" % (code, rng.randint(100, 2999))
        legs = ["Depart %s %s, arrive %s (%s, %dh %02dm)" % (
            _md(depart), _clock(dep), _clock(dep + dur),
            "nonstop" if not stops else "1 stop", dur // 60, dur % 60)]
        if round_trip:
            legs.append("Return %s %s, arrive %s" % (_md(ret),
                                                      _clock(ret_dep), _clock(ret_dep + dur)))
        out.append(_option("F%d" % n, "AIR", name, "%s %s to %s" % (number, origin, destination),
                           legs + [label], fare, 1, cabin != "business",
                           "Business class is above the coach class allowed by policy.", badges, sort=dep))
    return out


def hotels(location, state, check_in, check_out, lodging_max):
    nights = _days(check_in, check_out)
    if nights <= 0:
        return []
    rng = _rng("hotel", location, state, check_in, check_out)
    out = []
    place = location.lower() + " "
    if any(w in place for w in BASE_WORDS):
        rate = round(lodging_max * 0.55)
        out.append(_option("H0", "LODGING", "Government lodging", "On-installation lodging, %s" % location,
                           ["On the installation", "Check with lodging for availability"],
                           rate, nights, True, "", ["Government lodging"], sort=0.0))
    for n, (brand, factor) in enumerate(HOTELS, start=1):
        rate = round(lodging_max * factor * rng.uniform(0.9, 1.06))
        miles = round(rng.uniform(0.3, 9.5), 1)
        area = AREAS[rng.randrange(len(AREAS))]
        badges = ["FedRooms"] if rate <= lodging_max and rng.random() < 0.5 else []
        out.append(_option("H%d" % n, "LODGING", brand, "%s %s %s" % (brand, location, area),
                           ["%.1f miles from %s" % (miles, location), "%d night%s, %s to %s" % (
                               nights, "" if nights == 1 else "s", _md(check_in), _md(check_out))],
                           rate, nights, rate <= lodging_max,
                           "Nightly rate is above the $%.2f lodging maximum for %s." % (lodging_max, location),
                           badges, sort=miles))
    return out


def cars(location, pick_up, drop_off, lodging_max):
    days = max(_days(pick_up, drop_off), 1)
    rng = _rng("car", location, pick_up, drop_off)
    local = min(max(lodging_max / 110.0, 0.9), 1.35)
    out = []
    for n, (cls, daily, ok) in enumerate(CAR_CLASSES):
        for v in rng.sample(CAR_VENDORS, 2):
            rate = round(daily * local * rng.uniform(0.9, 1.12), 2)
            out.append(_option("C%d%s" % (n, v[0]), "RENTAL_CAR", v, "%s %s" % (v, cls),
                               ["Pick up %s at %s area airport" % (_md(pick_up), location), "Return %s" % _md(drop_off),
                                "Unlimited miles, government rate includes insurance (CDW/LDW)"],
                               rate, days, ok, "%s is above the intermediate size allowed by policy." % cls,
                               ["Government rate"], sort=n))
    return out


def search(kind, **params):
    return {"AIR": flights, "LODGING": hotels, "RENTAL_CAR": cars}[kind](**params)


def find(kind, option_id, **params):
    return next((o for o in search(kind, **params) if o["id"] == option_id), None)


def arrange(options, sort="price", in_policy_only=False):
    if in_policy_only:
        options = [o for o in options if o["in_policy"]]
    if sort == "policy":
        return sorted(options, key=lambda o: (not o["in_policy"], o["total"]))
    if sort == "other":
        return sorted(options, key=lambda o: (o["sort"], o["total"]))
    return sorted(options, key=lambda o: o["total"])


def multi_city(legs):
    """Itineraries for several one-way legs [(origin, destination, date)]:
    each option pairs the n-th cheapest in-policy flight of every leg."""
    per_leg = [sorted((f for f in flights(o, d, day, day) if f["in_policy"]), key=lambda f: f["total"])
               for o, d, day in legs]
    out = []
    for n in range(min(len(p) for p in per_leg)):
        picks = [p[n] for p in per_leg]
        route = " to ".join([legs[0][0]] + [d for _o, d, _day in legs])
        details = ["Leg %d: %s, %s" % (i + 1, p["title"], p["details"][0]) for i, p in enumerate(picks)]
        out.append(_option("M%d" % n, "AIR", " / ".join(sorted({p["vendor"] for p in picks})),
                           "Multi-city %s" % route, details, sum(p["total"] for p in picks), 1, True, "",
                           ["GSA City Pair"] if all("GSA City Pair" in p["badges"] for p in picks) else [],
                           sort=picks[0]["sort"]))
        seen = {}
        for p in picks:
            seen.setdefault(p["vendor"], p["brands"][0])
        out[-1]["brands"] = list(seen.values())
    return out
