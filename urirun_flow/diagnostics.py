# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.
#
# Experience-driven DIAGNOSTIC PLAYBOOK: failure-signature → named root cause → specific
# remediation (as URIs, some auto-applicable). Encodes the recurring NL→URI→desktop
# failure classes learned in the field so the recovery engine can DIAGNOSE and FIX instead
# of emitting a generic "inspect the route". Consulted by recovery.recovery_plan, which
# attaches the diagnosis to every failed step's timeline entry — so the same knowledge
# reaches a human, the dashboard, an auto-repair loop, or an LLM re-planner.
#
# A rule matches on the casefolded error message (regex), optionally gated by error category
# and/or URI scheme. ``remediation`` actions carry ``automatic: True`` when they are safe to
# apply unattended (idempotent provisioning / bounded retries) — the contract an auto-repair
# loop keys on. Add a rule whenever a new failure class is understood; nothing else changes.
from __future__ import annotations

import re
from typing import Any, Callable

from urirun.node.routing import route_target


def _target(step: dict | None) -> str:
    uri = str((step or {}).get("uri") or "")
    try:
        t = route_target(uri)
        if t:
            return t
    except Exception:  # noqa: BLE001
        pass
    if "://" in uri:
        return uri.split("://", 1)[1].split("/", 1)[0] or "host"
    return "host"


def _target_of(actions: list[dict]) -> str:
    """The node a diagnosis is about, read back from its remediation URIs — fit_to_environment
    has no step, only the actions. Falls back to ``host``."""
    for a in actions or []:
        u = str(a.get("uri") or "")
        if "://" in u:
            return u.split("://", 1)[1].split("/", 1)[0] or "host"
    return "host"


class _Rule:
    def __init__(self, rid: str, patterns: list[str], cause: str, remediation: Callable[[str], list[dict]],
                 *, categories: set[str] | None = None, schemes: set[str] | None = None,
                 confidence: float = 0.85) -> None:
        self.id = rid
        self.regexes = [re.compile(p) for p in patterns]
        self.cause = cause
        self.remediation = remediation
        self.categories = categories
        self.schemes = schemes
        self.confidence = confidence

    def matches(self, message: str, category: str, scheme: str) -> bool:
        if self.categories and category not in self.categories:
            return False
        if self.schemes and scheme not in self.schemes:
            return False
        return any(rx.search(message) for rx in self.regexes)


