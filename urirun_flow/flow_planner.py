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

from urirun.runtime import v2_service
from urirun.node.reversible import TwinMemory
from urirun_flow.envelope import result_data
from urirun_flow._util import now_id, quiet_completion, slug
from urirun_connector_router.routing import (
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


def _time_query_uri_for_target(route_uris: set[str], target: str) -> str | None:
    """Return the typed time query route for a target when the action space offers it."""
    uri = f"time://{target}/clock/query/now"
    return uri if uri in route_uris else None


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


def _configured_llm_model(override: str | None = None) -> str | None:
    for value in (override, os.getenv("URIRUN_LLM_MODEL"), os.getenv("LLM_MODEL")):
        model = str(value or "").strip()
        if model:
            return model
    return None


def _flow_intents_llm(prompt: str, llm_model: str | None = None) -> dict[str, bool] | None:
    """Ask the LLM to classify the prompt into the known intent set.

    Returns a complete {intent: bool} dict on success, None when LLM is not
    configured or the call fails — callers fall back to the default intent."""
    model = _configured_llm_model(llm_model)
    if not model:
        return None
    try:
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
    intents["screen"] = has(
        r"\bscreenshot\b", r"\bcapture\b", r"\bscreen\b", r"\bmonitor(?:a|ze|ow|y)?\b",
        r"\bzrzut\w*\b", r"\bekran\w*\b", r"\bscreen\w*\b", r"\bprzechwyc\w*\b",
    )
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

def _append_target_steps(steps: list[dict], route_uris: set, target: str, intents: dict[str, bool],
                         url: str, previous, *, prompt: str = "",
                         environments: list[dict] | None = None,
                         window_anchor: dict | None = None):
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
        capture_payload = _screenshot_capture_payload(prompt, environments=environments, target=target)
        window_uri = f"kvm://{target}/window/query/list"
        capture_uri = f"kvm://{target}/screen/query/capture"
        if window_anchor and window_uri in route_uris and capture_uri in route_uris:
            window_payload = dict(window_anchor.get("payload") or {})
            window_step = append_if_available(steps, route_uris, window_uri, window_payload, previous)
            if window_step:
                payload = {**capture_payload, "monitor_from": f"{window_step}.result.value.selected.monitor"}
                payload.pop("monitor", None)
                payload.pop("scope", None)
                previous = append_if_available(steps, route_uris, capture_uri, payload, window_step)
            else:
                previous = append_if_available(steps, route_uris, capture_uri, capture_payload, previous)
        else:
            previous = append_if_available(steps, route_uris, capture_uri, capture_payload, previous)
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
        time_uri = _time_query_uri_for_target(route_uris, target)
        if time_uri:
            previous = append_if_available(steps, route_uris, time_uri, {}, previous)
        else:
            previous = ensure_health(previous)
            previous = append_if_available(steps, route_uris, f"shell://{target}/command/date", {}, previous)
    if intents["uname"]:
        previous = ensure_health(previous)
        previous = append_if_available(steps, route_uris, f"shell://{target}/command/uname", {}, previous)
    if intents["logs"]:
        previous = ensure_health(previous)
        previous = append_if_available(steps, route_uris, f"log://{target}/session/query/recent", {"limit": _DEFAULT_LOG_LIMIT}, previous)
    return previous


_WINDOW_ANCHOR_STOPWORDS = {
    "the", "and", "for", "with", "where", "showing", "open", "opened",
    "main", "stage", "desktop", "icons", "frame", "window",
}


def _text_tokens(text: object) -> set[str]:
    return {
        tok for tok in re.findall(r"[a-z0-9]{3,}", nl_key(str(text or "")))
        if tok not in _WINDOW_ANCHOR_STOPWORDS
    }


def _environment_windows(env: dict) -> list[dict]:
    """Return window inventory from the planner environment, regardless of carrier shape."""
    for key in ("windows", "windowList"):
        val = env.get(key)
        if isinstance(val, list):
            return [w for w in val if isinstance(w, dict)]
    profile = env.get("profile") if isinstance(env.get("profile"), dict) else {}
    val = profile.get("windows") or profile.get("windowList")
    return [w for w in val if isinstance(w, dict)] if isinstance(val, list) else []


def _window_anchor_from_environment(prompt: str, environments: list[dict] | None, target: str) -> dict | None:
    """Resolve a named app/window mention against LIVE twin window inventory.

    This is intentionally data-driven: it does not know about Chrome, VS Code, terminals, etc.
    It matches prompt tokens against the app/title tokens that `window/query/list` actually
    observed, then asks the connector to select that window. If no observed window name is
    mentioned, the env-enum gate remains responsible for asking the user.
    """
    prompt_tokens = _text_tokens(prompt)
    if not prompt_tokens:
        return None
    best: tuple[int, int, dict] | None = None
    for env in environments or []:
        if str(env.get("node") or target) != target:
            continue
        for win in _environment_windows(env):
            app_tokens = _text_tokens(win.get("app"))
            title_tokens = _text_tokens(win.get("title"))
            app_matches = app_tokens & prompt_tokens
            title_matches = title_tokens & prompt_tokens
            if not app_matches and not title_matches:
                continue
            payload: dict[str, str] = {}
            if app_matches:
                payload["app"] = sorted(app_matches, key=lambda s: (-len(s), s))[0]
            elif title_matches:
                payload["title"] = sorted(title_matches, key=lambda s: (-len(s), s))[0]
            score = len(app_matches) * 3 + len(title_matches)
            has_monitor = 1 if win.get("monitor") is not None or win.get("monitorConnector") else 0
            candidate = (score, has_monitor, payload)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
    return {"payload": best[2]} if best else None


def _prompt_needs_window_inventory(prompt: str) -> bool:
    """True when planner context should include live windows, not only monitor inventory.

    This is a prefetch decision, not a route plan: the actual selected app/window still comes from
    live `window/query/list` data via `_window_anchor_from_environment`.
    """
    low = nl_key(prompt)
    if not _flow_intents_lexical(prompt).get("screen"):
        return False
    return bool(
        re.search(r"\b(na\s+kt[oó]r\w+|gdzie|where|which|contains?|zawiera|jest)\b", low)
        or re.search(r"\b(okn\w+|window|app|aplikac\w+|browser|przegl\w+)\b", low)
    )


def heuristic_flow(prompt: str, routes: list[dict], nodes: list[dict], selected_nodes: list[str] | None = None,
                   *, use_llm: bool = True, environments: list[dict] | None = None) -> dict:
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
    if not targets and not selected:
        for route in selected_routes:
            target = route_target(str(route.get("uri") or ""))
            if target and target not in targets:
                targets.append(target)
    lowered = nl_key(prompt)
    intents = _flow_intents(prompt, use_llm=use_llm)
    url = first_url(prompt) or ("https://www.linkedin.com/feed/" if "linkedin" in lowered else "https://example.com/")
    path = requested_folder_path(lowered)
    steps: list[dict] = []
    previous = None
    for target in targets:
        previous = _append_target_steps(
            steps,
            route_uris,
            target,
            intents,
            path if (intents["files"] or intents["invoices"]) else url,
            previous,
            prompt=prompt,
            environments=environments,
            window_anchor=_window_anchor_from_environment(prompt, environments, target),
        )

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


# JSON/URI utilities and flow normalization extracted to _flow_normalize
from ._flow_normalize import (  # noqa: E402
    json_from_text,
    _uri_segments,
    _uri_matches_template,
    _uri_is_available,
    _CDP_PAGE_TO_UI_SUFFIX,
    _replace_uri_action_path,
    _fallback_ui_uri_for_unavailable_cdp,
    _rewrite_payload_for_fallback_uri,
    _infeasibility_error,
    _step_is_infeasible,
    _schema_placeholder,
    _payload_for_schema_validation,
    _canonicalize_template_refs,
    _validate_step_payload,
    _unique_step_id,
    _normalize_flow_step,
    _normalize_flow_task,
    _WINDOW_LIST_SUFFIX,
    _WINDOW_FOCUS_SUFFIX,
    _SCREEN_CAPTURE_SUFFIX,
    _positive_int,
    _capture_needs_window_monitor,
    _capture_scope_conflicts_with_ref,
    _needs_producer,
    _scope_conflicts_with_producer,
    _wire_env_consumer,
    _focus_uri_for_window_uri,
    _window_focus_selector,
    _capture_uses_window_monitor,
    _generated_step_id,
    _depends_on_window_focus,
    _dedupe_equivalent_focus_steps,
    _focus_window_before_monitor_capture,
    _CDP_ENSURE_SUFFIX,
    _CDP_READY_SUFFIX,
    _CDP_PAGE_PREFIX
)

# env-enum domain wiring: mutable to allow tests to add specs without new code.
_ENV_DOMAIN_PRODUCERS = (
    {"domain": "env:monitors.id", "consumer_suffix": _SCREEN_CAPTURE_SUFFIX,
     "producer_suffix": _WINDOW_LIST_SUFFIX, "param": "monitor",
     "path": "result.value.selected.monitor",
     "skip_scopes": ("all", "all-monitors", "desktop")},
)


def _bind_env_domain_producers(steps: list[dict]) -> list[dict]:
    """Wire each env-enum CONSUMER param to an earlier PRODUCER of that domain, driven by the
    declarative _ENV_DOMAIN_PRODUCERS table — no per-capability branching. The window query owns
    selection; the capture consumes it; a new env-enum is a table row, not a code path."""
    out: list[dict] = []
    latest: dict = {}
    for step in steps:
        uri = str(step.get("uri") or "")
        for spec in _ENV_DOMAIN_PRODUCERS:
            if uri.endswith(spec["producer_suffix"]):
                latest[spec["producer_suffix"]] = step["id"]
        for spec in _ENV_DOMAIN_PRODUCERS:
            producer_id = latest.get(spec["producer_suffix"])
            if uri.endswith(spec["consumer_suffix"]) and producer_id:
                step = _wire_env_consumer(step, {s["id"] for s in out}, spec, producer_id)
        out.append(step)
    return out


def _bind_window_monitor_capture(steps: list[dict]) -> list[dict]:
    """Back-compat shim: the window-list → capture.monitor binding is now ONE row in
    _ENV_DOMAIN_PRODUCERS, applied generically by _bind_env_domain_producers."""
    return _bind_env_domain_producers(steps)


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
    "screen grab", "capture screen", "capture monitor", "snap screen",
    "screena", "zrzutuj", "przechwyc", "monitor",
})

