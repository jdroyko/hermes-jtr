import json
import re

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from .. import rbac
from ..db import audit, get_db
from ._util import Form, me
from .main import _profile_form

bp = Blueprint("admin", __name__, url_prefix="/admin")

ADMIN_AREAS = [
    ("admin.users", "admin.users", "Users & role assignments", "Create accounts, edit profiles, grant and revoke roles."),
    ("admin.roles", "admin.roles", "Roles", "Create, clone and edit roles as bundles of permissions."),
    ("admin.matrix", "admin.roles", "Permission matrix", "Edit every role's permissions in one grid."),
    ("admin.permissions", "admin.roles", "Permission catalog", "See every permission and add custom ones."),
    ("admin.sod", "admin.sod", "Separation of duties", "Turn conflict-of-interest rules on or off."),
    ("admin.policy", "admin.policy_io", "Import / export policy", "Back up or move the RBAC policy as JSON."),
    ("admin.routing", "routing.manage", "Routing lists", "Define approval steps and who can act on them."),
    ("admin.orgs", "org.manage", "Organizations", "Maintain the organization hierarchy."),
    ("admin.loas", "loa.manage", "Lines of accounting", "Maintain budgets and LOAs."),
    ("admin.perdiem", "perdiem.manage", "Per diem rates", "Maintain lodging and M&IE rates."),
    ("admin.settings", "admin.settings", "JTR & system settings", "Mileage rate, receipt threshold and other rules."),
    ("admin.audit_log", "audit.view", "Audit log", "Review security-relevant events."),
]


def need(key, org_id=None):
    if not rbac.has_perm(me(), key, org_id):
        abort(403)


def _orgs():
    return get_db().execute("SELECT * FROM orgs ORDER BY code").fetchall()


def _scoped_orgs(key):
    scope = rbac.scoped_org_ids(me(), key)
    orgs = _orgs()
    return orgs if scope is None else [o for o in orgs if o["id"] in scope], scope is None


@bp.route("/")
def index():
    areas = [a for a in ADMIN_AREAS if rbac.has_perm(me(), a[1])]
    if not areas:
        abort(403)
    return render_template("admin/index.html", areas=areas)


# -------------------------------------------------------------------- users
@bp.route("/users")
def users():
    need("admin.users")
    orgs, _ = _scoped_orgs("admin.users")
    ids = [o["id"] for o in orgs] or [0]
    q = request.args.get("q", "").strip()
    sql = ("SELECT u.*, o.code org_code FROM users u JOIN orgs o ON o.id=u.org_id WHERE u.org_id IN (%s)"
           % ",".join("?" * len(ids)))
    params = list(ids)
    if q:
        sql += " AND (u.username LIKE ? OR u.last_name LIKE ? OR u.first_name LIKE ?)"
        params += ["%" + q + "%"] * 3
    rows = get_db().execute(sql + " ORDER BY u.last_name", params).fetchall()
    return render_template("admin/users.html", users=rows, q=q)


@bp.route("/users/new", methods=["GET", "POST"])
def user_new():
    need("admin.users")
    orgs, _ = _scoped_orgs("admin.users")
    values = {"username": "", "first_name": "", "last_name": "", "email": "", "org_id": "", "password": ""}
    errors = {}
    if request.method == "POST":
        f = Form(request.form)
        values.update({k: f.get(k) for k in values})
        un = f.required("username", "Username")
        if un and not re.fullmatch(r"[a-z0-9._-]{3,40}", un):
            f.errors["username"] = "Username must be 3–40 lowercase letters, numbers, dots, dashes or underscores."
        elif un and get_db().execute("SELECT 1 FROM users WHERE username=?", (un,)).fetchone():
            f.errors["username"] = "That username is already taken."
        f.required("first_name", "First name")
        f.required("last_name", "Last name")
        f.required("email", "Email address")
        oid = f.get("org_id")
        if not any(str(o["id"]) == oid for o in orgs):
            f.errors["org_id"] = "Select an organization."
        pw = f.get("password")
        if len(pw) < 12:
            f.errors["password"] = "Temporary password must be at least 12 characters."
        errors = f.errors
        if f.ok:
            db = get_db()
            uid = db.execute("INSERT INTO users(username, password_hash, first_name, last_name, email, org_id) "
                             "VALUES (?,?,?,?,?,?)", (un, generate_password_hash(pw, method="pbkdf2:sha256:600000"), values["first_name"],
                                                      values["last_name"], values["email"], int(oid))).lastrowid
            trav = db.execute("SELECT id FROM roles WHERE key='traveler'").fetchone()
            if trav:
                db.execute("INSERT INTO role_assignments(user_id, role_id, org_id, include_children, granted_by)"
                           " VALUES (?,?,?,0,?)", (uid, trav["id"], int(oid), me()))
            audit(me(), "USER_CREATE", "user:%d" % uid)
            db.commit()
            flash("User created with the Traveler role.", "success")
            return redirect(url_for("admin.user_edit", user_id=uid))
    return render_template("admin/user_new.html", values=values, errors=errors, orgs=orgs)


