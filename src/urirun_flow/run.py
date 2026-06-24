"""Execute a typed flow through urirun — the same semantics as the YAML runner, so a
typed flow runs identically to its YAML form. Requires `urirun` to be installed.

    from urirun_flow import Flow
    from urirun_flow.run import run_flow
    results = run_flow(flow, base_dir=".", execute=True)

Resilience (per-step, all optional — a flow with none behaves as before, only it no longer
crashes when a dependent references a failed step):

* ``retry`` ``{max, backoff_ms, on}`` — re-run the step while it fails with a RETRYABLE error
  category (UNAVAILABLE / DEADLINE_EXCEEDED / RESOURCE_EXHAUSTED / ABORTED by default).
* ``fallback`` — an alternative URI (same payload) tried once after retries are exhausted.
* ``catch`` — ``"continue"`` (default: dependents skip) or ``"abort"`` (stop the flow).
* an ``assertion`` step that does not pass gates its dependents (they skip).

The return shape is unchanged — ``{step_id: envelope}`` — with skipped steps carrying a
synthetic ``{ok: False, skipped: True}`` envelope and run steps annotated with ``attempts`` /
``fallbackUsed``.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from . import Flow, Step

# gRPC categories worth retrying — a transient/dependency failure, not a bad request.
RETRYABLE = {"UNAVAILABLE", "DEADLINE_EXCEEDED", "RESOURCE_EXHAUSTED", "ABORTED"}


def _result_value(env: dict) -> Any:
    """The connector's own payload inside a run envelope (mirrors urirun.result_data)."""
    result = env.get("result")
    if isinstance(result, dict) and isinstance(result.get("value"), dict):
        return result["value"]
    return result


def envelope_ok(env: dict | None) -> bool:
    """An envelope succeeded only if the run AND the connector's own ``ok`` are truthy."""
    if not env or not env.get("ok"):
        return False
    for candidate in (_result_value(env), env.get("result")):
        if isinstance(candidate, dict) and candidate.get("ok") is False:
            return False
    return True


def error_category(env: dict | None) -> str | None:
    """The error category of a failed envelope — explicit if stamped, else classified from
    the error type/message (so retry decisions work even on un-stamped envelopes)."""
    err = (env or {}).get("error") or {}
    if not err:
        return None
    if err.get("category"):
        return err["category"]
    try:  # classify lazily; never let observability break execution
        from urirun.runtime import errors
        return errors.classify(str(err.get("type") or ""), str(err.get("message") or ""))
    except Exception:  # noqa: BLE001
        return None


def _skip_envelope(step: Step, reason: str) -> dict:
    return {"uri": step.uri, "ok": False, "skipped": True,
            "error": {"type": "dependency", "category": "FAILED_PRECONDITION", "message": reason}}


def flow_summary(results: dict[str, Any]) -> dict[str, Any]:
    """Roll up a ``{step_id: envelope}`` result into a partial-result summary so a broken chain
    SURFACES instead of vanishing: which steps succeeded / failed / were skipped, and the first
    error. Tagged via the shared artifact/widget contract (``urirun.tag``) as a frozen
    ``flow-failure`` artifact when anything failed (else ``flow-result``), so the dashboard can
    render it and ``error://`` / a ticket can pick it up. Pure — pass it the run_flow output."""
    succeeded, failed, skipped, first_error = [], [], [], None
    for sid, env in results.items():  # dict preserves flow (insertion) order
        if env.get("skipped"):
            skipped.append(sid)
        elif envelope_ok(env):
            succeeded.append(sid)
        else:
            failed.append(sid)
            if first_error is None:
                first_error = {"step": sid, "uri": env.get("uri"),
                               "category": error_category(env), "error": env.get("error")}
    summary = {"ok": not failed, "steps": len(results), "succeeded": succeeded,
               "failed": failed, "skipped": skipped, "firstError": first_error}
    kind = "flow-failure" if failed else "flow-result"
    try:  # use the shared contract when urirun is importable; degrade to inline fields otherwise
        import urirun
        return urirun.tag(summary, kind, live=False)
    except Exception:  # noqa: BLE001
        summary["kind"], summary["live"] = kind, False
        return summary


