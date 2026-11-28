import datetime as dt

from app import config, sources, sync


def _probe(monkeypatch, feed_time, now):
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(sources.Angel, "available", classmethod(lambda cls: True))
    monkeypatch.setattr(sources.Angel, "token", classmethod(lambda cls, s: "26000"))
    monkeypatch.setattr(sources.Angel, "quotes", classmethod(
        lambda cls, by, mode="FULL": {"26000": {"exchFeedTime": feed_time, "exchTradeTime": "01-Jan-1970 05:30:00"}}))
    sync._holiday.clear()
    return sync.is_holiday("NSE", now)


def test_pre_open_heartbeat_on_a_holiday_is_not_a_trading_update(monkeypatch):
    # 24 Nov 2026 (Guru Nanak Jayanti): Angel's index feed time was 06:40 that morning, nothing after
    assert _probe(monkeypatch, "24-Nov-2026 06:40:05", dt.datetime(2026, 11, 24, 10, 0)) is True


def test_normal_day_feed_after_open_is_open(monkeypatch):
    assert _probe(monkeypatch, "24-Nov-2026 09:59:58", dt.datetime(2026, 11, 24, 10, 0)) is False


def test_feed_from_yesterday_is_a_holiday(monkeypatch):
    assert _probe(monkeypatch, "23-Nov-2026 15:30:00", dt.datetime(2026, 11, 24, 10, 0)) is True
