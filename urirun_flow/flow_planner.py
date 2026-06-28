# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.
#
# Plan-generation layer extracted from flow.py. Contains all NL→URI planning
# helpers: intent classification, heuristic flow building, LLM flow generation,
# flow normalization, planner environment fetching, and the thin kvm-query
# helpers used both by the planner and by the execution self-heal path.
from __future__ import annotations

import json
import os
import re
import unicodedata

from urirun import result_data
from urirun.runtime import v2_service
from urirun.node._util import now_id, slug
from urirun.node.reversible import TwinMemory
from urirun.node.routing import (
    registry_from_routes,
    route_target,
    route_targets_for_nodes,
    safe_route,
    target_nodes,
)


# ── Simple URL / text helpers ─────────────────────────────────────────────────

def first_url(prompt: str) -> str | None:
    match = re.search(r"https?://[^\s\"']+", prompt)
    return match.group(0) if match else None


def nl_key(text: str) -> str:
    """Lowercase NL prompt with diacritics stripped for small heuristic matchers."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return " ".join(plain.lower().split())


def append_if_available(steps: list[dict], route_uris: set[str], uri: str, payload: dict, previous: str | None) -> str | None:
    if uri not in route_uris:
        return previous
    step_id = slug(uri.replace("://", "_").replace("/", "_"))
    if any(step["id"] == step_id for step in steps):
        step_id = f"{step_id}_{len(steps) + 1}"
    steps.append({"id": step_id, "uri": uri, "payload": payload, "depends_on": [previous] if previous else []})
    return step_id


# ── Intent classification constants and helpers ───────────────────────────────

_DEFAULT_LOG_LIMIT = 20
_PROCESS_LIST_LIMIT = 12

_INTENT_NAMES: frozenset[str] = frozenset({
    "browser", "screen", "files", "invoices", "processes",
    "logs", "python", "git", "date", "health", "uname",
})


def requested_folder_path(lowered: str) -> str:
    """Best-effort folder path for common NL prompts.

    Keep this conservative: it only maps well-known aliases. More specific paths should
    come from an LLM planner or an explicit YAML flow, not from brittle string parsing.
    """
    lowered = lowered.lower()
    if any(word in lowered for word in ("downloads", "download", "pobrane", "pobran")):
        return "~/Downloads"
    return "."


def _flow_intents_llm(prompt: str) -> dict[str, bool] | None:
    """Ask the LLM to classify the prompt into the known intent set.

    Returns a complete {intent: bool} dict on success, None when LLM is not
    configured or the call fails — callers fall back to the default intent."""
    model = os.getenv("URIRUN_LLM_MODEL") or os.getenv("LLM_MODEL")
    if not model:
        return None
    try:
        from urirun.node._util import quiet_completion
        import json as _json
        names_csv = ", ".join(sorted(_INTENT_NAMES))
        resp = quiet_completion(
            model=model,
            messages=[
                {"role": "system", "content": (
                    f"Classify the user prompt. Return JSON with boolean fields: {names_csv}. "
                    "Set true for each capability the user clearly wants to use. "
                    "Respond with JSON only, no commentary."
                )},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        parsed = _json.loads(resp.choices[0].message.content or "{}")
        return {k: bool(parsed.get(k, False)) for k in _INTENT_NAMES}
    except Exception:  # noqa: BLE001 — LLM unavailable must not crash the heuristic path
        return None


def _flow_intents_lexical(prompt: str) -> dict[str, bool]:
    """Conservative no-LLM classifier for explicit, read-oriented host tasks."""
    lowered = nl_key(prompt)
    intents = {k: False for k in _INTENT_NAMES}

    def has(*patterns: str) -> bool:
        return any(re.search(pattern, lowered) for pattern in patterns)

    intents["health"] = has(r"\bhealth\b", r"\bhealthy\b", r"\bstatus\b", r"\bping\b", r"\bzdrow")
    intents["date"] = has(r"\bdate\b", r"\bcurrent date\b", r"\bdata\b", r"\bczas\b", r"\bgodzin")
    intents["processes"] = has(r"\bprocess(?:es)?\b", r"\bproces", r"\bps\b")
    intents["logs"] = has(r"\blogs?\b", r"\bdziennik")
    intents["python"] = has(r"\bpython3?\b")
    intents["git"] = has(r"\bgit\b")
    intents["uname"] = has(r"\buname\b", r"\bsystem info\b", r"\bkernel\b", r"\bos\b")
    intents["files"] = has(r"\bfiles?\b", r"\bfolder\b", r"\bdirectory\b", r"\bdownloads?\b", r"\bpliki\b", r"\bpobrane\b")
    intents["invoices"] = has(r"\binvoices?\b", r"\bfaktur", r"\brachun")
    intents["screen"] = has(r"\bscreenshot\b", r"\bcapture\b", r"\bscreen\b", r"\bzrzut\b", r"\bekran")
    return intents


def _flow_intents(prompt: str, *, use_llm: bool = True) -> dict[str, bool]:
    """Classify the prompt into host intents.

    With ``use_llm=True`` (default) attempts LLM classification. Returns the LLM
    result when available; if LLM is not configured or fails, falls back to a
    conservative lexical classifier for explicit read-oriented tasks.

    With ``use_llm=False`` skips LLM entirely and uses the same lexical
    classifier. Unrecognized prompts still produce no steps rather than a silent
    broad guess."""
    if not use_llm:
        return _flow_intents_lexical(prompt)
    intents = _flow_intents_llm(prompt)
    if intents is None:
        return _flow_intents_lexical(prompt)
    if not any(intents.values()):
        intents = _flow_intents_lexical(prompt)
        if not any(intents.values()):
            intents["processes"] = True
    return intents


# ── Heuristic flow building ───────────────────────────────────────────────────

def _append_target_steps(steps: list[dict], route_uris: set, target: str, intents: dict[str, bool], url: str, previous):
    """Append the available steps for one target node, returning the new previous-step id."""
    health_added = False

    def ensure_health(previous_id: str | None) -> str | None:
        nonlocal health_added
        if health_added:
            return previous_id
        health_added = True
        return append_if_available(steps, route_uris, f"env://{target}/runtime/query/health", {}, previous_id)

    if intents["health"]:
        previous = ensure_health(previous)
    if intents["invoices"]:
        folder = requested_folder_path(url)
        previous = append_if_available(
            steps,
            route_uris,
            f"invoice://{target}/folder/query/audit",
            {"root": folder, "extensions": "pdf,txt", "recursive": True},
            previous,
        )
    if intents["files"] or intents["invoices"]:
        previous = append_if_available(
            steps,
            route_uris,
            f"fs://{target}/dir/query/list",
            {"path": requested_folder_path(url)},
            previous,
        )
    if intents["screen"]:
        previous = ensure_health(previous)
        previous = append_if_available(steps, route_uris, f"screen://{target}/portal/query/capture", {}, previous)
        previous = append_if_available(
            steps,
            route_uris,
            f"browser://{target}/kvm/screen/query/inspect",
            {"contains": "LinkedIn" if "linkedin" in url.lower() else ""},
            previous,
        )
        previous = append_if_available(steps, route_uris, f"browser://{target}/page/query/screenshot", {}, previous)
    if intents["processes"]:
        previous = ensure_health(previous)
        previous = append_if_available(steps, route_uris, f"proc://{target}/process/query/list", {"limit": _PROCESS_LIST_LIMIT}, previous)
    if intents["browser"]:
        previous = ensure_health(previous)
        previous = append_if_available(steps, route_uris, f"browser://{target}/page/command/open", {"url": url}, previous)
        previous = append_if_available(steps, route_uris, f"browser://{target}/cdp/page/command/navigate", {"url": url}, previous)
        previous = append_if_available(
            steps,
            route_uris,
            f"browser://{target}/cdp/page/query/eval",
            {"expr": "({title: document.title, href: location.href, text: document.body ? document.body.innerText.slice(0, 1000) : ''})"},
            previous,
        )
        previous = append_if_available(steps, route_uris, f"browser://{target}/cdp/page/query/tabs", {}, previous)
    for binary, enabled in (("python3", intents["python"]), ("git", intents["git"])):
        if enabled:
            previous = ensure_health(previous)
            previous = append_if_available(steps, route_uris, f"shell://{target}/command/which", {"binary": binary}, previous)
    if intents["date"]:
        previous = ensure_health(previous)
        previous = append_if_available(steps, route_uris, f"shell://{target}/command/date", {}, previous)
    if intents["uname"]:
        previous = ensure_health(previous)
        previous = append_if_available(steps, route_uris, f"shell://{target}/command/uname", {}, previous)
    if intents["logs"]:
        previous = ensure_health(previous)
        previous = append_if_available(steps, route_uris, f"log://{target}/session/query/recent", {"limit": _DEFAULT_LOG_LIMIT}, previous)
    return previous


def heuristic_flow(prompt: str, routes: list[dict], nodes: list[dict], selected_nodes: list[str] | None = None, *, use_llm: bool = True) -> dict:
    selected = target_nodes(prompt, nodes, selected_nodes)

    def selected_route(route: dict) -> bool:
        if not selected:
            return True
        if route.get("node"):
            return route.get("node") in selected
        try:
            return route_target(str(route.get("uri") or "")) in selected
        except Exception:
            return False

    selected_routes = [route for route in routes if safe_route(route) and selected_route(route)]
    route_uris = {route["uri"] for route in selected_routes}
    targets = route_targets_for_nodes(selected_routes, selected)
    lowered = nl_key(prompt)
    intents = _flow_intents(prompt, use_llm=use_llm)
    url = first_url(prompt) or ("https://www.linkedin.com/feed/" if "linkedin" in lowered else "https://example.com/")
    path = requested_folder_path(lowered)
    steps: list[dict] = []
    previous = None
    for target in targets:
        previous = _append_target_steps(steps, route_uris, target, intents, path if (intents["files"] or intents["invoices"]) else url, previous)

    # The health probe is a companion to real work, never a result on its own: when the
    # user's actual intent (browser, screen, …) had no available route, every real step is
    # skipped and only `ensure_health` survives. Returning that lone probe would report a
    # misleading "ok: 1 URI step" for a request we couldn't fulfil — so drop it and let the
    # caller raise the honest "no URI steps; check the mesh config" explanation instead.
    if not intents["health"] and all(step["uri"].endswith("/runtime/query/health") for step in steps):
        steps = []

    return {
        "task": {"id": f"nl_uri_flow_{now_id()}", "title": "NL to URI host flow", "source": "heuristic"},
        "steps": steps,
    }


# ── JSON / URI utilities ──────────────────────────────────────────────────────

def json_from_text(text: str) -> dict:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.S)
    if fenced:
        stripped = fenced.group(1)
    elif not stripped.startswith("{"):
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            stripped = stripped[start : end + 1]
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

def _validate_step_payload(uri: str, payload: dict, routes: "list[dict] | None") -> None:
    """Raise ValueError when routes include an inputSchema that payload doesn't satisfy."""
    if not routes:
        return
    route = next((r for r in routes if r.get("uri") == uri), None)
    if not (route and route.get("inputSchema")):
        return
    import jsonschema  # noqa: PLC0415
    try:
        jsonschema.validate(instance=payload, schema=route["inputSchema"])
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


