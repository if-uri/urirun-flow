"""Recovery helpers for URI flow execution.

This module deliberately does not execute connector-management actions. It turns
runtime failures into a stable recovery contract that callers can render, retry
safely, or hand to a higher-level manager.
"""
from __future__ import annotations

from typing import Any

from urirun_flow.diagnostics import diagnose
from urirun.node.routing import route_target
from urirun.runtime import errors as uri_errors


TRANSIENT_CATEGORIES = {"UNAVAILABLE", "DEADLINE_EXCEEDED", "ABORTED"}

# Only these remediation kinds may be fired UNATTENDED by the self-heal loop. They are
# idempotent provisioning / preconditions / safe retries / read-only discovery. A `payload`
# fix needs the caller to repair arguments and `auth` needs a human credential — firing those
# automatically would loop or act on the user's behalf, so they stay human-gated even if a
# rule mistakenly marks them automatic.
AUTO_REMEDIATION_KINDS = {"provision", "precondition", "retry", "discovery"}


def _infer_category(out: dict) -> str:
    message = str(out.get("message") or "").casefold()
    if "missing dependenc" in message or "urirun_llm_model" in message or "llm_model" in message:
        return "FAILED_PRECONDITION"
    return uri_errors.classify(str(out.get("type") or ""), str(out.get("message") or ""))


def normalize_error(error: Any, *, uri: str = "") -> dict:
    if isinstance(error, dict):
        out = dict(error)
    else:
        out = {"type": type(error).__name__, "message": str(error)}
    out.setdefault("type", "Error")
    out.setdefault("message", "")
    if not out.get("category"):
        out["category"] = _infer_category(out)
    status, severity, _ = uri_errors.category_meta(str(out.get("category") or "UNKNOWN"))
    out.setdefault("status", status)
    out.setdefault("severity", severity)
    scheme = uri.split("://", 1)[0] if "://" in uri else ""
    out.setdefault("code", uri_errors.error_code(str(out.get("type") or ""), str(out.get("message") or ""), scheme))
    out.setdefault("uri", uri_errors.address(str(out.get("code") or "")))
    out.setdefault("help", uri_errors.help_url(str(out.get("code") or ""), str(out.get("category") or "")))
    return out


def exception_error(exc: BaseException, *, uri: str = "") -> dict:
    return normalize_error({"type": type(exc).__name__, "message": str(exc)}, uri=uri)


def step_target(step: dict) -> str:
    try:
        return route_target(str(step.get("uri") or ""))
    except Exception:  # noqa: BLE001 - recovery must not mask the original failure.
        return ""


def route_for_step(step: dict, routes: list[dict]) -> dict:
    uri = str(step.get("uri") or "")
    for route in routes:
        if route.get("uri") == uri:
            return route
    return {}


def _llm_model_actions() -> list[dict]:
    return [{
        "id": "use-known-intent-or-configure-llm",
        "kind": "planner",
        "automatic": False,
        "label": "Use a deterministic intent when available, or set URIRUN_LLM_MODEL/LLM_MODEL.",
    }]


def _transient_actions(target: str) -> list[dict]:
    actions: list[dict] = []
    if target:
        actions.append({
            "id": "check-target-health",
            "kind": "diagnostic",
            "automatic": False,
            "uri": f"env://{target}/runtime/query/health",
            "label": f"Check whether target {target!r} is reachable.",
        })
    actions.append({
        "id": "retry-transient-step",
        "kind": "retry",
        "automatic": True,
        "label": "Retry once when the URI is a query or this is a dry-run.",
    })
    actions.append({
        "id": "refresh-discovery",
        "kind": "discovery",
        "automatic": False,
        "label": "Refresh node/service discovery before another attempt.",
    })
    return actions