def resolve_step(step: Step, payload: dict, run_call: Callable[[str, dict], dict], *,
                 retryable: set[str] = RETRYABLE, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Run one step with its retry/fallback/assertion policy and return the final envelope.

    ``run_call(uri, payload)`` executes one URI and returns a run envelope. Pure w.r.t. the
    flow graph — the caller resolves ``payload`` and decides what to do with the result — so
    it is unit-testable with a scripted ``run_call``.
    """
    retry = step.retry or {}
    max_retries = max(0, int(retry.get("max", 0)))
    on = set(retry.get("on") or retryable)
    backoff_ms = int(retry.get("backoff_ms", 0))

    env = run_call(step.uri, payload)
    attempts = 1
    while not envelope_ok(env) and attempts <= max_retries and error_category(env) in on:
        if backoff_ms:
            sleep(backoff_ms / 1000.0)
        env = run_call(step.uri, payload)
        attempts += 1
    env = dict(env)
    env["attempts"] = attempts

    if not envelope_ok(env) and step.fallback:
        primary_error = env.get("error")
        fb = dict(run_call(step.fallback, payload))
        fb["fallbackUsed"] = True
        fb["fallbackFor"] = step.uri
        if primary_error is not None:
            fb["primaryError"] = primary_error
        env = fb

    # An assertion step gates its dependents: it "passes" only if it ran ok and its result
    # does not explicitly say passed=False / ok=False.
    if step.kind == "assertion" and env.get("ok"):
        val = _result_value(env)
        passed = not (isinstance(val, dict) and val.get("passed") is False)
        if not passed:
            env = {**env, "ok": False,
                   "error": {"type": "assertion", "category": "FAILED_PRECONDITION",
                             "message": f"assertion {step.id!r} did not pass"}}
    return env


def run_flow(flow: Flow, base_dir: str | Path = ".", *, execute: bool = False,
             allow: list[str] | None = None, secret_allow: list[str] | None = None) -> dict[str, Any]:
    """Run each step in dependency order, chaining `<key>_from` references through prior
    results, gated by the flow's `allow` policy and each step's resilience policy. Returns
    {step_id: envelope}. A step whose dependency failed/was skipped is skipped (not crashed)."""
    try:
        from urirun import v2, _runtime
        from urirun.node import mesh
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("`urirun-flow run` needs urirun installed: pip install urirun") from exc

    data = flow.to_dict()
    if not data.get("registry"):
        raise ValueError("flow has no `registry`; set Flow(registry=...) to run it")
    registry = v2.load_registry_arg(str(Path(base_dir) / data["registry"]))
    policy = _runtime.build_policy(
        None,
        list(allow or data.get("allow") or []),
        None,
        list(secret_allow or data.get("secretAllow") or []),
    )
    mode = "execute" if execute else "dry-run"

    results: dict[str, Any] = {}
    status: dict[str, str] = {}  # step id -> "ok" | "failed" | "skipped"
    aborted = False

    for step in flow.order():
        if aborted:
            results[step.id] = _skip_envelope(step, "flow aborted by an upstream step (catch=abort)")
            status[step.id] = "skipped"
            continue

        failed_deps = [d for d in step.depends_on if status.get(d) != "ok"]
        if failed_deps:
            results[step.id] = _skip_envelope(step, f"skipped: dependencies did not succeed: {failed_deps}")
            status[step.id] = "skipped"
            continue

        try:
            payload = mesh.resolve_step_payload(step.payload or {}, results)
        except Exception as exc:  # noqa: BLE001 - a dangling chain ref must skip, not crash
            results[step.id] = _skip_envelope(step, f"skipped: could not resolve payload ({exc})")
            status[step.id] = "skipped"
            continue

        step_policy = policy if not step.timeout_ms else {**policy, "timeout": step.timeout_ms / 1000.0}

        def run_call(uri: str, pl: dict, _pol: dict = step_policy) -> dict:
            return v2.run(uri, registry, pl, mode=mode, policy=_pol)

        env = resolve_step(step, payload, run_call)
        results[step.id] = env
        ok = envelope_ok(env)
        status[step.id] = "ok" if ok else "failed"
        if not ok and (step.catch or "continue") == "abort":
            aborted = True

    return results