# ── CDP probe injection ───────────────────────────────────────────────────────

_CDP_ENSURE_SUFFIX = "/cdp/session/command/ensure"
_CDP_READY_SUFFIX = "/cdp/session/query/ready"
_CDP_PAGE_PREFIX = "/cdp/page/"


def _needs_session_ready_after_ensure(prev_uri: str, next_uri: str | None) -> bool:
    """True when an ensure→page jump skips the readiness probe the launch/probe split
    requires. ``cdp/session/command/ensure`` returns ``launching:true`` (launch fired,
    port NOT bound yet); ``cdp/page/*`` opens a WS to that port, so it deadlocks until
    the bind happens. ``cdp/session/query/ready`` is the idempotent poll that closes
    that gap without spawning a competing Chrome (re-calling ensure would)."""
    if not prev_uri.endswith(_CDP_ENSURE_SUFFIX):
        return False
    if next_uri is None:
        return False
    # /cdp/session/query/ready (the probe) and /cdp/session/query/status do NOT need
    # the port bound — only anything that opens a page-level WS does.
    if next_uri.endswith(_CDP_READY_SUFFIX):
        return False
    target = route_target(prev_uri)
    return _CDP_PAGE_PREFIX in next_uri and route_target(next_uri) == target


_SCREENSHOT_KWS = frozenset({
    "screenshot", "zrzut ekranu", "zrzut", "screenshota", "printscreen",
    "screen grab", "capture screen", "snap screen",
})


