"""Per-step resilience: retry (by error category), fallback URI, assertion gating, and a flow
that skips a failed step's dependents instead of crashing.

The resolve_step tests are pure (a scripted run_call, no urirun). The run_flow tests exercise
the real runtime with a tiny argv registry.
"""
import json

import pytest

from urirun_flow import Flow, Step
from urirun_flow.run import resolve_step, run_flow, flow_summary, envelope_ok, error_category

_NOSLEEP = lambda _s: None


def _okenv():
    return {"ok": True, "result": {"value": {"ok": True}}}


def _failenv(category):
    return {"ok": False, "error": {"type": "x", "category": category, "message": "boom"}}


# --- resolve_step (pure) --------------------------------------------------------------------

def test_retry_retryable_then_succeeds():
    calls = []

    def run_call(uri, pl):
        calls.append(uri)
        return _okenv() if len(calls) >= 3 else _failenv("UNAVAILABLE")

    step = Step(id="s", uri="demo://h/x/command/run", retry={"max": 3, "backoff_ms": 0})
    env = resolve_step(step, {}, run_call, sleep=_NOSLEEP)
    assert envelope_ok(env) and env["attempts"] == 3 and len(calls) == 3


def test_terminal_error_not_retried():
    calls = []

    def run_call(uri, pl):
        calls.append(uri)
        return _failenv("NOT_FOUND")  # terminal — must not retry

    step = Step(id="s", uri="demo://h/x/command/run", retry={"max": 5, "backoff_ms": 0})
    env = resolve_step(step, {}, run_call, sleep=_NOSLEEP)
    assert not envelope_ok(env) and env["attempts"] == 1 and len(calls) == 1


def test_fallback_used_when_primary_fails():
    def run_call(uri, pl):
        return _failenv("NOT_FOUND") if "primary" in uri else _okenv()

    step = Step(id="s", uri="demo://h/primary/command/run", fallback="demo://h/backup/command/run")
    env = resolve_step(step, {}, run_call, sleep=_NOSLEEP)
    assert envelope_ok(env) and env["fallbackUsed"] and env["fallbackFor"].endswith("/primary/command/run")
    assert env["primaryError"]["category"] == "NOT_FOUND"


def test_retry_exhausted_then_fallback():
    calls = []

    def run_call(uri, pl):
        calls.append(uri)
        return _failenv("UNAVAILABLE") if "primary" in uri else _okenv()

    step = Step(id="s", uri="demo://h/primary/command/run", retry={"max": 2, "backoff_ms": 0},
                fallback="demo://h/backup/command/run")
    env = resolve_step(step, {}, run_call, sleep=_NOSLEEP)
    primary_calls = [u for u in calls if "primary" in u]
    assert len(primary_calls) == 3  # 1 + 2 retries
    assert envelope_ok(env) and env["fallbackUsed"]


def test_assertion_failure_marks_step_failed():
    def run_call(uri, pl):
        return {"ok": True, "result": {"value": {"ok": True, "passed": False}}}

    step = Step(id="a", uri="check://h/x/assertion/passes")
    assert step.kind == "assertion"
    env = resolve_step(step, {}, run_call, sleep=_NOSLEEP)
    assert not envelope_ok(env) and env["error"]["type"] == "assertion"


def test_assertion_pass_stays_ok():
    def run_call(uri, pl):
        return {"ok": True, "result": {"value": {"ok": True, "passed": True}}}

    step = Step(id="a", uri="check://h/x/assertion/passes")
    env = resolve_step(step, {}, run_call, sleep=_NOSLEEP)
    assert envelope_ok(env)


def test_error_category_classifies_unstamped():
    # no explicit category -> classified from the exception type
    assert error_category({"error": {"type": "TimeoutError", "message": "timed out"}}) == "DEADLINE_EXCEEDED"


# --- flow_summary (surface a broken chain as a tagged artifact) -----------------------------

def test_flow_summary_partial_failure():
    results = {
        "a": _okenv(),
        "b": {"ok": False, "error": {"type": "x", "category": "UNAVAILABLE", "message": "down"}},
        "c": {"ok": False, "skipped": True, "error": {"message": "skipped: dependencies ..."}},
    }
    s = flow_summary(results)
    assert s["ok"] is False and s["steps"] == 3
    assert s["succeeded"] == ["a"] and s["failed"] == ["b"] and s["skipped"] == ["c"]
    assert s["firstError"]["step"] == "b" and s["firstError"]["category"] == "UNAVAILABLE"
    # surfaced as a frozen artifact via the shared contract
    assert s["kind"] == "flow-failure" and s["live"] is False


def test_flow_summary_all_ok():
    s = flow_summary({"a": _okenv(), "b": _okenv()})
    assert s["ok"] is True and not s["failed"] and not s["skipped"]
    assert s["kind"] == "flow-result" and s["live"] is False


# --- run_flow (real runtime) ----------------------------------------------------------------

pytest.importorskip("urirun")


def _registry(tmp_path):
    from urirun import v2
    doc = {"version": v2.VERSION, "bindings": {
        "demo://host/ok/command/run": {"uri": "demo://host/ok/command/run", "kind": "command",
            "adapter": "argv-template", "argv": ["true"], "policy": {"allowExecute": True}},
        "demo://host/fail/command/run": {"uri": "demo://host/fail/command/run", "kind": "command",
            "adapter": "argv-template", "argv": ["false"], "policy": {"allowExecute": True}},
        "demo://host/echo/command/say": {"uri": "demo://host/echo/command/say", "kind": "command",
            "adapter": "argv-template", "argv": ["printf", "%s", "ok"], "policy": {"allowExecute": True}}}}
    (tmp_path / "reg.json").write_text(json.dumps(v2.compile_registry(doc)))


def test_failed_step_skips_dependents_no_crash(tmp_path):
    _registry(tmp_path)
    flow = Flow(registry="reg.json", allow=["demo://*"])
    a = flow.step("demo://host/fail/command/run", id="a")
    flow.step("demo://host/echo/command/say", id="b", after=[a])
    results = run_flow(flow, tmp_path, execute=True)  # must not raise
    assert results["a"]["ok"] is False
    assert results["b"].get("skipped") is True
    assert "a" in results["b"]["error"]["message"]


def test_step_fallback_in_flow(tmp_path):
    _registry(tmp_path)
    flow = Flow(registry="reg.json", allow=["demo://*"])
    flow.step("demo://host/fail/command/run", id="a", fallback="demo://host/ok/command/run")
    results = run_flow(flow, tmp_path, execute=True)
    assert results["a"]["ok"] is True and results["a"].get("fallbackUsed") is True


def test_catch_abort_stops_flow(tmp_path):
    _registry(tmp_path)
    flow = Flow(registry="reg.json", allow=["demo://*"])
    flow.step("demo://host/fail/command/run", id="a", catch="abort")
    flow.step("demo://host/ok/command/run", id="b")  # independent, but flow aborts before it
    results = run_flow(flow, tmp_path, execute=True)
    assert results["a"]["ok"] is False
    assert results["b"].get("skipped") is True


def test_happy_flow_unchanged(tmp_path):
    # a flow with no resilience config and all-ok steps behaves exactly as before
    _registry(tmp_path)
    flow = Flow(registry="reg.json", allow=["demo://*"])
    a = flow.step("demo://host/ok/command/run", id="a")
    flow.step("demo://host/echo/command/say", id="b", after=[a])
    results = run_flow(flow, tmp_path, execute=True)
    assert results["a"]["ok"] and results["b"]["ok"]