@bp.route("/users/<int:user_id>", methods=["GET", "POST"])
def user_edit(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        abort(404)
    if not (rbac.has_perm(me(), "admin.users", user["org_id"]) or rbac.has_perm(me(), "profile.view.org", user["org_id"])):
        abort(403)
    if request.method == "POST" and request.form.get("form") == "status":
        need("admin.users", user["org_id"])
        db.execute("UPDATE users SET active=? WHERE id=?", (0 if user["active"] else 1, user_id))
        audit(me(), "USER_DEACTIVATE" if user["active"] else "USER_ACTIVATE", "user:%d" % user_id)
        db.commit()
        flash("Account %s." % ("deactivated" if user["active"] else "reactivated"), "success")
        return redirect(request.path)
    if request.method == "POST" and request.form.get("form") == "assign":
        return _assign(user)
    if request.method == "POST" and request.form.get("form") == "revoke":
        ra = db.execute("SELECT * FROM role_assignments WHERE id=? AND user_id=?",
                        (request.form.get("assignment_id"), user_id)).fetchone()
        if ra:
            need("admin.assign", ra["org_id"])
            db.execute("DELETE FROM role_assignments WHERE id=?", (ra["id"],))
            audit(me(), "ROLE_REVOKE", "user:%d" % user_id, "assignment %d role %d" % (ra["id"], ra["role_id"]))
            db.commit()
            flash("Role assignment revoked.", "success")
        return redirect(request.path + "#roles")
    return _user_page(user)


@bp.route("/users/<int:user_id>/profile", methods=["GET", "POST"])
def user_profile(user_id):
    user = get_db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        abort(404)
    if not rbac.has_perm(me(), "profile.view.org", user["org_id"]):
        abort(403)
    return _profile_form(user, self_service=False)


def _user_page(user, assign_errors=None, assign_values=None):
    db = get_db()
    assignments = db.execute(
        "SELECT ra.*, r.name role_name, r.key role_key, o.code org_code, gb.username granted_by_name "
        "FROM role_assignments ra JOIN roles r ON r.id=ra.role_id LEFT JOIN orgs o ON o.id=ra.org_id "
        "LEFT JOIN users gb ON gb.id=ra.granted_by WHERE ra.user_id=? ORDER BY r.name", (user["id"],)).fetchall()
    orgs, global_ok = _scoped_orgs("admin.assign")
    org = db.execute("SELECT * FROM orgs WHERE id=?", (user["org_id"],)).fetchone()
    effective = rbac.user_permissions(user["id"])
    return render_template("admin/user_edit.html", subject=user, org=org, assignments=assignments,
                           roles=db.execute("SELECT * FROM roles ORDER BY name").fetchall(),
                           assign_orgs=orgs, global_ok=global_ok, effective=effective,
                           can_assign=rbac.has_perm(me(), "admin.assign"),
                           can_users=rbac.has_perm(me(), "admin.users", user["org_id"]),
                           assign_errors=assign_errors or {}, av=assign_values or {"include_children": "1"})


def _assign(user):
    db = get_db()
    f = Form(request.form)
    av = {k: f.get(k) for k in ("role_id", "scope_org_id", "include_children", "expires_at")}
    role = db.execute("SELECT * FROM roles WHERE id=?", (av["role_id"] or 0,)).fetchone()
    if not role:
        f.errors["role_id"] = "Select a role."
    scope = av["scope_org_id"]
    org_id = None if scope == "GLOBAL" else (int(scope) if scope.isdigit() else -1)
    if org_id == -1:
        f.errors["scope_org_id"] = "Select a scope."
    exp = f.date("expires_at", "Expiration date", required=False)
    if f.ok:
        if org_id is None and rbac.scoped_org_ids(me(), "admin.assign") is not None:
            f.errors["scope_org_id"] = "Only administrators with global scope can grant global roles."
        else:
            ok, reason = rbac.can_assign(me(), role["id"], user["id"], org_id)
            if not ok:
                f.errors["role_id"] = reason
    if not f.ok:
        return _user_page(user, f.errors, av), 400
    db.execute("INSERT INTO role_assignments(user_id, role_id, org_id, include_children, expires_at, granted_by) "
               "VALUES (?,?,?,?,?,?)", (user["id"], role["id"], org_id, 1 if av["include_children"] else 0, exp, me()))
    audit(me(), "ROLE_GRANT", "user:%d" % user["id"],
          "role=%s org=%s children=%s expires=%s" % (role["key"], org_id, bool(av["include_children"]), exp))
    db.commit()
    rbac.clear_cache()
    flash("Granted %s." % role["name"], "success")
    return redirect(url_for("admin.user_edit", user_id=user["id"]) + "#roles")


# -------------------------------------------------------------------- roles
@bp.route("/roles")
def roles():
    need("admin.roles")
    rows = get_db().execute(
        "SELECT r.*, (SELECT COUNT(*) FROM role_permissions WHERE role_id=r.id) nperm, "
        "(SELECT COUNT(*) FROM role_assignments WHERE role_id=r.id) nassign FROM roles r ORDER BY r.name").fetchall()
    return render_template("admin/roles.html", roles=rows)


def _perm_groups():
    groups = {}
    for p in get_db().execute("SELECT * FROM permissions ORDER BY category, key"):
        groups.setdefault(p["category"], []).append(p)
    return groups


@bp.route("/roles/new", methods=["GET", "POST"])
@bp.route("/roles/<int:role_id>", methods=["GET", "POST"])
def role_edit(role_id=None):
    need("admin.roles")
    db = get_db()
    role = db.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone() if role_id else None
    if role_id and not role:
        abort(404)
    clone = request.args.get("clone")
    src = db.execute("SELECT * FROM roles WHERE id=?", (clone,)).fetchone() if clone else None
    base = role or src
    selected = {p["key"] for p in db.execute(
        "SELECT p.key FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id WHERE rp.role_id=?",
        (base["id"],))} if base else set()
    grantable = {r["key"] for r in db.execute(
        "SELECT r.key FROM role_grantable rg JOIN roles r ON r.id=rg.grantable_role_id WHERE rg.role_id=?",
        (base["id"],))} if base else set()
    values = {"key": role["key"] if role else (src["key"] + "_copy" if src else ""),
              "name": base["name"] + ("" if role else " (copy)") if base else "",
              "description": base["description"] if base else "",
              "legacy_level": (base["legacy_level"] or "") if base else ""}
    errors = {}
    if request.method == "POST":
        f = Form(request.form)
        values.update({k: f.get(k) for k in values})
        selected = set(request.form.getlist("permissions"))
        grantable = set(request.form.getlist("can_grant"))
        if not role:
            k = f.required("key", "Role key")
            if k and not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", k):
                f.errors["key"] = "Role key must start with a letter and use only lowercase letters, numbers and underscores."
            elif k and db.execute("SELECT 1 FROM roles WHERE key=?", (k,)).fetchone():
                f.errors["key"] = "A role with that key already exists."
        f.required("name", "Role name")
        if not selected:
            f.errors["permissions"] = "Select at least one permission."
        errors = f.errors
        if f.ok:
            if role:
                db.execute("UPDATE roles SET name=?, description=?, legacy_level=? WHERE id=?",
                           (values["name"], values["description"], values["legacy_level"] or None, role["id"]))
                rid = role["id"]
            else:
                rid = db.execute("INSERT INTO roles(key, name, description, legacy_level) VALUES (?,?,?,?)",
                                 (values["key"], values["name"], values["description"],
                                  values["legacy_level"] or None)).lastrowid
            rbac.set_role_permissions(db, rid, selected)
            rbac.set_role_grantable(db, rid, grantable)
            audit(me(), "ROLE_SAVE", "role:%d" % rid,
                  "perms=%s; can_grant=%s" % (",".join(sorted(selected)), ",".join(sorted(grantable))))
            db.commit()
            rbac.clear_cache()
            flash("Role saved.", "success")
            return redirect(url_for("admin.role_edit", role_id=rid))
    holders = db.execute("SELECT u.id, u.username, u.first_name, u.last_name, o.code FROM role_assignments ra "
                         "JOIN users u ON u.id=ra.user_id LEFT JOIN orgs o ON o.id=ra.org_id WHERE ra.role_id=? "
                         "ORDER BY u.last_name", (role_id,)).fetchall() if role else []
    return render_template("admin/role_edit.html", role=role, values=values, errors=errors, groups=_perm_groups(),
                           selected=selected, holders=holders, grantable=grantable,
                           all_roles=db.execute("SELECT * FROM roles ORDER BY name").fetchall())


@bp.route("/roles/<int:role_id>/delete", methods=["POST"])
def role_delete(role_id):
    need("admin.roles")
    db = get_db()
    role = db.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone()
    if not role:
        abort(404)
    n = db.execute("SELECT COUNT(*) c FROM role_assignments WHERE role_id=?", (role_id,)).fetchone()["c"]
    if n and not request.form.get("force"):
        flash("This role is assigned to %d user(s). Confirm removal of those assignments to delete it." % n, "error")
        return redirect(url_for("admin.role_edit", role_id=role_id) + "#delete")
    db.execute("DELETE FROM roles WHERE id=?", (role_id,))
    audit(me(), "ROLE_DELETE", "role:%d" % role_id, role["key"])
    db.commit()
    rbac.clear_cache()
    flash("Role %s deleted." % role["name"], "success")
    return redirect(url_for("admin.roles"))


@bp.route("/matrix", methods=["GET", "POST"])
def matrix():
    need("admin.roles")
    db = get_db()
    roles = db.execute("SELECT * FROM roles ORDER BY name").fetchall()
    if request.method == "POST":
        grants = set(request.form.getlist("grant"))  # "roleid:permkey"
        for r in roles:
            keys = [g_.split(":", 1)[1] for g_ in grants if g_.split(":", 1)[0] == str(r["id"])]
            rbac.set_role_permissions(db, r["id"], keys)
        audit(me(), "MATRIX_SAVE", "roles", "%d grants" % len(grants))
        db.commit()
        rbac.clear_cache()
        flash("Permission matrix saved.", "success")
        return redirect(request.path)
    have = {(r["role_id"], r["key"]) for r in db.execute(
        "SELECT rp.role_id, p.key FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id")}
    return render_template("admin/matrix.html", roles=roles, groups=_perm_groups(), have=have)


@bp.route("/permissions", methods=["GET", "POST"])
def permissions():
    need("admin.roles")
    db = get_db()
    errors, values = {}, {"key": "", "name": "", "category": "Custom", "description": ""}
    if request.method == "POST":
        if request.form.get("action") == "delete":
            p = db.execute("SELECT * FROM permissions WHERE id=? AND is_system=0",
                           (request.form.get("perm_id"),)).fetchone()
            if p:
                db.execute("DELETE FROM permissions WHERE id=?", (p["id"],))
                audit(me(), "PERM_DELETE", p["key"])
                db.commit()
                flash("Custom permission deleted.", "success")
            return redirect(request.path)
        f = Form(request.form)
        values.update({k: f.get(k) for k in values})
        k = f.required("key", "Permission key")
        if k and not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)+", k):
            f.errors["key"] = "Use dotted lowercase form, like custom.travel_waiver."
        elif k and db.execute("SELECT 1 FROM permissions WHERE key=?", (k,)).fetchone():
            f.errors["key"] = "That permission already exists."
        f.required("name", "Name")
        errors = f.errors
        if f.ok:
            db.execute("INSERT INTO permissions(key, category, name, description, is_system) VALUES (?,?,?,?,0)",
                       (k, values["category"] or "Custom", values["name"], values["description"]))
            audit(me(), "PERM_CREATE", k)
            db.commit()
            flash("Custom permission added. Use it on routing steps and add it to roles.", "success")
            return redirect(request.path)
    return render_template("admin/permissions.html", groups=_perm_groups(), values=values, errors=errors)