def _inject_capture_if_needed(flow: dict, prompt: str, allowed_uris: set[str]) -> dict:
    """Append screen/query/capture as the last step when the prompt asks for a screenshot
    but the LLM forgot to include it. Idempotent: no-op when a capture step already exists
    or when no capture route is served by the mesh."""
    low = nl_key(prompt)
    if not any(kw in low for kw in _SCREENSHOT_KWS):
        return flow
    steps = list(flow.get("steps") or [])
    if any("screen/query/capture" in str(s.get("uri") or "") for s in steps):
        return flow
    capture_uri = next(
        (u for u in sorted(allowed_uris) if "screen/query/capture" in u), None
    )
    if not capture_uri:
        return flow
    last_id = steps[-1]["id"] if steps else None
    steps.append({
        "id": "capture_screen",
        "uri": capture_uri,
        "payload": {},
        "depends_on": [last_id] if last_id else [],
    })
    return {**flow, "steps": steps}


def _inject_cdp_ready_probes(steps: list[dict], allowed_uris: set[str],
                             used: set[str], routes: list[dict] | None = None) -> list[dict]:
    """Insert a ``cdp/session/query/ready`` step between every ensure→page jump, when
    the probe URI is available. Idempotent: skips when a probe is already present, and
    never injects when the route isn't served (keeps flows runnable on meshes that
    don't expose kvm/cdp). The injected step is built through ``_normalize_flow_step``
    so it carries the same validated shape as planner-authored steps."""
    out: list[dict] = []
    for index, step in enumerate(steps):
        out.append(step)
        next_step = steps[index + 1] if index + 1 < len(steps) else None
        next_uri = next_step.get("uri") if isinstance(next_step, dict) else None
        if not _needs_session_ready_after_ensure(step["uri"], next_uri):
            continue
        target = route_target(step["uri"])
        probe_uri = f"kvm://{target}{_CDP_READY_SUFFIX}"
        if not _uri_is_available(probe_uri, allowed_uris):
            continue
        probe = _normalize_flow_step(
            {"id": f"{step['id']}_await_ready", "uri": probe_uri,
             "payload": {"timeout": 25}, "depends_on": [step["id"]]},
            index=len(steps) + len(out), allowed_uris=allowed_uris, used=used, routes=routes
        )
        out.append(probe)
        # re-point the next step's depends_on at the probe so the chain stays linear.
        if isinstance(next_step, dict):
            deps = next_step.setdefault("depends_on", [])
            deps = [probe["id"] if d == step["id"] else d for d in deps]
            if probe["id"] not in deps:
                deps.insert(0, probe["id"])
            next_step["depends_on"] = deps
    return out


