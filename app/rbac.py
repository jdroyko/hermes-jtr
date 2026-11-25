"""Role-based access control.

Model
-----
* **Permission** – an atomic capability identified by a key such as
  ``document.approve``. Keys are what the code checks. The catalog below is
  synced into the database on start-up; admins may add *custom* permission
  keys (e.g. for a bespoke routing step) through the admin UI.
* **Role** – an editable, named bundle of permissions. Built-in roles mirror
  the DTS roles / permission levels but every one of them can be changed,
  cloned or deleted, and new roles can be created.
* **Role assignment** – (user, role, scope, expiry). Scope is either global
  (``org_id`` NULL) or a single org, optionally including all descendant
  orgs. Expired assignments are ignored.
* **Separation-of-duties (SoD) rules** – toggleable constraints evaluated on
  top of permissions (e.g. "a user cannot approve their own document").

Evaluation: ``has_perm(user_id, key, org_id)`` is True if *any* active
assignment grants a role containing ``key`` whose scope covers ``org_id``.
Passing ``org_id=None`` asks "does the user hold this anywhere?" (used for
showing navigation).
"""
from __future__ import annotations

import datetime as _dt
import json
from functools import wraps

from flask import abort, g

from .db import get_db

# ---------------------------------------------------------------------------
# Permission catalog: key -> (category, name, description)
# ---------------------------------------------------------------------------
PERMISSIONS = {
    # Travel documents -------------------------------------------------------
    "document.create.self":   ("Documents", "Create own documents", "Create authorizations, vouchers and local vouchers for yourself."),
    "document.create.others": ("Documents", "Create documents for others", "Prepare documents on behalf of other travelers in scope (NDEA / document preparer)."),
    "document.view.self":     ("Documents", "View own documents", "View documents where you are the traveler."),
    "document.view.org":      ("Documents", "View org documents", "View any document belonging to an org in scope."),
    "document.edit.self":     ("Documents", "Edit own documents", "Edit your own documents while in CREATED or RETURNED status."),
    "document.edit.others":   ("Documents", "Edit others' documents", "Edit documents of travelers in scope while in CREATED or RETURNED status."),
    "document.sign.self":     ("Documents", "Sign own documents", "Apply the SIGNED stamp to your own document and start routing."),
    "document.sign.others":   ("Documents", "Sign on behalf (NDEA)", "Sign documents for travelers without DTS access (Non-DTS Entry Agent)."),
    "document.cancel":        ("Documents", "Cancel documents", "Cancel a document that has not been paid."),
    "document.amend":         ("Documents", "Amend authorizations", "Amend an approved authorization (creates an amendment)."),
    "plan.assign":            ("Documents", "Assign travel plans", "Build a group travel plan and issue it as authorizations to travelers in scope."),
    # Workflow ---------------------------------------------------------------
    "workflow.review":        ("Workflow", "Review", "Apply the REVIEWED stamp on routing steps that require it."),
    "workflow.approve":       ("Workflow", "Approve (AO)", "Apply the APPROVED stamp as Authorizing / Approving Official."),
    "workflow.certify":       ("Workflow", "Certify (CO)", "Certify funds / payment as Certifying Officer."),
    "workflow.budget":        ("Workflow", "Funds review", "Apply the FUNDS VERIFIED stamp as budget/resource advisor."),
    "workflow.return":        ("Workflow", "Return documents", "Return a document to the traveler for correction."),
    "workflow.override":      ("Workflow", "Override routing", "Skip or reassign a routing step (requires remarks)."),
    # Travel agency ----------------------------------------------------------
    "booking.view":           ("Travel Agency", "View booking queue", "View approved travel plans waiting for, or completed by, the travel agency."),
    "booking.manage":         ("Travel Agency", "Record bookings", "Add reservations to an approved travel plan and mark it booked."),
    # Profiles ---------------------------------------------------------------
    "profile.edit.self":      ("Profiles", "Edit own profile", "Edit your own personal, banking and card data."),
    "profile.view.org":       ("Profiles", "View org profiles", "View traveler profiles in scope."),
    "profile.edit.org":       ("Profiles", "Edit org profiles", "Edit traveler profiles in scope (DTS permission level 6)."),
    # Configuration ----------------------------------------------------------
    "routing.manage":         ("Configuration", "Manage routing lists", "Create and edit routing lists and steps (DTS permission level 5)."),
    "org.manage":             ("Configuration", "Manage organizations", "Create and edit organizations and hierarchy."),
    "loa.view":               ("Finance", "View lines of accounting", "View LOAs and funds status in scope."),
    "loa.manage":             ("Finance", "Manage budgets / LOAs", "Create and edit LOAs and budgets (DTS permission level 3)."),
    "perdiem.manage":         ("Configuration", "Manage per diem rates", "Maintain per diem rate tables and JTR settings."),
    "debt.manage":            ("Finance", "Manage debts", "Track and manage traveler debts (debt management monitor)."),
    # Reports & audit --------------------------------------------------------
    "reports.view":           ("Reports", "View reports", "Run reports for orgs in scope (DTS permission level 1)."),
    "reports.export":         ("Reports", "Export reports", "Download report data as CSV."),
    "audit.view":             ("Reports", "View audit log", "View the system audit log."),
    # Administration ---------------------------------------------------------
    "admin.users":            ("Administration", "Manage users", "Create, deactivate and edit user accounts."),
    "admin.roles":            ("Administration", "Manage roles & permissions", "Create, edit and delete roles and their permission sets."),
    "admin.assign":           ("Administration", "Assign roles", "Grant and revoke role assignments within scope."),
    "admin.sod":              ("Administration", "Manage SoD rules", "Enable or disable separation-of-duties rules."),
    "admin.policy_io":        ("Administration", "Import/export policy", "Import and export the full RBAC policy as JSON."),
    "admin.settings":         ("Administration", "Manage settings", "Change system-wide settings."),
}

