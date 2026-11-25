from app import jtr

CFG = {"first_last_day_pct": 75, "ltdy_enabled": 1, "ltdy_pct_31_180": 75, "ltdy_pct_181": 55}


def stop(loc, state, a, d, nightly=None, gq=0):
    return {"location": loc, "state": state, "arrive_date": a, "depart_date": d,
            "lodging_nightly": nightly, "gov_quarters": gq}


def test_three_day_trip_first_last_75(app):
    with app.app_context():
        # San Diego: lodging 215, M&IE 86 (sample data)
        r = jtr.compute_per_diem("2027-03-01", "2027-03-03", [stop("San Diego", "CA", "2027-03-01", "2027-03-03")], cfg=CFG)
        days = r["lines"]
        assert len(days) == 3
        assert days[0]["mie"] == 64.5 and days[0]["lodging"] == 215
        assert days[1]["mie"] == 86 and days[1]["lodging"] == 215
        assert days[2]["mie"] == 64.5 and days[2]["lodging"] == 0
        assert r["total"] == 215 * 2 + 64.5 * 2 + 86


def test_voucher_actual_lodging_capped(app):
    with app.app_context():
        r = jtr.compute_per_diem("2027-03-01", "2027-03-02",
                                 [stop("San Diego", "CA", "2027-03-01", "2027-03-02", nightly=300)], actual=True, cfg=CFG)
        assert r["lines"][0]["lodging"] == 215
        assert r["lines"][0]["lodging_claimed"] == 300


def test_gov_quarters_no_lodging(app):
    with app.app_context():
        r = jtr.compute_per_diem("2027-03-01", "2027-03-02",
                                 [stop("San Diego", "CA", "2027-03-01", "2027-03-02", gq=1)], cfg=CFG)
        assert r["lodging"] == 0


def test_seasonal_rate(app):
    with app.app_context():
        assert jtr.rate_for("Washington", "DC", "2027-07-15") is not None  # fallback to standard
        summer = jtr.rate_for("Washington, DC", "DC", "2027-07-15")
        spring = jtr.rate_for("Washington, DC", "DC", "2027-04-15")
        assert summer["lodging"] == 197 and spring["lodging"] == 276


def test_long_term_flat_rate(app):
    with app.app_context():
        r = jtr.compute_per_diem("2027-01-01", "2027-03-01",
                                 [stop("San Antonio", "TX", "2027-01-01", "2027-03-01")], cfg=CFG)
        mid = r["lines"][10]
        assert mid["lodging"] == 112.5  # 75% of 150
        assert mid["mie"] == 55.5      # 75% of 74
        assert any("Long-term" in n for n in r["notes"])


def test_same_day_no_per_diem(app):
    with app.app_context():
        r = jtr.compute_per_diem("2027-01-01", "2027-01-01", [stop("San Antonio", "TX", "2027-01-01", "2027-01-02")], cfg=CFG)
        assert r["total"] == 0 and r["notes"]


def test_preaudit_flags(app):
    with app.app_context():
        doc = {"doc_type": "VOUCHER", "start_date": "2027-03-01", "end_date": "2027-03-03", "status": "CREATED", "loa_id": None}
        exps = [{"category": "AIRFARE", "amount": 400, "miles": None, "payment_method": "PERSONAL",
                 "receipt_attached": 0, "description": "", "expense_date": "2027-03-01"},
                {"category": "RENTAL_CAR", "amount": 50, "miles": None, "payment_method": "GTCC",
                 "receipt_attached": 0, "description": "", "expense_date": "2027-03-01"}]
        codes = {f["code"] for f in jtr.preaudit(doc, [], exps, {"lines": []})}
        assert {"NOLOA", "RECEIPT", "GTCC", "RENTAL"} <= codes