# The accumulated field experience, as rules. Order = priority (first match wins).
PLAYBOOK: list[_Rule] = [
    _Rule(
        "ui-target-not-located",
        [r"target not located", r"no on-screen text matches", r"no control strategy could",
         r"vision: target not located",
         # browser-control / CDP-DOM phrasings (cdp/page/command/click|fill -> element not found)
         r"element not found", r"no element (matching|found)", r"could not find element",
         r"no (dom )?node (found|matching)",
         # ui/query/verify gate failures: the asserted on-screen text/label isn't present.
         # Phrased "required text not found on screen: '<label>'" — a required login/presence
         # gate that fires exactly the same root cause (label-language mismatch, not loaded, or
         # an authwall where the control doesn't exist). Without this the gate failure matched no
         # rule and surfaced with zero remediation.
         r"required text not found", r"text not found on screen", r"expected text .*not (found|present)"],
        "The UI target was not found by the active control strategy — typically OCR on a dark / "
        "late-rendered page, a role/label-language mismatch (e.g. Polish vs English labels), the page "
        "not loaded yet, OR (on a browser/CDP step) a login/authwall where the target simply doesn't "
        "exist. Targeting by DOM role/name via CDP is OCR-immune and language-agnostic; if a surface "
        "probe shows a login page this upgrades to not-logged-in (auth re-launch, human-gated).",
        lambda t: [
            {"id": "ensure-cdp-dom", "kind": "provision", "automatic": True,
             "uri": f"kvm://{t}/cdp/session/command/ensure",
             "label": "Bring up a CDP session so the router targets by DOM role/name (OCR-immune)."},
            {"id": "wait-page-ready", "kind": "precondition", "automatic": True,
             "uri": f"kvm://{t}/cdp/page/query/ready",
             "label": "Wait for the page to finish loading before locating the target."},
            {"id": "retry-via-act", "kind": "retry", "automatic": True,
             "uri": f"kvm://{t}/ui/command/act",
             "label": "Re-run via the self-orchestrating ui/command/act (cdp→atspi→vision, retried, verified)."},
        ],
        confidence=0.9,
    ),
    _Rule(
        "cdp-debugger-down",
        [r"debugger did not come up", r"no cdp page", r"remote-debugging-port"],
        "Chrome's debug port never bound: a bare --remote-debugging-port is silently dropped when a "
        "Chrome on the default profile already runs (the launch just forwards the URL). A dedicated "
        "user-data-dir forces a separate instance that actually opens the port.",
        lambda t: [
            {"id": "ensure-cdp-dedicated-profile", "kind": "provision", "automatic": True,
             "uri": f"kvm://{t}/cdp/session/command/ensure",
             "label": "Launch/reuse a dedicated-profile CDP Chrome that actually binds the debug port."},
        ],
        confidence=0.95,
    ),
    _Rule(
        "cdp-session-still-launching",
        [r"page not ready within timeout", r"debugger not reachable within timeout",
         r"page is still loading", r"document not ready"],
        "The CDP session is mid launch or the page is mid navigation: ``cdp/session/command/ensure`` "
        "FIRES the launch and returns immediately (launching:true, port NOT yet bound), and "
        "``cdp/page/query/ready`` polls document.readyState over a WebSocket to that port — so a "
        "page-level query fired before the bind completes times out (and the connect-then-eval loop "
        "looks like 'navigating' forever). Re-calling ``ensure`` would spawn a competing Chrome over "
        "the profile SingletonLock; the launch/probe split requires the idempotent readiness poll "
        "``cdp/session/query/ready`` (no launch, just waits for the port to bind), then retry the page query.",
        lambda t: [
            {"id": "poll-cdp-session-ready", "kind": "precondition", "automatic": True,
             "uri": f"kvm://{t}/cdp/session/query/ready",
             "label": "Poll the CDP debug endpoint until it binds (launch/probe split — does NOT re-launch)."},
            {"id": "retry-page-ready", "kind": "retry", "automatic": True,
             "uri": f"kvm://{t}/cdp/page/query/ready",
             "label": "Retry the page-ready poll now that the session has bound the debug port."},
        ],
        categories={"DEADLINE_EXCEEDED"},
        confidence=0.9,
    ),
    _Rule(
        "node-exec-timeout",
        [r"timeoutexpired", r"timed out after \d+\s*second"],
        "A single op exceeded the node's subprocess cap (~30s) — heavy OCR/portal capture, a hung CDP "
        "eval, or node contention (concurrent agents). Bound the work and keep OCR off the polling path.",
        lambda t: [
            {"id": "retry-bounded", "kind": "retry", "automatic": True,
             "uri": f"kvm://{t}/ui/command/act",
             "label": "Retry via ui/command/act (time-budgeted; cheap DOM/a11y presence, OCR only on the act)."},
            {"id": "check-node-load", "kind": "diagnostic", "automatic": False,
             "uri": f"proc://{t}/process/query/list",
             "label": "Check node load — concurrent captures/CDP can starve the portal; one agent per node."},
        ],
        confidence=0.85,
    ),
    _Rule(
        "empty-ui-target",
        [r"a target \(text/name/role\) is required", r"^text is required", r"value is required"],
        "A UI action was invoked with no locator — the planner likely emitted a textbox step with only "
        "role/name that did not map to a query, or omitted the value for a fill.",
        lambda t: [
            {"id": "repair-target", "kind": "payload", "automatic": False,
             "label": "Supply a concrete text/role/name locator (role=textbox targets the active field)."},
        ],
        confidence=0.8,
    ),
    _Rule(
        "environment-drift",
        [r"display reconfigured", r"resolution (changed|fluctuat)", r"screen size changed",
         r"monitor (added|removed|changed)", r"device.?scale.?factor"],
        "The environment changed under the flow — display reconfigured / resolution fluctuated "
        "(the 3200x1800<->1440x900 class). Cached coordinates and the chosen surface are stale; "
        "re-capture the environment profile (and re-establish the surface) before retrying, rather "
        "than acting on a moved target.",
        lambda t: [
            {"id": "recapture-environment", "kind": "discovery", "automatic": True,
             "uri": f"kvm://{t}/env/query/profile",
             "label": "Re-capture the environment profile (display/surface drifted from known-good)."},
            {"id": "resurface", "kind": "provision", "automatic": True,
             "uri": f"kvm://{t}/surface/query/current",
             "label": "Re-read the foreground surface so the router re-picks cdp/atspi/vision for the new env."},
        ],
        confidence=0.8,
    ),
    _Rule(
        "not-logged-in",
        [r"authwall", r"login required", r"sign ?in", r"not logged in", r"zaloguj", r"\b401\b"],
        "The browser session is not authenticated — a fresh CDP profile lands on the login / "
        "authwall, so the target controls (compose / post / account UI) aren't present. Re-launch "
        "the CDP Chrome on a COPY of the user's logged-in profile (the persistent-context trick).",
        lambda t: [
            {"id": "relaunch-cdp-with-auth", "kind": "provision", "automatic": False,
             "uri": f"kvm://{t}/cdp/session/command/ensure",
             "label": "Re-launch CDP Chrome with copy_from=<user chrome profile> so it's logged in (needs consent)."},
        ],
        confidence=0.8,
    ),
    _Rule(
        "connector-required",
        [r"connector_required", r"needs a dedicated connector",
         r"require a dedicated connector/service",
         r"require a dedicated connector"],
        "The URI scheme needs a connector that implements its protocol, but no such connector "
        "is installed on this node. The error response includes connectorHint.package and "
        "connectorHint.installCommand. Install the connector, then re-deploy it to the node "
        "(urirun host deploy --merge) and adopt it (node://…/registry/command/adopt).",
        lambda t: [
            {"id": "check-connector-installed", "kind": "diagnostic", "automatic": False,
             "uri": f"node://{t}/capability/query/check",
             "label": "Verify which connectors are installed and adopted on this node."},
            {"id": "install-connector", "kind": "provision", "automatic": False,
             "label": "Install the connector package (see connectorHint.installCommand in the error response)."},
            {"id": "deploy-connector", "kind": "provision", "automatic": False,
             "label": "Deploy the connector to the node: urirun host deploy --merge <node_url>."},
            {"id": "adopt-connector", "kind": "provision", "automatic": True,
             "uri": f"node://{t}/registry/command/adopt",
             "label": "Adopt the installed connector so its routes go live immediately."},
        ],
        confidence=0.9,
    ),
    _Rule(
        "stale-node-urirun",
        [r"route not found.*(capability|env[./]query[./]profile|ui[./]command[./]act|cdp[./]session|registry[./]command[./]adopt)",
         r"(capability|env[./]query[./]profile|ui[./]command[./]act|cdp[./]session).*route not found"],
        "A route from a NEWER urirun / connector is absent on the node — the node runs an older "
        "urirun, or the connector was not (re)deployed. Update urirun on the node and/or re-deploy "
        "the connector with --merge.",
        lambda t: [
            {"id": "check-node-runtime", "kind": "diagnostic", "automatic": False,
             "uri": f"node://{t}/runtime/query/info", "label": "Check the node's urirun version."},
            {"id": "check-capability", "kind": "diagnostic", "automatic": False,
             "uri": f"node://{t}/capability/query/check",
             "label": "Probe whether the scheme/route is served by an installed connector here."},
            {"id": "update-or-redeploy", "kind": "provision", "automatic": False,
             "label": "Update urirun on the node (node://…/package/command/install urirun) or re-deploy the connector."},
        ],
        categories={"NOT_FOUND"},
        confidence=0.75,
    ),
    _Rule(
        "auth-required",
        [r"api key (not set|missing|required)", r"secret(?: not)? (found|resolved|set)",
         r"secretref.*(not found|missing|unresolvable)",
         r"\b403\b", r"authentication required", r"unauthorized",
         r"credentials (not|missing|required)", r"api.?key.*required"],
        "An API credential (secretRef, API key, token) is missing or unresolvable — "
        "the secret layer could not find the referenced key in env / dotenv / keyring. "
        "Configure the credential via secret:// reference or set the env var directly.",
        lambda t: [
            {"id": "check-secret-config", "kind": "diagnostic", "automatic": False,
             "uri": f"secret://{t}/config/query/status",
             "label": "Inspect the secret configuration — which provider holds this key."},
            {"id": "set-credential", "kind": "auth", "automatic": False,
             "label": "Set the missing credential (env var / keyring entry / .env file) and retry."},
        ],
        confidence=0.85,
    ),
    _Rule(
        "service-stopped",
        [r"connection refused", r"service (is )?not running", r"service (is )?stopped",
         r"failed to connect", r"(host|port) (is )?unreachable",
         r"econnrefused", r"broken pipe", r"nodename nor servname provided"],
        "The target service is not running or not reachable on its configured port — "
        "either the service crashed, was never started, or is listening on a different port. "
        "Start or restart the service and verify the URL/port.",
        lambda t: [
            {"id": "check-service-health", "kind": "diagnostic", "automatic": True,
             "uri": f"node://{t}/runtime/query/health",
             "label": "Probe the node's /health endpoint to distinguish down-node from down-service."},
            {"id": "restart-service", "kind": "provision", "automatic": False,
             "label": "Start or restart the service (dashboard://host/service/<name>/command/restart)."},
        ],
        confidence=0.8,
    ),
    _Rule(
        "port-busy",
        [r"address already in use", r"port.*already.*bound", r"eaddrinuse",
         r"port.*occupied", r"bind.*(failed|error).*port"],
        "The service could not bind its port — another process is already listening there. "
        "Find and stop the occupying process, or configure a different port.",
        lambda t: [
            {"id": "find-port-owner", "kind": "diagnostic", "automatic": False,
             "label": "Find what holds the port: ss -tlnp | grep :<port>  or  lsof -i :<port>."},
            {"id": "restart-service-force", "kind": "provision", "automatic": False,
             "label": "Stop the occupying process and restart the service (port-replace mode)."},
        ],
        confidence=0.9,
    ),
    _Rule(
        "verification-failed",
        [r"verification (failed|contract)", r"expected.*actual.*count",
         r"(file|doc|document|artifact) count mismatch",
         r"named check.*failed", r"post-condition.*not met", r"goal check.*failed"],
        "A side-effecting operation completed its steps but the POST-CONDITION was not met "
        "— the expected artifact count, file hash, or named check did not match the actual state. "
        "This is a silent partial-success: the steps ran but the intended outcome was not achieved.",
        lambda t: [
            {"id": "verify-state", "kind": "diagnostic", "automatic": True,
             "uri": f"node://{t}/runtime/query/health",
             "label": "Re-probe the node's state to understand what was actually written/changed."},
            {"id": "retry-operation", "kind": "retry", "automatic": False,
             "label": "Retry the operation — some verification failures are transient (file not synced yet)."},
            {"id": "inspect-error", "kind": "diagnostic", "automatic": False,
             "label": "Inspect the verification block in the result for expected vs actual counts."},
        ],
        confidence=0.85,
    ),
    _Rule(
        "unreachable-node",
        [r"node not reachable at",
         r"is [`']?urirun node serve[`']? running",
         r"node\(s\).*offline or unreachable",
         r"offline or unreachable.*node",
         r"node.*did not answer.*urirun.*health",
         r"urirun node serve.*running there"],
        "A named urirun node is not responding — the `urirun node serve` process is not running on that "
        "host (or the host itself is down / not on the network). This is different from a service like "
        "the scanner or chat being stopped: it is the *node daemon itself* that is absent. "
        "Fix: SSH to the target host and start `urirun node serve --host 0.0.0.0 --port PORT`; "
        "or run `urirun host nodes` to see which nodes are reachable from this host.",
        lambda t: [
            {"id": "check-node-list", "kind": "diagnostic", "automatic": False,
             "uri": f"dashboard://host/node/query/list",
             "label": "Run 'urirun host nodes' — shows URL, reachability and route count per node."},
            {"id": "start-node-serve", "kind": "provision", "automatic": False,
             "label": "SSH to the target host and run: urirun node serve --host 0.0.0.0 --port 8765"},
            {"id": "check-network", "kind": "diagnostic", "automatic": False,
             "label": "Verify network path: ping / nmap the node's host:port from this machine."},
        ],
        confidence=0.92,
    ),
    _Rule(
        "no-routes-discovered",
        [r"no uri steps",
         r"discovered 0 safe route",
         r"discovered \d+ safe route\(s\) on node\(s\) \[\]"],
        "The planner found no safe routes on the target nodes — either the node(s) are offline/unreachable "
        "or discover_mesh returned an empty list. The prompt cannot be converted to URI steps without live "
        "routes. Fix: ensure the target node is running and reachable (urirun node list / urirun host nodes), "
        "add the node URL with --node-url, or if the required capability is missing use 'urirun host ensure'.",
        lambda t: [
            {"id": "check-node-health", "kind": "diagnostic", "automatic": False,
             "label": "Run 'urirun host nodes' to see which nodes are reachable and report their route counts."},
            {"id": "add-node-url", "kind": "precondition", "automatic": False,
             "label": "Pass --node-url NAME=http://HOST:PORT to make the node URL visible to the planner."},
            {"id": "ensure-capability", "kind": "provision", "automatic": False,
             "label": "If the node is up but lacks the required scheme, run 'urirun host ensure NODE SCHEME'."},
        ],
        categories={"INVALID_ARGUMENT"},
        confidence=0.95,
    ),
    _Rule(
        "no-routes-for-intent",
        [r"no uri steps", r"safe route.*\[\]", r"discovered \d+ safe route"],
        "The planner found routes but none matched the intent — the required scheme/capability may not be "
        "deployed on the target node. For example, asking to 'open browser' requires a kvm:// or app:// "
        "connector serving browser routes; if those aren't in the mesh the planner produces empty steps. "
        "Fix: deploy the connector for the needed capability, or phrase the request in terms of "
        "available routes (urirun host routes shows what's ready).",
        lambda t: [
            {"id": "check-routes", "kind": "diagnostic", "automatic": False,
             "label": "Run 'urirun host routes' to see which URIs and schemes are currently available."},
            {"id": "ensure-capability", "kind": "provision", "automatic": False,
             "label": "Run 'urirun host ensure NODE SCHEME' to deploy the missing connector to the node."},
        ],
        categories={"INVALID_ARGUMENT"},
        confidence=0.75,
    ),
    _Rule(
        "missing-llm-model",
        [r"llm.?model.*not (set|configured|found)", r"llm.?model.*missing",
         r"no model configured", r"openai.*api.?key.*not set",
         r"no llm provider", r"llm.*unavailable", r"model.*not available"],
        "The LLM planner cannot generate a flow because no model is configured — "
        "LLM_MODEL env var is missing or the API key for the provider is absent. "
        "Set LLM_MODEL and the provider's API key (e.g. OPENROUTER_API_KEY) in the .env file.",
        lambda t: [
            {"id": "set-llm-model", "kind": "auth", "automatic": False,
             "label": "Set LLM_MODEL=<model-id> and the provider API key in the project .env file."},
            {"id": "retry-no-llm", "kind": "retry", "automatic": False,
             "label": "Retry with --no-llm to use the rule-based planner (no LLM required)."},
        ],
        confidence=0.9,
    ),
    _Rule(
        "route-not-served",
        [r"route not found", r"no available backend for"],
        "The URI's route is not live on the node — bindings were deployed without handler code, or the "
        "scheme's connector is installed but not adopted/served (or the node runs an older urirun).",
        lambda t: [
            {"id": "check-capability", "kind": "diagnostic", "automatic": False,
             "uri": f"node://{t}/capability/query/check",
             "label": "Probe whether the scheme/route is served by an installed connector here."},
            {"id": "adopt-scheme", "kind": "provision", "automatic": True,
             "uri": f"node://{t}/registry/command/adopt",
             "label": "Adopt the installed connector so its routes go live (or host deploy --merge)."},
        ],
        categories={"NOT_FOUND"},
        confidence=0.8,
    ),
    _Rule(
        "missing-precondition",
        [r"missing dependencies", r"precondition not met", r"portal not granted",
         r"permission denied", r"not satisf"],
        "A step's precondition is unmet — a required permission (portal grant, login), a missing "
        "dependency step result, or a resource lock not acquired. The acquire→prove→retry loop "
        "handles this: ready://ensure acquires automatically when possible, or surfaces a one-tap "
        "item for human-gated preconditions.",
        lambda t: [
            {"id": "ensure-precondition", "kind": "precondition", "automatic": True,
             "uri": f"ready://{t}/ready/command/ensure",
             "label": "Acquire the missing precondition via the readiness kernel (auto or one-tap)."},
            {"id": "check-readiness", "kind": "diagnostic", "automatic": True,
             "uri": f"ready://{t}/ready/query/report",
             "label": "Report all known preconditions and their current status."},
        ],
        categories={"FAILED_PRECONDITION", "PERMISSION_DENIED"},
        confidence=0.8,
    ),
]


