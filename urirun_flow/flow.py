# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.
#
# Natural-language flow planning and flow document execution helpers. Kept free
# of mesh.py server/CLI concerns so host automation can import this layer
# without loading the whole node HTTP stack.
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

from urirun import result_data
from urirun.runtime import v2_service
from urirun.node._util import json_write, now_id, slug
from urirun_flow.diagnostics import diagnose, fit_to_environment
from urirun_flow.flow_thin import (  # noqa: F401
    FlowEnvelope,
    _THIN_GOAL_URI,
    _THIN_PREFLIGHT_URI,
    _next_kind,
    _extract_inverse,
    _resolve_inverse_uri,
    _thin_circuit_break,
    _thin_rollback,
    _thin_handle_non_continue,
    _thin_update_ledger,
    _thin_step_entry,
    _thin_fold_inner_ok,
    _thin_goal_verify,
    _results_degraded,
    _enrich_remember_with_degraded,
    _thin_driver,
    _dig_path,
    resolve_step_payload,
    _action_ok,
    _action_error,
    _circuit_break,
)
from urirun.node.reversible import (
    CallableTransport,
    ReversibleProcess,
    Twin,
    TwinMemory,
    ledger_from_execution,
    parse as _rev_parse,
)
from urirun_flow.recovery import (
    apply_auto_remediation,
    can_retry_step,
    exception_error,
    normalize_error,
    recovery_plan,
    step_target,
)
from urirun.node.routing import (
    diagnose_plan as diagnose_routing_plan,
    execution_layers as routing_execution_layers,
    registry_from_routes,
    route_target,
    route_targets_for_nodes,
    safe_route,
    target_nodes,
)


def _flow_format(path: str | Path, requested: str | None = None) -> str:
    if requested:
        return requested
    return "json" if Path(path).suffix.lower() == ".json" else "yaml"


