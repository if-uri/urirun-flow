"""Flow normalization, URI utilities and wiring helpers extracted from flow_planner.py."""
from __future__ import annotations

import json
import re

from urirun_flow._util import slug
from urirun_connector_router.routing import route_target


# ── JSON / URI utilities ──────────────────────────────────────────────────────

def json_from_text(text: str) -> dict:
    stripped = text.strip()
    decoder = json.JSONDecoder()

    def _candidates(raw: str) -> list[dict]:
        found: list[dict] = []
        for idx, ch in enumerate(raw):
            if ch != "{":
                continue
            try:
                parsed, _end = decoder.raw_decode(raw[idx:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                found.append(parsed)
        return found

    blocks = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(.*?)\s*```", stripped, re.S)]
    candidates: list[dict] = []
    for block in blocks or [stripped]:
        candidates.extend(_candidates(block))
    if candidates:
        for candidate in candidates:
            if isinstance(candidate.get("task"), dict) and isinstance(candidate.get("steps"), list):
                return candidate
        return candidates[0]
    return json.loads(stripped)


def _uri_segments(uri: str) -> tuple[str, list[str]]:
    scheme, _, rest = str(uri).partition("://")
    return scheme, rest.split("/")


def _uri_matches_template(concrete: str, template: str) -> bool:
    """True if ``concrete`` fits a templated allowed route, e.g. ``kvm://kvm/display/query/info``
    matches ``kvm://{host}/display/query/info`` — a ``{param}`` segment binds any one segment."""
    cs, cseg = _uri_segments(concrete)
    ts, tseg = _uri_segments(template)
    if cs != ts or len(cseg) != len(tseg):
        return False
    return all(t == c or (t.startswith("{") and t.endswith("}")) for t, c in zip(tseg, cseg))


def _uri_is_available(uri: str, allowed_uris: set[str]) -> bool:
    if uri in allowed_uris:
        return True
    # The planner's catalog lists parametrized routes with literal ``{host}``/``{id}`` segments;
    # a concrete URI the LLM filled in (the node binds the param at /run) is still available.
    return any(_uri_matches_template(uri, allowed) for allowed in allowed_uris if "{" in allowed)


_CDP_PAGE_TO_UI_SUFFIX = {
    "/cdp/page/command/click": "/ui/command/click",
    "/cdp/page/command/fill": "/ui/command/fill",
}


def _replace_uri_action_path(uri: str, suffix: str) -> str:
    scheme, _, rest = str(uri).partition("://")
    target = rest.split("/", 1)[0] if rest else "host"
    return f"{scheme}://{target}{suffix}"


def _fallback_ui_uri_for_unavailable_cdp(uri: str, allowed_uris: set[str]) -> str | None:
    """Map old/nonexistent CDP click/fill routes onto the KVM UI router when available.

    Some planners still emit cdp/page/command/click|fill because CDP navigate/ready exists.
    The KVM connector exposes DOM-backed click/fill through ui/command/* instead; normalize to
    that real route rather than failing the entire flow as "URI is not available"."""
    for cdp_suffix, ui_suffix in _CDP_PAGE_TO_UI_SUFFIX.items():
        if not uri.endswith(cdp_suffix):
            continue
        scheme, _, _rest = str(uri).partition("://")
        candidates = [_replace_uri_action_path(uri, ui_suffix)]
        if scheme == "kvm":
            candidates.append(f"kvm://host{ui_suffix}")
        for candidate in candidates:
            if _uri_is_available(candidate, allowed_uris):
                return candidate
    return None


def _rewrite_payload_for_fallback_uri(uri: str, payload: dict) -> dict:
    if not uri.endswith("/ui/command/fill"):
        return payload
    out = dict(payload)
    if not out.get("value") and out.get("text"):
        out["value"] = out.pop("text")
    return out


# ── Infeasibility helpers ─────────────────────────────────────────────────────

def _infeasibility_error(uri: str, c: dict) -> str:
    """Format the ValueError message for an infeasible step — mirrors 'URI is not available'."""
    return (
        f"URI '{uri}' is infeasible on this environment: "
        f"{c['what']} via surface '{c['surface']}' — {c['reason']} "
        f"(use '{c['fix']}' instead)"
    )


def _step_is_infeasible(uri: str, infeasible_constraints: list[dict]) -> dict | None:
    """Return the first infeasible constraint that matches this URI's action suffix, or None.

    A constraint matches when the URI's path contains the forbidden suffix (e.g.
    '/input/command/type') AND there is no better surface available — detected by checking
    whether the URI belongs to a blocked OS surface path. This is a structural check:
    `browser://host/cdp/page/command/fill` does NOT contain '/input/command/type', so CDP
    fill is never blocked. Only OS-surface routes that share a path suffix with `what`."""
    for c in infeasible_constraints:
        if c.get("kind") != "infeasible":
            continue
        what = c.get("what") or ""
        if what and what in uri:
            return c
    return None


# ── Flow normalization ────────────────────────────────────────────────────────

def _schema_placeholder(schema: dict) -> object:
    """Return a minimal value that satisfies the common scalar schema shapes.

    Flow payloads may contain ``<param>_from`` references that are resolved at
    execution time. For contract validation they stand in for the target
    connector parameter, so the validator needs a typed placeholder rather than
    the DSL key itself.
    """
    if not isinstance(schema, dict):
        return None
    if schema.get("enum"):
        return schema["enum"][0]
    if isinstance(schema.get("const"), (str, int, float, bool)) or schema.get("const") is None:
        if "const" in schema:
            return schema.get("const")
    if schema.get("anyOf"):
        return _schema_placeholder(schema["anyOf"][0])
    if schema.get("oneOf"):
        return _schema_placeholder(schema["oneOf"][0])
    typ = schema.get("type")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), typ[0] if typ else None)
    if typ == "integer":
        return 0
    if typ == "number":
        return 0
    if typ == "boolean":
        return False
    if typ == "array":
        return []
    if typ == "object":
        return {}
    return ""


def _payload_for_schema_validation(payload: dict, schema: dict) -> dict:
    """Translate flow-level result references into connector-level fields.

    ``monitor_from: "step.result.value.selected.monitor"`` is valid flow DSL
    when the connector declares a ``monitor`` input. Unknown ``*_from`` keys are
    left untouched so strict schemas still reject them.
    """
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return payload
    literal_from_keys = {"copy_from"}
    out: dict = {}
    for key, value in (payload or {}).items():
        if key.endswith("_from") and key not in literal_from_keys and isinstance(value, str):
            base = key[: -len("_from")]
            if base in props:
                out[base] = _schema_placeholder(props.get(base) or {})
                continue
        out[key] = value
    return out


def _canonicalize_template_refs(payload: dict) -> dict:
    """Convert ``{{step.result...path}}`` mustache-style values into canonical ``<field>_from``
    data-flow references. Many LLMs (esp. local/coder models) express a data dependency as
    ``monitor: "{{list_chrome_windows.result.value.selected.monitor}}"`` — that is the SAME intent
    as ``monitor_from: "list_chrome_windows.result.value.selected.monitor"``, but a raw template
    string fails the connector's typed schema (``monitor`` must be an int). Accept the convention
    instead of hard-failing the plan. This is syntax tolerance for the planner — NOT anchor-phrase
    hard-coding; it widens which models can drive the LLM track, it does not encode any intent."""
    if not isinstance(payload, dict):
        return payload
    out: dict = {}
    for key, value in payload.items():
        if isinstance(value, str) and not key.endswith("_from"):
            m = re.fullmatch(r"\s*\{\{\s*(.+?)\s*\}\}\s*", value)
            if m:
                out[f"{key}_from"] = m.group(1)
                continue
        out[key] = value
    return out


def _mask_invalid_domain_values(payload: dict, schema: dict, route: dict) -> dict:
    """Replace contract-domain enum params whose value fails the property schema with a
    typed placeholder for validation purposes only.

    A label-like value ('DP-2' where the contract wants an int monitor index) is a
    deferred input, not a plan error: env-enum resolution grounds it later (label
    coercion / preference / needs-selection). Hard-failing here would discard the
    whole LLM plan over a parameter the resolver owns anyway."""
    domains = ((route.get("meta") or {}).get("contract") or {}).get("domains") or {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not domains or not isinstance(props, dict):
        return payload
    import jsonschema  # noqa: PLC0415
    out = dict(payload)
    for param in domains:
        if param in out and param in props:
            try:
                jsonschema.validate(instance=out[param], schema=props[param] or {})
            except jsonschema.ValidationError:
                out[param] = _schema_placeholder(props[param] or {})
    return out


def _validate_step_payload(uri: str, payload: dict, routes: "list[dict] | None") -> None:
    """Raise ValueError when routes include an inputSchema that payload doesn't satisfy."""
    if not routes:
        return
    route = next((r for r in routes if r.get("uri") == uri), None)
    if not (route and route.get("inputSchema")):
        return
    import jsonschema  # noqa: PLC0415
    schema = route["inputSchema"]
    validation_payload = _payload_for_schema_validation(payload, schema)
    validation_payload = _mask_invalid_domain_values(validation_payload, schema, route)
    try:
        jsonschema.validate(instance=validation_payload, schema=schema)
    except jsonschema.ValidationError as e:
        raise ValueError(f"Payload validation failed for {uri}: {e.message}")


def _unique_step_id(step: dict, index: int, used: set) -> str:
    """Return a slug step id that is unique within `used`, then register it."""
    step_id = slug(str(step.get("id") or f"step_{index}"))
    if step_id in used:
        step_id = f"{step_id}_{index}"
    used.add(step_id)
    return step_id


def _normalize_flow_step(step: dict, index: int, allowed_uris: set[str], used: set[str],
                         routes: "list[dict] | None" = None,
                         infeasible_constraints: "list[dict] | None" = None) -> dict:
    """Validate and canonicalize one flow step; `used` tracks taken ids to keep them unique."""
    uri = str(step.get("uri", ""))
    payload = step.get("payload") if isinstance(step.get("payload"), dict) else {}
    payload = _canonicalize_template_refs(payload)
    if not _uri_is_available(uri, allowed_uris):
        fallback = _fallback_ui_uri_for_unavailable_cdp(uri, allowed_uris)
        if fallback:
            uri = fallback
            payload = _rewrite_payload_for_fallback_uri(uri, payload)
        else:
            raise ValueError(f"URI is not available: {uri}")
    if infeasible_constraints:
        c = _step_is_infeasible(uri, infeasible_constraints)
        if c is not None:
            raise ValueError(_infeasibility_error(uri, c))
    _validate_step_payload(uri, payload, routes)
    step_id = _unique_step_id(step, index, used)
    deps = [slug(str(dep)) for dep in step.get("depends_on", []) if isinstance(dep, str)]
    return {"id": step_id, "uri": uri, "payload": payload, "depends_on": deps}


def _normalize_flow_task(task: dict) -> dict:
    return {
        "id": slug(str(task.get("id") or task.get("title") or "nl_uri_flow")),
        "title": str(task.get("title") or "NL to URI host flow"),
        "source": str(task.get("source") or "llm"),
    }


_WINDOW_LIST_SUFFIX = "/window/query/list"
_WINDOW_FOCUS_SUFFIX = "/window/command/focus"
_SCREEN_CAPTURE_SUFFIX = "/screen/query/capture"


def _positive_int(value: object) -> bool:
    try:
        return int(value) > 0
    except Exception:
        return False


def _capture_needs_window_monitor(payload: dict) -> bool:
    if "monitor_from" in payload:
        return False
    if _positive_int(payload.get("monitor")):
        return False
    scope = str(payload.get("scope") or "").strip().lower()
    return scope in {"", "all", "all-monitors", "desktop"} or payload.get("monitor") in (None, "", 0, -1)


def _capture_scope_conflicts_with_ref(payload: dict) -> bool:
    """True when the LLM set ``monitor_from`` but also left an all-monitors ``scope`` that the
    contract's skipWhen would let override the resolved single monitor."""
    return bool(payload.get("monitor_from")) and str(
        payload.get("scope") or "").strip().lower() in {"all", "all-monitors", "desktop"}


def _needs_producer(payload: dict, param: str, skip_scopes) -> bool:
    """Generic form of _capture_needs_window_monitor: a consumer needs a producer for ``param``
    when no explicit value/ref is set and the scope leaves the env-enum unresolved."""
    if f"{param}_from" in payload:
        return False
    if _positive_int(payload.get(param)):
        return False
    scope = str(payload.get("scope") or "").strip().lower()
    return scope in {"", *skip_scopes} or payload.get(param) in (None, "", 0, -1)


def _scope_conflicts_with_producer(payload: dict, param: str, skip_scopes) -> bool:
    """A ``<param>_from`` ref paired with an all-scope the contract's skipWhen would let override it."""
    return bool(payload.get(f"{param}_from")) and str(
        payload.get("scope") or "").strip().lower() in set(skip_scopes)


def _wire_env_consumer(step: dict, prior_ids: set, spec: dict, producer_id: str) -> dict:
    param, skip, pfrom = spec["param"], spec["skip_scopes"], f"{spec['param']}_from"
    payload = dict(step.get("payload") or {})
    if _needs_producer(payload, param, skip) or _scope_conflicts_with_producer(payload, param, skip):
        payload.pop("scope", None)
        if not _positive_int(payload.get(param)):
            payload.pop(param, None)
        if _needs_producer(payload, param, skip):
            payload[pfrom] = f"{producer_id}.{spec['path']}"
    # a <param>_from reference REQUIRES its producer declared in depends_on (data dep, not luck)
    deps = list(step.get("depends_on") or [])
    if str(payload.get(pfrom) or "").split(".")[0] == producer_id and producer_id in prior_ids \
            and producer_id not in deps:
        deps.append(producer_id)
    return {**step, "payload": payload, "depends_on": deps}


def _focus_uri_for_window_uri(window_uri: str, allowed_uris: set[str]) -> str | None:
    candidate = window_uri.replace(_WINDOW_LIST_SUFFIX, _WINDOW_FOCUS_SUFFIX)
    if candidate in allowed_uris:
        return candidate
    target = route_target(window_uri)
    for uri in sorted(allowed_uris):
        if uri.endswith(_WINDOW_FOCUS_SUFFIX) and route_target(uri) == target:
            return uri
    return None


def _window_focus_selector(step: dict) -> str:
    payload = step.get("payload") if isinstance(step.get("payload"), dict) else {}
    return str(payload.get("title") or payload.get("app") or "").strip()


def _capture_uses_window_monitor(step: dict, window_step_id: str) -> bool:
    payload = step.get("payload") if isinstance(step.get("payload"), dict) else {}
    ref = str(payload.get("monitor_from") or "")
    return ref.startswith(f"{window_step_id}.")


def _generated_step_id(base: str, used: set[str]) -> str:
    root = slug(base) or "step"
    candidate = root
    i = 2
    while candidate in used:
        candidate = f"{root}_{i}"
        i += 1
    used.add(candidate)
    return candidate


def _depends_on_window_focus(step: dict, steps_by_id: dict[str, dict], window_id: str) -> bool:
    for dep in step.get("depends_on") or []:
        dep_step = steps_by_id.get(str(dep))
        if not dep_step:
            continue
        if str(dep_step.get("uri") or "").endswith(_WINDOW_FOCUS_SUFFIX) and window_id in (
            dep_step.get("depends_on") or []
        ):
            return True
    return False


def _dedupe_equivalent_focus_steps(steps: list[dict]) -> list[dict]:
    seen: dict[tuple, str] = {}
    replace: dict[str, str] = {}
    out: list[dict] = []
    for step in steps:
        step_id = str(step.get("id") or "")
        if str(step.get("uri") or "").endswith(_WINDOW_FOCUS_SUFFIX):
            key = (
                str(step.get("uri") or ""),
                json.dumps(step.get("payload") or {}, sort_keys=True),
                tuple(step.get("depends_on") or []),
            )
            if key in seen and step_id:
                replace[step_id] = seen[key]
                continue
            if step_id:
                seen[key] = step_id
        out.append(step)
    if not replace:
        return out
    rewritten: list[dict] = []
    for step in out:
        deps: list[str] = []
        for dep in step.get("depends_on") or []:
            dep = replace.get(str(dep), str(dep))
            if dep not in deps:
                deps.append(dep)
        rewritten.append({**step, "depends_on": deps})
    return rewritten


def _focus_window_before_monitor_capture(steps: list[dict], allowed_uris: set[str]) -> list[dict]:
    """Raise the window before monitor capture when capture is bound to window/query/list.

    A monitor screenshot records the visible desktop, not the accessibility tree. If the
    selected app window exists but is covered by another app, `monitor_from` alone picks the
    right monitor yet still produces the wrong visual evidence. Focus is best-effort in the
    KVM connector, so this keeps the flow admissible while making screenshots match the NL
    intent ("screen where Chrome is").
    """
    steps = _dedupe_equivalent_focus_steps(steps)
    if not allowed_uris or any("/cdp/" in str(step.get("uri") or "") for step in steps):
        return steps
    used = {str(step.get("id") or "") for step in steps if step.get("id")}
    steps_by_id = {str(step.get("id") or ""): step for step in steps if step.get("id")}
    out: list[dict] = []
    latest_window_step: dict | None = None
    focus_by_window: dict[str, str] = {}
    for step in steps:
        uri = str(step.get("uri") or "")
        if uri.endswith(_WINDOW_LIST_SUFFIX):
            latest_window_step = step
            out.append(step)
            continue
        if uri.endswith(_SCREEN_CAPTURE_SUFFIX) and latest_window_step is not None:
            window_id = str(latest_window_step.get("id") or "")
            selector = _window_focus_selector(latest_window_step)
            focus_uri = _focus_uri_for_window_uri(str(latest_window_step.get("uri") or ""), allowed_uris)
            if (
                window_id and selector and focus_uri
                and _capture_uses_window_monitor(step, window_id)
                and not _depends_on_window_focus(step, steps_by_id, window_id)
            ):
                focus_id = focus_by_window.get(window_id)
                if focus_id is None:
                    focus_id = _generated_step_id(f"focus_{window_id}", used)
                    focus_by_window[window_id] = focus_id
                    out.append({
                        "id": focus_id,
                        "uri": focus_uri,
                        "payload": {"title": selector},
                        "depends_on": [window_id],
                    })
                deps = list(step.get("depends_on") or [])
                deps = [focus_id if dep == window_id else dep for dep in deps]
                if focus_id not in deps:
                    deps.append(focus_id)
                step = {**step, "depends_on": deps}
        out.append(step)
    return out


# ── CDP probe injection ───────────────────────────────────────────────────────

_CDP_ENSURE_SUFFIX = "/cdp/session/command/ensure"
_CDP_READY_SUFFIX = "/cdp/session/query/ready"
_CDP_PAGE_PREFIX = "/cdp/page/"