_ALL_MONITOR_KWS = (
    "all monitors", "all screens", "whole desktop", "entire desktop",
    "wszystkie monitory", "wszystkich monitorow", "wszystkich monitorw",
    "wszystkie ekrany", "wszystkich ekranow", "wszystkich ekranw",
    "caly pulpit", "calego pulpitu", "cay desktop", "caego pulpitu",
)

_MONITOR_ORDINALS = {
    "pierwszy": 1, "pierwszego": 1, "first": 1,
    "drugi": 2, "drugiego": 2, "second": 2,
    "trzeci": 3, "trzeciego": 3, "third": 3,
    "czwarty": 4, "czwartego": 4, "fourth": 4,
}


def _primary_monitor_id_from_environment(
    environments: list[dict] | None,
    target: str | None,
) -> int | None:
    for env in environments or []:
        if target and str(env.get("node") or target) != target:
            continue
        inventory = env.get("inventory") if isinstance(env.get("inventory"), dict) else {}
        domains: dict = {}
        for source in (env.get("domains"), inventory.get("domains")):
            if isinstance(source, dict):
                domains.update(source)
        for opt in domains.get("env:monitors.id", []) or []:
            if not isinstance(opt, dict) or not opt.get("primary"):
                continue
            value = opt.get("value", opt.get("id", opt.get("index")))
            if isinstance(value, int) and value > 0:
                return value
        for mon in (env.get("monitors") or inventory.get("monitors") or []):
            if not isinstance(mon, dict) or not mon.get("primary"):
                continue
            value = mon.get("value", mon.get("id", mon.get("index")))
            if isinstance(value, int) and value > 0:
                return value
    return None


