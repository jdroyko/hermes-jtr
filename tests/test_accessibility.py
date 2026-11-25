"""Automated Section 508 / WCAG lint over every rendered page.

This catches structural problems (missing labels, titles, landmarks,
duplicate ids, table headers). It does NOT replace manual testing with a
screen reader, keyboard-only use and a contrast checker – see
docs/ACCESSIBILITY.md for the manual test plan.
"""
import re
from html.parser import HTMLParser

import pytest


class A11yParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids, self.dupes = set(), set()
        self.labels_for = set()
        self.inputs = []          # (tag, id, attrs)
        self.tags = []
        self.headings = []
        self.imgs_no_alt = 0
        self.lang = None
        self.title = ""
        self._in_title = False
        self.tables = 0
        self.th_count = 0
        self.buttons_empty = 0
        self._btn_text = None
        self._label_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append(tag)
        if "id" in a:
            if a["id"] in self.ids:
                self.dupes.add(a["id"])
            self.ids.add(a["id"])
        if tag == "html":
            self.lang = a.get("lang")
        if tag == "title":
            self._in_title = True
        if tag == "label":
            self._label_depth += 1
            if "for" in a:
                self.labels_for.add(a["for"])
        if tag in ("input", "select", "textarea"):
            if a.get("type") not in ("hidden", "submit", "button"):
                self.inputs.append((tag, a.get("id"), a, self._label_depth > 0))
        if tag == "img" and "alt" not in a:
            self.imgs_no_alt += 1
        if re.fullmatch(r"h[1-6]", tag):
            self.headings.append(int(tag[1]))
        if tag == "table":
            self.tables += 1
        if tag == "th":
            self.th_count += 1
        if tag == "button":
            self._btn_text = "" if not a.get("aria-label") else "x"

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "label":
            self._label_depth -= 1
        if tag == "button" and self._btn_text is not None:
            if not self._btn_text.strip():
                self.buttons_empty += 1
            self._btn_text = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._btn_text is not None:
            self._btn_text += data


def check(html, url):
    p = A11yParser()
    p.feed(html)
    problems = []
    if p.lang != "en":
        problems.append("html lang missing")
    if not p.title.strip():
        problems.append("empty <title>")
    for landmark in ("main", "header", "footer"):
        if landmark not in p.tags:
            problems.append("missing <%s> landmark" % landmark)
    if p.headings.count(1) != 1:
        problems.append("expected exactly one h1, found %d" % p.headings.count(1))
    for prev, cur in zip(p.headings, p.headings[1:]):
        if cur > prev + 1:
            problems.append("heading level skipped h%d -> h%d" % (prev, cur))
            break
    if p.dupes:
        problems.append("duplicate ids: %s" % ", ".join(sorted(p.dupes)))
    for tag, id_, attrs, wrapped in p.inputs:
        if not (wrapped or (id_ and id_ in p.labels_for) or attrs.get("aria-label") or attrs.get("aria-labelledby")):
            problems.append("unlabelled <%s id=%s name=%s>" % (tag, id_, attrs.get("name")))
    for tag, id_, attrs, _ in p.inputs:
        for ref in (attrs.get("aria-describedby") or "").split():
            if ref not in p.ids:
                problems.append("aria-describedby points to missing id %s" % ref)
    if p.imgs_no_alt:
        problems.append("%d <img> without alt" % p.imgs_no_alt)
    if p.tables and not p.th_count:
        problems.append("table without header cells")
    if p.buttons_empty:
        problems.append("%d button(s) without accessible name" % p.buttons_empty)
    if 'href="#main-content"' not in html:
        problems.append("no skip link")
    assert not problems, "%s: %s" % (url, "; ".join(problems))


PAGES = {
    None: ["/login", "/accessibility"],
    "traveler": ["/", "/profile", "/documents/", "/documents/queue", "/documents/board", "/documents/new?type=AUTH",
                 "/documents/new?type=LOCAL"],
    "admin": ["/admin/", "/admin/users", "/admin/users/new", "/admin/users/2", "/admin/users/2/profile",
              "/admin/roles", "/admin/roles/new", "/admin/roles/1", "/admin/roles/new?clone=1", "/admin/matrix",
              "/admin/permissions", "/admin/sod", "/admin/policy", "/admin/routing", "/admin/routing/1",
              "/admin/orgs", "/admin/loas", "/admin/perdiem", "/admin/settings", "/admin/audit",
              "/reports/?r=status", "/reports/?r=travelers", "/reports/?r=funds", "/reports/?r=aging"],
}