def _cdp_page_ready_actions(step: dict, target: str) -> list[dict]:
    """Recovery for a ``cdp/page/query/ready`` (or other page-level CDP query) that timed out.

    A page-level query opens a WebSocket to the debug port; it times out when the session
    is mid launch (``ensure`` returned ``launching:true``) or the page is mid navigation.
    Retrying the page query blindly re-opens that WS to the same unbound port. The right
    first action is the launch/probe split's idempotent readiness poll
    (``cdp/session/query/ready``), then retry the page query."""
    actions: list[dict] = []
    if target:
        actions.append({
            "id": "poll-cdp-session-ready",
            "kind": "precondition",
            "automatic": True,
            "uri": f"kvm://{target}/cdp/session/query/ready",
            "label": f"Poll the CDP debug endpoint on {target!r} until it binds (does NOT re-launch).",
        })
    actions.append({
        "id": "retry-page-ready",
        "kind": "retry",
        "automatic": True,
        "label": "Retry the page-level query now that the session has bound the debug port.",
    })
    if target:
        actions.append({
            "id": "check-target-health",
            "kind": "diagnostic",
            "automatic": False,
            "uri": f"env://{target}/runtime/query/health",
            "label": f"If the probe keeps timing out, check whether target {target!r} is reachable.",
        })
    return actions


def _is_cdp_page_level_query(uri: str) -> bool:
    """A CDP URI that opens a WebSocket to the debug port's page target — i.e. anything
    under ``/cdp/page/`` except the session-level queries. These time out the same way a
    ``page/query/ready`` does when the session is mid launch."""
    return "/cdp/page/" in uri


def _not_found_actions(message: str, error: dict, scheme: str) -> list[dict]:
    if "route not found" in message or str(error.get("type") or "") == "registry":
        actions = [{
            "id": "refresh-routes",
            "kind": "discovery",
            "automatic": False,
            "label": "Refresh /routes and rebuild the registry.",
        }]
        if scheme:
            actions.append({
                "id": "resolve-connector",
                "kind": "provision",
                "automatic": False,
                "scheme": scheme,
                "uri": f"connector://host/{scheme}/query/resolve",
                "label": f"Resolve or install a connector that serves {scheme}://.",
            })
        return actions
    return [{
        "id": "mark-missing-resource",
        "kind": "data",
        "automatic": False,
        "label": "Mark the referenced file/artifact as missing and avoid embedding stale previews.",
    }]


def _fallback_actions(step: dict, routes: list[dict]) -> list[dict]:
    actions: list[dict] = []
    route = route_for_step(step, routes)
    if route:
        actions.append({
            "id": "inspect-route",
            "kind": "diagnostic",
            "automatic": False,
            "label": "Inspect the route schema, policy and adapter metadata.",
            "route": {"uri": route.get("uri"), "kind": route.get("kind"), "adapter": route.get("adapter")},
        })
    actions.append({
        "id": "inspect-error",
        "kind": "diagnostic",
        "automatic": False,
        "label": "Open the error:// help record and decide whether retry or provisioning is safe.",
    })
    return actions


# Categories whose recovery is a single fixed action with no contextual branching.
_STATIC_CATEGORY_ACTIONS: dict[str, dict] = {
    "UNAUTHENTICATED": {
        "id": "authorize-target",
        "kind": "auth",
        "automatic": False,
        "label": "Enroll or pass a valid run token/identity before retrying this URI.",
    },
    "PERMISSION_DENIED": {
        "id": "authorize-target",
        "kind": "auth",
        "automatic": False,
        "label": "Enroll or pass a valid run token/identity before retrying this URI.",
    },
    "INVALID_ARGUMENT": {
        "id": "repair-payload",
        "kind": "payload",
        "automatic": False,
        "label": "Compare the payload with the route inputSchema and repair missing or invalid fields.",
    },
    "FAILED_PRECONDITION": {
        "id": "prepare-precondition",
        "kind": "precondition",
        "automatic": False,
        "label": "Prepare the missing dependency, confirmation, or previous step output.",
    },
}


def _is_llm_model_error(message: str) -> bool:
    return "urirun_llm_model" in message or "llm_model" in message