# ---------------------------------------------------------------------------
# Default roles. All are editable at runtime. ``legacy`` documents the
# closest DTS permission level so administrators can map existing users.
# ---------------------------------------------------------------------------
_TRAVELER = [
    "document.create.self", "document.view.self", "document.edit.self",
    "document.sign.self", "document.cancel", "document.amend", "profile.edit.self",
]

DEFAULT_ROLES = {
    "traveler": {
        "name": "Traveler", "legacy": "Level 0",
        "description": "Default role for every user. Creates and signs their own travel documents.",
        "permissions": _TRAVELER,
    },
    "preparer": {
        "name": "Document Preparer / NDEA", "legacy": "Level 0 + NDEA",
        "description": "Prepares and signs documents on behalf of travelers in scope.",
        "permissions": _TRAVELER + ["document.create.others", "document.edit.others",
                                    "document.sign.others", "document.view.org", "profile.view.org"],
    },
    "reviewer": {
        "name": "Reviewing Official", "legacy": "Level 2",
        "description": "Reviews documents in a routing list before approval.",
        "permissions": ["document.view.org", "workflow.review", "workflow.return"],
    },
    "approving_official": {
        "name": "Approving Official (AO)", "legacy": "Level 2",
        "description": "Approves authorizations and vouchers; accountable for the necessity of travel.",
        "permissions": ["document.view.org", "workflow.approve", "workflow.return",
                        "reports.view", "loa.view", "plan.assign"],
    },
    "certifying_officer": {
        "name": "Certifying Officer (CO)", "legacy": "Level 2",
        "description": "Certifies vouchers for payment.",
        "permissions": ["document.view.org", "workflow.certify", "workflow.return", "loa.view"],
    },
    "budget_dta": {
        "name": "Budget / Finance DTA", "legacy": "Level 3",
        "description": "Maintains budgets and lines of accounting; verifies funds.",
        "permissions": ["loa.view", "loa.manage", "workflow.budget", "workflow.return",
                        "document.view.org", "reports.view", "reports.export"],
    },
    "routing_official": {
        "name": "Routing List Manager", "legacy": "Level 5",
        "description": "Maintains routing lists for orgs in scope.",
        "permissions": ["routing.manage", "document.view.org"],
    },
    "org_dta": {
        "name": "Organizational DTA", "legacy": "Levels 1, 5, 6",
        "description": "Administers users, profiles, routing and role assignments for orgs in scope.",
        "permissions": ["profile.view.org", "profile.edit.org", "routing.manage", "org.manage",
                        "admin.users", "admin.assign", "document.view.org", "workflow.override",
                        "reports.view", "reports.export", "loa.view"],
    },
    "travel_agency": {
        "name": "Travel Agency (TMC)", "legacy": "CTO / TMC",
        "description": "Books reservations for approved travel plans and records confirmation numbers.",
        "permissions": ["booking.view", "booking.manage", "document.view.org"],
    },
    "debt_monitor": {
        "name": "Debt Management Monitor", "legacy": "Level 1",
        "description": "Tracks traveler debts and overpayments.",
        "permissions": ["debt.manage", "document.view.org", "reports.view"],
    },
    "auditor": {
        "name": "Auditor (read-only)", "legacy": "Level 1",
        "description": "Read-only access to documents, reports and audit log in scope.",
        "permissions": ["document.view.org", "profile.view.org", "loa.view", "reports.view",
                        "reports.export", "audit.view"],
    },
    "sys_admin": {
        "name": "System Administrator", "legacy": "Lead DTA (all levels)",
        "description": "Full access. Assign sparingly.",
        "permissions": list(PERMISSIONS.keys()),
    },
}