def _screenshot_capture_payload(
    prompt: str,
    *,
    environments: list[dict] | None = None,
    target: str | None = None,
) -> dict:
    """Derive capture-surface preferences from NL.

    KVM keeps ``monitor=0`` as its legacy default. Explicit user monitor
    numbers are stored 1-based (``monitor=2`` means "second monitor") so the
    backend can distinguish them from the default primary-monitor path.
    """
    low = nl_key(prompt)
    if any(kw in low for kw in _ALL_MONITOR_KWS):
        return {"scope": "all", "monitor": -1}
    m = re.search(r"\bmonitor(?:ze|a|ow)?\s*(?:numer\s+)?(\d+)\b", low)
    if m:
        return {"monitor": max(1, int(m.group(1)))}
    m = re.search(r"\b(\d+)\s+monitor(?:ze|a|ow)?\b", low)
    if m:
        return {"monitor": max(1, int(m.group(1)))}
    m = re.search(r"\bnumer\s+(\d+)\s+monitor(?:ze|a|ow)?\b", low)
    if m:
        return {"monitor": max(1, int(m.group(1)))}
    # A monitor index NL placed next to "ekran" (screen) rather than directly next to
    # "monitor" — e.g. "zrob zrzut 3 ekranu monitora" or "ekranu 3 monitora". Anchored on the
    # word "monitor" so a generic screenshot phrase ("zrob zrzut ekranu") never matches.
    m = re.search(r"\b(\d+)\s+ekran\w*\s+monitor\w*\b", low)
    if m:
        return {"monitor": max(1, int(m.group(1)))}
    m = re.search(r"\bekran\w*\s+(\d+)\s+monitor\w*\b", low)
    if m:
        return {"monitor": max(1, int(m.group(1)))}
    m = re.search(r"\bekran\w*\s+(\d+)\b|\bscreen\s+(\d+)\b", low)
    if m:
        return {"monitor": max(1, int(m.group(1) or m.group(2)))}
    m = re.search(r"\b(\d+)\s+ekran\w*\b|\b(\d+)\s+screen\b", low)
    if m:
        return {"monitor": max(1, int(m.group(1) or m.group(2)))}
    for word, number in _MONITOR_ORDINALS.items():
        if re.search(rf"\b{re.escape(word)}\s+monitor\b|\bmonitor\s+{re.escape(word)}\b", low):
            return {"monitor": number}
    if re.search(r"\b(primary|main|glown\w*)\b", low):
        primary = _primary_monitor_id_from_environment(environments, target)
        if primary is not None:
            return {"monitor": primary}
    return {}


def _apply_screenshot_capture_payload(steps: list[dict], prompt: str) -> list[dict]:
    from ._planner_cdp import _is_screen_capture_step  # noqa: PLC0415 - lazy avoids flow_planner↔_planner_cdp cycle
    payload_hint = _screenshot_capture_payload(prompt)
    if not payload_hint:
        return steps
    out: list[dict] = []
    for step in steps:
        new_step = dict(step)
        if _is_screen_capture_step(new_step):
            payload = dict(new_step.get("payload") or {})
            payload.update(payload_hint)
            # Deliberately do NOT clear a recalled all-desktop scope here. A recalled
            # {scope: all} must keep its scope so the env-enum recall gate
            # (urirun_flow.env_selection.recall_env_enum_replan_required) detects the skipWhen
            # and routes the flow to retrieve→propose, rather than this layer silently
            # re-binding a remembered episode. Similarity proposes; the gate admits.
            new_step["payload"] = payload
        out.append(new_step)
    return out