@bp.route("/sod", methods=["GET", "POST"])
def sod():
    need("admin.sod")
    db = get_db()
    if request.method == "POST":
        on = set(request.form.getlist("enabled"))
        for r in db.execute("SELECT key FROM sod_rules").fetchall():
            db.execute("UPDATE sod_rules SET enabled=? WHERE key=?", (1 if r["key"] in on else 0, r["key"]))
        audit(me(), "SOD_SAVE", "sod", ",".join(sorted(on)))
        db.commit()
        flash("Separation-of-duties rules saved.", "success")
        return redirect(request.path)
    return render_template("admin/sod.html", rules=db.execute("SELECT * FROM sod_rules ORDER BY name").fetchall())


@bp.route("/policy", methods=["GET", "POST"])
def policy():
    need("admin.policy_io")
    error = None
    if request.method == "POST":
        raw = request.form.get("policy", "")
        up = request.files.get("policy_file")
        if up and up.filename:
            raw = up.read().decode("utf-8", "replace")
        try:
            summary = rbac.import_policy(json.loads(raw), replace_roles=bool(request.form.get("replace")))
            audit(me(), "POLICY_IMPORT", "rbac", "; ".join(summary)[:2000])
            get_db().commit()
            flash("Policy imported: %d item(s) updated." % len(summary), "success")
            return redirect(request.path)
        except (ValueError, KeyError, TypeError) as exc:
            error = "Policy could not be imported: %s" % exc
    if request.args.get("download"):
        audit(me(), "POLICY_EXPORT", "rbac")
        get_db().commit()
        return Response(rbac.policy_json(), mimetype="application/json",
                        headers={"Content-Disposition": "attachment; filename=rbac-policy.json"})
    return render_template("admin/policy.html", policy=rbac.policy_json(), error=error), (400 if error else 200)


