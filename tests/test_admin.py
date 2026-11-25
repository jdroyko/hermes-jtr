from app.db import get_db


def rl_and_steps(app):
    with app.test_request_context():
        db = get_db()
        rl = db.execute("SELECT id FROM routing_lists LIMIT 1").fetchone()["id"]
        steps = db.execute("SELECT id FROM routing_steps WHERE routing_list_id=? ORDER BY seq", (rl,)).fetchall()
        return rl, [s["id"] for s in steps]


def seqs(app, rl):
    with app.test_request_context():
        db = get_db()
        rows = db.execute("SELECT id, seq FROM routing_steps WHERE routing_list_id=? ORDER BY seq", (rl,)).fetchall()
        return [r["id"] for r in rows]


def test_reorder_move_down(app, as_user):
    rl, ids = rl_and_steps(app)
    c = as_user("dta")
    r = c.post("/admin/routing/%d/reorder" % rl, data={"move_down": str(ids[0])})
    assert r.status_code == 302
    assert seqs(app, rl) == [ids[1], ids[0]] + ids[2:]


def test_reorder_move_up_at_top_is_noop(app, as_user):
    rl, ids = rl_and_steps(app)
    c = as_user("dta")
    r = c.post("/admin/routing/%d/reorder" % rl, data={"move_up": str(ids[0])})
    assert r.status_code == 302
    assert seqs(app, rl) == ids


def test_reorder_bulk_drag(app, as_user):
    rl, ids = rl_and_steps(app)
    c = as_user("dta")
    new_order = list(reversed(ids))
    r = c.post("/admin/routing/%d/reorder" % rl, data={"step_id": [str(i) for i in new_order]})
    assert r.status_code == 302
    assert seqs(app, rl) == new_order


def test_reorder_rejects_mismatched_ids(app, as_user):
    rl, ids = rl_and_steps(app)
    c = as_user("dta")
    r = c.post("/admin/routing/%d/reorder" % rl, data={"step_id": [str(ids[0]), "999999"]})
    assert r.status_code == 400
    assert seqs(app, rl) == ids


def test_reorder_requires_routing_manage(app, as_user):
    rl, ids = rl_and_steps(app)
    c = as_user("traveler")
    r = c.post("/admin/routing/%d/reorder" % rl, data={"move_down": str(ids[0])})
    assert r.status_code == 403
    assert seqs(app, rl) == ids