_RULE_BY_ID = {r.id: r for r in PLAYBOOK}
_LOGIN_HINTS = ("authwall", "login", "log in", "sign in", "signin", "sign-in",
                "zaloguj", "logowanie", "/uas/", "/login")


def _is_login_surface(surface: dict | None) -> bool:
    """The foreground surface (kvm ``surface/query/current``) is a login / authwall page."""
    if not isinstance(surface, dict):
        return False
    b = surface.get("browser") or {}
    win = surface.get("window") or {}
    blob = f"{b.get('url', '')} {b.get('title', '')} {win.get('title', '')}".lower()
    return any(h in blob for h in _LOGIN_HINTS)


def _build(rule: _Rule, step: dict | None) -> dict:
    actions = rule.remediation(_target(step))
    return {"rule": rule.id, "cause": rule.cause, "confidence": rule.confidence,
            "remediation": actions,
            "autoApplicable": [a["id"] for a in actions if a.get("automatic")]}


def _decode_error_ctx(error: dict, step: dict | None) -> tuple[str, str, str, str]:
    message = str((error or {}).get("message") or "").casefold()
    category = str((error or {}).get("category") or "")
    uri = str((step or {}).get("uri") or "")
    scheme = uri.split("://", 1)[0] if "://" in uri else ""
    return message, category, uri, scheme