# Delegated administration: role -> roles its holders may grant (in their admin.assign scope)
DEFAULT_GRANTABLE = {
    "org_dta": ["traveler", "preparer", "reviewer", "approving_official", "certifying_officer",
                "budget_dta", "routing_official", "debt_monitor", "auditor", "travel_agency"],
}

SOD_RULES = {
    "no_self_approve": ("No self-approval",
                        "A user cannot apply any routing stamp (review, approve, certify, funds) to a document where they are the traveler."),
    "no_preparer_approve": ("Preparer cannot approve",
                            "The person who created a document cannot apply routing stamps to it."),
    "distinct_approver_certifier": ("Approver ≠ Certifier",
                                    "The same person cannot both approve and certify one document."),
    "no_escalation": ("No privilege escalation",
                      "A user may only assign roles whose permissions they themselves hold in that scope, "
                      "unless one of their roles explicitly lists the target role as grantable (delegated administration)."),
    "no_self_assign": ("No self-assignment",
                       "A user cannot grant roles to themselves."),
}


# ---------------------------------------------------------------------------
# Sync catalog into the database
# ---------------------------------------------------------------------------
def sync_catalog(db, seed_roles: bool = False) -> None:
    for key, (cat, name, desc) in PERMISSIONS.items():
        db.execute(
            "INSERT INTO permissions(key, category, name, description, is_system) VALUES (?,?,?,?,1) "
            "ON CONFLICT(key) DO UPDATE SET category=excluded.category, is_system=1",
            (key, cat, name, desc),
        )
    for key, (name, desc) in SOD_RULES.items():
        db.execute("INSERT OR IGNORE INTO sod_rules(key, name, description) VALUES (?,?,?)",
                   (key, name, desc))
    if seed_roles:
        for key, spec in DEFAULT_ROLES.items():
            cur = db.execute(
                "INSERT OR IGNORE INTO roles(key, name, description, legacy_level, is_system) VALUES (?,?,?,?,1)",
                (key, spec["name"], spec["description"], spec["legacy"]),
            )
            if cur.rowcount:
                set_role_permissions(db, cur.lastrowid, spec["permissions"])
        for key, targets in DEFAULT_GRANTABLE.items():
            rid = db.execute("SELECT id FROM roles WHERE key=?", (key,)).fetchone()["id"]
            set_role_grantable(db, rid, targets)
        db.execute("INSERT OR IGNORE INTO settings(key, value, description) VALUES (?, '1', ?)",
                   (_PLAN_ROLES_FLAG, _PLAN_ROLES_FLAG_DESC))
    else:
        _migrate_plan_roles(db)
    db.commit()


