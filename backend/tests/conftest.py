"""Test environment: synthetic 1-minute data in temp folders, no network (CHARTLAB_OFFLINE=1).
Environment variables must be set before `app` is imported, so this runs at collection time."""
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

TMP = Path(tempfile.mkdtemp(prefix="chartlab_test_"))
for d in ("index", "stock", "data", "custom"):
    (TMP / d).mkdir()
os.environ.update(CHARTLAB_INDEX_DIR=str(TMP / "index"), CHARTLAB_STOCK_DIR=str(TMP / "stock"),
                  CHARTLAB_DATA_DIR=str(TMP / "data"), CHARTLAB_CUSTOM_DIR=str(TMP / "custom"), CHARTLAB_OFFLINE="1",
                  CHARTLAB_ANGEL_CREDS=str(TMP / "no_creds.env"))
REAL_INDEX = Path(os.path.expanduser("~/.cache/kagglehub/datasets/debashis74017/nifty-50-minute-data"))


def synthetic_minutes(days=30, seed=7, start="2024-01-01", base=20000.0, volume=False) -> pd.DataFrame:
    """Random-walk 1-minute bars with trends and swings so pattern indicators have something to find."""
    rng = np.random.default_rng(seed)
    out, price = [], base
    for d in pd.bdate_range(start, periods=days):
        idx = pd.date_range(d + pd.Timedelta(minutes=555), periods=375, freq="1min")
        drift = rng.normal(0, 0.6)
        steps = rng.normal(drift, 6, len(idx)) + 25 * np.sin(np.arange(len(idx)) / 37)
        close = price + np.cumsum(steps) * 0.3
        open_ = np.r_[price, close[:-1]]
        hi = np.maximum(open_, close) + rng.uniform(0, 4, len(idx))
        lo = np.minimum(open_, close) - rng.uniform(0, 4, len(idx))
        vol = rng.integers(1000, 50000, len(idx)) if volume else np.zeros(len(idx))
        out.append(pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close, "volume": vol}, index=idx))
        price = close[-1] + rng.normal(0, 20)
    df = pd.concat(out)
    df.index.name = "date"
    return df


def _write(df, path):
    df.reset_index().assign(date=lambda x: x.date.dt.strftime("%Y-%m-%d %H:%M:%S")).to_csv(path, index=False)


_write(synthetic_minutes(), TMP / "index" / "NIFTY 50_minute.csv")
_write(synthetic_minutes(seed=11, base=45000), TMP / "index" / "NIFTY BANK_minute.csv")
_write(synthetic_minutes(seed=3, base=2500, volume=True), TMP / "stock" / "TESTSTOCK_minute.csv")


@pytest.fixture(scope="session")
def tmpdirs():
    return TMP


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def isolated_sync_cache():
    """Each test starts without synced (extension) bars, so tests cannot leak data into each other."""
    from app import store
    yield
    for f in (TMP / "data" / "cache" / "ext").glob("*.parquet"):
        f.unlink()
    store._cache.clear()
    store._tf_cache.clear()
