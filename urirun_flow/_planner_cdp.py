# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.

"""CDP/browser and result-reference helpers for the flow planner.

Extracted from flow_planner.py to keep the main module under 1800 lines.
This module is only ever imported lazily (from inside function bodies in
flow_planner.py), so flow_planner is fully initialised in sys.modules when
the module-level imports below execute — there is no circular import.
"""

from __future__ import annotations

from typing import Any

from urirun_connector_router.routing import route_target
# Pulled from flow_planner lazily — safe because this module is only imported
# from inside function bodies (never at module load time).
from .flow_planner import (
    _CDP_ENSURE_SUFFIX,
    _CDP_READY_SUFFIX,
    _normalize_flow_step,
    _uri_is_available,
    _needs_session_ready_after_ensure,
)

def _is_screen_capture_step(step: dict) -> bool:
    return "screen/query/capture" in str((step or {}).get("uri") or "")


def _is_cdp_step(step: dict | None) -> bool:
    return isinstance(step, dict) and "/cdp/" in str(step.get("uri") or "")


def _prefer_browser_capture_scope_after_cdp(steps: list[dict]) -> list[dict]:
    """Mark screen captures after browser-CDP steps as browser-scoped.

    On live GNOME/Wayland multi-monitor hosts, an OS-level capture can legally
    return a monitor that does not contain the browser window. When the flow has
    already driven a browser through CDP on the same target, the meaningful
    screenshot is the active page viewport, so ask the KVM connector to prefer
    CDP capture. Plain desktop screenshots remain unchanged.
    """
    seen_cdp_targets: set[str] = set()
    out: list[dict] = []
    for step in steps:
        new_step = dict(step)
        uri = str(new_step.get("uri") or "")
        target = route_target(uri)
        if _is_screen_capture_step(new_step) and target in seen_cdp_targets:
            payload = dict(new_step.get("payload") or {})
            payload.setdefault("scope", "browser")
            new_step["payload"] = payload
        out.append(new_step)
        if _is_cdp_step(new_step):
            seen_cdp_targets.add(target)
    return out


def _is_required_verify_step(step: dict | None) -> bool:
    if not isinstance(step, dict):
        return False
    payload = step.get("payload") if isinstance(step.get("payload"), dict) else {}
    return "/ui/query/verify" in str(step.get("uri") or "") and payload.get("required") is True


def _is_nonblocking_screenshot_tail(step: dict) -> bool:
    uri = str(step.get("uri") or "")
    if _is_screen_capture_step(step):
        return True
    if "/query/" in uri:
        return True
    return uri.endswith("/input/command/wait")


