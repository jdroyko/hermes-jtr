from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import ACCESSIBLE_THEME, MODERN_THEME, THEMES, inventory, rbac, workflow
from ..db import audit, get_db
from ._util import Form, me

bp = Blueprint("main", __name__)


@bp.route("/")
def home():
    db = get_db()
    mine = db.execute("SELECT * FROM documents WHERE traveler_id=? AND is_template=0 AND status NOT IN ('CANCELLED') "
                      "ORDER BY updated_at DESC LIMIT 10", (me(),)).fetchall()
    queue = workflow.queue_for(me())
    returned = [d for d in mine if d["status"] == "RETURNED"]
    approved_auths = db.execute(
        "SELECT a.* FROM documents a WHERE a.traveler_id=? AND a.doc_type='AUTH' AND a.status='APPROVED' "
        "AND NOT EXISTS (SELECT 1 FROM documents v WHERE v.parent_auth_id=a.id AND v.status<>'CANCELLED')",
        (me(),)).fetchall()
    return render_template("home.html", mine=mine, queue=queue, returned=returned, approved_auths=approved_auths)


@bp.route("/profile", methods=["GET", "POST"])
def profile():
    return _profile_form(g.user, self_service=True)


@bp.route("/profile/memberships", methods=["POST"])
def memberships():
    db = get_db()
    if request.form.get("action") == "remove":
        db.execute("DELETE FROM user_memberships WHERE id=? AND user_id=?", (request.form.get("membership_id"), me()))
        db.commit()
        flash("Membership removed.", "success")
        return redirect(url_for(".profile") + "#memberships-title")
    program = request.form.get("program", "")
    last4 = (request.form.get("member_last4") or "").strip()
    if program not in inventory.PROGRAMS or (last4 and not (last4.isalnum() and len(last4) == 4)):
        flash("Choose a program, and enter exactly 4 letters or digits for the member number, or leave it blank.",
              "error")
        return redirect(url_for(".profile") + "#memberships-title")
    db.execute("INSERT INTO user_memberships(user_id, program, member_last4) VALUES (?,?,?) "
               "ON CONFLICT(user_id, program) DO UPDATE SET member_last4=excluded.member_last4",
               (me(), program, last4.upper()))
    audit(me(), "MEMBERSHIP_ADD", "user:%d" % me(), program)
    db.commit()
    flash("%s added. Results from %s now show a member badge." % (inventory.PROGRAMS[program][1],
          " and ".join(inventory.PROGRAMS[program][2][:2]) + (" and others" if len(inventory.PROGRAMS[program][2]) > 2 else "")),
          "success")
    return redirect(url_for(".profile") + "#memberships-title")


@bp.route("/profile/theme", methods=["POST"])
def theme():
    choice = request.form.get("theme")
    if choice not in THEMES:
        abort(400)
    get_db().execute("UPDATE users SET theme=? WHERE id=?", (choice, me()))
    audit(me(), "THEME_CHANGE", "user:%d" % me(), choice)
    get_db().commit()
    flash("Display style changed to %s." % THEMES[choice][0], "success")
    return redirect(url_for(".profile"))


def _profile_form(user, self_service):
    db = get_db()
    if self_service and not rbac.has_perm(me(), "profile.edit.self"):
        editable = False
    elif not self_service:
        editable = rbac.has_perm(me(), "profile.edit.org", user["org_id"])
    else:
        editable = True
    errors = {}
    values = dict(user)
    if request.method == "POST":
        if not editable:
            return redirect(request.path)
        f = Form(request.form)
        values.update({k: f.get(k) for k in ("first_name", "last_name", "email", "phone", "rank_grade",
                                               "duty_station", "residence_city", "gtcc_last4", "eft_last4")})
        f.required("first_name", "First name")
        f.required("last_name", "Last name")
        email = f.required("email", "Email address")
        if email and "@" not in email:
            f.errors["email"] = "Enter an email address in the correct format, like name@example.mil."
        for k, label in (("gtcc_last4", "GTCC last 4 digits"), ("eft_last4", "Bank account last 4 digits")):
            v = f.get(k)
            if v and not (v.isdigit() and len(v) == 4):
                f.errors[k] = "%s must be exactly 4 digits." % label
        rl = f.get("default_routing_list_id")
        values["default_routing_list_id"] = int(rl) if rl else None
        errors = f.errors
        if f.ok:
            db.execute("UPDATE users SET first_name=?, last_name=?, email=?, phone=?, rank_grade=?, duty_station=?,"
                       " residence_city=?, gtcc_last4=?, eft_last4=?, default_routing_list_id=? WHERE id=?",
                       (values["first_name"], values["last_name"], values["email"], values["phone"],
                        values["rank_grade"], values["duty_station"], values["residence_city"],
                        values["gtcc_last4"], values["eft_last4"], values["default_routing_list_id"], user["id"]))
            audit(me(), "PROFILE_UPDATE", "user:%d" % user["id"])
            db.commit()
            flash("Profile saved.", "success")
            return redirect(request.path)
    routing_lists = db.execute("SELECT rl.*, o.code FROM routing_lists rl JOIN orgs o ON o.id=rl.org_id "
                               "WHERE rl.org_id IN (%s)" % ",".join(str(i) for i in rbac.org_ancestors(user["org_id"]))
                               ).fetchall()
    org = db.execute("SELECT * FROM orgs WHERE id=?", (user["org_id"],)).fetchone()
    return render_template("profile.html", values=values, errors=errors, editable=editable, org=org,
                           routing_lists=routing_lists, self_service=self_service, subject=user,
                           perms=rbac.user_permissions(user["id"]) if self_service else None,
                           memberships=db.execute("SELECT * FROM user_memberships WHERE user_id=? ORDER BY program",
                                                  (user["id"],)).fetchall() if self_service else None)


@bp.route("/profile/theme/toggle", methods=["POST"])
def theme_toggle():
    """Header switch between the 508 accessible design and the modern design."""
    row = get_db().execute("SELECT theme FROM users WHERE id=?", (me(),)).fetchone()
    choice = ACCESSIBLE_THEME if row["theme"] == MODERN_THEME else MODERN_THEME
    get_db().execute("UPDATE users SET theme=? WHERE id=?", (choice, me()))
    audit(me(), "THEME_CHANGE", "user:%d" % me(), choice)
    get_db().commit()
    nxt = request.form.get("next", "")
    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
        nxt = url_for(".home")
    flash("Switched to the %s." % ("modern design (not 508 compliant)" if choice == MODERN_THEME
                                  else "508 accessible design"), "success")
    return redirect(nxt)
