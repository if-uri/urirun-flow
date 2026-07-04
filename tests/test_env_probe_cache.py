from __future__ import annotations

import pytest

from urirun_flow import _env_probe_cache as cache


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


def test_cacheable_only_for_stable_parameterless_probes():
    assert cache.cacheable("kvm://host/env/query/profile") is True
    assert cache.cacheable("kvm://lenovo/display/query/info") is True
    # volatile probes never cache: a flow may launch a browser mid-run
    assert cache.cacheable("kvm://host/browser/query/sessions") is False
    assert cache.cacheable("kvm://host/window/query/list") is False
    assert cache.cacheable("kvm://host/surface/query/current") is False
    # a payload parameterizes the probe — not cacheable
    assert cache.cacheable("kvm://host/env/query/profile", {"deep": True}) is False


def test_get_returns_copy_within_ttl():
    cache.put("kvm://host/env/query/profile", {"ok": True, "monitors": [{"id": 1}]})
    hit = cache.get("kvm://host/env/query/profile")
    assert hit == {"ok": True, "monitors": [{"id": 1}]}
    hit["monitors"].append({"id": 99})  # caller mutation must not poison the cache
    assert cache.get("kvm://host/env/query/profile") == {"ok": True, "monitors": [{"id": 1}]}


def test_expired_entry_misses(monkeypatch):
    cache.put("kvm://host/env/query/profile", {"ok": True})
    import time
    real = time.monotonic()
    monkeypatch.setattr(cache.time, "monotonic", lambda: real + cache._TTL_S + 1)
    assert cache.get("kvm://host/env/query/profile") is None


def test_empty_or_non_dict_values_not_cached():
    cache.put("kvm://host/env/query/profile", {})
    assert cache.get("kvm://host/env/query/profile") is None


def test_call_route_value_uses_cache(monkeypatch):
    from urirun_flow import flow as F
    calls = {"n": 0}

    def fake_call(uri, payload, registry, mode="execute"):
        calls["n"] += 1
        return {"ok": True, "result": {"value": {"ok": True, "platform": "linux-wayland"}}}

    monkeypatch.setattr(F.v2_service, "call", fake_call)
    r1 = F._call_route_value("kvm://host/env/query/profile", {}, {})
    r2 = F._call_route_value("kvm://host/env/query/profile", {}, {})
    assert r1 == r2
    assert calls["n"] == 1  # second call served from cache
    # volatile probe is dispatched every time
    F._call_route_value("kvm://host/browser/query/sessions", {}, {})
    F._call_route_value("kvm://host/browser/query/sessions", {}, {})
    assert calls["n"] == 3