def diagnose(error: dict, *, step: dict | None = None, routes: list[dict] | None = None,
             environment: dict | None = None, surface: dict | None = None) -> dict | None:
    """Match an error against the playbook → a structured diagnosis, or None if no rule fits.

    Returns ``{rule, cause, confidence, remediation, autoApplicable}`` where ``remediation`` is
    the list of fix actions (URIs) and ``autoApplicable`` lists the ids safe to apply unattended.
    ``environment`` (node ``env/query/profile``) FITS the remediation to the machine. ``surface``
    (node ``surface/query/current``) lets a login/authwall page UPGRADE a generic "target not
    located" to the real cause — ``not-logged-in`` — so the self-heal recommends an auth re-launch
    (human-gated) instead of futilely ensuring CDP and retrying against a login wall."""
    message, category, uri, scheme = _decode_error_ctx(error, step)
    login = _is_login_surface(surface)

    matched = _match_rule(message, category, scheme)
    matched = _surface_upgrade(matched, login, scheme)
    if matched is None:
        return None
    diag = _build(matched, step)
    if surface is not None:
        diag["surface"] = {"kind": surface.get("kind"), "loginDetected": login}
    return fit_to_environment(diag, environment) if environment else diag


def _match_rule(message: str, category: str, scheme: str) -> _Rule | None:
    """First playbook rule whose signature matches (or None) — first match wins by order."""
    if not message:
        return None
    return next((r for r in PLAYBOOK if r.matches(message, category, scheme)), None)


