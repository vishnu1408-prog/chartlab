"""Finds built-in indicators and user indicators in the custom folder (hot-reloaded when a file changes).
A custom file must define a subclass of `Indicator` (from chartlab: `from app.indicators.base import ...`)
or a module-level `INDICATOR` instance. Errors in one file are reported, never crash the app."""
import importlib.util
import inspect
import sys
import threading
import traceback
from pathlib import Path

from .. import config
from .base import Indicator
from .builtin import BUILTINS

_lock = threading.Lock()
_custom: dict = {}            # path → (mtime, [indicators], error)


def _load_file(path: Path):
    name = f"chartlab_custom_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    found = []
    if isinstance(getattr(mod, "INDICATOR", None), Indicator):
        found.append(mod.INDICATOR)
    for _, obj in inspect.getmembers(mod, inspect.isclass):
        if issubclass(obj, Indicator) and obj is not Indicator and obj.__module__ == name and obj.id:
            if not any(type(f) is obj for f in found):
                found.append(obj())
    if not found:
        raise ValueError("no Indicator subclass (with an `id`) or INDICATOR instance found")
    for f in found:
        f.source = "custom"
        f.file = path.name
    return found


def _scan():
    folder = Path(config.CUSTOM_DIR)
    seen = set()
    if folder.exists():
        for p in sorted(folder.glob("*.py")):
            if p.name.startswith("_"):
                continue
            seen.add(p)
            m = p.stat().st_mtime
            if p in _custom and _custom[p][0] == m:
                continue
            try:
                _custom[p] = (m, _load_file(p), "")
            except Exception:
                _custom[p] = (m, [], traceback.format_exc(limit=3))
    for p in list(_custom):
        if p not in seen:
            _custom.pop(p)


def all_indicators() -> dict:
    with _lock:
        _scan()
        out = {i.id: i for i in BUILTINS}
        for _, inds, _ in _custom.values():
            for i in inds:
                out[i.id] = i
        return out


def errors() -> list:
    with _lock:
        _scan()
        return [{"file": p.name, "error": e} for p, (_, _, e) in _custom.items() if e]


def get(ind_id: str) -> Indicator:
    i = all_indicators().get(ind_id)
    if i is None:
        raise KeyError(f"unknown indicator {ind_id!r}")
    return i
