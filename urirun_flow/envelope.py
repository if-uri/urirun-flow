"""Small envelope helpers shared by the standalone flow package.

These mirror the stable urirun run-envelope shape without importing the hub runtime just to unwrap
``result.value`` / ``result.stdout``.  Runtime execution still imports ``urirun`` at call sites.
"""
from __future__ import annotations

import json
from typing import Any


def result_data(env: dict) -> Any:
    """Extract a connector payload from a URI run envelope.

    Shapes handled:
      - local-function: ``{"result": {"value": ...}}``
      - argv/shell: ``{"result": {"stdout": "...json or text..."}}``
      - fetch/dry-run: ``{"result": {...}}``
    """
    if not isinstance(env, dict):
        return env
    result = env.get("result")
    if not isinstance(result, dict):
        return result if result is not None else env
    if "value" in result:
        return result["value"]
    stdout = result.get("stdout")
    if isinstance(stdout, str) and stdout.strip():
        try:
            return json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return {"stdout": stdout}
    return result