def _surface_upgrade(matched: _Rule | None, login: bool, scheme: str) -> _Rule | None:
    """On a login page, a UI failure (or no rule, on a browser/kvm step) is an AUTH problem,
    not a missing element — upgrade to not-logged-in (the more specific cause)."""
    if not login:
        return matched
    no_rule_ui_step = matched is None and scheme in ("kvm", "browser")
    weak_ui_rule = matched is not None and matched.id == "ui-target-not-located"
    return _RULE_BY_ID["not-logged-in"] if (no_rule_ui_step or weak_ui_rule) else matched


def _cdp_feasible(env: dict) -> bool:
    cs = env.get("controlStrategies") or {}
    return bool(cs.get("cdp")) or bool(env.get("cdpFeasible"))


def _controllable(env: dict) -> bool:
    cs = env.get("controlStrategies") or {}
    return bool(env.get("controllable", any(cs.values()) if cs else True))


def _mark_feasibility(remediation: list, cdp_feasible: bool, controllable: bool) -> None:
    """Tag each remediation action with whether THIS environment can support it."""
    for a in remediation:
        aid = str(a.get("id") or "")
        if "/cdp/" in str(a.get("uri") or "") or aid.startswith("ensure-cdp"):
            a["feasible"] = cdp_feasible
        elif aid in ("retry-via-act", "retry-bounded", "wait-page-ready"):
            a["feasible"] = controllable
        else:
            a["feasible"] = True


