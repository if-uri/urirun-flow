from __future__ import annotations

import contextlib
import json
import re
import sys
import time
from pathlib import Path


def now_id() -> str:
    return str(int(time.time()))


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")[:64] or "step"


def json_write(path: str | Path, data: dict) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(f"{json.dumps(data, indent=2, ensure_ascii=False)}\n", encoding="utf-8")


def quiet_completion(**kwargs):
    moved = sys.modules.get("urirun.node._util") or sys.modules.get("urirun_node._util")
    if moved is not None:
        patched = getattr(moved, "quiet_completion", None)
        if patched is not None and patched is not quiet_completion:
            return patched(**kwargs)

    import litellm

    litellm.suppress_debug_info = True
    with contextlib.redirect_stdout(sys.stderr):
        return litellm.completion(**kwargs)
