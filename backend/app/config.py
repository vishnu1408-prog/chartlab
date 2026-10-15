"""Paths and switches. Everything can be overridden with environment variables (the Docker image sets them)."""
import glob
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _latest(pattern: str) -> str:
    """Newest Kaggle dataset version (numeric sort: versions/18 > versions/9)."""
    def ver(p):
        tail = os.path.basename(os.path.normpath(p))
        return int(tail) if tail.isdigit() else -1
    found = sorted(glob.glob(os.path.expanduser(pattern)), key=ver)
    return found[-1] if found else ""


INDEX_DIR = Path(os.environ.get("CHARTLAB_INDEX_DIR") or _latest(
    "~/.cache/kagglehub/datasets/debashis74017/nifty-50-minute-data/versions/*/"))
STOCK_DIR = Path(os.environ.get("CHARTLAB_STOCK_DIR") or _latest(
    "~/.cache/kagglehub/datasets/debashis74017/stock-market-data-nifty-50-stocks-1-min-data/versions/*/"))
DATA_DIR = Path(os.environ.get("CHARTLAB_DATA_DIR", ROOT / "data"))
HF_DIR = Path(os.environ.get("CHARTLAB_HF_DIR", DATA_DIR / "hf"))   # Hugging Face 1-minute spot + option files
CUSTOM_DIR = Path(os.environ.get("CHARTLAB_CUSTOM_DIR", ROOT / "custom_indicators"))
FRONTEND_DIR = Path(os.environ.get("CHARTLAB_FRONTEND_DIR", ROOT / "frontend"))
ANGEL_CREDS = Path(os.path.expanduser(os.environ.get("CHARTLAB_ANGEL_CREDS", "~/.config/angel/creds.env")))
OFFLINE = os.environ.get("CHARTLAB_OFFLINE") == "1"          # no network calls at all (tests)

SESSION_START = 9 * 60 + 15      # 09:15 IST, minutes of day
SESSION_END = 15 * 60 + 30       # 15:30 IST (exclusive)

for sub in ("cache/base", "cache/ext", "drawings"):
    (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
for sub in ("index", "options"):
    (HF_DIR / sub).mkdir(parents=True, exist_ok=True)