# ------------------------------------------------------------------ routing
@bp.route("/routing", methods=["GET", "POST"])
def routing():
    need("routing.manage")
    db = get_db()
    orgs, _ = _scoped_orgs("routing.manage")
    errors, values = {}, {"name": "", "org_id": "", "description": ""}
    if request.method == "POST":
        f = Form(request.form)
        values.update({k: f.get(k) for k in values})
        f.required("name", "Name")
        if not any(str(o["id"]) == values["org_id"] for o in orgs):
            f.errors["org_id"] = "Select an organization."
        errors = f.errors
        if f.ok:
            rid = db.execute("INSERT INTO routing_lists(org_id, name, description) VALUES (?,?,?)",
                             (int(values["org_id"]), values["name"], values["description"])).lastrowid
            audit(me(), "ROUTING_CREATE", "routing:%d" % rid)
            db.commit()
            flash("Routing list created. Add its steps.", "success")
            return redirect(url_for("admin.routing_edit", rl_id=rid))
    ids = [o["id"] for o in orgs] or [0]
    lists = db.execute("SELECT rl.*, o.code, (SELECT COUNT(*) FROM routing_steps WHERE routing_list_id=rl.id) n "
                       "FROM routing_lists rl JOIN orgs o ON o.id=rl.org_id WHERE rl.org_id IN (%s) ORDER BY o.code"
                       % ",".join("?" * len(ids)), ids).fetchall()
    return render_template("admin/routing.html", lists=lists, orgs=orgs, values=values, errors=errors)