def _os_level_unreliable(env: dict) -> bool:
    """Prefer the Mutter ground-truth flag (surface_report -> env.osLevelReliable); fall back
    to the wayland+best heuristic only when reliability is unknown."""
    reliable = env.get("osLevelReliable")
    if reliable is False:
        return True
    return reliable is None and bool(env.get("wayland")) and env.get("best") in (None, "atspi", "vision")


def _rem_has_ui_failure(rem: list) -> bool:
    return any(s in str(a.get("uri") or "") for a in rem for s in ("/ui/", "/cdp/", "/input/"))


def _rem_already_cdp(rem: list) -> bool:
    return any(
        str(a.get("id") or "").startswith("ensure-cdp")
        or ("/cdp/" in str(a.get("uri") or "") and a.get("automatic"))
        for a in rem
    )


def _maybe_escalate_surface(diagnosis: dict, env: dict, cdp_feasible: bool) -> None:
    """The three-sessions lesson as a recovery INPUT: on an unreliable Wayland os-level surface,
    a UI/input failure that has no CDP fix yet gets the WHOLE surface escalated to coordinate-free
    DOM control instead of retrying mis-mapped pixels."""
    rem = diagnosis.get("remediation") or []
    if _rem_has_ui_failure(rem) and cdp_feasible and not _rem_already_cdp(rem) and _os_level_unreliable(env):
        rem.insert(0, {"id": "escalate-surface-cdp", "kind": "provision", "automatic": True,
                       "feasible": True, "uri": f"kvm://{_target_of(rem)}/cdp/session/command/ensure",
                       "label": "OS-level pixel input is unreliable on this Wayland surface — escalate to "
                                "CDP (coordinate-free DOM control) instead of retrying os-level pixels."})
        diagnosis["surfaceEscalation"] = "os-level->cdp"