def _inject_capture_if_needed(flow: dict, prompt: str, allowed_uris: set[str]) -> dict:
    """Append screen/query/capture as the last step when the prompt asks for a screenshot
    but the LLM forgot to include it. Idempotent: no-op when a capture step already exists
    or when no capture route is served by the mesh."""
    from ._planner_cdp import (  # noqa: PLC0415 - lazy avoids flow_planner↔_planner_cdp cycle
        _prepare_capture_after_required_verify, _is_screen_capture_step,
        _ensure_result_reference_dependencies, _prefer_browser_capture_scope_after_cdp,
        _capture_dependency_ids,
    )
    low = nl_key(prompt)
    if not any(kw in low for kw in _SCREENSHOT_KWS):
        return flow
    steps = list(flow.get("steps") or [])
    steps = _prepare_capture_after_required_verify(steps)
    if any(_is_screen_capture_step(s) for s in steps):
        steps = _apply_screenshot_capture_payload(steps, prompt)
        steps = _focus_window_before_monitor_capture(steps, allowed_uris)
        steps = _ensure_result_reference_dependencies(steps)
        return {**flow, "steps": _prefer_browser_capture_scope_after_cdp(steps)}
    capture_uri = next(
        (u for u in sorted(allowed_uris) if "screen/query/capture" in u), None
    )
    if not capture_uri:
        return {**flow, "steps": steps}
    deps = _capture_dependency_ids(steps)
    steps.append({
        "id": "capture_screen",
        "uri": capture_uri,
        "payload": _screenshot_capture_payload(prompt),
        "depends_on": deps,
    })
    steps = _prepare_capture_after_required_verify(steps)
    steps = _focus_window_before_monitor_capture(steps, allowed_uris)
    steps = _ensure_result_reference_dependencies(steps)
    steps = _prefer_browser_capture_scope_after_cdp(steps)
    return {**flow, "steps": steps}


def prepare_screenshot_capture_flow(flow: dict, prompt: str, allowed_uris: set[str] | None = None) -> dict:
    """Normalize screenshot flows so capture is not blocked by an informational verify gate.

    Recalled episodes can contain an old shape where ``screen/query/capture`` depends on a
    required ``ui/query/verify``. If the page text changed or the user is on an authwall, that
    verify aborts before the screenshot is taken, despite the user asking for evidence of the
    current browser state. Screenshot flows should capture the state after navigation/page-ready;
    a page-presence verify may remain as optional telemetry.
    """
    return _inject_capture_if_needed(flow, prompt, allowed_uris or set())


def normalize_flow(flow: dict, allowed_uris: set[str], routes: list[dict] | None = None,
                   infeasible_constraints: list[dict] | None = None) -> dict:
    from ._planner_cdp import (  # noqa: PLC0415 - lazy avoids flow_planner↔_planner_cdp cycle
        _strip_focus_from_cdp_flows, _rewrite_cdp_profile_for_auth,
        _inject_cdp_ready_probes, _normalize_result_reference_payloads,
        _ensure_result_reference_dependencies, _assert_result_refs_satisfiable,
        _collect_infeasible_constraints,
    )
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
    steps = _normalize_result_reference_payloads(steps)
    steps = _bind_env_domain_producers(steps)
    steps = _focus_window_before_monitor_capture(steps, allowed_uris)
    steps = _ensure_result_reference_dependencies(steps)
    steps = _assert_result_refs_satisfiable(steps)
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
    from ._planner_cdp import _collect_infeasible_constraints  # noqa: PLC0415
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
        hint = _empty_flow_hint(len(allowed_uris), planner_reason)
        raise ValueError(
            "NL flow generated no URI steps. "
            f"Discovered {detail['safeRoutes']} safe route(s) on node(s) {nodes or '[]'}"
            f"{'; selected ' + repr(selected_nodes) if selected_nodes else ''}. "
            f"{hint} "
            f"Sample routes: {sample}{reason}"
        ) from exc


def _empty_flow_hint(safe_route_count: int, planner_reason: str = "") -> str:
    reason = str(planner_reason or "").casefold()
    llm_outage = any(signal in reason for signal in (
        "litellm",
        "openrouter",
        "key limit exceeded",
        "insufficient credit",
        "quota",
        "rate limit",
        "llm planner",
    ))
    if safe_route_count and llm_outage:
        return (
            "LLM planner/provider failed and the deterministic fallback produced no steps; "
            "check LLM model/key/quota, retry with noLlm=true when covered, or use a verified known-good episode."
        )
    return "Check the mesh config or pass --node-url [NAME=]URL."


# ── LLM flow generation ───────────────────────────────────────────────────────

def _llm_contract_view(contract: dict) -> dict:
    """Small contract slice for PROPOSE-stage prompts.

    Full contracts carry examples and rich output schemas that are useful to gates and docs but
    expensive in LLM context. The planner needs only the invariants that constrain choices; the
    full contract is still used later by router/runtime admission.
    """
    if not isinstance(contract, dict):
        return {}
    return {
        key: contract[key]
        for key in ("effect", "reversible", "domains")
        if key in contract
    }


