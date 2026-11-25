from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .. import rbac, workflow
from ..db import audit, get_db
from ._util import Form, me

bp = Blueprint("main", __name__)


@bp.route("/")
def home():
    db = get_db()
    mine = db.execute("SELECT * FROM documents WHERE traveler_id=? AND status NOT IN ('CANCELLED') "
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
                           perms=rbac.user_permissions(user["id"]) if self_service else None)
