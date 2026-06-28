"""Flow verification helpers.

After a flow executes, these functions check whether the intended goal state
was reached — distinct from step-level success. A flow where every step returned
ok but the intended end-condition was never achieved fails here.

All functions are pure (no side effects) or call only an injected dispatch
callable, making them testable without a running mesh.
"""
from __future__ import annotations

from typing import Any

from urirun import result_data


def _flow_stdout(envelope: dict) -> str:
    result = envelope.get("result")
    if not isinstance(result, dict):
        result = (envelope.get("response") or {}).get("result")
    stdout = (result or {}).get("stdout") if isinstance(result, dict) else ""
    return stdout if isinstance(stdout, str) else ""


def _run_goal_check(goal: dict, dispatch) -> tuple[bool, dict]:
    """Verify the GOAL STATE after a flow — the end-condition the task was FOR, not whether each
    step returned ok. Calls ``goal['uri']`` (a state signature: a CDP eval of location/DOM, an
    OCR verify, a file check…), pulls a value at the dotted ``path``, and asserts
    ``contains``/``equals``/``present``. This is what closes the "every step green, nothing
    achieved" gap — clicked 'Post' ok, but is the post actually on the feed?"""
    try:
        env = dispatch(goal["uri"], goal.get("payload") or {})
    except Exception as exc:  # noqa: BLE001
        return False, {"error": str(exc)[:160]}
    val = result_data(env) if isinstance(env, dict) else None
    actual = _dig_value(val, goal.get("path"))
    env_ok = bool(isinstance(env, dict) and env.get("ok"))
    passed = _goal_passed(env_ok, actual, goal)
    return passed, {"actual": str(actual)[:160] if actual is not None else None}


def _dig_value(val: Any, path: str | None) -> Any:
    """Pull a dotted ``path`` out of a nested dict (``a.b.c``); stops at the first non-dict."""
    actual = val
    for key in str(path or "").split("."):
        if key and isinstance(actual, dict):
            actual = actual.get(key)
    return actual


def _goal_passed(env_ok: bool, actual: Any, goal: dict) -> bool:
    """Assert the goal post-condition: contains / equals / present, else plain transport ok."""
    if "contains" in goal:
        return env_ok and str(goal["contains"]) in str(actual or "")
    if "equals" in goal:
        return env_ok and str(actual) == str(goal["equals"])
    if goal.get("present"):
        return env_ok and actual not in (None, "", [], {})
    return env_ok


def _verify_log_fragment_check(spec: dict, execution: dict, executed: bool) -> tuple[bool | None, dict]:
    """Check `expected_log_fragment`; returns (passed, entry) or (None, {}) when the spec is absent."""
    fragment = spec.get("expected_log_fragment")
    step_id = spec.get("read_back_step")
    if not fragment or not step_id:
        return None, {}
    if not executed:
        return True, {"check": "expected_log_fragment", "ok": True, "skipped": "dry-run"}
    stdout = _flow_stdout((execution.get("results") or {}).get(step_id) or {})
    passed = str(fragment) in stdout
    return passed, {"check": "expected_log_fragment", "step": step_id, "ok": passed}


def _verify_goal_check(spec: dict, executed: bool, dispatch) -> tuple[bool | None, dict]:
    """Check the `goal` end-state spec; returns (passed, entry) or (None, {}) when not applicable."""
    goal = spec.get("goal")
    if not isinstance(goal, dict) or not goal.get("uri"):
        return None, {}
    if not executed:
        return True, {"check": "goal", "ok": True, "skipped": "dry-run"}
    if dispatch is None:
        return True, {"check": "goal", "ok": True, "skipped": "no-dispatch"}
    passed, detail = _run_goal_check(goal, dispatch)
    return passed, {"check": "goal", "uri": goal["uri"], "ok": passed, **detail}


def verify_flow_execution(document: dict, execution: dict, *, executed: bool, dispatch=None) -> dict | None:
    spec = document.get("verification")
    if not isinstance(spec, dict):
        return None
    checks: list[dict] = []
    ok = True
    if spec.get("require_ok", True):
        passed = bool(execution.get("ok"))
        checks.append({"check": "require_ok", "ok": passed})
        ok = ok and passed
    frag_passed, frag_entry = _verify_log_fragment_check(spec, execution, executed)
    if frag_entry:
        checks.append(frag_entry)
        if frag_passed is not None:
            ok = ok and frag_passed
    # GOAL-VERIFY: did the flow reach its goal STATE, not just run green steps? A flow can pass
    # every step yet achieve nothing (a click that missed); a goal check on the end-state fails
    # the flow honestly even when all steps were ok.
    goal_passed, goal_entry = _verify_goal_check(spec, executed, dispatch)
    if goal_entry:
        checks.append(goal_entry)
        if goal_passed is not None:
            ok = ok and goal_passed
    return {"ok": ok, "checks": checks}