def _collect_infeasible_constraints(environments: list[dict] | None) -> list[dict]:
    """Flatten `constraints` entries with kind='infeasible' from all planner environments."""
    if not environments:
        return []
    result = []
    for env in environments:
        for c in (env.get("constraints") or []):
            if c.get("kind") == "infeasible":
                result.append(c)
    return result


def _strip_focus_from_cdp_flows(steps: list[dict]) -> list[dict]:
    """Remove window/command/focus steps from flows that use CDP.

    CDP communicates directly with the browser process — window focus is irrelevant
    and blocks flows when the window title doesn't match yet (e.g. pre-navigation).
    Repairs LLM non-compliance with the CDP FOCUS RULE in the planner prompt.

    Graph bypass: when step B (removed) depends on A, and step C depends on B,
    C's depends_on is rewritten to A (B's predecessors), not left empty.
    """
    has_cdp = any("cdp/session/command/ensure" in str(s.get("uri", "")) for s in steps)
    if not has_cdp:
        return steps

    removed: dict[str, list[str]] = {}  # id → its own deps (for bypass rewriting)
    for step in steps:
        if "window/command/focus" in str(step.get("uri", "")):
            removed[step.get("id", "")] = list(step.get("depends_on") or [])

    if not removed:
        return steps

    def _bypass(deps: list[str]) -> list[str]:
        """Replace any dep on a removed step with that step's own deps (transitive)."""
        result: list[str] = []
        for d in deps:
            if d in removed:
                result.extend(_bypass(removed[d]))
            else:
                result.append(d)
        return result

    return [
        {**s, "depends_on": _bypass(list(s.get("depends_on") or []))}
        for s in steps if s.get("id", "") not in removed
    ]


# Real Chrome-family user-data-dir roots. A user_data_dir under one of these is the user's LIVE
# profile, which must NOT be handed to a debug Chrome directly: launching --remote-debugging-port
# over a profile that the user's own Chrome already holds fights the SingletonLock (the launch
# forwards to the running browser or opens a throwaway), so NO session cookies reach the CDP
# profile (authCopied:[] → the page lands on the login wall). The auth path is copy_from, which
# CLONES the minimal auth files into a dedicated /tmp CDP profile (urirun_cdp.cdp._copy_auth) —
# lock-safe AND logged in. Markers are matched case-insensitively (macOS paths are mixed-case).
_BROWSER_PROFILE_MARKERS = (
    ".config/google-chrome", ".config/chromium", ".config/microsoft-edge",
    ".config/bravesoftware", "library/application support/google/chrome",
    "library/application support/chromium",
)


def _chrome_profile_root(path: str | None) -> str | None:
    """The user-data-dir ROOT (the dir holding ``Local State`` + the ``Default/`` profile) for a
    Chrome-family profile path, or None when ``path`` isn't a recognised browser profile. ``copy_from``
    resolves ``_AUTH_FILES`` (e.g. ``Default/Cookies``) against this root, so a path that points INTO
    the profile (…/google-chrome/Default) is trimmed back to …/google-chrome. Temp / already-dedicated
    CDP dirs are rejected (they hold no real session)."""
    raw = str(path or "").strip()
    low = raw.lower()
    if not raw or low.startswith("/tmp/") or "urirun-cdp" in low:
        return None
    for marker in _BROWSER_PROFILE_MARKERS:
        idx = low.find(marker)
        if idx != -1:
            return raw[: idx + len(marker)]
    return None