def flow_document(flow: dict, *, prompt: str | None = None, generator: dict | None = None) -> dict:
    """Wrap a normalized flow with portable metadata for YAML/JSON storage."""
    source = {"generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if prompt is not None:
        source["nl"] = prompt
    if generator is not None:
        source["generator"] = generator
    return {"version": "urirun.flow.v1", "source": source, **flow}


def write_flow_document(path: str | Path, document: dict, fmt: str | None = None) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if _flow_format(output, fmt) == "json":
        json_write(output, document)
        return
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional PyYAML.
        raise RuntimeError("PyYAML is required to write YAML flow files; use --flow-format json") from exc
    output.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")


def load_flow_document(path: str | Path) -> dict:
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    if source.suffix.lower() == ".json":
        doc = json.loads(text)
    else:
        try:
            import yaml
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional PyYAML.
            raise RuntimeError("PyYAML is required to read YAML flow files") from exc
        doc = yaml.safe_load(text)
    if not isinstance(doc, dict) or not isinstance(doc.get("steps"), list):
        raise ValueError(f"invalid flow document: {source}")
    return doc


from .flow_planner import (
    _DEFAULT_LOG_LIMIT,
    _PROCESS_LIST_LIMIT,
    _INTENT_NAMES,
    _CDP_ENSURE_SUFFIX,
    _CDP_READY_SUFFIX,
    _CDP_PAGE_PREFIX,
    first_url,
    nl_key,
    append_if_available,
    requested_folder_path,
    _flow_intents_llm,
    _flow_intents,
    _append_target_steps,
    heuristic_flow,
    json_from_text,
    _uri_segments,
    _uri_matches_template,
    _uri_is_available,
    _infeasibility_error,
    _step_is_infeasible,
    _normalize_flow_step,
    _normalize_flow_task,
    _needs_session_ready_after_ensure,
    _inject_cdp_ready_probes,
    _collect_infeasible_constraints,
    normalize_flow,
    normalize_flow_or_explain,
    llm_flow,
    _build_session_map,
    _append_session_guidance,
    fetch_planner_environments,
    make_flow,
    _fetch_kvm_query,
    _fetch_env_profile,
    _fetch_surface,
)
from .flow_verify import (  # noqa: F401
    _flow_stdout, _run_goal_check, _dig_value, _goal_passed,
    _verify_log_fragment_check, _verify_goal_check, verify_flow_execution,
)


def _flow_step_failure(step: dict, exc: BaseException, routes: list[dict], environment: dict | None = None) -> dict:
    error = exception_error(exc, uri=str(step.get("uri") or ""))
    return {
        "id": step.get("id"),
        "uri": step.get("uri"),
        "target": step_target(step),
        "ok": False,
        "error": error,
        "recovery": recovery_plan(error, step=step, routes=routes, environment=environment),
    }


def _flow_timeline_entry(step: dict, env: dict, routes: list[dict], *, attempt: int = 0,
                         environment: dict | None = None) -> dict:
    ok = _action_ok(env)
    uri = step["uri"]
    entry = {
        "id": step["id"],
        "uri": uri,
        "target": route_target(uri),
        "ok": ok,
    }
    if attempt:
        entry["attempt"] = attempt + 1
    if not ok:
        raw = env.get("error") or _action_error(env)
        error = exception_error(Exception("unknown URI error"), uri=uri) if not raw else normalize_error(raw, uri=uri)
        entry["error"] = error
        entry["recovery"] = recovery_plan(error, step=step, routes=routes, environment=environment)
    # Read-only steps (/query/) are trivially reversible; command steps without an explicit inverse are not.
    entry["reversible"] = "/query/" in uri
    return entry




def _self_heal_via_uri(step: dict, entry: dict, routes: list, env_profile: dict | None,
                       surface: dict | None, registry: dict, dispatch_uri,
                       diagnosis: dict) -> tuple[dict, list[dict]]:
    """Classify + remediate through dispatch_uri (observable URI bus path)."""
    node = route_target(step.get("uri") or "") or "host"
    cls = dispatch_uri(
        f"diag://{node}/error/command/classify",
        {"error": entry["error"], "step": step, "routes": routes,
         "environment": env_profile, "surface": surface},
    ) or {}
    if cls.get("ok") and cls.get("diagnosis"):
        diagnosis = cls["diagnosis"]
    elif env_profile:
        diagnosis = fit_to_environment(diagnosis, env_profile)
    rem = dispatch_uri(
        f"fix://{node}/error/command/remediate",
        {"diagnosis": diagnosis, "registry": registry},
    ) or {}
    return diagnosis, rem.get("applied") or []


def _attempt_self_heal(
    step: dict,
    entry: dict,
    registry: dict,
    routes: list[dict],
    *,
    dispatch_uri=None,
) -> tuple[dict | None, bool]:
    """Re-diagnose with the node's LIVE capabilities + foreground surface, apply the auto
    remediation ONCE, and return (self-heal timeline entry, healed_ok). Re-contextualising avoids
    futile round-trips: a CDP fix where no Chrome exists, an OCR retry where no tesseract, or
    looping ensure-CDP against a LOGIN page (really not-logged-in). (None, False) when there is
    nothing auto-applicable to try.

    `dispatch_uri` is the seam: when provided it routes every capability call (diag://, fix://)
    through a bus or stub (e.g. in tests), making the diagnosis visible in the event stream.
    When None the function falls back to direct in-process calls (identical behaviour, keeps all
    existing tests green without modification)."""
    diagnosis = (entry.get("recovery") or {}).get("diagnosis")
    if not (diagnosis and diagnosis.get("autoApplicable")):
        return None, False
    env_profile = _fetch_env_profile(step, registry)
    surface = _fetch_surface(step, registry)
    if dispatch_uri is not None:
        diagnosis, applied = _self_heal_via_uri(
            step, entry, routes, env_profile, surface, registry, dispatch_uri, diagnosis)
    else:
        recontext = diagnose(entry["error"], step=step, routes=routes,
                             environment=env_profile, surface=surface)
        if recontext:
            diagnosis = recontext
        elif env_profile:
            diagnosis = fit_to_environment(diagnosis, env_profile)
        applied = apply_auto_remediation(diagnosis, registry)
    healed_ok = any(a.get("ok") for a in applied)
    heal_entry = {"id": f"{step['id']}:self-heal", "uri": step["uri"],
                  "target": route_target(step["uri"]), "ok": healed_ok, "type": "recovery",
                  "action": "self-heal", "rule": diagnosis.get("rule"), "applied": applied}
    return heal_entry, healed_ok


def _cdp_needs_provision(prof: dict) -> bool:
    """True when CDP is feasible on the node but not yet reachable — needs ensure."""
    if not prof:
        return False
    cdp = prof.get("cdp") or {}
    return not cdp.get("reachable") and bool(cdp.get("feasible") or prof.get("cdpFeasible"))


def _provision_cdp_surface(target: str, registry: dict) -> dict:
    """Call cdp/session/command/ensure for target; return a preflight timeline entry."""
    uri = f"kvm://{target}/cdp/session/command/ensure"
    try:
        env = v2_service.call(uri, {}, registry, mode="execute")
        ok = bool(env.get("ok"))
    except Exception:  # noqa: BLE001 - a failed preflight must not abort the flow
        ok = False
    return {"id": f"preflight:cdp:{target}", "uri": uri, "target": target,
            "ok": ok, "type": "preflight", "action": "provision-surface"}


def _preflight(flow: dict, registry: dict) -> list[dict]:
    """Provision the surfaces a flow KNOWS up-front it will need, BEFORE running — proactive,
    not reactive. A flow with ``cdp/page/*`` steps needs a live CDP session; if CDP is feasible
    but not reachable on that node, bring it up once now so the first ``cdp/page`` step doesn't
    fail-then-self-heal. Idempotent (``ensure`` reuses an existing session)."""
    steps = flow.get("steps") or []
    cdp_targets = sorted({route_target(str(s.get("uri") or ""))
                          for s in steps if "/cdp/page/" in str(s.get("uri") or "")})
    entries: list[dict] = []
    for target in cdp_targets:
        if not target:
            continue
        prof = _fetch_env_profile({"uri": f"kvm://{target}/_"}, registry) or {}
        if _cdp_needs_provision(prof):
            entries.append(_provision_cdp_surface(target, registry))
    return entries


def _kvm_targets(flow: dict) -> list[str]:
    """Distinct node targets whose steps interact with a kvm-controlled surface, so the twin
    memory captures one known-good profile per real machine (not per step)."""
    seen: list[str] = []
    for s in flow.get("steps") or []:
        target = route_target(str(s.get("uri") or ""))
        if target and target not in seen and (
            "/cdp/page/" in str(s.get("uri") or "")
            or str(s.get("uri") or "").startswith(f"kvm://{target}/")
        ):
            seen.append(target)
    return seen


def suggest_recall(flow: dict, memory: TwinMemory) -> dict | None:
    """Return the remembered known-good record for this flow's URI sequence, or None.

    Callers use this to offer a "replay known-good" path: if the LLM would produce the
    same step URIs, the remembered execution can be shown to the user or replayed without
    an LLM round-trip. Returns None when no memory exists for this key (novel plan)."""
    return memory.recall_flow(_flow_key(flow))


def _flow_key(flow: dict) -> str:
    """Stable key for a flow: SHA-1 of its step-URI sequence (scheme+path, no payloads).

    Structurally identical flows (same URI order, different payloads or nodes) share one
    slot in the flow_store — the latest successful run overwrites. Payload-independent so
    the known-good is matched when the same PLAN is re-used with a different input text."""
    uris = "|".join(str(s.get("uri") or "") for s in (flow.get("steps") or []))
    return hashlib.sha1(uris.encode("utf-8", "replace")).hexdigest()[:16]


def _remember_known_good_flow(
    flow: dict, execution: dict, memory: TwinMemory, prompt: str = "", ts: str = ""
) -> None:
    """Store a successful flow execution as a flow record (known-good, or degraded-only).

    Called once after execute_flow returns ok=True and after _update_known_good so the
    environment profile is already up-to-date when the flow record is written. The record
    is keyed by the step-URI fingerprint (_flow_key) so recall is structure-based, not
    prompt-string-based — similar prompts that produce the same URI plan share one entry.

    ``ok=True`` here means the flow did not abort; if any step ran degraded (e.g. a capture
    blocked by the Wayland portal), the record is flagged ``degraded`` and remember_flow keeps
    it OUT of the known-good store — known-good must mean fully succeeded, not merely no-crash."""
    key = _flow_key(flow)
    degraded, reason = _results_degraded(execution.get("results") or {})
    from urirun.node.episode import intent_signature as _intent_sig  # noqa: PLC0415
    memory.remember_flow(key, {
        "flowKey": key,
        "prompt": prompt,
        "intent_sig": _intent_sig(prompt) if prompt else "",
        "steps": flow.get("steps") or [],
        "timeline": execution.get("timeline") or [],
        "nodes": sorted({str(s.get("node") or "") for s in (flow.get("steps") or []) if s.get("node")}),
        "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ok": True,
        "degraded": degraded,
        "degradedReason": reason,
    })


def _capture_known_good(flow: dict, registry: dict, memory: TwinMemory) -> None:
    """Snapshot-on-success, but only on the FIRST run: read each target's live environment profile
    once and remember it as the known-good fingerprint. On later runs this is a no-op — the
    baseline is sticky, so a drifted environment is detected against the *original* known-good,
    not silently adopted. Best-effort: a node that won't answer ``env/query/profile`` is simply
    left without a baseline (drift() will report ``known: false``), never an error."""
    for target in _kvm_targets(flow):
        if memory.known_good(target) is not None:
            continue                                    # baseline already established; keep it sticky
        prof = _fetch_env_profile({"uri": f"kvm://{target}/_"}, registry)
        if isinstance(prof, dict):
            memory.remember(target, prof)


def _update_known_good(flow: dict, registry: dict, memory: TwinMemory) -> None:
    """Advance the known-good to the current environment after a SUCCESSFUL flow.
    Unlike _capture_known_good (sticky first-run baseline), this unconditionally overwrites
    so that drift is always measured against the last successfully executed state."""
    for target in _kvm_targets(flow):
        prof = _fetch_env_profile({"uri": f"kvm://{target}/_"}, registry)
        if isinstance(prof, dict):
            memory.remember(target, prof)


def _drift_timeline(flow: dict, registry: dict, memory: TwinMemory) -> list[dict]:
    """Compare each target's LIVE profile to its just-captured known-good and emit a timeline
    entry when they differ. Diagnosis only — does NOT abort, force dry-run, or auto-remeasure;
    the flow continues so an operator (or the recovery layer) decides what a drift means."""
    entries: list[dict] = []
    for target in _kvm_targets(flow):
        prof = _fetch_env_profile({"uri": f"kvm://{target}/_"}, registry)
        if not isinstance(prof, dict):
            continue
        d = memory.drift(target, prof)
        if d.get("drifted") or not d.get("known"):
            entries.append({
                "id": f"twin:drift:{target}", "target": target, "type": "twin-drift",
                "ok": True, "action": "environment-drift",
                "drift": d,
                "uri": f"kvm://{target}/env/query/profile",
            })
    return entries


def _circuit_break_if_over(start: float, max_wall_clock: float, remediations_used: int,
                           max_remediations: int, timeline: list, results: dict, recoveries: list) -> dict | None:
    if time.monotonic() - start > max_wall_clock:
        return _circuit_break(f"flow exceeded {max_wall_clock:.0f}s wall-clock", timeline, results, recoveries)
    if remediations_used > max_remediations:
        return _circuit_break(f"flow exceeded {max_remediations} self-heal remediations", timeline, results, recoveries)
    return None


def _resolve_payload_or_fail(step: dict, results: dict, routes: list, timeline: list,
                             recoveries: list) -> tuple[dict | None, dict | None]:
    """(resolved payload, None) on success, or (None, failure envelope) on a missing dependency
    or a payload-resolution error."""
    missing = [dep for dep in step.get("depends_on", []) if dep not in results]
    if missing:
        exc = RuntimeError(f"{step['id']} missing dependencies: {missing}")
        return None, _step_fail_envelope(step, exc, routes, timeline, results, recoveries)
    try:
        return resolve_step_payload(step.get("payload") or {}, results), None
    except Exception as exc:  # noqa: BLE001 - surface as a structured step error.
        return None, _step_fail_envelope(step, exc, routes, timeline, results, recoveries)


def _step_fail_envelope(step: dict, exc: BaseException, routes: list, timeline: list,
                        results: dict, recoveries: list) -> dict:
    entry = _flow_step_failure(step, exc, routes)
    timeline.append(entry)
    recoveries.append({"stepId": step["id"], "uri": step["uri"], "error": entry["error"], "plan": entry["recovery"]})
    return {"ok": False, "timeline": timeline, "results": results, "error": entry["error"], "recovery": recoveries}



def _set_service_map(mesh: dict) -> str | None:
    old = os.environ.get("URI_SERVICE_MAP")
    os.environ["URI_SERVICE_MAP"] = json.dumps(mesh.get("serviceMap") or {})
    return old


def _restore_service_map(old: str | None) -> None:
    if old is None:
        os.environ.pop("URI_SERVICE_MAP", None)
    else:
        os.environ["URI_SERVICE_MAP"] = old



def _make_memory_dispatch(base_dispatch, memory: TwinMemory, flow: dict, registry: dict):
    """Wrap dispatch_uri to handle Twin memory operations in-process.

    Intercepts two URI patterns:
    - ``*/env/query/drift``: capture known-good (sticky first-run) then check for drift.
      Returns ``{ok, drifted, known, reason, next: {kind: continue}}`` — informational,
      never blocks the flow.
    - ``*/memory/command/remember``: update known-good per-node and save the flow record.
      Called at the end of a successful flow.

    Everything else passes through to base_dispatch unchanged."""
    flow_key = _flow_key(flow)

    def _live_profile(node: str) -> dict:
        # Use _fetch_env_profile for consistency with the orchestrator path and
        # testability (tests that patch _fetch_env_profile work unchanged).
        prof = _fetch_env_profile({"uri": f"kvm://{node}/_"}, registry)
        return prof if isinstance(prof, dict) else {}

    def dispatch(uri: str, payload: dict) -> dict:
        if "/env/query/drift" in uri:
            node = payload.get("node") or route_target(uri) or "host"
            profile = _live_profile(node)
            if not profile:
                return {"ok": True, "known": False, "drifted": False,
                        "skipped": "no-profile", "next": {"kind": "continue"}}
            if memory.known_good(node) is None:
                memory.remember(node, profile)  # sticky first-run baseline
            dr = memory.drift(node, profile)
            result = {"ok": True, **dr, "next": {"kind": "continue"}}
            if dr.get("drifted") or not dr.get("known"):
                result["type"] = "twin-drift"
                result["action"] = "environment-drift"
                result["drift"] = dr
            return result

        if "/memory/command/remember" in uri:
            nodes = payload.get("nodes") or []
            for node in nodes:
                profile = _live_profile(node)
                if profile:
                    memory.remember(node, profile)
            record = dict(payload.get("record") or {})
            record["flowKey"] = flow_key
            memory.remember_flow(flow_key, record)  # degraded records go to degraded_store, not known-good
            degraded = bool(record.get("degraded"))
            # Write capture proofs to proof_store so /api/twin/state proofs[] is non-empty.
            for cp in (record.get("captureProofs") or []):
                pkey = f"proof:{flow_key}:{cp.get('step', '')}:captured"
                if hasattr(memory, "remember_proof"):
                    memory.remember_proof(pkey, {"verdict": True, "flowKey": flow_key, **cp})
            return {"ok": True, "remembered": not degraded, "degraded": degraded,
                    "degradedReason": record.get("degradedReason"), "flowKey": flow_key}

        return base_dispatch(uri, payload)

    return dispatch


_THIN_DRIFT_SUFFIX = "/env/query/drift"
_THIN_REMEMBER_URI = "twin://host/memory/command/remember"


def _plan_with_preflight(steps: list[dict], *, execute: bool) -> list[dict]:
    """Prepend a twin://host/flow/command/preflight step when the plan has CDP steps.

    Preflight is ``optional`` — a NOT_FOUND or provisioning failure degrades gracefully;
    the reactive self-heal backstop in the driver handles CDP failures step-by-step."""
    if not execute:
        return steps
    has_cdp = any("/cdp/page/" in str(s.get("uri") or "") for s in steps)
    if not has_cdp:
        return steps
    return [{"id": "preflight", "uri": _THIN_PREFLIGHT_URI,
             "payload": {"steps": steps}, "depends_on": [], "optional": True}, *steps]


def _thin_remember_record(flow: dict, nodes: list[str]) -> dict:
    """Flow record stored by the thin-plan remember step. Carries the prompt (the LLM task
    title) + intent signature so /api/twin/state and the twin monitor show a real label, not
    '(no prompt)'. (The richer orchestrator path already stores these; this is the thin path.)"""
    from urirun.node.episode import intent_signature  # noqa: PLC0415
    prompt = str((flow.get("task") or {}).get("title") or "")
    return {"steps": flow.get("steps") or [], "prompt": prompt,
            "intent_sig": intent_signature(prompt) if prompt else "",
            "nodes": list(nodes), "ok": True}


def _route_node_for_uri(uri: str, routes: list[dict] | None) -> str:
    try:
        routed = routing_execution_layers(uri, {"routes": routes or []}, probe=False)
        if routed.get("runsOn"):
            return str(routed["runsOn"])
    except Exception:  # noqa: BLE001 - routing diagnostics must not block legacy flow construction
        pass
    return route_target(uri)


def _kvm_step_targets(steps: list[dict], routes: list[dict] | None = None) -> list[str]:
    targets = {_route_node_for_uri(str(s.get("uri") or ""), routes)
               for s in steps if str(s.get("uri") or "").startswith("kvm://")}
    return [t for t in sorted(targets) if t]


def _drift_steps_for(kvm_targets: list[str], routes_list: list) -> list[dict]:
    return [
        {"id": f"twin:drift:{t}", "uri": f"twin://{t}{_THIN_DRIFT_SUFFIX}",
         "payload": {"node": t, "routes": routes_list}, "depends_on": [], "optional": True}
        for t in kvm_targets
    ]


def _build_thin_plan(steps: list[dict], flow: dict, *, execute: bool,
                     memory: "TwinMemory | None" = None,
                     routes: list[dict] | None = None) -> list[dict]:
    """Build the complete step plan for _thin_driver.

    Order: drift checks → preflight (CDP) → original steps → remember.

    Memory steps (drift/remember) are only injected when ``memory`` is explicitly provided.
    The URI handlers (_uri_env_drift, _uri_memory_remember) are available via the twin
    connector and reachable through the mesh regardless of this gate — the gate just controls
    whether the thin driver automatically inserts them into every kvm flow.

    ``routes`` is forwarded into the drift/remember step payloads so the URI handlers
    can build a registry and call ``kvm://{node}/env/query/profile`` without
    an ambient global registry."""
    plan = _plan_with_preflight(steps, execute=execute)
    if not execute:
        return plan
    routes_list = routes or []
    kvm_targets = _kvm_step_targets(steps, routes_list)
    if not kvm_targets:
        return plan
    flow_key = _flow_key(flow)
    remember_step = {
        "id": "memory:remember",
        "uri": _THIN_REMEMBER_URI,
        "payload": {"nodes": kvm_targets, "routes": routes_list,
                    "flow_key": flow_key,
                    "record": _thin_remember_record(flow, kvm_targets)},
        "depends_on": [], "optional": True,
    }
    return _drift_steps_for(kvm_targets, routes_list) + plan + [remember_step]


def _default_dispatch_uri(execute: bool, registry: dict):
    _mode = "execute" if execute else "dry-run"
    return lambda u, p=None: v2_service.call(u, p or {}, registry or {}, mode=_mode)  # noqa: E731


def _make_flow_envelope(flow: dict, envelope: "FlowEnvelope | None") -> "FlowEnvelope":
    if envelope is not None:
        return envelope
    return FlowEnvelope(
        flow_id=str(flow.get("task", {}).get("id") or ""),
        goal=flow.get("task") or {},
    )


def _resolve_dispatch(dispatch_uri, memory, flow: dict, registry: dict):
    if memory is None:
        return dispatch_uri
    return _make_memory_dispatch(dispatch_uri, memory, flow, registry)


def _mesh_routes(mesh: dict) -> list:
    return (mesh or {}).get("routes") or []


def _flow_routing_report(flow: dict, mesh: dict, routes: list[dict], *, probe: bool = False) -> dict:
    """Resolve where every user step will run before dispatching it."""
    routing_mesh = {
        "nodes": (mesh or {}).get("nodes") or [],
        "routes": routes or [],
    }
    try:
        return diagnose_routing_plan(flow.get("steps") or [], routing_mesh, probe=probe)
    except Exception as exc:  # noqa: BLE001 - report failure without changing flow execution semantics
        return {
            "ok": False,
            "stepCount": len(flow.get("steps") or []),
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "steps": [],
            "blockedSteps": [],
            "runsOnByStep": {},
        }


def _routing_timeline(report: dict) -> list[dict]:
    entries: list[dict] = []
    for idx, step in enumerate(report.get("steps") or [], start=1):
        entries.append({
            "id": f"router:step{idx}",
            "uri": step.get("uri"),
            "ok": bool(step.get("ok")),
            "target": step.get("runsOn") or step.get("uriTarget") or "",
            "type": "routing",
            "blockedAt": step.get("blockedAt"),
            "reversible": True,
        })
    return entries


def _routing_block_result(report: dict, envelope: "FlowEnvelope") -> dict:
    blocked = report.get("blockedSteps") or []
    first = blocked[0] if blocked else {}
    message = (
        f"routing blocked before execution at {first.get('blockedAt')}: {first.get('uri')}"
        if first else "routing blocked before execution"
    )
    error = {
        "category": "ROUTING_BLOCKED",
        "type": "RoutingPreflight",
        "message": message,
        "severity": "error",
        "status": 412,
    }
    return {
        "ok": False,
        "timeline": _routing_timeline(report),
        "results": {},
        "error": error,
        "routing": report,
        "next": {"kind": "blocked"},
        "envelope": dataclasses.asdict(envelope),
    }


def _apply_routing_targets(result: dict, report: dict) -> None:
    runs_on = {
        str(step.get("uri") or ""): step.get("runsOn")
        for step in (report.get("steps") or [])
        if step.get("runsOn")
    }
    if not runs_on:
        return
    for entry in result.get("timeline") or []:
        uri = str(entry.get("uri") or "")
        if uri in runs_on:
            entry["target"] = runs_on[uri]


def execute_flow(flow: dict, mesh: dict, registry: dict, execute: bool, *, recover: bool = True,
                 max_retries: int = 1, max_wall_clock: float = 180.0, max_remediations: int = 6,
                 rollback_on_failure: bool = True,
                 memory: TwinMemory | None = None,
                 dispatch_uri=None,
                 envelope: FlowEnvelope | None = None,
                 router_guard: bool = False) -> dict:
    # Thin-driver is the sole engine. Callers that don't provide a dispatch_uri
    # get one backed by v2_service.call, so the same observable envelope-aware
    # path is always used — no second engine, no silent fallback.
    if dispatch_uri is None:
        dispatch_uri = _default_dispatch_uri(execute, registry)
    envelope = _make_flow_envelope(flow, envelope)
    old_map = _set_service_map(mesh)
    try:
        _dispatch = _resolve_dispatch(dispatch_uri, memory, flow, registry)
        routes = _mesh_routes(mesh)
        routing_report = _flow_routing_report(flow, mesh, routes, probe=bool(execute and router_guard))
        if execute and router_guard and not routing_report.get("ok", True):
            return _routing_block_result(routing_report, envelope)
        steps = _build_thin_plan(flow.get("steps") or [], flow,
                                 execute=execute, memory=memory, routes=routes)
        result = _thin_driver(steps, envelope, _dispatch,
                              registry=registry, execute=execute,
                              max_retries=max_retries,
                              max_remediations=max_remediations,
                              max_wall_clock=max_wall_clock)
        if isinstance(result, dict):
            result.setdefault("routing", routing_report)
            _apply_routing_targets(result, routing_report)
        return result
    finally:
        _restore_service_map(old_map)


def _should_compensate(ok: bool, execute: bool, rollback_on_failure: bool, document: dict) -> bool:
    return not ok and execute and (rollback_on_failure or bool(document.get("rollbackOnFailure")))


def _apply_compensation(result: dict, execution: dict, mesh: dict, scan_uri, dispatch_uri) -> None:
    if dispatch_uri is not None:
        rollback_result = dispatch_uri(
            "twin://host/flow/command/rollback",
            {"execution": execution, "mesh": mesh, "scan_uri": scan_uri},
        )
        result["compensation"] = {k: v for k, v in rollback_result.items() if k != "ok"}
        result["compensation"]["ok"] = rollback_result.get("ok", False)
    else:
        result["compensation"] = rollback_flow(execution, mesh, scan_uri=scan_uri)


def _apply_reversibility(
    result: dict, execution: dict, ok: bool, execute: bool,
    rollback_on_failure: bool, document: dict, mesh: dict,
    dispatch_uri=None,
) -> dict:
    """Attach the reversible-transitions ledger and, when eligible, run SAGA compensation.
    Mutates and returns `result`."""
    led = ledger_from_execution(execution)
    if not led:
        return result
    result["reversible"] = {
        "rollbackable": len(led),
        "transitions": [{"forward": t.forward.uri, "inverse": t.inverse.uri,
                         "args": t.inverse.args} for t in led],
    }
    # SAGA compensation: flow FAILED yet left mutations — unwind them LIFO so a failed run
    # leaves no partial mess (opt-in: only when explicitly requested).
    if _should_compensate(ok, execute, rollback_on_failure, document):
        scan_uri = (document.get("verification") or {}).get("scan_uri")
        _apply_compensation(result, execution, mesh, scan_uri, dispatch_uri)
    return result


def run_flow_document(document: dict, mesh: dict, *, execute: bool, rollback_on_failure: bool = False) -> dict:
    route_uris = {route["uri"] for route in mesh["routes"] if safe_route(route)}
    flow = normalize_flow(document, route_uris, routes=mesh["routes"])
    registry = registry_from_routes(mesh["routes"])
    mode = "execute" if execute else "dry-run"
    dispatch_uri = make_dispatch_uri(registry, mode)
    execution = execute_flow(flow, mesh, registry, execute=execute, dispatch_uri=dispatch_uri)
    goal_dispatch = lambda uri, payload=None: v2_service.call(uri, payload or {}, registry, mode="execute")
    verification = verify_flow_execution(document, execution, executed=execute, dispatch=goal_dispatch)
    ok = bool(execution.get("ok")) and (verification is None or bool(verification.get("ok")))
    result = {"flow": flow, **execution}
    result["ok"] = ok
    if document.get("source"):
        result["source"] = document.get("source")
    if verification is not None:
        result["verification"] = verification
    return _apply_reversibility(result, execution, ok, execute, rollback_on_failure, document, mesh,
                                dispatch_uri=None)


def _inproc_category(env: dict) -> str:
    return (env.get("error") or {}).get("category") or ""


def _inproc_result(env: dict) -> dict:
    """Normalize a urirun.run envelope into the dispatch fallback's {ok, result, error} shape."""
    val = (env.get("result") or {}).get("value") if isinstance(env.get("result"), dict) else None
    return {"ok": bool(env.get("ok")), "result": val,
            "error": (env.get("error") or {}).get("message") if not env.get("ok") else None}


def _in_process_discovery(uri: str, payload: dict | None = None) -> "dict | None":
    """Tier-2 fallback: resolve *uri* through installed connectors (entry-point scan),
    then through DECORATED_BINDINGS (connector.handler() registrations not in entry points).

    Called by make_dispatch_uri when Tier 1 (mesh) returns NOT_FOUND.  Returns None when
    the connector is also absent in-process, letting make_dispatch surface the NOT_FOUND."""
    try:
        import urirun as _u  # noqa: PLC0415
        from urirun.runtime import discovery as _disc, v2 as _v2  # noqa: PLC0415
        reg2 = _disc.registry_for_uri(uri, "urirun.bindings")
        env = _u.run(uri, reg2, payload=dict(payload or {}),
                     mode="execute", policy={"allowExecute": True})
        if _inproc_category(env) != "NOT_FOUND":
            return _inproc_result(env)
        # Tier 2b: DECORATED_BINDINGS — connector.handler() registrations that have no entry point
        # (e.g. the twin:// connector registered by flow.py at module import time).
        live_binding = _v2.decorated_bindings()["bindings"].get(uri)
        if live_binding is None:
            return None
        reg3 = _u.compile_registry(_v2.build_binding_document([live_binding]))
        env = _u.run(uri, reg3, payload=dict(payload or {}),
                     mode="execute", policy={"allowExecute": True})
        if _inproc_category(env) == "NOT_FOUND":
            return None
        return _inproc_result(env)
    except Exception as _exc:  # noqa: BLE001
        return {"ok": False, "invokedUri": uri,
                "error": {"message": str(_exc), "category": "INPROCESS_ERROR"}}


def make_dispatch_uri(registry: dict, mode: str = "execute"):
    """Build a two-tier dispatch callable for execute_flow / run_flow_document.

    Tier 1 (mesh): v2_service.call with the mesh registry — covers all served nodes.
    Tier 2 (in-process): urirun.runtime.discovery — covers connectors installed locally
    that aren't exposed as mesh routes (diag://, fix://, twin://, widget://, …).

    Returns a callable ``(uri, payload=None) → dict`` suitable for dispatch_uri=.

    Delegates to ``v2_service.make_dispatch`` — single implementation of the two-tier
    pattern, shared with the HTTP transport layer."""
    return v2_service.make_dispatch(registry, mode, fallback=_in_process_discovery)


def _flow_transport(mesh: dict) -> CallableTransport:
    """A Transport bound to the mesh that UNWRAPS each run envelope to the connector's own
    ``{ok, inverse, state, ...}`` result — so the reversible engine sees the contract payload,
    not the transport envelope."""
    registry = registry_from_routes(mesh["routes"])

    def _call(uri: str, payload: dict | None = None) -> dict:
        env = v2_service.call(uri, payload or {}, registry, mode="execute")
        val = result_data(env) if isinstance(env, dict) else None
        return val if isinstance(val, dict) else {"ok": bool(env.get("ok"))}

    return CallableTransport(_call)


def rollback_flow(execution: dict, mesh: dict, *, scan_uri: str | None = None) -> dict:
    """Undo a completed flow by navigating its registered inverses LIFO — consumes the
    reversible engine on a NORMAL execute_flow result. When ``scan_uri`` (a connector scan route
    returning ``{state}``) is given, a state RE-SCAN proves the return (final state == pre-flow);
    otherwise the inverses are applied without the per-flow proof and the result says so."""
    ledger = ledger_from_execution(execution)
    if not ledger:
        return {"ok": True, "undone": [], "note": "flow registered no reversible transitions"}
    transport = _flow_transport(mesh)
    proc = ReversibleProcess(transport)
    if scan_uri:
        try:
            twin = Twin.scan(transport, scan_uri)
            return proc.rollback_flow(twin, ledger, before_sig=None)
        except Exception as exc:  # noqa: BLE001 - fall back to proof-less rollback below.
            scan_uri = None
    # proof-less: apply the inverses LIFO, report honestly that no state re-scan confirmed it.
    undone = []
    for tr in reversed(ledger):
        res = transport.call(tr.inverse.uri, tr.inverse.args)
        if not res.get("ok"):
            return {"ok": False, "undone": undone, "stuck": tr.inverse.uri,
                    "reason": f"inverse failed ({res.get('error')}) — KNOWN-BAD → escalate"}
        undone.append(tr.inverse.uri)
    return {"ok": True, "undone": undone, "proof": "none (no scan route given)"}


# ── URI surfaces: twin://host/flow/ handlers ─────────────────────────────────
# These make the orchestrator's internal operations addressable on the mesh so
# the thin driver (and remote callers) can route to them by URI.

def _uri_goal_verify(payload: dict) -> dict:
    """Handler for twin://<node>/flow/goal/query/verify.

    Payload: {goal: {uri, path?, contains?, equals?, present?}, results: {…}, mesh?: {…}}
    Returns: {ok, checks: [{check, ok, …}]}

    Two paths:
    • goal has a `uri` → call it through mesh/registry to read end-state, assert the
      post-condition (contains/equals/present). This is what closes the 'every step green
      but nothing achieved' gap.
    • goal has no `uri` (e.g. {reached: true} from FlowEnvelope) → treat as trivially
      passed (the envelope sets goal, but without a verification spec there is nothing
      to assert — report honestly rather than silently failing)."""
    import urirun  # noqa: PLC0415
    goal = payload.get("goal") or {}
    results = payload.get("results") or {}
    mesh = payload.get("mesh") or {}

    goal_uri = goal.get("uri")
    if not goal_uri:
        # No assertion URI — the envelope carries task metadata, not a verifiable spec.
        return urirun.ok(checks=[], note="no goal URI in envelope — nothing to assert",
                         next={"kind": "done"})

    registry = registry_from_routes((mesh.get("routes") or []))
    dispatch = lambda uri, pl=None: v2_service.call(uri, pl or {}, registry, mode="execute")
    passed, detail = _run_goal_check(goal, dispatch)
    checks = [{"check": "goal", "uri": goal_uri, "ok": passed, **detail}]
    return {**urirun.ok(ok=passed), "checks": checks,
            "next": {"kind": "done" if passed else "rollback"}}


def _uri_preflight(payload: dict) -> dict:
    """Handler for twin://<node>/flow/command/preflight.

    Payload: {steps: […], mesh?: {…}}
    Returns: {ok, timeline: [{…preflight entries…}]}

    Called by _thin_driver before the main loop when the plan has CDP steps.
    Provisions CDP sessions proactively so the first cdp/page step doesn't
    fail-then-self-heal."""
    import urirun  # noqa: PLC0415
    steps = payload.get("steps") or []
    mesh = payload.get("mesh") or {}
    registry = registry_from_routes((mesh.get("routes") or []))
    flow = {"steps": steps}
    entries = _preflight(flow, registry)
    all_ok = all(e.get("ok", True) for e in entries)
    return {**urirun.ok(ok=all_ok), "timeline": entries, "count": len(entries)}


def _uri_env_drift(payload: dict) -> dict:
    """Handler for twin://<node>/env/query/drift.

    Payload: {node, routes?: [{…}]}
    Returns: {ok, next: {kind: continue}, drifted?, known?, reason?}

    On the first call for a node: captures the live environment profile as the
    sticky known-good baseline (snapshot-on-success equivalent).
    On subsequent calls: compares live profile to the baseline and reports drift.
    Always returns next: {kind: continue} — drift is advisory, never abort.

    Uses twin_store.durable_memory() so the baseline persists across restarts."""
    import urirun  # noqa: PLC0415
    from urirun.node.twin_store import durable_memory  # noqa: PLC0415
    node = payload.get("node") or "host"
    routes = payload.get("routes") or []
    registry = registry_from_routes(routes)
    try:
        prof_r = v2_service.call(f"kvm://{node}/env/query/profile",
                                 {}, registry, mode="execute")
        val = (prof_r.get("result") or {}).get("value")
        profile = val if isinstance(val, dict) else {}
    except Exception:  # noqa: BLE001 - a missing kvm route is not a drift error
        profile = {}
    if not profile:
        return urirun.ok(known=False, drifted=False,
                         skipped="no-profile", next={"kind": "continue"})
    memory = durable_memory()
    if memory.known_good(node) is None:
        memory.remember(node, profile)          # sticky first-run baseline
    dr = memory.drift(node, profile)
    result = {**urirun.ok(), **dr, "next": {"kind": "continue"}}
    if dr.get("drifted") or not dr.get("known"):
        result["type"] = "twin-drift"
        result["action"] = "environment-drift"
    return result


def _remember_node_profile(memory, node: str, registry: dict) -> None:
    try:
        prof_r = v2_service.call(f"kvm://{node}/env/query/profile",
                                 {}, registry, mode="execute")
        val = (prof_r.get("result") or {}).get("value")
        if isinstance(val, dict) and val:
            memory.remember(node, val)
    except Exception:  # noqa: BLE001 - best-effort; a missing route is not fatal
        pass


def _uri_memory_remember(payload: dict) -> dict:
    """Handler for twin://host/memory/command/remember.

    Payload: {nodes: [str], routes?: [{…}], flow_key?: str,
              record: {steps: […], …}}
    Returns: {ok, remembered: bool, nodes: […], flowKey?: str}

    Updates the known-good environment profile for each node (unconditional
    overwrite — advances the baseline to the just-completed successful state),
    then stores the flow record keyed by flow_key for recall/replay.

    Uses twin_store.durable_memory() for durable persistence."""
    import urirun  # noqa: PLC0415
    from urirun.node.twin_store import durable_memory  # noqa: PLC0415
    nodes = payload.get("nodes") or []
    routes = payload.get("routes") or []
    registry = registry_from_routes(routes)
    flow_key = str(payload.get("flow_key") or payload.get("flowKey") or "")
    memory = durable_memory()
    for node in nodes:
        _remember_node_profile(memory, node, registry)
    record = dict(payload.get("record") or {})
    record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    remembered = False
    degraded = bool(record.get("degraded"))
    if flow_key:
        record["flowKey"] = flow_key
        memory.remember_flow(flow_key, record)  # degraded records go to degraded_store, not known-good
        remembered = not degraded
    return urirun.ok(remembered=remembered, degraded=degraded,
                     degradedReason=record.get("degradedReason"),
                     nodes=nodes, flowKey=flow_key if flow_key else None)


try:
    import urirun as _urirun  # noqa: PLC0415
    _flow_conn = _urirun.connector("flow", scheme="twin")
    _flow_conn.handler("flow/goal/query/verify",
                       meta={"label": "Verify flow goal state post-execution"})(_uri_goal_verify)
    _flow_conn.handler("flow/command/preflight",
                       meta={"label": "Provision surfaces before the main flow loop"})(_uri_preflight)
    _flow_conn.handler("env/query/drift",
                       meta={"label": "Twin drift detection — compare live env to known-good baseline"})(_uri_env_drift)
    _flow_conn.handler("memory/command/remember",
                       meta={"label": "Record known-good execution and advance env baseline"})(_uri_memory_remember)
except Exception:  # noqa: BLE001 - connector registration is optional
    pass