_PLAN_ROLES_FLAG = "rbac_migration_plans_v1"
_PLAN_ROLES_FLAG_DESC = "Internal: travel-plan roles and permissions were added to this database."


def _migrate_plan_roles(db) -> None:
    """One-time upgrade for databases seeded before travel plans existed.
    Guarded by a settings flag so later admin edits to these roles stick."""
    if db.execute("SELECT 1 FROM settings WHERE key=?", (_PLAN_ROLES_FLAG,)).fetchone():
        return
    spec = DEFAULT_ROLES["travel_agency"]
    cur = db.execute("INSERT OR IGNORE INTO roles(key, name, description, legacy_level, is_system) VALUES (?,?,?,?,1)",
                     ("travel_agency", spec["name"], spec["description"], spec["legacy"]))
    if cur.rowcount:
        set_role_permissions(db, cur.lastrowid, spec["permissions"])
    for role_key, keys in (("approving_official", ["plan.assign"]),
                           ("sys_admin", ["plan.assign", "booking.view", "booking.manage"])):
        role = db.execute("SELECT id FROM roles WHERE key=?", (role_key,)).fetchone()
        if not role:
            continue
        for k in keys:
            db.execute("INSERT OR IGNORE INTO role_permissions SELECT ?, id FROM permissions WHERE key=?",
                       (role["id"], k))
    dta = db.execute("SELECT id FROM roles WHERE key='org_dta'").fetchone()
    agency = db.execute("SELECT id FROM roles WHERE key='travel_agency'").fetchone()
    if dta and agency:
        db.execute("INSERT OR IGNORE INTO role_grantable VALUES (?,?)", (dta["id"], agency["id"]))
    db.execute("INSERT INTO settings(key, value, description) VALUES (?, '1', ?)",
               (_PLAN_ROLES_FLAG, _PLAN_ROLES_FLAG_DESC))


def set_role_grantable(db, role_id: int, role_keys) -> None:
    db.execute("DELETE FROM role_grantable WHERE role_id=?", (role_id,))
    for k in set(role_keys):
        row = db.execute("SELECT id FROM roles WHERE key=?", (k,)).fetchone()
        if row:
            db.execute("INSERT INTO role_grantable VALUES (?,?)", (role_id, row["id"]))


def set_role_permissions(db, role_id: int, keys) -> None:
    db.execute("DELETE FROM role_permissions WHERE role_id=?", (role_id,))
    for k in set(keys):
        row = db.execute("SELECT id FROM permissions WHERE key=?", (k,)).fetchone()
        if row:
            db.execute("INSERT INTO role_permissions VALUES (?,?)", (role_id, row["id"]))


# ---------------------------------------------------------------------------
# Org hierarchy helpers
# ---------------------------------------------------------------------------
def org_ancestors(org_id):
    """Return [org_id, parent, grandparent, ...]."""
    db = get_db()
    out, seen = [], set()
    while org_id is not None and org_id not in seen:
        seen.add(org_id)
        out.append(org_id)
        row = db.execute("SELECT parent_id FROM orgs WHERE id=?", (org_id,)).fetchone()
        org_id = row["parent_id"] if row else None
    return out


def org_descendants(org_id):
    db = get_db()
    rows = db.execute(
        "WITH RECURSIVE t(id) AS (SELECT ? UNION ALL SELECT o.id FROM orgs o JOIN t ON o.parent_id=t.id) "
        "SELECT id FROM t", (org_id,)).fetchall()
    return [r["id"] for r in rows]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