def _rewrite_cdp_profile_for_auth(steps: list[dict]) -> list[dict]:
    """Repair the login-profile anti-pattern in ``cdp/session/command/ensure`` steps: when the LLM
    (per the LOGIN PROFILE prompt rule) sets ``user_data_dir`` to the user's live Chrome profile,
    rewrite it to ``copy_from`` of the profile ROOT so the connector clones the auth files into a
    dedicated CDP profile instead of fighting the live profile's SingletonLock (the cause of
    authCopied:[] → login wall). Idempotent: only ensure steps whose ``user_data_dir`` is a real
    browser profile and that don't already set ``copy_from`` are touched."""
    out: list[dict] = []
    for step in steps:
        uri = str(step.get("uri") or "")
        payload = step.get("payload")
        if uri.endswith(_CDP_ENSURE_SUFFIX) and isinstance(payload, dict) and not payload.get("copy_from"):
            root = _chrome_profile_root(payload.get("user_data_dir"))
            if root:
                new_payload = {k: v for k, v in payload.items() if k != "user_data_dir"}
                new_payload["copy_from"] = root
                out.append({**step, "payload": new_payload})
                continue
        out.append(step)
    return out


def normalize_flow(flow: dict, allowed_uris: set[str], routes: list[dict] | None = None,
                   infeasible_constraints: list[dict] | None = None) -> dict:
    task = flow.get("task") if isinstance(flow.get("task"), dict) else {}
    raw_steps = flow.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("flow must contain non-empty steps")
    used: set[str] = set()
    steps = [_normalize_flow_step(step, index, allowed_uris, used, routes=routes,
                                  infeasible_constraints=infeasible_constraints)
             for index, step in enumerate(raw_steps, start=1)]
    steps = _strip_focus_from_cdp_flows(steps)
    steps = _rewrite_cdp_profile_for_auth(steps)
    steps = _inject_cdp_ready_probes(steps, allowed_uris, used, routes=routes)
    return {"task": _normalize_flow_task(task), "steps": steps}


def normalize_flow_or_explain(
    flow: dict,
    allowed_uris: set[str],
    *,
    routes: list[dict],
    selected_nodes: list[str] | None = None,
    planner_reason: str = "",
    environments: list[dict] | None = None,
) -> dict:
    infeasible = _collect_infeasible_constraints(environments)
    try:
        return normalize_flow(flow, allowed_uris, routes=routes,
                              infeasible_constraints=infeasible or None)
    except ValueError as exc:
        if str(exc) != "flow must contain non-empty steps":
            raise
        nodes = sorted({str(route.get("node") or "") for route in routes if route.get("node")})
        sample = sorted(allowed_uris)[:8]
        detail = {
            "safeRoutes": len(allowed_uris),
            "nodes": nodes,
            "selectedNodes": selected_nodes or [],
            "routeSample": sample,
        }
        reason = f"; planner reason: {planner_reason}" if planner_reason else ""
        raise ValueError(
            "NL flow generated no URI steps. "
            f"Discovered {detail['safeRoutes']} safe route(s) on node(s) {nodes or '[]'}"
            f"{'; selected ' + repr(selected_nodes) if selected_nodes else ''}. "
            "Check the mesh config or pass --node-url [NAME=]URL. "
            f"Sample routes: {sample}{reason}"
        ) from exc


# ── LLM flow generation ───────────────────────────────────────────────────────