@pytest.mark.parametrize("user,url", [(u, p) for u, ps in PAGES.items() for p in ps])
def test_page_a11y(client, as_user, user, url):
    c = as_user(user) if user else client
    r = c.get(url)
    assert r.status_code == 200, url
    check(r.get_data(as_text=True), url)


def test_document_pages_a11y(as_user):
    import datetime as dt
    t = as_user("traveler")
    s = (dt.date.today() + dt.timedelta(days=7)).isoformat()
    e = (dt.date.today() + dt.timedelta(days=9)).isoformat()
    r = t.post("/documents/new", data={"type": "AUTH", "traveler_id": "2", "trip_purpose": "TDY",
                                       "trip_description": "A11y trip", "start_date": s, "end_date": e})
    doc = int(re.search(r"/documents/(\d+)/", r.headers["Location"]).group(1))
    t.post("/documents/%d/itinerary" % doc, data={"action": "add", "location_text": "Standard CONUS, US", "arrive_date": s, "depart_date": e})
    t.post("/documents/%d/expenses" % doc, data={"action": "add", "expense_date": s, "category": "TAXI",
                                                 "amount": "30", "payment_method": "GTCC"})
    for url in ("/documents/%d" % doc, "/documents/%d/trip" % doc, "/documents/%d/itinerary" % doc,
                "/documents/%d/expenses" % doc):
        resp = t.get(url)
        assert resp.status_code == 200
        check(resp.get_data(as_text=True), url)


def test_board_a11y_with_cards(as_user):
    """Exercise the kanban board with a non-empty 'needs my action' column,
    so the per-card Actions disclosure (stamp form, remarks field) is
    actually rendered and linted, not just the empty-state page."""
    import datetime as dt
    t = as_user("traveler")
    s = (dt.date.today() + dt.timedelta(days=7)).isoformat()
    e = (dt.date.today() + dt.timedelta(days=9)).isoformat()
    r = t.post("/documents/new", data={"type": "AUTH", "traveler_id": "2", "trip_purpose": "TDY",
                                       "trip_description": "Board a11y trip", "start_date": s, "end_date": e})
    doc = int(re.search(r"/documents/(\d+)/", r.headers["Location"]).group(1))
    t.post("/documents/%d/itinerary" % doc, data={"action": "add", "location_text": "Standard CONUS, US", "arrive_date": s, "depart_date": e})
    t.post("/documents/%d/sign" % doc, data={"certify": "1"})

    budget = as_user("budget")
    resp = budget.get("/documents/board")
    assert resp.status_code == 200
    check(resp.get_data(as_text=True), "/documents/board (needs-action card)")

    budget.post("/documents/%d/action" % doc, data={"action": "stamp"})
    ao = as_user("ao")
    resp = ao.get("/documents/board")
    assert resp.status_code == 200
    check(resp.get_data(as_text=True), "/documents/board (ao needs-action card)")

    ao.post("/documents/%d/action" % doc, data={"action": "return", "remarks": "Fix the dates"})
    resp = t.get("/documents/board")
    assert resp.status_code == 200
    check(resp.get_data(as_text=True), "/documents/board (returned card, signable)")


def test_error_states_a11y(client, as_user):
    r = client.post("/login", data={"username": "", "password": ""})
    html = r.get_data(as_text=True)
    assert "error-summary" in html and 'aria-invalid="true"' in html
    check(html, "/login (errors)")
    t = as_user("traveler")
    r = t.post("/documents/new", data={"type": "AUTH", "traveler_id": "2", "trip_purpose": "",
                                       "trip_description": "", "start_date": "bad", "end_date": ""})
    html = r.get_data(as_text=True)
    assert html.count('aria-invalid="true"') >= 3
    check(html, "/documents/new (errors)")
