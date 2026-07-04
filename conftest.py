"""Pytest path guard for running ``urirun-flow/tests`` from the monorepo root.

The monorepo contains a top-level ``urirun/`` directory, while the real Python
package lives under ``urirun/adapters/python/urirun``. When pytest chooses this
package as its rootdir, the repo-root conftest is not loaded, so importing
``urirun.v2`` can bind to the empty namespace shell. Keep this package's tests
self-contained by preferring the sibling adapter package when it exists.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ADAPTERS = _HERE.parent / "urirun" / "adapters" / "python"

if (_ADAPTERS / "urirun").is_dir():
    # Fallback ONLY: appended so editable installs (and this repo's own sources) win.
    # The adapters dir carries stale snapshots of externally-owned packages
    # (urirun_flow, urirun_connector_router) — putting it in front would shadow them.
    adapters = str(_ADAPTERS)
    if adapters not in sys.path:
        sys.path.append(adapters)
    mod = sys.modules.get("urirun")
    if mod is not None and getattr(mod, "__file__", None) is None:
        for name in [n for n in list(sys.modules) if n == "urirun" or n.startswith("urirun.")]:
            del sys.modules[name]

# Sibling repos that OWN packages the adapters dir still shadows with stale snapshots
# (e.g. urirun_connector_router). They must precede the adapters fallback on sys.path —
# editable finders can't help, they sit at the END of sys.meta_path behind PathFinder.
for _sibling in ("urirun-connector-router", "urirun-connector-twin", "urirun-contract"):
    _p = _HERE.parent / _sibling
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
_router_mod = sys.modules.get("urirun_connector_router")
if _router_mod is not None and "/adapters/" in str(getattr(_router_mod, "__file__", "")):
    for name in [n for n in list(sys.modules)
                 if n == "urirun_connector_router" or n.startswith("urirun_connector_router.")]:
        del sys.modules[name]

# These tests must always exercise THIS repo's ``urirun_flow`` sources: the repo root
# goes first on sys.path, and a copy already imported from anywhere else is purged so
# pytest re-imports it from here.
_LOCAL = str(_HERE)
if _LOCAL in sys.path:
    sys.path.remove(_LOCAL)
sys.path.insert(0, _LOCAL)
_flow_mod = sys.modules.get("urirun_flow")
if _flow_mod is not None and not str(getattr(_flow_mod, "__file__", "")).startswith(_LOCAL):
    for name in [n for n in list(sys.modules) if n == "urirun_flow" or n.startswith("urirun_flow.")]:
        del sys.modules[name]


# Isolate the short-TTL env-probe cache between tests (see urirun_flow/_env_probe_cache.py).
import pytest as _pytest


@_pytest.fixture(autouse=True)
def _clear_env_probe_cache():
    from urirun_flow import _env_probe_cache
    _env_probe_cache.clear()
    yield
