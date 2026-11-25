from app import rbac
from app.db import get_db


def uid(name):
    return get_db().execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()["id"]


def oid(code):
    return get_db().execute("SELECT id FROM orgs WHERE code=?", (code,)).fetchone()["id"]


def test_scope_includes_children(app):
    with app.test_request_context():
        ao = uid("ao")
        assert rbac.has_perm(ao, "workflow.approve", oid("HQ-W1-S10"))   # child of HQ-W1
        assert not rbac.has_perm(ao, "workflow.approve", oid("HQ-W2"))   # sibling wing
        assert not rbac.has_perm(ao, "workflow.approve", oid("HQ"))      # parent


def test_scope_without_children(app):
    with app.test_request_context():
        db = get_db()
        db.execute("UPDATE role_assignments SET include_children=0 WHERE user_id=?", (uid("ao"),))
        rbac.clear_cache()
        assert rbac.has_perm(uid("ao"), "workflow.approve", oid("HQ-W1"))
        assert not rbac.has_perm(uid("ao"), "workflow.approve", oid("HQ-W1-S10"))


def test_global_admin(app):
    with app.test_request_context():
        assert rbac.has_perm(uid("admin"), "admin.roles", oid("HQ-W2"))
        assert rbac.scoped_org_ids(uid("admin"), "admin.roles") is None


def test_expired_assignment_ignored(app):
    with app.test_request_context():
        get_db().execute("UPDATE role_assignments SET expires_at='2000-01-01' WHERE user_id=?", (uid("ao"),))
        rbac.clear_cache()
        assert not rbac.has_perm(uid("ao"), "workflow.approve")


def test_editing_role_changes_access(app):
    with app.test_request_context():
        db = get_db()
        rid = db.execute("SELECT id FROM roles WHERE key='approving_official'").fetchone()["id"]
        rbac.set_role_permissions(db, rid, ["document.view.org"])
        rbac.clear_cache()
        assert not rbac.has_perm(uid("ao"), "workflow.approve")


def test_no_escalation(app):
    with app.test_request_context():
        dta = uid("dta")
        sysadmin = get_db().execute("SELECT id FROM roles WHERE key='sys_admin'").fetchone()["id"]
        ok, reason = rbac.can_assign(dta, sysadmin, uid("traveler"), oid("HQ-W1"))
        assert not ok and "cannot grant" in reason
        reviewer = get_db().execute("SELECT id FROM roles WHERE key='traveler'").fetchone()["id"]
        # DTA lacks document.create.self? org_dta role has no traveler perms, but dta also holds traveler@HQ-W1
        ok, _ = rbac.can_assign(dta, reviewer, uid("traveler"), oid("HQ-W1"))
        assert ok
        ok, reason = rbac.can_assign(dta, reviewer, dta, oid("HQ-W1"))
        assert not ok and "yourself" in reason


def test_policy_round_trip(app):
    with app.test_request_context():
        data = rbac.export_policy()
        data["roles"].append({"key": "alt_ao", "name": "Alternate AO", "permissions": ["workflow.approve"]})
        data["custom_permissions"].append({"key": "custom.waiver", "name": "Waiver", "category": "Custom"})
        data["roles"][-1]["permissions"].append("custom.waiver")
        rbac.import_policy(data)
        again = rbac.export_policy()
        alt = next(r for r in again["roles"] if r["key"] == "alt_ao")
        assert alt["permissions"] == ["custom.waiver", "workflow.approve"]


def test_policy_rejects_unknown_perm(app):
    import pytest
    with app.test_request_context():
        with pytest.raises(ValueError):
            rbac.import_policy({"version": 1, "roles": [{"key": "x", "name": "X", "permissions": ["nope.nope"]}]})


def test_delegated_grant(app):
    """Org DTA can appoint an AO in their wing (delegation) but not outside it, and not a sys admin."""
    with app.test_request_context():
        db = get_db()
        ao_role = db.execute("SELECT id FROM roles WHERE key='approving_official'").fetchone()["id"]
        ok, reason = rbac.can_assign(uid("dta"), ao_role, uid("traveler"), oid("HQ-W1-S10"))
        assert ok, reason
        ok, _ = rbac.can_assign(uid("dta"), ao_role, uid("traveler"), oid("HQ-W2"))
        assert not ok
        # remove delegation -> falls back to subset rule -> denied
        dta_role = db.execute("SELECT id FROM roles WHERE key='org_dta'").fetchone()["id"]
        rbac.set_role_grantable(db, dta_role, [])
        ok, _ = rbac.can_assign(uid("dta"), ao_role, uid("traveler"), oid("HQ-W1"))
        assert not ok