def llm_flow(prompt: str, routes: list[dict], nodes: list[dict],
             environments: list[dict] | None = None) -> dict:
    model = os.getenv("URIRUN_LLM_MODEL") or os.getenv("LLM_MODEL")
    if not model:
        raise RuntimeError("URIRUN_LLM_MODEL or LLM_MODEL is not set")
    from urirun.node._util import quiet_completion

    allowed_routes = [
        {
            "uri": route["uri"],
            "node": route.get("node"),
            "kind": route.get("kind"),
            "title": route.get("title"),
            "inputSchema": route.get("inputSchema") or {"type": "object"},
        }
        for route in routes
        if safe_route(route)
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "Return strict JSON only. Build a safe urirun flow for a host that controls nodes. "
                "Use only allowedRoutes. If the request mentions all nodes, use every matching node. "
                "Do not invent URIs. "
                # Desktop/UI grounding hints — make NL desktop-control flows execute correctly:
                "language. If the user's request is in a non-English language (like Polish), assume the UI is likely localized to that language and use appropriate translated labels (e.g. 'Zacznij publikację' instead of 'Start a post'). When targeting by 'text', omit the 'role' field if you are unsure of the exact HTML element type. "
                "After any launch or navigation, insert an input/command/wait (a few seconds) "
                "before the first interaction so the page can settle. "
                # Launch/probe split: ensure FIRES the launch and returns fast (launching:true,
                # port NOT bound yet); the next cdp/page/* step opens a WS to that port and
                # would deadlock until the bind happens. session/query/ready is the idempotent
                # poll that closes the gap without spawning a competing Chrome (re-calling
                # ensure would fight over the profile lock). The normalizer injects this probe
                # automatically when missing, but emitting it explicitly keeps the plan honest.
                "CDP launch is a two-step launch/probe split: 'cdp/session/command/ensure' FIRES "
                "the launch and returns immediately (launching:true, port NOT yet bound); the "
                "next step must be 'cdp/session/query/ready' (polls the debug port, idempotent — "
                "never re-call ensure, it would spawn a competing Chrome over the profile lock). "
                "Only then run any 'cdp/page/*' step (it opens a WS to that port). Never emit "
                "'cdp/session/command/launch' — it does not exist; use 'ensure'. "
                # Route-selection preference: DOM-level (CDP) beats pixel-level (OCR) for web content.
                "ROUTE PREFERENCE — when the target is web content in a browser and the allowedRoutes "
                "expose CDP page commands (uris containing 'cdp/page/command/click' or "
                "'cdp/page/command/fill'), PREFER THEM for clicking buttons/links and filling fields: "
                "Do not infer cdp/page/command/click or cdp/page/command/fill from cdp/page/command/navigate; "
                "click/fill are separate routes and must appear explicitly in allowedRoutes. "
                "they act through the DOM by role/visible-label, so they are coordinate-free and immune "
                "to OCR misreads. For those CDP commands pass the target as 'text' (the visible label) "
                "and 'role' (e.g. 'button', 'link', 'textbox') — NOT a CSS or Playwright selector — and "
                "for fill put the content in 'value'. Use the pixel/OS "
                "routes ('ui/command/click', "
                "'ui/command/click-text', 'input/command/type') only for NATIVE desktop apps, or as a "
                "fallback when no CDP session/route is available. "
                "CDP FOCUS RULE: NEVER emit 'window/command/focus' when the flow uses CDP (cdp/session/command/ensure) — CDP communicates directly with the browser process and does not require the window to be focused or visible. Only use 'window/command/focus' for native desktop apps that do not have a CDP session. "
                "CRITICAL: Always break down the task into very detailed, atomic declarative steps. "
                "When the task says 'open <website>', ALWAYS include cdp/page/command/navigate (payload: {url: 'https://...'}) BEFORE any page interaction or verification. Never skip the navigate step even if a CDP session is already running. "
                "Always add explicit validation steps (e.g., using 'ui/query/verify', 'cdp/page/query/ready', or evaluating page state) after actions to confirm success before proceeding. "
                "NOTE: 'ui/query/verify' requires the field 'expect' (not 'text') — payload must be {\"expect\": \"<visible text to assert\"}. "
                "GATE VERIFY: when a verify step checks for login/presence of a UI element that is REQUIRED for the next action (e.g. 'Zacznij publikację' before clicking Publish), add {\"required\": true} to the verify payload — this fails the flow early when not logged in, instead of continuing into failing click steps. "
                "LOGIN PROFILE: when the task requires being logged in to a service (LinkedIn, Google, GitHub…), set {\"copy_from\": \"~/.config/google-chrome\"} (the user-data-dir ROOT, not the Default subdir) in the cdp/session/command/ensure payload — this CLONES the saved session cookies into a dedicated CDP profile so Chrome opens already logged in WITHOUT fighting the live profile's lock. Do NOT set user_data_dir to the live profile (it launches over the SingletonLock and copies no cookies → login wall); never use an empty or temp profile for tasks that require authentication. "
                "SCREENSHOT RULE: when the request contains 'screenshot', 'zrzut ekranu', 'capture', 'snap' or similar, the LAST step MUST be screen/query/capture — ALWAYS, regardless of login state, page content, or what verify found. Never substitute a log note for a screenshot step. "
                # Concrete-state grounding: when an 'environments' field is present it is the LIVE
                # capability profile + foreground surface of each node — GROUND your steps on it:
                "honour each node's 'bestSurface' and ALL items in its 'guidance' list (they are "
                "hard environment rules, not suggestions — e.g. if guidance says TYPE via atspi/uinput "
                "is NOT EXECUTABLE, NEVER emit a fill/type step via those surfaces). "
                "Check 'actionMatrix' per node: if an action's value for a surface is 'not_executable' "
                "or 'blocked', do NOT plan that action on that surface — use the surface where the same "
                "action is 'executable' instead (e.g. type → cdp only). "
                "Check 'sessionMap' per node: if the task involves a service (linkedin, google, github…) "
                "and that service appears in sessionMap with running=false or throwaway=true or cdp_port=null, "
                "the FIRST step must be cdp/session/command/ensure with copy_from set to the profile path "
                "from sessionMap — this copies the real session cookies to the CDP profile so navigation "
                "lands on the logged-in page, NOT the login page. NEVER skip this step for service tasks. "
                "Use the foreground page's REAL on-screen labels (its language), "
                "and refuse UI steps where controllable is false."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "request": prompt,
                    "nodes": [{"name": node["name"], "reachable": node.get("reachable")} for node in nodes],
                    "environments": environments or [],
                    "allowedRoutes": allowed_routes,
                    "shape": {
                        "task": {"id": "short_id", "title": "title"},
                        "steps": [{"id": "id", "uri": "uri", "payload": {}, "depends_on": []}],
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]
    response = quiet_completion(model=model, messages=messages, temperature=0, response_format={"type": "json_object"})
    return json_from_text(response.choices[0].message.content)


# ── Session helpers ───────────────────────────────────────────────────────────

def _build_session_map(browser_sessions: list) -> dict:
    """Build service→{browser,profile,cdp_port,running,throwaway} from raw browser session list."""
    session_map: dict[str, dict] = {}
    for entry in browser_sessions:
        for svc, active in (entry.get("sessions") or {}).items():
            if active and svc not in session_map:
                session_map[svc] = {
                    "browser": entry.get("browser"),
                    "profile": entry.get("profile"),
                    "cdp_port": entry.get("cdp_port"),
                    "running": entry.get("running", False),
                    "throwaway": entry.get("throwaway", False),
                }
    return session_map


def _append_session_guidance(ctx: dict, session_map: dict) -> None:
    """Append planner guidance lines for each discovered session."""
    for svc, info in session_map.items():
        if not info["running"] or info["throwaway"] or info["cdp_port"] is None:
            profile_path = info.get("profile") or "unknown profile"
            ctx["guidance"].append(
                f"SERVICE SESSION '{svc}': session cookies found in {info['browser']} "
                f"profile '{profile_path}' but that browser is NOT running with CDP. "
                f"To use this session: launch Chrome with copy_from='{profile_path}' "
                f"via cdp/session/command/ensure (copies auth files to CDP profile), "
                f"then proceed with CDP steps. Do NOT navigate to {svc}.com in the "
                f"throwaway CDP profile — it will show a login page."
            )
        elif info["cdp_port"]:
            ctx["guidance"].append(
                f"SERVICE SESSION '{svc}': active in {info['browser']} on CDP port "
                f"{info['cdp_port']}. Use that CDP endpoint for {svc} tasks directly."
            )


# ── KVM read helpers (shared by planner and execution self-heal) ──────────────

def _fetch_kvm_query(step: dict, registry: dict, route: str, marker: str) -> dict | None:
    """Best-effort fetch of a kvm read-only query (env/query/profile, surface/query/current)
    for the failing node, so the self-heal fits its remediation to the live machine + surface.
    None on any hiccup — this context is an optimisation, never a correctness dependency."""
    target = route_target(str(step.get("uri") or ""))
    if not target:
        return None
    candidates = [f"kvm://{target}/{route}"]
    routed = _kvm_query_uri_for_node(registry, target, route)
    if routed and routed not in candidates:
        candidates.append(routed)
    if target == "host":
        # For host-targeted calls keep the historical direct path only. A remote node can
        # advertise kvm://host/...; callers that mean that node pass kvm://<node>/... and
        # get routed through the registry metadata fallback above.
        candidates = candidates[:1]
    for uri in candidates:
        try:
            env = v2_service.call(uri, {}, registry, mode="execute")
            value = result_data(env)
            if isinstance(value, dict) and marker in value:
                return value
        except Exception:  # noqa: BLE001
            continue
    return None


def _kvm_query_uri_for_node(registry: dict, node: str, route: str) -> str | None:
    """Return the advertised KVM query URI for a host-config node name.

    Remote nodes often serve local capabilities as kvm://host/... while the compiled registry
    carries the real mesh node in route metadata (meta.node). Planner/memory code asks for
    "lenovo"; dispatch must call the advertised URI, not invent kvm://lenovo/... when that
    route does not exist.
    """
    suffix = f"/{route}"
    for item in (registry or {}).get("index", {}).values():
        if not isinstance(item, dict):
            continue
        uri = str(item.get("uri") or "")
        meta = item.get("meta") or {}
        if uri.startswith("kvm://") and uri.endswith(suffix) and str(meta.get("node") or "") == node:
            return uri
    return None


def _fetch_env_profile(step: dict, registry: dict) -> dict | None:
    return _fetch_kvm_query(step, registry, "env/query/profile", "controlStrategies")


def _fetch_surface(step: dict, registry: dict) -> dict | None:
    return _fetch_kvm_query(step, registry, "surface/query/current", "kind")


# ── Planner environment fetching ──────────────────────────────────────────────

def fetch_planner_environments(node_names: list[str], registry: dict, mesh: dict | None = None,
                               *, memory: "TwinMemory | None" = None) -> list[dict]:
    """Best-effort live capability profile + foreground surface per node, formatted as
    planner_context facts+guidance — so the planner GROUNDS on reality (surface, language,
    known-good, drift) instead of guessing. Sets the serviceMap from ``mesh`` so the kvm queries
    route to the node; skips any node that doesn't answer (non-kvm / unreachable); never raises.
    ``memory`` threads the durable TwinMemory into planner_context so drift guidance is included."""
    from urirun.node.reversible import planner_context
    old_map = os.environ.get("URI_SERVICE_MAP")
    if mesh is not None:
        os.environ["URI_SERVICE_MAP"] = json.dumps(mesh.get("serviceMap") or {})
    out: list[dict] = []
    try:
        for name in node_names or []:
            prof = _fetch_kvm_query({"uri": f"kvm://{name}/x"}, registry, "env/query/profile", "controlStrategies")
            if not prof:
                continue
            surf = _fetch_kvm_query({"uri": f"kvm://{name}/x"}, registry, "surface/query/current", "kind")
            ctx = planner_context(name, prof, surf, memory=memory)
            # Task-aware session discovery: scan running browsers and installed profiles so the
            # planner knows which browser/profile is logged in to which service. Cheap, non-blocking.
            browser_sess = _fetch_kvm_query({"uri": f"kvm://{name}/x"}, registry, "browser/query/sessions", "browsers")
            if browser_sess is not None:
                raw = browser_sess.get("browsers", []) if isinstance(browser_sess, dict) else browser_sess
                ctx["browserSessions"] = raw if isinstance(raw, list) else []
                session_map = _build_session_map(ctx["browserSessions"])
                ctx["sessionMap"] = session_map
                _append_session_guidance(ctx, session_map)
            out.append(ctx)
    finally:
        if mesh is not None:
            if old_map is None:
                os.environ.pop("URI_SERVICE_MAP", None)
            else:
                os.environ["URI_SERVICE_MAP"] = old_map
    return out


# ── Top-level flow generation entry point ────────────────────────────────────

def make_flow(prompt: str, mesh: dict, selected_nodes: list[str] | None = None, use_llm: bool = True,
              environments: list[dict] | None = None) -> tuple[dict, dict]:
    routes = [route for route in mesh["routes"] if safe_route(route)]
    allowed = {route["uri"] for route in routes}
    if use_llm:
        try:
            flow = normalize_flow_or_explain(
                llm_flow(prompt, routes, mesh["nodes"], environments=environments),
                allowed,
                routes=routes,
                selected_nodes=selected_nodes,
                environments=environments,
            )
            return _inject_capture_if_needed(flow, prompt, allowed), {"provider": "litellm", "fallback": False}
        except Exception as exc:  # noqa: BLE001 - host should still be usable without an LLM.
            flow = heuristic_flow(prompt, routes, mesh["nodes"], selected_nodes, use_llm=True)
            flow = normalize_flow_or_explain(
                flow,
                allowed,
                routes=routes,
                selected_nodes=selected_nodes,
                planner_reason=str(exc),
                environments=environments,
            )
            return _inject_capture_if_needed(flow, prompt, allowed), {"provider": "heuristic", "fallback": True, "reason": str(exc)}
    flow = heuristic_flow(prompt, routes, mesh["nodes"], selected_nodes, use_llm=False)
    flow = normalize_flow_or_explain(
        flow,
        allowed,
        routes=routes,
        selected_nodes=selected_nodes,
        planner_reason="LLM disabled",
        environments=environments,
    )
    return _inject_capture_if_needed(flow, prompt, allowed), {"provider": "heuristic", "fallback": True, "reason": "LLM disabled"}
