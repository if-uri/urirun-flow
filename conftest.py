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
    adapters = str(_ADAPTERS)
    if adapters not in sys.path:
        sys.path.insert(0, adapters)
    mod = sys.modules.get("urirun")
    if mod is not None and getattr(mod, "__file__", None) is None:
        for name in [n for n in list(sys.modules) if n == "urirun" or n.startswith("urirun.")]:
            del sys.modules[name]