@bp.route("/routing/<int:rl_id>", methods=["GET", "POST"])
def routing_edit(rl_id):
    db = get_db()
    rl = db.execute("SELECT rl.*, o.code FROM routing_lists rl JOIN orgs o ON o.id=rl.org_id WHERE rl.id=?",
                    (rl_id,)).fetchone()
    if not rl:
        abort(404)
    need("routing.manage", rl["org_id"])
    errors = {}
    values = {"seq": "", "name": "", "stamp": "APPROVED", "required_permission": "workflow.approve",
              "assignee_user_id": "", "doc_types": ["AUTH", "VOUCHER", "LOCAL"]}
    if request.method == "POST":
        act = request.form.get("action")
        if act == "delete_step":
            db.execute("DELETE FROM routing_steps WHERE id=? AND routing_list_id=?",
                       (request.form.get("step_id"), rl_id))
            audit(me(), "ROUTING_STEP_DELETE", "routing:%d" % rl_id)
            db.commit()
            flash("Step removed.", "success")
            return redirect(request.path)
        if act == "delete_list":
            used = db.execute("SELECT COUNT(*) c FROM documents WHERE routing_list_id=? AND status='ROUTING'",
                              (rl_id,)).fetchone()["c"]
            if used:
                flash("%d document(s) are currently routing on this list; it cannot be deleted." % used, "error")
                return redirect(request.path)
            db.execute("UPDATE users SET default_routing_list_id=NULL WHERE default_routing_list_id=?", (rl_id,))
            db.execute("UPDATE documents SET routing_list_id=NULL WHERE routing_list_id=?", (rl_id,))
            db.execute("DELETE FROM routing_lists WHERE id=?", (rl_id,))
            audit(me(), "ROUTING_DELETE", "routing:%d" % rl_id)
            db.commit()
            flash("Routing list deleted.", "success")
            return redirect(url_for("admin.routing"))
        f = Form(request.form)
        values.update({k: f.get(k) for k in ("seq", "name", "stamp", "required_permission", "assignee_user_id")})
        values["doc_types"] = request.form.getlist("doc_types")
        seq = f.number("seq", "Step order", minimum=1)
        f.required("name", "Step name")
        stamp = f.required("stamp", "Stamp").upper()
        if not db.execute("SELECT 1 FROM permissions WHERE key=?", (values["required_permission"],)).fetchone():
            f.errors["required_permission"] = "Select a permission."
        if not values["doc_types"]:
            f.errors["doc_types"] = "Select at least one document type."
        errors = f.errors
        if f.ok:
            db.execute("INSERT INTO routing_steps(routing_list_id, seq, name, stamp, required_permission, "
                       "assignee_user_id, doc_types) VALUES (?,?,?,?,?,?,?)",
                       (rl_id, int(seq), values["name"], stamp, values["required_permission"],
                        int(values["assignee_user_id"]) if values["assignee_user_id"] else None,
                        ",".join(values["doc_types"])))
            audit(me(), "ROUTING_STEP_ADD", "routing:%d" % rl_id, stamp)
            db.commit()
            flash("Step added.", "success")
            return redirect(request.path)
    steps = db.execute("SELECT s.*, u.username FROM routing_steps s LEFT JOIN users u ON u.id=s.assignee_user_id "
                       "WHERE routing_list_id=? ORDER BY seq", (rl_id,)).fetchall()
    if not values["seq"]:
        values["seq"] = str((steps[-1]["seq"] + 1) if steps else 1)
    perms = db.execute("SELECT * FROM permissions WHERE key LIKE 'workflow.%' OR is_system=0 ORDER BY key").fetchall()
    users = db.execute("SELECT id, username, first_name, last_name FROM users WHERE active=1 ORDER BY last_name").fetchall()
    return render_template("admin/routing_edit.html", rl=rl, steps=steps, values=values, errors=errors,
                           perms=perms, users=users)