def _dedupe_ids(ids: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in ids:
        if item and item not in seen:
            out.append(item)
            seen.add(item)
    return out


def _bypass_required_verify_deps(deps: list[str], by_id: dict[str, dict], seen: set[str] | None = None) -> list[str]:
    seen = seen or set()
    out: list[str] = []
    for dep in deps:
        if dep in seen:
            continue
        seen.add(dep)
        gate = by_id.get(dep)
        if _is_required_verify_step(gate):
            out.extend(_bypass_required_verify_deps(list(gate.get("depends_on") or []), by_id, seen))
        else:
            out.append(dep)
    return _dedupe_ids(out)


def _capture_dependency_ids(steps: list[dict]) -> list[str]:
    if not steps:
        return []
    by_id = {str(s.get("id")): s for s in steps if s.get("id")}
    last_id = str(steps[-1].get("id") or "")
    return _bypass_required_verify_deps([last_id], by_id)


def _required_verify_only_blocks_capture(index: int, steps: list[dict]) -> bool:
    for later in steps[index + 1:]:
        if _is_screen_capture_step(later):
            return True
        if not _is_nonblocking_screenshot_tail(later):
            return False
    return False


def _make_verify_optional(step: dict) -> dict:
    payload = dict(step.get("payload") or {})
    payload["required"] = False
    return {**step, "payload": payload, "optional": True}


def _prepare_capture_after_required_verify(steps: list[dict]) -> list[dict]:
    if not steps:
        return []
    by_id = {str(s.get("id")): s for s in steps if s.get("id")}
    out: list[dict] = []
    for index, step in enumerate(steps):
        new_step = dict(step)
        if _is_screen_capture_step(new_step):
            new_step["depends_on"] = _bypass_required_verify_deps(list(new_step.get("depends_on") or []), by_id)
        elif _is_required_verify_step(new_step) and _required_verify_only_blocks_capture(index, steps):
            new_step = _make_verify_optional(new_step)
        out.append(new_step)
    return out


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


def _normalize_result_reference_payloads(steps: list[dict]) -> list[dict]:
    """Canonicalize flow result references after step ids are canonical.

    LLMs sometimes copy placeholder notation into the DSL, e.g.
    ``<step_1>.result.value.id``. ``*_from`` values are not connector payload
    fields; they are flow references, so normalize only that syntax surface.
    """
    ids = {str(step.get("id") or "") for step in steps}
    literal_from_keys = {"copy_from"}
    out: list[dict] = []
    for step in steps:
        payload = step.get("payload")
        if not isinstance(payload, dict):
            out.append(step)
            continue
        changed = False
        new_payload: dict = {}
        for key, value in payload.items():
            if key.endswith("_from") and key not in literal_from_keys and isinstance(value, str):
                head, sep, tail = value.strip().partition(".")
                clean_head = head.strip().strip("<>").strip()
                if clean_head in ids and clean_head != head:
                    new_payload[key] = f"{clean_head}{sep}{tail.rstrip('>')}"
                    changed = True
                    continue
            new_payload[key] = value
        out.append({**step, "payload": new_payload} if changed else step)
    return out


def _ensure_result_reference_dependencies(steps: list[dict]) -> list[dict]:
    """Every ``*_from`` flow reference must name an earlier producer in ``depends_on``.

    This is a structural data-flow invariant, independent of the domain. The LLM may emit
    ``monitor_from``/``id_from`` correctly but omit the dependency edge; later repair passes
    can also rewrite capture dependencies. Normalize the edge once, generically, so the
    router/executor never relies on incidental step order.
    """
    literal_from_keys = {"copy_from"}
    seen: set[str] = set()
    out: list[dict] = []
    for step in steps:
        step_id = str(step.get("id") or "")
        payload = step.get("payload") if isinstance(step.get("payload"), dict) else {}
        deps = [str(dep) for dep in (step.get("depends_on") or []) if isinstance(dep, str)]
        for key, value in payload.items():
            if not key.endswith("_from") or key in literal_from_keys or not isinstance(value, str):
                continue
            ref_id = value.strip().split(".", 1)[0].strip().strip("<>").strip()
            if ref_id and ref_id in seen and ref_id != step_id and ref_id not in deps:
                deps.append(ref_id)
        out.append({**step, "depends_on": deps})
        if step_id:
            seen.add(step_id)
    return out


def _assert_result_refs_satisfiable(steps: list[dict]) -> list[dict]:
    """Final data-flow gate: every ``*_from`` reference must name a step that EXISTS and is EARLIER.

    ``_ensure_result_reference_dependencies`` adds the missing edge for a valid earlier producer, but
    a reference to an UNKNOWN step (hallucinated/typo'd id) or a LATER step (producer after consumer)
    is unsatisfiable — at execution it resolves to nothing and the connector silently falls back to a
    default (the exact ``silent monitor=0`` failure the system forbids). Reject it loudly here so a
    bad plan fails at normalization, not as a wrong capture. Generic over EVERY ``*_from``, not just
    ``monitor_from`` — the LLM omits/garbles the edge as a CLASS, not a Chrome special case."""
    literal_from_keys = {"copy_from"}
    order = {str(s.get("id") or ""): i for i, s in enumerate(steps)}
    for i, step in enumerate(steps):
        payload = step.get("payload") if isinstance(step.get("payload"), dict) else {}
        sid = str(step.get("id") or "")
        for key, value in payload.items():
            if not key.endswith("_from") or key in literal_from_keys or not isinstance(value, str):
                continue
            ref = value.strip().split(".", 1)[0].strip().strip("<>").strip()
            if not ref or ref == sid:
                continue
            if ref not in order:
                raise ValueError(
                    f"flow step {sid!r} references {key}={value!r} but no step {ref!r} exists "
                    "(unsatisfiable data-flow reference)")
            if order[ref] >= i:
                raise ValueError(
                    f"flow step {sid!r} references {key}={value!r} but its producer {ref!r} is not "
                    "earlier (producer must precede consumer)")
    return steps


