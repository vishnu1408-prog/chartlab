import pandas as pd

from app.indicators.builtin import option_bep as ob


def test_atm_and_levels():
    assert ob.atm(25514.3, 50) == 25500
    L = dict((n, p) for n, p, *_ in ob.bep_levels(25500, 120.0, 100.0, False))
    assert L["Straddle +"] == 25720 and L["Straddle −"] == 25280
    assert L["Half +"] == 25610 and L["Call BEP"] == 25620 and L["Put BEP"] == 25400
    assert len(ob.bep_levels(25500, 120.0, 100.0, True)) == 15


def test_uses_previous_day_premiums(monkeypatch):
    idx = pd.date_range("2026-11-19 09:15", periods=6, freq="75min").append(pd.date_range("2026-11-20 09:15", periods=6, freq="75min"))
    df = pd.DataFrame({"open": 25500.0, "high": 25510.0, "low": 25490.0, "close": 25510.0, "volume": 0}, index=idx)
    df.attrs["symbol"] = "NIFTY 50"
    seen = {}

    def fake(hf, day, prev_day, K):
        seen.update(day=day, prev=prev_day, K=K)
        return (pd.Timestamp("2026-11-24").date(), 120.0, 100.0)
    monkeypatch.setattr(ob, "prev_premiums", fake)
    out = ob.OptionBEP().compute(df, days=10, fractions=False, labels=True)
    assert seen["prev"] == pd.Timestamp("2026-11-19") and seen["day"] == pd.Timestamp("2026-11-20") and seen["K"] == 25500
    first_today = int(ob.times(df)[6])
    assert out.shapes and all(s["visible_from"] == first_today for s in out.shapes)   # known at the open, not before
    assert "25,280" in out.info["latest"] and "25,720" in out.info["latest"]
