import csv
import io

from flask import Blueprint, Response, abort, render_template, request

from .. import rbac
from ..db import audit, get_db
from ._util import me

bp = Blueprint("reports", __name__, url_prefix="/reports")


def _scope_clause():
    orgs = rbac.scoped_org_ids(me(), "reports.view")
    if orgs is None:
        return "1=1", []
    if not orgs:
        return "0=1", []
    return "d.org_id IN (%s)" % ",".join("?" * len(orgs)), list(orgs)


REPORTS = {
    "status": ("Document status by organization",
               "SELECT o.code AS org, d.doc_type AS type, d.status, COUNT(*) AS documents, "
               "ROUND(SUM(d.total_estimate),2) AS total FROM documents d JOIN orgs o ON o.id=d.org_id "
               "WHERE {scope} GROUP BY o.code, d.doc_type, d.status ORDER BY o.code, d.doc_type, d.status"),
    "travelers": ("Travel by traveler",
                  "SELECT u.last_name || ', ' || u.first_name AS traveler, o.code AS org, COUNT(*) AS documents, "
                  "ROUND(SUM(CASE WHEN d.doc_type='AUTH' THEN d.total_estimate ELSE 0 END),2) AS authorized, "
                  "ROUND(SUM(CASE WHEN d.doc_type<>'AUTH' AND d.status='PAID' THEN d.total_estimate ELSE 0 END),2) AS paid "
                  "FROM documents d JOIN users u ON u.id=d.traveler_id JOIN orgs o ON o.id=d.org_id "
                  "WHERE {scope} AND d.status<>'CANCELLED' GROUP BY u.id ORDER BY traveler"),
    "funds": ("Funds status by line of accounting",
              "SELECT l.label AS loa, l.code, l.fiscal_year AS fy, l.budget_amount AS budget, "
              "ROUND(COALESCE(SUM(CASE WHEN d.doc_type IN ('AUTH','LOCAL') AND d.status IN ('ROUTING','APPROVED','PAID') "
              "THEN d.total_estimate END),0),2) AS obligated, "
              "ROUND(l.budget_amount - COALESCE(SUM(CASE WHEN d.doc_type IN ('AUTH','LOCAL') AND d.status IN "
              "('ROUTING','APPROVED','PAID') THEN d.total_estimate END),0),2) AS available "
              "FROM loas l LEFT JOIN documents d ON d.loa_id=l.id WHERE {scope_loa} GROUP BY l.id ORDER BY l.label"),
    "aging": ("Documents awaiting action (oldest first)",
              "SELECT d.doc_number AS document, d.doc_type AS type, o.code AS org, d.current_step_seq AS step, "
              "d.updated_at AS since, ROUND(julianday('now') - julianday(d.updated_at),1) AS days_waiting "
              "FROM documents d JOIN orgs o ON o.id=d.org_id WHERE {scope} AND d.status='ROUTING' "
              "ORDER BY d.updated_at"),
}


def run(key):
    title, sql = REPORTS[key]
    scope, params = _scope_clause()
    scope_loa = scope.replace("d.org_id", "l.org_id")
    cur = get_db().execute(sql.format(scope=scope, scope_loa=scope_loa), params)
    cols = [c[0] for c in cur.description]
    return title, cols, cur.fetchall()


@bp.route("/")
def index():
    if not rbac.has_perm(me(), "reports.view"):
        abort(403)
    key = request.args.get("r", "status")
    if key not in REPORTS:
        abort(404)
    title, cols, rows = run(key)
    if request.args.get("format") == "csv":
        if not rbac.has_perm(me(), "reports.export"):
            abort(403)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        w.writerows([tuple(r) for r in rows])
        audit(me(), "REPORT_EXPORT", key)
        get_db().commit()
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=%s.csv" % key})
    return render_template("reports.html", reports=REPORTS, key=key, title=title, cols=cols, rows=rows)
