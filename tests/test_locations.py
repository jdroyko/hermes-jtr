"""TDY locations: travelers may type any city, state or base. A match on
the per diem rate list uses that locality rate; anything else is kept as
typed and paid at Standard CONUS (lower 48 + DC) or Standard OCONUS."""
import datetime as dt
import re

from app.db import get_db


def future(days):
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()


def _doc_id(resp):
    m = re.search(r"/documents/(\d+)/", resp.headers["Location"])
    return int(m.group(1))


def _new_auth(client, start, end):
    r = client.post("/documents/new", data={"type": "AUTH", "traveler_id": "2", "trip_purpose": "TRAINING",
                                             "trip_description": "Course", "start_date": start, "end_date": end,
                                             "loa_id": "2"})
    assert r.status_code == 302
    return _doc_id(r)


def _add(client, doc, s, e, **data):
    return client.post("/documents/%d/itinerary" % doc,
                       data=dict({"action": "add", "arrive_date": s, "depart_date": e}, **data))


def _stop(app, doc):
    with app.app_context():
        return get_db().execute("SELECT s.*, r.location rate_location FROM document_stops s "
                                "JOIN per_diem_rates r ON r.id=s.rate_id WHERE s.document_id=?", (doc,)).fetchone()


def test_unlisted_conus_city_uses_standard_conus(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(13)
    doc = _new_auth(t, s, e)
    before = _count_rates(app)
    r = _add(t, doc, s, e, location_text="Manhattan, KS")
    assert r.status_code == 302
    stop = _stop(app, doc)
    assert stop["rate_location"] == "Standard CONUS"
    assert (stop["location_text"], stop["location_state"]) == ("Manhattan", "KS")
    assert _count_rates(app) == before  # no new rate rows, no AO approval needed
    page = t.get("/documents/%d/itinerary" % doc).get_data(as_text=True)
    assert "Manhattan, KS" in page and "Standard CONUS rate" in page


def test_full_state_name_and_separate_field(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(13)
    doc = _new_auth(t, s, e)
    _add(t, doc, s, e, location_text="Fort Riley", new_state="Kansas")
    stop = _stop(app, doc)
    assert stop["rate_location"] == "Standard CONUS" and stop["location_state"] == "KS"


def test_foreign_alaska_and_territory_use_standard_oconus(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(13)
    for text in ("Ramstein AB, Germany", "Fairbanks, AK", "Andersen AFB, GU"):
        doc = _new_auth(t, s, e)
        assert _add(t, doc, s, e, location_text=text).status_code == 302
        assert _stop(app, doc)["rate_location"] == "Standard OCONUS", text


def test_state_alone_is_accepted(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(13)
    doc = _new_auth(t, s, e)
    assert _add(t, doc, s, e, location_text="Kansas").status_code == 302
    assert _stop(app, doc)["rate_location"] == "Standard CONUS"


def test_city_without_state_is_accepted_and_state_inferred(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(13)
    doc = _new_auth(t, s, e)
    assert _add(t, doc, s, e, location_text="SAn Francisco").status_code == 302
    stop = _stop(app, doc)
    assert (stop["location_text"], stop["location_state"]) == ("San Francisco", "CA")
    assert stop["rate_location"] == "Standard CONUS"
    assert "San Francisco, CA" in t.get("/documents/%d/itinerary" % doc).get_data(as_text=True)


def test_unknown_place_without_state_is_accepted_as_conus(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(13)
    doc = _new_auth(t, s, e)
    r = _add(t, doc, s, e, location_text="nowhere base")
    assert r.status_code == 302
    stop = _stop(app, doc)
    assert stop["location_text"] == "Nowhere Base" and stop["location_state"] is None
    assert stop["rate_location"] == "Standard CONUS"
    assert "outside the continental US" in t.get("/documents/%d/itinerary" % doc).get_data(as_text=True)


def test_known_base_without_state_uses_its_country(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(13)
    doc = _new_auth(t, s, e)
    _add(t, doc, s, e, location_text="Ramstein AB")
    stop = _stop(app, doc)
    assert stop["location_text"] == "Ramstein AB" and stop["rate_location"] == "Standard OCONUS"


def test_case_insensitive_match_uses_locality_rate(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(13)
    doc = _new_auth(t, s, e)
    assert _add(t, doc, s, e, location_text="san diego, ca").status_code == 302
    stop = _stop(app, doc)
    assert stop["rate_location"] == "San Diego" and stop["location_text"] is None


def test_listed_location_without_state_matches(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(13)
    doc = _new_auth(t, s, e)
    assert _add(t, doc, s, e, location_text="San Antonio").status_code == 302
    assert _stop(app, doc)["rate_location"] == "San Antonio"


def test_custom_location_computes_at_standard_rate(app, as_user):
    t = as_user("traveler")
    s, e = future(10), future(12)
    doc = _new_auth(t, s, e)
    _add(t, doc, s, e, location_text="Manhattan, KS")
    with app.app_context():
        from app import jtr
        from app.views._util import load_children
        stops, _ = load_children(doc)
        pd = jtr.compute_per_diem(s, e, stops, cfg={"first_last_day_pct": 75, "ltdy_enabled": 0,
                                                     "ltdy_pct_31_180": 75, "ltdy_pct_181": 55})
        assert pd["lines"][1]["mie_rate"] == 68 and "Manhattan, KS" in pd["lines"][1]["location"]


def _count_rates(app):
    with app.app_context():
        return get_db().execute("SELECT COUNT(*) c FROM per_diem_rates").fetchone()["c"]