@bp.route("/routing/<int:rl_id>/reorder", methods=["POST"])
def routing_reorder(rl_id):
    db = get_db()
    rl = db.execute("SELECT * FROM routing_lists WHERE id=?", (rl_id,)).fetchone()
    if not rl:
        abort(404)
    need("routing.manage", rl["org_id"])
    steps = db.execute("SELECT id, seq FROM routing_steps WHERE routing_list_id=? ORDER BY seq", (rl_id,)).fetchall()
    ids_in_seq = [s["id"] for s in steps]

    move_up, move_down = request.form.get("move_up"), request.form.get("move_down")
    if move_up or move_down:
        step_id = int(move_up or move_down)
        if step_id not in ids_in_seq:
            abort(400)
        i = ids_in_seq.index(step_id)
        j = i - 1 if move_up else i + 1
        if 0 <= j < len(ids_in_seq):
            ids_in_seq[i], ids_in_seq[j] = ids_in_seq[j], ids_in_seq[i]
    else:
        order = request.form.getlist("step_id")
        if not order or {int(i) for i in order} != set(ids_in_seq):
            abort(400)
        ids_in_seq = [int(i) for i in order]

    for i, step_id in enumerate(ids_in_seq, start=1):
        db.execute("UPDATE routing_steps SET seq=? WHERE id=? AND routing_list_id=?", (i, step_id, rl_id))
    audit(me(), "ROUTING_STEP_REORDER", "routing:%d" % rl_id)
    db.commit()
    flash("Step order updated.", "success")
    return redirect(url_for("admin.routing_edit", rl_id=rl_id))