def _is_cdp_deadline(category: str, uri: str) -> bool:
    return category == "DEADLINE_EXCEEDED" and _is_cdp_page_level_query(uri)


def _uri_scheme(uri: str) -> str:
    return uri.split("://", 1)[0] if "://" in uri else ""


def _dispatch_recovery(category: str, message: str, uri: str, step: dict, routes: list, error: dict) -> list[dict]:
    if _is_llm_model_error(message):
        return _llm_model_actions()
    if category in {"UNAVAILABLE", "DEADLINE_EXCEEDED"}:
        # A CDP page-level query that times out is the launch/probe split's signature
        # failure: ``ensure`` fired the launch (launching:true, port not yet bound) and
        # the page query raced ahead. The generic "retry the step" re-opens a WS to the
        # same unbound port; the right first action is the idempotent session-ready poll.
        if _is_cdp_deadline(category, uri):
            return _cdp_page_ready_actions(step, step_target(step))
        return _transient_actions(step_target(step))
    if category in _STATIC_CATEGORY_ACTIONS:
        return [dict(_STATIC_CATEGORY_ACTIONS[category])]
    if category == "NOT_FOUND":
        return _not_found_actions(message, error, _uri_scheme(uri))
    return _fallback_actions(step, routes)


def recovery_actions(error: dict, *, step: dict | None = None, routes: list[dict] | None = None) -> list[dict]:
    step = step or {}
    routes = routes or []
    return _dispatch_recovery(
        category=str(error.get("category") or ""),
        message=str(error.get("message") or "").casefold(),
        uri=str(step.get("uri") or ""),
        step=step,
        routes=routes,
        error=error,
    )


def failure_signature(error: dict) -> str:
    """A stable, LOW-CARDINALITY key for an error message, so unrecognized failure CLASSES can
    be counted (not every unique string). Strips URIs, paths, quoted literals and digits — what
    remains is the SHAPE of the failure, the unit a new playbook rule would key on."""
    import re as _re
    msg = str((error or {}).get("message") or "").casefold()
    msg = _re.sub(r"[a-z]+://\S+", "<uri>", msg)
    msg = _re.sub(r"(?<=\s)/\S+|^/\S+", "<path>", msg)
    msg = _re.sub(r"'[^']*'|\"[^\"]*\"", "<v>", msg)
    msg = _re.sub(r"\d+", "<n>", msg)
    msg = _re.sub(r"\s+", " ", msg).strip()
    return msg[:120] or "<empty>"


def recovery_plan(error: dict, *, step: dict | None = None, routes: list[dict] | None = None,
                  environment: dict | None = None, surface: dict | None = None) -> dict:
    actions = recovery_actions(error, step=step, routes=routes)
    plan = {
        "recoverable": bool(actions),
        "category": error.get("category"),
        "actions": actions,
    }
    # Experience-driven layer: name the root cause + a specific, partly auto-applicable fix,
    # fitted to the node's environment + foreground surface when those are supplied.
    diagnosis = diagnose(error, step=step, routes=routes, environment=environment, surface=surface)
    if diagnosis:
        plan["diagnosis"] = diagnosis
    else:
        # No playbook rule matched: a NEW failure class. Flag it + a low-cardinality signature so
        # it's COUNTABLE downstream (the unrecognized-signature counter that grows the playbook),
        # instead of being silently absorbed by the generic fallback actions.
        plan["unrecognized"] = True
        plan["signature"] = failure_signature(error)
    return plan


def _apply_one_remediation(action: dict, call, result_data_fn) -> dict:
    aid, uri = action["id"], action["uri"]
    if action.get("kind") not in AUTO_REMEDIATION_KINDS:
        # belt-and-suspenders: a payload/auth/diagnostic action must never auto-fire even
        # if a rule marked it automatic — those need a human, not an unattended retry loop.
        return {"id": aid, "uri": uri, "ok": False,
                "skipped": f"kind {action.get('kind')!r} not auto-applicable (needs a human)"}
    if action.get("feasible") is False:
        return {"id": aid, "uri": uri, "ok": False, "skipped": "infeasible"}
    try:
        env = call(uri)
        value = result_data_fn(env) if isinstance(env, dict) else None
        value_ok = not isinstance(value, dict) or value.get("ok", True)
        return {"id": aid, "uri": uri, "ok": bool(env.get("ok") and value_ok)}
    except Exception as exc:  # noqa: BLE001 - a failed fix must not crash the flow
        return {"id": aid, "uri": uri, "ok": False, "error": str(exc)}


