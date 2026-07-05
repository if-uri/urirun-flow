# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.
#
"""Z6 latency: the remember step reuses the drift probe (short-TTL cache) when the flow
was PROVABLY read-only (every step a /query/); any /command/ forces a live post-flow
probe because the flow itself may have provisioned the environment."""
from __future__ import annotations
import pytest as _pytest_guard  # noqa: E402
_pytest_guard.importorskip("jsonschema")  # integration tests need the full urirun runtime (not in the isolated package-test env)

import urirun_flow.flow as F
from urirun_flow import _env_probe_cache


class FakeMemory:
    def __init__(self):
        self.remembered = {}

    def remember(self, node, profile):
        self.remembered[node] = profile


def test_flow_env_stable_true_for_query_only_steps():
    steps = [{"uri": "kvm://host/screen/query/capture"},
             {"uri": "kvm://host/display/query/info"}]
    assert F._flow_env_stable(steps) is True


def test_flow_env_stable_false_when_any_command_present():
    steps = [{"uri": "kvm://host/screen/query/capture"},
             {"uri": "kvm://host/cdp/session/command/ensure"}]
    assert F._flow_env_stable(steps) is False


def test_remember_env_stable_reuses_cached_probe(monkeypatch):
    _env_probe_cache.clear()
    _env_probe_cache.put("kvm://host/env/query/profile", {"best": "cdp", "wayland": True})
    calls = []
    monkeypatch.setattr(F.v2_service, "call",
                        lambda *a, **k: calls.append(a) or {"result": {"value": {}}})
    mem = FakeMemory()

    F._remember_node_profile(mem, "host", {}, env_stable=True)

    assert mem.remembered["host"] == {"best": "cdp", "wayland": True}
    assert calls == []                       # no dispatch — the drift probe was reused


def test_remember_live_probe_when_env_not_stable(monkeypatch):
    _env_probe_cache.clear()
    _env_probe_cache.put("kvm://host/env/query/profile", {"best": "stale"})
    live = {"best": "cdp", "cdpReady": True}
    monkeypatch.setattr(F.v2_service, "call",
                        lambda *a, **k: {"result": {"value": dict(live)}})
    mem = FakeMemory()

    F._remember_node_profile(mem, "host", {}, env_stable=False)

    assert mem.remembered["host"] == live    # live probe wins, cache ignored
    # and the live value refreshed the cache for followers
    assert _env_probe_cache.get("kvm://host/env/query/profile") == live


def test_remember_env_stable_falls_back_to_live_on_cold_cache(monkeypatch):
    _env_probe_cache.clear()
    live = {"best": "atspi"}
    monkeypatch.setattr(F.v2_service, "call",
                        lambda *a, **k: {"result": {"value": dict(live)}})
    mem = FakeMemory()

    F._remember_node_profile(mem, "host", {}, env_stable=True)

    assert mem.remembered["host"] == live    # TTL expired mid-flow → honest live probe