def _llm_route_relevant(prompt: str, route: dict) -> bool:
    uri = str(route.get("uri") or "")
    low = nl_key(prompt)
    intents = _flow_intents_lexical(prompt)
    browserish = bool(first_url(prompt) or re.search(
        r"\b(chrome|browser|przegl\w+|linkedin|github|google|stron\w+|page|url|cdp|debug|debugg\w+|sesj\w+|gotow\w+)\b",
        low,
    ))
    artifactish = bool(re.search(r"\b(artifact|artefakt|zapisz|dolacz|dołącz|attachment)\b", low))
    if intents.get("screen"):
        keep = ("/screen/", "/window/", "/display/", "/surface/", "/env/")
        if browserish:
            keep = (*keep, "/cdp/", "/browser/", "/ui/", "/input/")
        if artifactish:
            keep = (*keep, "artifact://")
        return any(part in uri for part in keep)
    if browserish:
        return any(part in uri for part in ("/cdp/", "/browser/", "/window/", "/screen/", "/ui/", "/input/", "/env/", "/surface/"))
    if intents.get("health"):
        return any(part in uri for part in ("/runtime/query/health", "/env/", "/status", "/query/info"))
    if intents.get("files") or intents.get("invoices"):
        return any(part in uri for part in ("fs://", "invoice://", "artifact://", "/dir/query/", "/folder/query/"))
    if intents.get("processes") or intents.get("logs"):
        return any(part in uri for part in ("proc://", "log://", "shell://", "/runtime/query/health"))
    return True


def llm_flow(prompt: str, routes: list[dict], nodes: list[dict],
             environments: list[dict] | None = None,
             retrieval: dict | None = None,
             llm_model: str | None = None) -> dict:
    model = _configured_llm_model(llm_model)
    if not model:
        raise RuntimeError("URIRUN_LLM_MODEL or LLM_MODEL is not set")

    allowed_routes = [
        {
            "uri": route["uri"],
            "node": route.get("node"),
            "kind": route.get("kind"),
            "title": route.get("title"),
            "inputSchema": route.get("inputSchema") or {"type": "object"},
            "contract": _llm_contract_view(
                (route.get("meta") or {}).get("contract") or route.get("contract") or {}
            ),
        }
        for route in routes
        if safe_route(route) and _llm_route_relevant(prompt, route)
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
                "CDP READINESS REQUESTS: when the user asks whether a CDP session/debug port is ready, "
                "emit 'cdp/session/query/ready'. When they ask to launch/start Chrome with a debug port, "
                "emit 'cdp/session/command/ensure' followed by 'cdp/session/query/ready'. If they also ask "
                "whether the page is ready, add 'cdp/page/query/ready' after session readiness. "
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
                "WINDOW-MONITOR RULE: when the request asks for the monitor/screen that contains a named app/window/browser, add a window/query/list step (with that app or title) BEFORE screen/query/capture. The runtime then wires the captured monitor from the window selection automatically — do NOT set monitor_from, depends_on or scope yourself; just emit the window/query/list step and a plain screen/query/capture. "
                # Concrete-state grounding: when an 'environments' field is present it is the LIVE
                # capability profile + foreground surface of each node — GROUND your steps on it:
                "honour each node's 'bestSurface' and ALL items in its 'guidance' list (they are "
                "hard environment rules, not suggestions — e.g. if guidance says TYPE via atspi/uinput "
                "is NOT EXECUTABLE, NEVER emit a fill/type step via those surfaces). "
                "Check 'actionMatrix' per node: if an action's value for a surface is 'not_executable' "
                "or 'blocked', do NOT plan that action on that surface — use the surface where the same "
                "action is 'executable' instead (e.g. type → cdp only). "
                "For any allowedRoute with contract.domains, ground payload parameters on the matching "
                "environment domains. Example: if monitor declares domain env:monitors.id, use only values "
                "from environments[].domains['env:monitors.id'] unless the user explicitly asks for an "
                "unavailable value; in that case preserve the user's requested value so the deterministic "
                "router can reject it with an actionable env-domain-invalid diagnostic. Do not guess hidden "
                "monitor numbers or hard-code monitor labels. "
                "If a retrieval object is present, treat its episodes/routes/preferences as PROPOSE-stage "
                "candidates with provenance, not as accepted plans. Reuse their shape when it fits the "
                "current request and environment, but the final plan must still use only allowedRoutes and "
                "must be valid for the current environment. "
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
                    "retrieval": retrieval or {},
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
    content = response.choices[0].message.content or "{}"
    flow = json_from_text(content)
    if allowed_routes and not (isinstance(flow.get("steps"), list) and flow.get("steps")):
        repair_messages = [
            *messages,
            {"role": "assistant", "content": content},
            {"role": "user", "content": (
                "Your JSON plan had an empty 'steps' array. That is invalid when allowedRoutes are available. "
                "Return strict JSON again with at least one atomic step using only allowedRoutes. "
                "For readiness/status requests, a matching query/ready or status query step is enough. "
                "For launch/open requests, include the launch/open command and then a readiness/status query. "
                "For browser/CDP tasks, use this available URI pattern when present: "
                "cdp/session/command/ensure -> cdp/session/query/ready -> "
                "cdp/page/command/navigate -> cdp/page/query/ready -> ui/query/verify; "
                "include only the steps required by the request. "
                "Do not return empty steps."
            )},
        ]
        response = quiet_completion(
            model=model,
            messages=repair_messages,
            temperature=0,
            response_format={"type": "json_object"},
        )
        flow = json_from_text(response.choices[0].message.content or "{}")
    return flow


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

def _inproc_category(env: dict) -> str:
    return (env.get("error") or {}).get("category") or ""


def _inproc_result(env: dict) -> dict:
    val = (env.get("result") or {}).get("value") if isinstance(env.get("result"), dict) else None
    return {"ok": bool(env.get("ok")), "result": val,
            "error": (env.get("error") or {}).get("message") if not env.get("ok") else None}


def _local_inprocess_query(uri: str, payload: dict | None = None) -> dict | None:
    """Resolve a local planner query through installed connector entry points.

    Host planner probes must not route through a stale mesh serviceMap entry such as
    ``host -> http://host:8080/run``. They are read-only facts about the local process
    environment, so prefer the installed local connector when it owns the URI.
    """
    try:
        import urirun as _u  # noqa: PLC0415
        from urirun.runtime import discovery as _disc, v2 as _v2  # noqa: PLC0415
        reg = _disc.registry_for_uri(uri, "urirun.bindings")
        env = _u.run(uri, reg, payload=dict(payload or {}),
                     mode="execute", policy={"allowExecute": True})
        if _inproc_category(env) != "NOT_FOUND":
            return _inproc_result(env)
        live_binding = _v2.decorated_bindings()["bindings"].get(uri)
        if live_binding is None:
            return None
        reg2 = _u.compile_registry(_v2.build_binding_document([live_binding]))
        env = _u.run(uri, reg2, payload=dict(payload or {}),
                     mode="execute", policy={"allowExecute": True})
        if _inproc_category(env) == "NOT_FOUND":
            return None
        return _inproc_result(env)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "result": None, "error": str(exc)}