def apply_auto_remediation(diagnosis: dict, registry: dict, *, dispatch=None) -> list[dict]:
    """Execute the ``automatic: True`` remediation URIs of a diagnosis (idempotent
    provisioning / preconditions like cdp/session/ensure, cdp/page/ready, registry adopt).
    Returns one ``{id, uri, ok, error?}`` per applied action. The step is retried by the
    caller AFTER this — so a diagnosed failure is actually FIXED, not just reported. The
    dispatch fn is injectable for testing (defaults to the URI service call)."""
    from urirun import result_data
    from urirun.runtime import v2_service
    call = dispatch or (lambda uri: v2_service.call(uri, {}, registry, mode="execute"))
    applied: list[dict] = []
    for action in diagnosis.get("remediation") or []:
        if not action.get("automatic") or not action.get("uri"):
            continue
        applied.append(_apply_one_remediation(action, call, result_data))
    return applied


def can_retry_step(error: dict, *, step: dict, routes: list[dict], execute: bool, attempt: int, max_retries: int) -> bool:
    if attempt >= max_retries:
        return False
    if str(error.get("category") or "") not in TRANSIENT_CATEGORIES:
        return False
    if not execute:
        return True
    route = route_for_step(step, routes)
    return str(route.get("kind") or "").lower() == "query"


def planner_failure(exc: BaseException, *, prompt: str, selected_nodes: list[str] | None = None,
                    selected_targets: list[str] | None = None) -> dict:
    error = exception_error(exc, uri="flow://host/planner/command/make")
    step = {"id": "plan", "uri": "flow://host/planner/command/make"}
    return {
        "ok": False,
        "prompt": prompt,
        "selectedNodes": selected_nodes or [],
        "selectedTargets": selected_targets or [],
        "flow": {"task": {"id": "planner-error", "title": "Planner failed"}, "steps": []},
        "timeline": [{
            "id": "plan",
            "uri": step["uri"],
            "target": "host",
            "ok": False,
            "error": error,
            "recovery": recovery_plan(error, step=step),
        }],
        "results": {},
        "error": error,
        "recovery": [{"stepId": "plan", "uri": step["uri"], "error": error, "plan": recovery_plan(error, step=step)}],
    }


# ── URI surface: fix://host/error/command/remediate ───────────────────────────
# Exposes `apply_auto_remediation` as an addressable URI capability.
# Human-gated remediation actions are SKIPPED here (the bus policy enforces the gate
# on `fix://` itself; this handler applies only the automatic subset).
def _uri_remediate(payload: dict) -> dict:
    """Handler for fix://<node>/error/command/remediate.

    Payload: {diagnosis, registry?}
    Returns: {ok, applied[{id,uri,ok}]}"""
    import urirun  # noqa: PLC0415
    diagnosis = payload.get("diagnosis") or {}
    registry = payload.get("registry") or {}
    dispatch = payload.get("_dispatch")  # injectable for tests
    applied = apply_auto_remediation(diagnosis, registry, dispatch=dispatch)
    healed_ok = any(a.get("ok") for a in applied)
    return urirun.ok(applied=applied, healedOk=healed_ok, count=len(applied))


try:
    import urirun as _urirun  # noqa: PLC0415
    _fix_conn = _urirun.connector("fix", scheme="fix")
    _fix_conn.handler("error/command/remediate", meta={"label": "Apply auto remediation actions"})(_uri_remediate)
except Exception:  # noqa: BLE001 - connector registration is optional
    pass