def _active_grants(user_id):
    """[(perm_key, org_id|None, include_children)] for all active assignments."""
    cache = g.setdefault("_grant_cache", {})
    if user_id in cache:
        return cache[user_id]
    today = _dt.date.today().isoformat()
    rows = get_db().execute(
        """SELECT p.key, ra.org_id, ra.include_children
             FROM role_assignments ra
             JOIN role_permissions rp ON rp.role_id = ra.role_id
             JOIN permissions p ON p.id = rp.permission_id
             JOIN users u ON u.id = ra.user_id
            WHERE ra.user_id = ? AND u.active = 1
              AND (ra.expires_at IS NULL OR ra.expires_at >= ?)""",
        (user_id, today)).fetchall()
    cache[user_id] = [(r["key"], r["org_id"], r["include_children"]) for r in rows]
    return cache[user_id]


def has_perm(user_id, key, org_id=None) -> bool:
    if user_id is None:
        return False
    grants = [gr for gr in _active_grants(user_id) if gr[0] == key]
    if not grants:
        return False
    if org_id is None:
        return True
    ancestors = org_ancestors(org_id)
    for _, scope_org, children in grants:
        if scope_org is None:
            return True
        if scope_org == org_id:
            return True
        if children and scope_org in ancestors:
            return True
    return False


def user_permissions(user_id):
    return sorted({gr[0] for gr in _active_grants(user_id)})


def scoped_org_ids(user_id, key):
    """All org ids where user holds ``key``. Returns None for global scope."""
    ids = set()
    for k, scope_org, children in _active_grants(user_id):
        if k != key:
            continue
        if scope_org is None:
            return None
        ids.update(org_descendants(scope_org) if children else [scope_org])
    return ids


def sod_enabled(key) -> bool:
    row = get_db().execute("SELECT enabled FROM sod_rules WHERE key=?", (key,)).fetchone()
    return bool(row and row["enabled"])


def clear_cache():
    g.pop("_grant_cache", None)


def can_assign(granter_id, role_id, target_user_id, org_id):
    """Return (ok, reason)."""
    if not has_perm(granter_id, "admin.assign", org_id):
        return False, "You do not have permission to assign roles in that organization."
    if sod_enabled("no_self_assign") and granter_id == target_user_id:
        return False, "Separation of duties: you cannot assign roles to yourself."
    if sod_enabled("no_escalation") and not _delegated(granter_id, role_id, org_id):
        keys = [r["key"] for r in get_db().execute(
            "SELECT p.key FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id WHERE rp.role_id=?",
            (role_id,))]
        missing = [k for k in keys if not has_perm(granter_id, k, org_id)]
        if missing:
            return False, ("Separation of duties: you cannot grant permissions you do not hold "
                           "in that scope (" + ", ".join(missing[:5]) + ("…" if len(missing) > 5 else "") + ").")
    return True, ""


def _delegated(granter_id, role_id, org_id):
    """True if granter holds, in a scope covering org_id, a role that lists role_id as grantable."""
    today = _dt.date.today().isoformat()
    rows = get_db().execute(
        """SELECT ra.org_id, ra.include_children FROM role_assignments ra
             JOIN role_grantable rg ON rg.role_id = ra.role_id
            WHERE ra.user_id=? AND rg.grantable_role_id=? AND (ra.expires_at IS NULL OR ra.expires_at >= ?)""",
        (granter_id, role_id, today)).fetchall()
    if not rows:
        return False
    if org_id is None:
        return any(r["org_id"] is None for r in rows)
    anc = org_ancestors(org_id)
    return any(r["org_id"] is None or r["org_id"] == org_id or (r["include_children"] and r["org_id"] in anc)
               for r in rows)


