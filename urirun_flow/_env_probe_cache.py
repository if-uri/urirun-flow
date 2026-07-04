"""Short-TTL, process-local cache for read-only environment probes.

One chat prompt probes the same environment 4-6 times: planner environments,
env-enum inventory resolution, the twin drift step, the inventory step and the
remember step each re-dispatch ``kvm://{node}/env/query/profile`` (and
``display/query/info``) — at ~0.4-0.5 s per dispatched probe that is seconds of
duplicated wall-clock per prompt.

Only STABLE, read-only probes are cacheable: monitor topology and display
geometry don't change mid-request. Volatile probes (browser sessions, window
list, current surface) are deliberately NOT cacheable — a flow may launch a
browser and immediately need a fresh session list.

The TTL bounds staleness: monitor hotplug is picked up at most ``_TTL_S``
seconds late, which is also the worst-case delay for drift detection between
two rapid prompts.
"""
from __future__ import annotations

import copy
import time

_TTL_S = 10.0
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHEABLE_SUFFIXES = ("/env/query/profile", "/display/query/info")


def cacheable(uri: str, payload: dict | None = None) -> bool:
    """True for parameterless probes of stable environment facts."""
    return not payload and any(uri.endswith(s) for s in _CACHEABLE_SUFFIXES)


def get(uri: str) -> dict | None:
    hit = _CACHE.get(uri)
    if hit and (time.monotonic() - hit[0]) < _TTL_S:
        return copy.deepcopy(hit[1])
    return None


def put(uri: str, value: dict) -> None:
    if isinstance(value, dict) and value:
        _CACHE[uri] = (time.monotonic(), copy.deepcopy(value))


def clear() -> None:
    """Drop all cached probes (tests; explicit invalidation after env mutation)."""
    _CACHE.clear()