def fit_to_environment(diagnosis: dict, environment: dict) -> dict:
    """Fit a diagnosis to what the machine can ACTUALLY do: mark each remediation ``feasible``,
    escalate the surface when os-level is unreliable, recompute ``autoApplicable`` to feasible
    auto actions, and — when nothing can drive the UI — prepend an honest install/grant action."""
    cdp_feasible, controllable = _cdp_feasible(environment), _controllable(environment)
    _mark_feasibility(diagnosis.get("remediation") or [], cdp_feasible, controllable)
    _maybe_escalate_surface(diagnosis, environment, cdp_feasible)
    diagnosis["autoApplicable"] = [a["id"] for a in (diagnosis.get("remediation") or [])
                                   if a.get("automatic") and a.get("feasible", True)]
    diagnosis["environmentFit"] = {"controllable": controllable,
                                   "best": environment.get("best"), "cdpFeasible": cdp_feasible}
    if not controllable:
        diagnosis.setdefault("remediation", []).insert(0, {
            "id": "enable-ui-control", "kind": "provision", "automatic": False, "feasible": True,
            "label": "This environment cannot drive a UI: install tesseract / grant /dev/uinput, "
                     "or launch a CDP Chrome (env/query/profile shows what's missing)."})
    return diagnosis


# ── URI surface: diag://host/error/command/classify ───────────────────────────
# Exposes `diagnose` + `fit_to_environment` as an addressable URI capability.
# Callers (flow.py, dashboard, remote nodes) reach this by URI, not by import,
# so the capability is observable, gateable, and remotable without changing callers.
def _uri_classify(payload: dict) -> dict:
    """Handler for diag://<node>/error/command/classify.

    Payload: {error, step?, routes?, environment?, surface?}
    Returns: {ok, diagnosis?, unrecognized?, rule?}"""
    import urirun  # noqa: PLC0415
    error = payload.get("error") or {}
    step = payload.get("step")
    routes = payload.get("routes")
    environment = payload.get("environment")
    surface = payload.get("surface")
    result = diagnose(error, step=step, routes=routes, environment=environment, surface=surface)
    if result is None:
        return urirun.ok(diagnosis=None, rule=None, unrecognized=True,
                         autoApplicable=[], message="no playbook rule matched")
    return urirun.ok(diagnosis=result, rule=result.get("rule"),
                     autoApplicable=result.get("autoApplicable") or [])


try:
    import urirun as _urirun  # noqa: PLC0415
    _diag_conn = _urirun.connector("diag", scheme="diag")
    _diag_conn.handler("error/command/classify", meta={"label": "Diagnose error against playbook"})(_uri_classify)
except Exception:  # noqa: BLE001 - connector registration is optional (not available in lean test envs)
    pass