# ---------------------------------------------------------------------------
# View decorator
# ---------------------------------------------------------------------------
def require(key):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if g.get("user") is None:
                abort(401)
            if not has_perm(g.user["id"], key):
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


# ---------------------------------------------------------------------------
# Policy import / export
# ---------------------------------------------------------------------------
def export_policy() -> dict:
    db = get_db()
    roles = []
    for r in db.execute("SELECT * FROM roles ORDER BY key"):
        perms = [p["key"] for p in db.execute(
            "SELECT p.key FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id "
            "WHERE rp.role_id=? ORDER BY p.key", (r["id"],))]
        grants = [x["key"] for x in db.execute(
            "SELECT r2.key FROM role_grantable rg JOIN roles r2 ON r2.id=rg.grantable_role_id "
            "WHERE rg.role_id=? ORDER BY r2.key", (r["id"],))]
        roles.append({"key": r["key"], "name": r["name"], "description": r["description"],
                      "legacy_level": r["legacy_level"], "permissions": perms, "can_grant": grants})
    custom_perms = [dict(category=p["category"], key=p["key"], name=p["name"], description=p["description"])
                    for p in db.execute("SELECT * FROM permissions WHERE is_system=0 ORDER BY key")]
    sod = {s["key"]: bool(s["enabled"]) for s in db.execute("SELECT * FROM sod_rules")}
    return {"version": 1, "exported_at": _dt.datetime.utcnow().isoformat() + "Z",
            "custom_permissions": custom_perms, "roles": roles, "sod_rules": sod}


def import_policy(data: dict, replace_roles: bool = False):
    """Upsert custom permissions, roles and SoD flags. Returns a summary list."""
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Unsupported policy file: expected an object with \"version\": 1.")
    db = get_db()
    summary = []
    for p in data.get("custom_permissions", []):
        db.execute("INSERT INTO permissions(key, category, name, description, is_system) VALUES (?,?,?,?,0) "
                   "ON CONFLICT(key) DO UPDATE SET name=excluded.name, description=excluded.description",
                   (p["key"], p.get("category", "Custom"), p.get("name", p["key"]), p.get("description", "")))
        summary.append("permission " + p["key"])
    incoming = set()
    for r in data.get("roles", []):
        incoming.add(r["key"])
        row = db.execute("SELECT id FROM roles WHERE key=?", (r["key"],)).fetchone()
        if row:
            db.execute("UPDATE roles SET name=?, description=?, legacy_level=? WHERE id=?",
                       (r["name"], r.get("description", ""), r.get("legacy_level"), row["id"]))
            rid = row["id"]
        else:
            rid = db.execute("INSERT INTO roles(key, name, description, legacy_level) VALUES (?,?,?,?)",
                             (r["key"], r["name"], r.get("description", ""), r.get("legacy_level"))).lastrowid
        unknown = [k for k in r.get("permissions", [])
                   if not db.execute("SELECT 1 FROM permissions WHERE key=?", (k,)).fetchone()]
        if unknown:
            raise ValueError("Role %s references unknown permissions: %s" % (r["key"], ", ".join(unknown)))
        set_role_permissions(db, rid, r.get("permissions", []))
        summary.append("role " + r["key"])
    for r in data.get("roles", []):
        if "can_grant" in r:
            rid = db.execute("SELECT id FROM roles WHERE key=?", (r["key"],)).fetchone()["id"]
            set_role_grantable(db, rid, r["can_grant"])
    if replace_roles:
        for row in db.execute("SELECT id, key FROM roles").fetchall():
            if row["key"] not in incoming:
                db.execute("DELETE FROM roles WHERE id=?", (row["id"],))
                summary.append("deleted role " + row["key"])
    for key, enabled in (data.get("sod_rules") or {}).items():
        db.execute("UPDATE sod_rules SET enabled=? WHERE key=?", (1 if enabled else 0, key))
    db.commit()
    clear_cache()
    return summary


def policy_json() -> str:
    return json.dumps(export_policy(), indent=2)
