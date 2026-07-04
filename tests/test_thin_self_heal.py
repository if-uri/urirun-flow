# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.
#
"""Self-heal in the THIN driver (Z3): a synthesized step failure gets exactly one
diagnose→remediate→retry before rollback — the thin twin of flow.py's _attempt_self_heal.
Uses the real PLAYBOOK rule 'cdp-debugger-down' (signature: 'debugger did not come up')
whose remediation is kvm://{node}/cdp/session/command/ensure (kind=provision, automatic)."""
from __future__ import annotations

from urirun_flow.flow_thin import FlowEnvelope, _thin_driver

CDP_FAIL = {"ok": False, "error": {"message": "debugger did not come up on port 9222"}}


class HealScriptDispatch:
    """Scripted dispatch: the step URI fails N times then succeeds; remediation and
    env-probe URIs are recorded and succeed."""

    def __init__(self, step_uri: str, fail_times: int = 1):
        self.step_uri = step_uri
        self.fail_times = fail_times
        self.calls: list[str] = []

    def __call__(self, uri, payload=None):
        self.calls.append(uri)
        if uri == self.step_uri:
            if self.fail_times > 0:
                self.fail_times -= 1
                return dict(CDP_FAIL)
            return {"ok": True, "next": {"kind": "continue"}}
        if "env/query/profile" in uri:
            return {"ok": True, "platform": "linux-wayland", "cdpFeasible": True}
        return {"ok": True}  # remediation / rollback / anything else


def test_synthesized_failure_heals_once_and_retries_to_green():
    d = HealScriptDispatch("kvm://host/cdp/page/command/navigate", fail_times=1)
    steps = [{"id": "nav", "uri": d.step_uri, "payload": {"url": "https://x"}}]
    env = FlowEnvelope(flow_id="heal-ok")

    result = _thin_driver(steps, env, d, {}, execute=True)

    assert result["ok"] is True
    ids = [e.get("id") for e in result["timeline"]]
    assert "nav:self-heal" in ids and "nav:retry" in ids
    heal = next(e for e in result["timeline"] if e.get("id") == "nav:self-heal")
    assert heal["rule"] == "cdp-debugger-down"
    assert any(a["ok"] and "cdp/session/command/ensure" in a["uri"] for a in heal["applied"])
    # remediation URI was actually dispatched, and the retry booked a remediation
    assert any("cdp/session/command/ensure" in c for c in d.calls)
    assert env.remediations_used == 1


def test_heal_happens_at_most_once_then_rolls_back():
    d = HealScriptDispatch("kvm://host/cdp/page/command/navigate", fail_times=99)
    steps = [{"id": "nav", "uri": d.step_uri}]

    result = _thin_driver(steps, FlowEnvelope(flow_id="heal-cap"), d, {}, execute=True)

    assert result["ok"] is False
    ids = [e.get("id") for e in result["timeline"]]
    assert ids.count("nav:self-heal") == 1        # exactly one heal attempt
    assert d.calls.count(d.step_uri) == 2         # original + one healed retry, no loop


def test_explicit_rollback_intent_is_not_healed():
    def dispatch(uri, payload=None):
        if "navigate" in uri:
            return {"ok": False, "next": {"kind": "rollback"},
                    "error": {"message": "debugger did not come up"}}
        return {"ok": True}

    steps = [{"id": "nav", "uri": "kvm://host/cdp/page/command/navigate"}]
    result = _thin_driver(steps, FlowEnvelope(flow_id="explicit-rb"), dispatch, {}, execute=True)

    assert result["ok"] is False
    assert not any(e.get("id") == "nav:self-heal" for e in result["timeline"])


def test_unrecognized_failure_rolls_back_without_heal():
    def dispatch(uri, payload=None):
        if "navigate" in uri:
            return {"ok": False, "error": {"message": "totally novel failure nobody diagnosed"}}
        return {"ok": True}

    steps = [{"id": "nav", "uri": "kvm://host/cdp/page/command/navigate"}]
    result = _thin_driver(steps, FlowEnvelope(flow_id="no-rule"), dispatch, {}, execute=True)

    assert result["ok"] is False
    assert not any(e.get("id") == "nav:self-heal" for e in result["timeline"])


# ---- in-core flow handlers must accept named kwargs (mesh/fallback dispatch) ----

def test_flow_handlers_accept_named_kwargs():
    """The binding runtime dispatches in-core handlers with NAMED kwargs, not a
    'payload' dict. Regression: signatures were def h(payload: dict) → compiled schema
    required a literal 'payload' property → fallback path failed 'payload is a required
    property'. Each handler must accept its fields as kwargs AND a positional dict."""
    import urirun_flow.flow as F

    # named-kwargs form (what mesh dispatch passes)
    r = F._uri_memory_remember(nodes=["host"], routes=[], env_stable=True, flow_key="k")
    assert r["ok"] and "profileSources" in r and r.get("nodes") == ["host"]
    d = F._uri_env_drift(node="host", routes=[])
    assert d["ok"] and "next" in d
    inv = F._uri_env_inventory(node="host", routes=[])
    assert inv["ok"]
    g = F._uri_goal_verify(goal={}, results={})
    assert isinstance(g, dict) and "ok" in g

    # positional dict form (tests / direct callers) still works
    r2 = F._uri_memory_remember({"nodes": ["host"], "routes": [], "flow_key": "k2"})
    assert r2["ok"]