def _fetch_kvm_query(step: dict, registry: dict, route: str, marker: str,
                     *, use_cache: bool = True) -> dict | None:
    """Best-effort fetch of a kvm read-only query (env/query/profile, surface/query/current)
    for the failing node, so the self-heal fits its remediation to the live machine + surface.
    None on any hiccup — this context is an optimisation, never a correctness dependency.

    ``use_cache=False`` forces a live probe (Twin memory paths — drift/known-good — must
    see reality, never a snapshot); the fresh value still refreshes the cache."""
    from urirun_flow import _env_probe_cache  # noqa: PLC0415
    target = route_target(str(step.get("uri") or ""))
    if not target:
        return None
    candidates = [f"kvm://{target}/{route}"]
    cache_key = candidates[0]
    if use_cache and _env_probe_cache.cacheable(cache_key):
        cached = _env_probe_cache.get(cache_key)
        if isinstance(cached, dict) and marker in cached:
            return cached
    routed = _kvm_query_uri_for_node(registry, target, route)
    if routed and routed not in candidates:
        candidates.append(routed)
    if target == "host":
        local = _local_inprocess_query(candidates[0], {})
        value = result_data(local) if isinstance(local, dict) else None
        if isinstance(value, dict) and marker in value:
            if _env_probe_cache.cacheable(cache_key):
                _env_probe_cache.put(cache_key, value)
            return value
        # For host-targeted calls keep the historical direct path only after the local
        # in-process probe. A remote node can advertise kvm://host/...; callers that mean
        # that node pass kvm://<node>/... and get routed through registry metadata above.
        candidates = candidates[:1]
    for uri in candidates:
        try:
            env = v2_service.call(uri, {}, registry, mode="execute")
            value = result_data(env)
            if isinstance(value, dict) and marker in value:
                if _env_probe_cache.cacheable(cache_key):
                    _env_probe_cache.put(cache_key, value)
                return value
        except Exception:  # noqa: BLE001
            continue
    return None


def _kvm_inventory_for_planner(node: str, registry: dict) -> dict | None:
    try:
        from urirun_flow.flow import _build_env_inventory  # noqa: PLC0415
        inventory = _build_env_inventory(node, registry)
    except Exception:  # noqa: BLE001 - inventory is advisory planner context
        return None
    if isinstance(inventory, dict) and (inventory.get("domains") or inventory.get("monitors")):
        return inventory
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


def _twin_host_query(node: str, registry: dict, route: str) -> dict | None:
    """Fetch a planner environment fact through the Digital Twin boundary.

    The planner should consume facts about the environment (profile/inventory), not the
    implementation detail that currently happens to measure those facts. The twin route may still
    call KVM internally, but that dependency is then owned by the twin connector, not by the
    NL→flow planner. ``None`` keeps the historical KVM fallback alive for older installs.
    """
    payload = {"node": node} if node else {}
    uri = f"twin://host/{route}"
    if str(node or "").casefold() in ("", "host", "localhost", "local", "127.0.0.1"):
        local = _local_inprocess_query(uri, payload)
        value = result_data(local) if isinstance(local, dict) and local.get("ok") else None
        if isinstance(value, dict) and value.get("ok", True) is not False:
            return value
    try:
        env = v2_service.call(uri, payload, registry, mode="execute")
        value = result_data(env)
        if isinstance(value, dict) and value.get("ok", True) is not False:
            return value
    except Exception:  # noqa: BLE001
        return None
    return None