# --------------------------------------------------------------------- orgs
@bp.route("/orgs", methods=["GET", "POST"])
def orgs():
    need("org.manage")
    db = get_db()
    errors, values = {}, {"code": "", "name": "", "parent_id": ""}
    scoped, global_ok = _scoped_orgs("org.manage")
    if request.method == "POST":
        f = Form(request.form)
        values.update({k: f.get(k) for k in values})
        code = f.required("code", "Org code").upper()
        if code and db.execute("SELECT 1 FROM orgs WHERE code=?", (code,)).fetchone():
            f.errors["code"] = "That org code already exists."
        f.required("name", "Org name")
        pid = values["parent_id"]
        if pid and not any(str(o["id"]) == pid for o in scoped):
            f.errors["parent_id"] = "Select a parent organization within your scope."
        if not pid and not global_ok:
            f.errors["parent_id"] = "Select a parent organization."
        errors = f.errors
        if f.ok:
            db.execute("INSERT INTO orgs(code, name, parent_id) VALUES (?,?,?)",
                       (code, values["name"], int(pid) if pid else None))
            audit(me(), "ORG_CREATE", code)
            db.commit()
            flash("Organization added.", "success")
            return redirect(request.path)
    tree = []

    def walk(parent, depth):
        for o in db.execute("SELECT * FROM orgs WHERE parent_id IS ? ORDER BY code", (parent,)).fetchall():
            tree.append((o, depth))
            walk(o["id"], depth + 1)
    walk(None, 0)
    return render_template("admin/orgs.html", tree=tree, scoped=scoped, values=values, errors=errors)


# --------------------------------------------------------------------- LOAs
@bp.route("/loas", methods=["GET", "POST"])
def loas():
    need("loa.manage")
    db = get_db()
    scoped, _ = _scoped_orgs("loa.manage")
    errors, values = {}, {"org_id": "", "label": "", "code": "", "fiscal_year": "2027", "budget_amount": ""}
    if request.method == "POST":
        f = Form(request.form)
        values.update({k: f.get(k) for k in values})
        if not any(str(o["id"]) == values["org_id"] for o in scoped):
            f.errors["org_id"] = "Select an organization."
        f.required("label", "Label")
        f.required("code", "Accounting code")
        fy = f.number("fiscal_year", "Fiscal year", minimum=2000)
        amt = f.number("budget_amount", "Budget amount")
        errors = f.errors
        if f.ok:
            db.execute("INSERT INTO loas(org_id, label, code, fiscal_year, budget_amount) VALUES (?,?,?,?,?)",
                       (int(values["org_id"]), values["label"], values["code"], int(fy), amt))
            audit(me(), "LOA_CREATE", values["code"])
            db.commit()
            flash("Line of accounting added.", "success")
            return redirect(request.path)
    ids = [o["id"] for o in scoped] or [0]
    rows = db.execute(
        "SELECT l.*, o.code org_code, (SELECT COALESCE(SUM(total_estimate),0) FROM documents d WHERE d.loa_id=l.id "
        "AND d.doc_type IN ('AUTH','LOCAL') AND d.status IN ('ROUTING','APPROVED','PAID')) obligated "
        "FROM loas l JOIN orgs o ON o.id=l.org_id WHERE l.org_id IN (%s) ORDER BY o.code" % ",".join("?" * len(ids)),
        ids).fetchall()
    return render_template("admin/loas.html", loas=rows, scoped=scoped, values=values, errors=errors)


