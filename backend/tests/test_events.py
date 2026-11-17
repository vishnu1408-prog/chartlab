import datetime as dt

from app import events


def test_us_eastern_to_ist_respects_daylight_saving():
    assert events.et_to_ist(dt.date(2026, 7, 10), 8, 30) == dt.datetime(2026, 7, 10, 18, 0)    # EDT +9:30
    assert events.et_to_ist(dt.date(2026, 12, 4), 8, 30) == dt.datetime(2026, 12, 4, 19, 0)    # EST +10:30
    assert events.et_to_ist(dt.date(2026, 10, 28), 14, 0) == dt.datetime(2026, 10, 28, 23, 30)


def test_rule_events_and_custom_events():
    g = events.generated(dt.date(2024, 7, 1), dt.date(2024, 7, 31), {"in", "us", "eia"})
    titles = {(e["ts"].date().isoformat(), e["title"]) for e in g}
    assert ("2024-07-23", "Union Budget") in titles
    assert ("2024-07-05", "US jobs report (NFP, approx.)") in titles
    assert any(t == "EIA crude inventories" for _, t in titles)
    assert not any("RBI" in t for _, t in titles)                       # no RBI meeting in July 2024
    assert any(e["title"] == "RBI policy decision" for e in events.generated(dt.date(2024, 8, 8), dt.date(2024, 8, 8), {"in"}))
    i = events.add_custom("2026-12-04T10:00", "RBI policy (my note)", "IN", "High")
    ev = events.events(dt.datetime(2026, 12, 1), dt.datetime(2026, 12, 6), {"in"}, "High")
    assert any(e["title"] == "RBI policy (my note)" for e in ev)
    events.delete_custom(i)
    assert not any(c["id"] == i for c in events.custom())