def _profile_from_twin_environment(profile: dict) -> tuple[dict, dict | None]:
    """Adapt ``twin://host/environment/query/profile`` into planner_context inputs."""
    inner = dict(profile.get("profile") or {})
    surface = profile.get("surface") if isinstance(profile.get("surface"), dict) else None
    if profile.get("bestSurface") is not None and not inner.get("best"):
        inner["best"] = profile.get("bestSurface")
    for key in ("controllable", "actionMatrix", "osLevelReliable", "display"):
        if profile.get(key) is not None and inner.get(key) is None:
            inner[key] = profile.get(key)
    if profile.get("constraints"):
        inner["_twinConstraints"] = list(profile.get("constraints") or [])
    if profile.get("warnings"):
        inner["_twinWarnings"] = list(profile.get("warnings") or [])
    if profile.get("sessionProbe"):
        inner["_twinSessionProbe"] = profile.get("sessionProbe")
    if profile.get("sessionSelection"):
        inner["_twinSessionSelection"] = profile.get("sessionSelection")
    return inner, surface


def _is_twin_environment_profile(value: dict | None) -> bool:
    if not isinstance(value, dict):
        return False
    return any(key in value for key in (
        "profile", "surface", "actionMatrix", "constraints", "bestSurface",
        "controllable", "host", "sessionProbe", "sessionSelection", "warnings",
    ))


def _planner_context_from_twin(node: str, twin_profile: dict, twin_inventory: dict | None,
                               *, memory: "TwinMemory | None" = None) -> dict:
    from urirun.node.reversible import planner_context
    profile, surface = _profile_from_twin_environment(twin_profile)
    ctx = planner_context(node, profile, surface, memory=memory)
    twin_constraints = profile.get("_twinConstraints") or []
    if twin_constraints:
        existing = list(ctx.get("constraints") or [])
        ctx["constraints"] = [*existing, *[c for c in twin_constraints if isinstance(c, dict)]]
    if twin_inventory:
        ctx["inventory"] = twin_inventory
    if profile.get("_twinWarnings"):
        ctx.setdefault("guidance", []).extend(
            f"Twin warning: {warning}" for warning in profile.get("_twinWarnings") or []
        )
    if profile.get("_twinSessionSelection"):
        ctx["sessionSelection"] = profile.get("_twinSessionSelection")
    if profile.get("_twinSessionProbe"):
        ctx["sessionProbe"] = profile.get("_twinSessionProbe")
    return ctx


def _fetch_env_profile(step: dict, registry: dict, *, use_cache: bool = True) -> dict | None:
    return _fetch_kvm_query(step, registry, "env/query/profile", "controlStrategies",
                            use_cache=use_cache)


def _fetch_surface(step: dict, registry: dict) -> dict | None:
    return _fetch_kvm_query(step, registry, "surface/query/current", "kind")


# ── Planner environment fetching ──────────────────────────────────────────────

def fetch_planner_environments(node_names: list[str], registry: dict, mesh: dict | None = None,
                               *, memory: "TwinMemory | None" = None, prompt: str = "") -> list[dict]:
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
            twin_profile = _twin_host_query(name, registry, "environment/query/profile")
            if _is_twin_environment_profile(twin_profile):
                twin_inventory = (
                    _twin_host_query(name, registry, "environment/query/inventory")
                    or _twin_host_query(name, registry, "env/query/inventory")
                )
                ctx = _planner_context_from_twin(name, twin_profile, twin_inventory, memory=memory)
                if _prompt_needs_window_inventory(prompt):
                    win = _fetch_kvm_query({"uri": f"kvm://{name}/x"}, registry, "window/query/list", "windows")
                    if isinstance(win, dict):
                        ctx["windows"] = [w for w in (win.get("windows") or []) if isinstance(w, dict)]
                out.append(ctx)
                continue
            prof = _fetch_kvm_query({"uri": f"kvm://{name}/x"}, registry, "env/query/profile", "controlStrategies")
            if not prof:
                continue
            surf = _fetch_kvm_query({"uri": f"kvm://{name}/x"}, registry, "surface/query/current", "kind")
            ctx = planner_context(name, prof, surf, memory=memory)
            inventory = _kvm_inventory_for_planner(name, registry)
            if inventory:
                ctx["inventory"] = inventory
            win = _fetch_kvm_query({"uri": f"kvm://{name}/x"}, registry, "window/query/list", "windows")
            if isinstance(win, dict):
                ctx["windows"] = [w for w in (win.get("windows") or []) if isinstance(w, dict)]
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

def _safe_planner_error(exc: BaseException) -> str:
    msg = str(exc).strip() or type(exc).__name__
    msg = re.sub(r"https?://\S+", "<url>", msg)
    msg = re.sub(r"keys/[A-Za-z0-9._:-]+", "keys/<redacted>", msg)
    msg = " ".join(msg.split())
    return msg[:500]


def _flow_from_retrieval(retrieval: dict | None) -> dict | None:
    """Build a flow from the best known-good retrieval candidate, or None when there is none.

    Recall is the planner's FALLBACK before the hardcoded heuristic: a known-good episode beats a
    hand-written per-intent URI sequence. It is material, not a literal replay — the normalize
    pipeline (env_selection / _bind_env_domain_producers) re-resolves env-enum values (monitor, …)
    against the CURRENT inventory, so a stale episode value cannot leak (Gen 6)."""
    if not isinstance(retrieval, dict):
        return None
    for key in ("flows", "episodes"):
        for cand in (retrieval.get(key) or []):
            steps = cand.get("steps") if isinstance(cand, dict) else None
            if steps:
                return {"steps": list(steps),
                        "task": {"id": "recall", "source": "recall-fallback",
                                 "title": str(cand.get("intent") or cand.get("prompt") or "")}}
    return None


