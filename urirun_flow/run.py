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
* ``degrade`` — id of an earlier step whose last-good result is served (re-tagged
  ``live=False``/``degraded=True``) when this step still fails, so a failing live widget
  falls back to the last frozen artifact and the chain stays alive on known-good data.
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
    succeeded, degraded, failed, skipped, first_error = [], [], [], [], None
    for sid, env in results.items():  # dict preserves flow (insertion) order
        if env.get("skipped"):
            skipped.append(sid)
        elif env.get("degraded"):
            degraded.append(sid)  # ran on last-good data — succeeded, but flagged stale
        elif envelope_ok(env):
            succeeded.append(sid)
        else:
            failed.append(sid)
            if first_error is None:
                first_error = {"step": sid, "uri": env.get("uri"),
                               "category": error_category(env), "error": env.get("error")}
    summary = {"ok": not failed, "steps": len(results), "succeeded": succeeded,
               "degraded": degraded, "failed": failed, "skipped": skipped, "firstError": first_error}
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


def _skip_step(step: Step, reason: str,
               results: dict[str, Any], status: dict[str, str]) -> None:
    results[step.id] = _skip_envelope(step, reason)
    status[step.id] = "skipped"


def _prereq_skip(step: Step, aborted: bool,
                 results: dict[str, Any], status: dict[str, str]) -> bool:
    """Record a skip envelope and return True when the step must not run."""
    if aborted:
        _skip_step(step, "flow aborted by an upstream step (catch=abort)", results, status)
        return True
    failed_deps = [d for d in step.depends_on if status.get(d) != "ok"]
    if failed_deps:
        _skip_step(step, f"skipped: dependencies did not succeed: {failed_deps}", results, status)
        return True
    return False


def _resolve_payload(step: Step, results: dict[str, Any],
                     status: dict[str, str], mesh: Any) -> tuple[dict | None, bool]:
    """Return (payload, ok). On resolution error records a skip and returns (None, False)."""
    try:
        return mesh.resolve_step_payload(step.payload or {}, results), True
    except Exception as exc:  # noqa: BLE001 - dangling chain ref must skip, not crash
        _skip_step(step, f"skipped: could not resolve payload ({exc})", results, status)
        return None, False


def _apply_degrade(step: Step, env: dict, ok: bool,
                   results: dict[str, Any], status: dict[str, str]) -> tuple[dict, bool]:
    """Return (env, ok) after applying the degrade policy (last-good result fallback)."""
    if not ok and step.degrade and status.get(step.degrade) == "ok":
        good = results[step.degrade]
        env = {"uri": step.uri, "ok": True, "degraded": True, "degradedFrom": step.degrade,
               "primaryError": env.get("error"), "result": good.get("result"),
               "kind": "degraded", "live": False}
        ok = True
    return env, ok


def _flow_policy(data: dict, allow: list[str] | None, secret_allow: list[str] | None,
                 runtime: Any) -> dict:
    allow_list = list(allow if allow is not None else (data.get("allow") or []))
    secret_list = list(secret_allow if secret_allow is not None else (data.get("secretAllow") or []))
    return runtime.build_policy(None, allow_list, None, secret_list)


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
    policy = _flow_policy(data, allow, secret_allow, _runtime)
    mode = "execute" if execute else "dry-run"

    results: dict[str, Any] = {}
    status: dict[str, str] = {}
    aborted = False

    for step in flow.order():
        if _prereq_skip(step, aborted, results, status):
            continue

        payload, resolved = _resolve_payload(step, results, status, mesh)
        if not resolved:
            continue

        step_policy = policy if not step.timeout_ms else {**policy, "timeout": step.timeout_ms / 1000.0}
        env = resolve_step(step, payload, lambda uri, pl, _p=step_policy: v2.run(uri, registry, pl, mode=mode, policy=_p))
        env, ok = _apply_degrade(step, env, envelope_ok(env), results, status)

        results[step.id] = env
        status[step.id] = "ok" if ok else "failed"
        catch = step.catch or "continue"
        if not ok and catch == "abort":
            aborted = True

    return results