# ----------------------------------------------------------------- per diem
@bp.route("/perdiem", methods=["GET", "POST"])
def perdiem():
    need("perdiem.manage")
    db = get_db()
    errors, values = {}, {"location": "", "state": "", "season_start": "01-01", "season_end": "12-31",
                          "lodging": "", "mie": "", "fiscal_year": "2027"}
    if request.method == "POST":
        if request.form.get("action") == "delete":
            rid = request.form.get("rate_id")
            if db.execute("SELECT 1 FROM document_stops WHERE rate_id=?", (rid,)).fetchone():
                flash("That rate is used by existing documents and cannot be deleted.", "error")
            else:
                db.execute("DELETE FROM per_diem_rates WHERE id=?", (rid,))
                audit(me(), "RATE_DELETE", "rate:%s" % rid)
                db.commit()
                flash("Rate deleted.", "success")
            return redirect(request.path)
        f = Form(request.form)
        values.update({k: f.get(k) for k in values})
        f.required("location", "Location")
        f.required("state", "State or country")
        for k, label in (("season_start", "Season start"), ("season_end", "Season end")):
            if not re.fullmatch(r"(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])", values[k]):
                f.errors[k] = "%s must be in MM-DD format, like 04-15." % label
        lodging = f.number("lodging", "Lodging rate")
        mie = f.number("mie", "M&IE rate")
        fy = f.number("fiscal_year", "Fiscal year", minimum=2000)
        errors = f.errors
        if f.ok:
            db.execute("INSERT INTO per_diem_rates(location, state, season_start, season_end, lodging, mie, fiscal_year)"
                       " VALUES (?,?,?,?,?,?,?)", (values["location"], values["state"].upper(), values["season_start"],
                                                   values["season_end"], lodging, mie, int(fy)))
            audit(me(), "RATE_CREATE", values["location"])
            db.commit()
            flash("Rate added.", "success")
            return redirect(request.path)
    rates = db.execute("SELECT * FROM per_diem_rates ORDER BY location, season_start").fetchall()
    return render_template("admin/perdiem.html", rates=rates, values=values, errors=errors)


# ----------------------------------------------------------------- settings
@bp.route("/settings", methods=["GET", "POST"])
def settings():
    need("admin.settings")
    db = get_db()
    rows = db.execute("SELECT * FROM settings ORDER BY key").fetchall()
    errors = {}
    if request.method == "POST":
        numeric = {"pov_mileage_rate", "receipt_threshold", "first_last_day_pct", "ltdy_pct_31_180",
                   "ltdy_pct_181", "ltdy_enabled", "session_timeout_minutes"}
        new = {}
        for r in rows:
            v = request.form.get(r["key"], "").strip()
            if r["key"] in numeric:
                try:
                    float(v)
                except ValueError:
                    errors[r["key"]] = "%s must be a number." % r["key"].replace("_", " ").capitalize()
            new[r["key"]] = v
        if not errors:
            for k, v in new.items():
                db.execute("UPDATE settings SET value=? WHERE key=?", (v, k))
            audit(me(), "SETTINGS_SAVE", "settings", json.dumps(new)[:2000])
            db.commit()
            flash("Settings saved.", "success")
            return redirect(request.path)
        rows = [dict(r, value=new.get(r["key"], r["value"])) for r in rows]
    return render_template("admin/settings.html", rows=rows, errors=errors)


@bp.route("/audit")
def audit_log():
    need("audit.view")
    rows = get_db().execute("SELECT a.*, u.username FROM audit_log a LEFT JOIN users u ON u.id=a.user_id "
                            "ORDER BY a.id DESC LIMIT 500").fetchall()
    return render_template("admin/audit.html", rows=rows)
