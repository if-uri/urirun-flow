from __future__ import annotations

import contextlib
import json
import os
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


def _default_max_tokens() -> int:
    raw = os.getenv("URIRUN_LLM_MAX_TOKENS") or os.getenv("LLM_MAX_TOKENS") or "4096"
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 4096
    return value if value > 0 else 4096


def _should_retry_with_fewer_tokens(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "fewer max_tokens" in msg or "requested up to" in msg


def quiet_completion(**kwargs):
    moved = sys.modules.get("urirun.node._util") or sys.modules.get("urirun_node._util")
    if moved is not None:
        patched = getattr(moved, "quiet_completion", None)
        if patched is not None and patched is not quiet_completion:
            return patched(**kwargs)

    import litellm

    defaulted = "max_tokens" not in kwargs and "max_completion_tokens" not in kwargs
    if defaulted:
        kwargs = {**kwargs, "max_tokens": _default_max_tokens()}
    litellm.suppress_debug_info = True
    with contextlib.redirect_stdout(sys.stderr):
        try:
            return litellm.completion(**kwargs)
        except Exception as exc:
            current = int(kwargs.get("max_tokens") or 0)
            if not (defaulted and current > 1024 and _should_retry_with_fewer_tokens(exc)):
                raise
            return litellm.completion(**{**kwargs, "max_tokens": 1024})
