"""Demo data. Per diem figures are ILLUSTRATIVE ONLY – load the official
DTMO/GSA tables before any real use."""
from werkzeug.security import generate_password_hash

DEMO_PASSWORD = "Password123!"

SETTINGS = [
    ("pov_mileage_rate", "0.70", "POV mileage reimbursement rate, dollars per mile (JTR 020203). Update each January."),
    ("receipt_threshold", "75", "Receipts required for any single expense at or above this amount, and for all lodging (JTR 010206)."),
    ("first_last_day_pct", "75", "Percent of M&IE paid on the first and last day of travel (JTR 020302)."),
    ("ltdy_pct_31_180", "75", "Long-term TDY flat-rate percent for 31–180 days at one location (JTR 020306)."),
    ("ltdy_pct_181", "55", "Long-term TDY flat-rate percent for more than 180 days at one location."),
    ("ltdy_enabled", "1", "Apply long-term TDY flat rate automatically (1 = yes, 0 = no)."),
    ("session_timeout_minutes", "15", "Idle minutes before automatic sign-out. Users are warned 2 minutes before."),
    ("org_name", "Demo Command", "Name displayed in the site header."),
]

ORGS = [  # code, name, parent code
    ("HQ", "Demo Command Headquarters", None),
    ("HQ-W1", "1st Support Wing", "HQ"),
    ("HQ-W1-S10", "10th Logistics Squadron", "HQ-W1"),
    ("HQ-W1-S20", "20th Communications Squadron", "HQ-W1"),
    ("HQ-W2", "2nd Training Wing", "HQ"),
]

USERS = [  # username, first, last, org, grade, roles [(role, org code or None, include children)]
    ("admin", "Alex", "Admin", "HQ", "GS-13", [("traveler", "HQ", 0), ("sys_admin", None, 1)]),
    ("traveler", "Taylor", "Smith", "HQ-W1-S10", "E-6", [("traveler", "HQ-W1-S10", 0)]),
    ("traveler2", "Jordan", "Lee", "HQ-W1-S20", "O-3", [("traveler", "HQ-W1-S20", 0)]),
    ("preparer", "Pat", "Rivera", "HQ-W1-S10", "GS-9", [("preparer", "HQ-W1-S10", 1)]),
    ("ao", "Morgan", "Jones", "HQ-W1", "O-5", [("traveler", "HQ-W1", 0), ("approving_official", "HQ-W1", 1)]),
    ("co", "Casey", "Nguyen", "HQ-W1", "GS-12", [("traveler", "HQ-W1", 0), ("certifying_officer", "HQ-W1", 1)]),
    ("budget", "Riley", "Patel", "HQ-W1", "GS-11", [("traveler", "HQ-W1", 0), ("budget_dta", "HQ-W1", 1)]),
    ("dta", "Drew", "Garcia", "HQ-W1", "GS-11", [("traveler", "HQ-W1", 0), ("org_dta", "HQ-W1", 1)]),
    ("auditor", "Sam", "Okafor", "HQ", "GS-14", [("auditor", "HQ", 1)]),
]

# Illustrative rates: location, state, season start, season end, lodging, M&IE
RATES = [
    ("Standard CONUS", "US", "01-01", "12-31", 110, 68),
    ("Washington, DC", "DC", "01-01", "06-30", 276, 92),
    ("Washington, DC", "DC", "07-01", "08-31", 197, 92),
    ("Washington, DC", "DC", "09-01", "12-31", 276, 92),
    ("San Diego", "CA", "01-01", "12-31", 215, 86),
    ("Colorado Springs", "CO", "01-01", "12-31", 150, 74),
    ("San Antonio", "TX", "01-01", "12-31", 150, 74),
    ("Norfolk / Virginia Beach", "VA", "01-01", "05-31", 117, 74),
    ("Norfolk / Virginia Beach", "VA", "06-01", "08-31", 200, 74),
    ("Norfolk / Virginia Beach", "VA", "09-01", "12-31", 117, 74),
    ("Honolulu", "HI", "01-01", "12-31", 277, 157),
]

LOAS = [  # org code, label, code, FY, budget
    ("HQ-W1", "1 SW O&M Travel FY27", "97 X4930 5CBX 001 2027 01 TRVL", 2027, 250000),
    ("HQ-W1-S10", "10 LRS Training Travel FY27", "57 3400 30 01 2027 TR 10LRS", 2027, 60000),
    ("HQ-W1-S20", "20 CS Mission Travel FY27", "57 3400 30 01 2027 MS 20CS", 2027, 45000),
]


def seed_demo(db):
    for k, v, d in SETTINGS:
        db.execute("INSERT OR REPLACE INTO settings(key, value, description) VALUES (?,?,?)", (k, v, d))

    org_ids = {}
    for code, name, parent in ORGS:
        org_ids[code] = db.execute("INSERT INTO orgs(code, name, parent_id) VALUES (?,?,?)",
                                   (code, name, org_ids.get(parent))).lastrowid

    role_ids = {r["key"]: r["id"] for r in db.execute("SELECT id, key FROM roles")}
    pw = generate_password_hash(DEMO_PASSWORD, method="pbkdf2:sha256:600000")
    user_ids = {}
    for username, first, last, org, grade, roles in USERS:
        uid = db.execute(
            "INSERT INTO users(username, password_hash, first_name, last_name, email, phone, org_id, rank_grade,"
            " duty_station, residence_city, gtcc_last4, eft_last4) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (username, pw, first, last, username + "@example.test", "555-0100", org_ids[org], grade,
             "Demo Base", "Demo City", "4321", "9876")).lastrowid
        user_ids[username] = uid
        for role, scope, children in roles:
            db.execute("INSERT INTO role_assignments(user_id, role_id, org_id, include_children) VALUES (?,?,?,?)",
                       (uid, role_ids[role], org_ids[scope] if scope else None, children))

    for r in RATES:
        db.execute("INSERT INTO per_diem_rates(location, state, season_start, season_end, lodging, mie, fiscal_year)"
                   " VALUES (?,?,?,?,?,?,2027)", r)

    for org, label, code, fy, amt in LOAS:
        db.execute("INSERT INTO loas(org_id, label, code, fiscal_year, budget_amount) VALUES (?,?,?,?,?)",
                   (org_ids[org], label, code, fy, amt))

    # Routing list for the wing (inherited by squadrons)
    rl = db.execute("INSERT INTO routing_lists(org_id, name, description) VALUES (?,?,?)",
                    (org_ids["HQ-W1"], "1 SW Standard TDY",
                     "Funds check and AO approval for authorizations; AO approval and CO certification for vouchers.")).lastrowid
    steps = [
        (1, "Funds verification", "FUNDS VERIFIED", "workflow.budget", "AUTH"),
        (2, "Approving Official", "APPROVED", "workflow.approve", "AUTH,VOUCHER,LOCAL"),
        (3, "Certifying Officer", "CERTIFIED", "workflow.certify", "VOUCHER,LOCAL"),
    ]
    for seq, name, stamp, perm, types in steps:
        db.execute("INSERT INTO routing_steps(routing_list_id, seq, name, stamp, required_permission, doc_types)"
                   " VALUES (?,?,?,?,?,?)", (rl, seq, name, stamp, perm, types))
    db.execute("UPDATE users SET default_routing_list_id=?", (rl,))