def make_flow(prompt: str, mesh: dict, selected_nodes: list[str] | None = None, use_llm: bool = True,
              environments: list[dict] | None = None,
              retrieval: dict | None = None,
              llm_model: str | None = None) -> tuple[dict, dict]:
    routes = [route for route in mesh["routes"] if safe_route(route)]
    allowed = {route["uri"] for route in routes}
    if use_llm:
        try:
            resolved_model = _configured_llm_model(llm_model)
            flow = normalize_flow_or_explain(
                llm_flow(prompt, routes, mesh["nodes"], environments=environments,
                         retrieval=retrieval, llm_model=llm_model),
                allowed,
                routes=routes,
                selected_nodes=selected_nodes,
                environments=environments,
            )
            flow = _inject_capture_if_needed(flow, prompt, allowed)
            return flow, {"provider": "litellm", "fallback": False,
                          **({"model": resolved_model} if resolved_model else {})}
        except Exception as exc:  # noqa: BLE001 - LLM leads; the heuristic is the explicit fallback.
            # Inverted planner policy: the LLM planner LEADS, and the deterministic heuristic is an
            # explicit, configurable fallback that is ON BY DEFAULT. So an LLM outage (no credits,
            # rate-limit, unreachable local model) DEGRADES to a heuristic plan instead of a hard
            # planner-error — the operator still gets a (possibly needs-selection) result, not a wall.
            # Opt into loud failure with URIRUN_STRICT_LLM_PLANNER=1 (e.g. CI that must not silently
            # degrade); legacy URIRUN_ALLOW_HEURISTIC_PLANNER_FALLBACK=0 also forces strict.
            _strict = os.getenv("URIRUN_STRICT_LLM_PLANNER", "").strip().lower() in {"1", "true", "yes"}
            if os.getenv("URIRUN_ALLOW_HEURISTIC_PLANNER_FALLBACK", "").strip().lower() in {"0", "false", "no"}:
                _strict = True
            if _strict:
                detail = _safe_planner_error(exc)
                raise RuntimeError(
                    "LLM planner failed and strict mode is on (URIRUN_STRICT_LLM_PLANNER). "
                    f"Planner error: {detail}. "
                    "Configure URIRUN_LLM_MODEL/LLM_MODEL, pass noLlm=true for explicit offline "
                    "diagnostics, or unset URIRUN_STRICT_LLM_PLANNER to degrade to the heuristic."
                ) from exc
            recalled = _flow_from_retrieval(retrieval)
            if recalled is not None:
                flow = normalize_flow_or_explain(recalled, allowed, routes=routes,
                    selected_nodes=selected_nodes, planner_reason=str(exc), environments=environments)
                return _inject_capture_if_needed(flow, prompt, allowed), {
                    "provider": "recall", "fallback": True, "reason": _safe_planner_error(exc)}
            flow = heuristic_flow(prompt, routes, mesh["nodes"], selected_nodes,
                                  use_llm=True, environments=environments)
            flow = normalize_flow_or_explain(
                flow,
                allowed,
                routes=routes,
                selected_nodes=selected_nodes,
                planner_reason=str(exc),
                environments=environments,
            )
            return _inject_capture_if_needed(flow, prompt, allowed), {
                "provider": "heuristic", "fallback": True, "reason": _safe_planner_error(exc)}
    recalled = _flow_from_retrieval(retrieval)
    if recalled is not None:
        flow = normalize_flow_or_explain(recalled, allowed, routes=routes,
            selected_nodes=selected_nodes, planner_reason="LLM disabled", environments=environments)
        return _inject_capture_if_needed(flow, prompt, allowed), {
            "provider": "recall", "fallback": True, "reason": "LLM disabled"}
    flow = heuristic_flow(prompt, routes, mesh["nodes"], selected_nodes,
                          use_llm=False, environments=environments)
    flow = normalize_flow_or_explain(
        flow,
        allowed,
        routes=routes,
        selected_nodes=selected_nodes,
        planner_reason="LLM disabled",
        environments=environments,
    )
    return _inject_capture_if_needed(flow, prompt, allowed), {"provider": "heuristic", "fallback": True, "reason": "LLM disabled"}


# ---------------------------------------------------------------------------
# PEP 562 re-export: functions moved to _planner_cdp to keep this module
# under 1800 lines; callers using ``from flow_planner import X`` still work.
_MOVED_TO_PLANNER_CDP: frozenset[str] = frozenset({
    "_is_screen_capture_step", "_is_cdp_step", "_prefer_browser_capture_scope_after_cdp",
    "_is_required_verify_step", "_is_nonblocking_screenshot_tail", "_dedupe_ids",
    "_bypass_required_verify_deps", "_capture_dependency_ids",
    "_required_verify_only_blocks_capture", "_make_verify_optional",
    "_prepare_capture_after_required_verify", "_inject_cdp_ready_probes",
    "_collect_infeasible_constraints", "_strip_focus_from_cdp_flows",
    "_chrome_profile_root", "_rewrite_cdp_profile_for_auth",
    "_normalize_result_reference_payloads", "_ensure_result_reference_dependencies",
    "_assert_result_refs_satisfiable",
})


def __getattr__(name: str):
    if name in _MOVED_TO_PLANNER_CDP:
        from urirun_flow import _planner_cdp  # noqa: PLC0415
        return getattr(_planner_cdp, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
