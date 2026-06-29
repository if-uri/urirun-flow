"""Owner unit tests for the pure flow-planning helpers (url/text parsing, URI segmentation +
template matching, JSON extraction). These live with the flow package — they need only
``urirun_flow``, no hub runtime. Moved here from the monorepo hub so the package owns its own
helper coverage (the hub keeps the integration tests that exercise execution through the shim)."""
from __future__ import annotations

import pytest

from urirun_flow import flow_planner as planner
from urirun_flow.flow import (
    _build_thin_plan,
    _build_env_inventory,
    first_url,
    json_from_text,
    nl_key,
    requested_folder_path,
    _uri_matches_template,
    _uri_segments,
)
from urirun_flow.flow_planner import (
    _safe_planner_error,
    heuristic_flow,
    make_flow,
    normalize_flow,
    normalize_flow_or_explain,
    prepare_screenshot_capture_flow,
)


# ─── first_url ───────────────────────────────────────────────────────────────

def test_first_url_extracts_https():
    assert first_url("check https://example.com/page now") == "https://example.com/page"


def test_first_url_extracts_http():
    assert first_url("open http://localhost:3000") == "http://localhost:3000"


def test_first_url_returns_none_when_absent():
    assert first_url("restart the phone scanner") is None


def test_first_url_returns_first_only():
    assert first_url("go to https://a.com and then https://b.com") == "https://a.com"


# ─── nl_key ──────────────────────────────────────────────────────────────────

def test_nl_key_lowercases():
    assert nl_key("HELLO WORLD") == "hello world"


def test_nl_key_strips_diacritics():
    result = nl_key("zażółć gęślą jaźń")
    assert "ż" not in result
    assert "ę" not in result


def test_nl_key_collapses_whitespace():
    assert nl_key("  foo   bar  ") == "foo bar"


# ─── requested_folder_path ───────────────────────────────────────────────────

def test_requested_folder_path_downloads():
    assert requested_folder_path("list the downloads folder") == "~/Downloads"
    assert requested_folder_path("pobrane pliki") == "~/Downloads"


def test_requested_folder_path_default():
    assert requested_folder_path("show processes") == "."


# ─── _uri_segments ───────────────────────────────────────────────────────────

def test_uri_segments_basic():
    scheme, segs = _uri_segments("kvm://laptop/display/query/info")
    assert scheme == "kvm"
    assert segs == ["laptop", "display", "query", "info"]


def test_uri_segments_no_path():
    scheme, segs = _uri_segments("env://node")
    assert scheme == "env"
    assert segs == ["node"]


# ─── _uri_matches_template ───────────────────────────────────────────────────

def test_uri_matches_template_exact():
    assert _uri_matches_template("kvm://laptop/display/query/info",
                                 "kvm://laptop/display/query/info") is True


def test_uri_matches_template_with_param():
    assert _uri_matches_template("kvm://laptop/display/query/info",
                                 "kvm://{host}/display/query/info") is True


def test_uri_matches_template_different_scheme():
    assert _uri_matches_template("env://laptop/x", "kvm://laptop/x") is False


def test_uri_matches_template_different_length():
    assert _uri_matches_template("kvm://laptop/a/b", "kvm://laptop/a") is False


def test_uri_matches_template_multi_param():
    assert _uri_matches_template("kvm://n1/window/cmd1/fire",
                                 "kvm://{host}/{id}/{verb}/fire") is True


# ─── json_from_text ──────────────────────────────────────────────────────────

def test_json_from_text_plain():
    result = json_from_text('{"steps": [{"uri": "env://n/x"}]}')
    assert result["steps"][0]["uri"] == "env://n/x"


def test_json_from_text_fenced():
    text = "Sure!\n```json\n{\"task\": \"done\"}\n```\n"
    assert json_from_text(text)["task"] == "done"


def test_json_from_text_embedded():
    text = "Here is the flow: {\"ok\": true} done."
    assert json_from_text(text)["ok"] is True


def test_normalize_binds_window_list_monitor_into_capture():
    allowed = {
        "kvm://host/window/query/list",
        "kvm://host/screen/query/capture",
    }
    routes = [
        {
            "uri": "kvm://host/window/query/list",
            "inputSchema": {
                "type": "object",
                "properties": {"app": {"type": "string"}, "title": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        {
            "uri": "kvm://host/screen/query/capture",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "monitor": {"type": "integer"},
                    "scope": {"type": "string"},
                    "output": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    ]
    flow = {
        "task": {"id": "shot"},
        "steps": [
            {
                "id": "list_windows",
                "uri": "kvm://host/window/query/list",
                "payload": {"app": "chrome"},
                "depends_on": [],
            },
            {
                "id": "capture_monitor",
                "uri": "kvm://host/screen/query/capture",
                "payload": {"monitor": -1, "scope": "all", "output": "chrome_monitor.png"},
                "depends_on": [],
            },
        ],
    }

    normalized = normalize_flow(flow, allowed, routes=routes)
    capture = normalized["steps"][1]

    assert capture["depends_on"] == ["list_windows"]
    assert capture["payload"]["monitor_from"] == "list_windows.result.value.selected.monitor"
    assert capture["payload"]["output"] == "chrome_monitor.png"
    assert "scope" not in capture["payload"]
    assert "monitor" not in capture["payload"]


def test_normalize_strips_conflicting_scope_all_when_llm_set_monitor_from():
    """Regression: the LLM set monitor_from itself BUT also left scope:all (its own
    WINDOW-MONITOR RULE violation). The old normalizer bailed when monitor_from was already
    present, so scope:all survived and the connector captured every monitor instead of the
    app's one. The conflict must be stripped so the resolved monitor wins."""
    allowed = {"kvm://host/window/query/list", "kvm://host/screen/query/capture"}
    routes = [
        {"uri": "kvm://host/window/query/list",
         "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}},
                         "additionalProperties": False}},
        {"uri": "kvm://host/screen/query/capture",
         "inputSchema": {"type": "object",
                         "properties": {"monitor": {"type": "integer"}, "scope": {"type": "string"}},
                         "additionalProperties": False}},
    ]
    flow = {
        "task": {"id": "shot"},
        "steps": [
            {"id": "list_chrome_windows", "uri": "kvm://host/window/query/list",
             "payload": {"app": "chrome"}, "depends_on": []},
            {"id": "capture_chrome_monitor", "uri": "kvm://host/screen/query/capture",
             "payload": {"monitor": -1, "scope": "all",
                         "monitor_from": "list_chrome_windows.result.value.selected.monitor"},
             "depends_on": ["list_chrome_windows"]},
        ],
    }

    capture = normalize_flow(flow, allowed, routes=routes)["steps"][1]

    assert "scope" not in capture["payload"]          # the conflicting scope:all is gone
    assert "monitor" not in capture["payload"]        # the placeholder monitor:-1 is gone
    assert capture["payload"]["monitor_from"] == "list_chrome_windows.result.value.selected.monitor"
    assert capture["depends_on"] == ["list_chrome_windows"]


def test_normalize_adds_dependency_for_llm_monitor_from_reference():
    allowed = {"kvm://host/window/query/list", "kvm://host/screen/query/capture"}
    routes = [
        {"uri": "kvm://host/window/query/list",
         "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}},
                         "additionalProperties": False}},
        {"uri": "kvm://host/screen/query/capture",
         "inputSchema": {"type": "object",
                         "properties": {"monitor": {"type": "integer"}, "monitor_from": {"type": "string"}},
                         "additionalProperties": False}},
    ]
    flow = {
        "task": {"id": "shot"},
        "steps": [
            {"id": "list_chrome_windows", "uri": "kvm://host/window/query/list",
             "payload": {"app": "chrome"}, "depends_on": []},
            {"id": "capture_chrome_monitor", "uri": "kvm://host/screen/query/capture",
             "payload": {"monitor_from": "list_chrome_windows.result.value.selected.monitor"},
             "depends_on": []},
        ],
    }

    capture = normalize_flow(flow, allowed, routes=routes)["steps"][1]

    assert capture["depends_on"] == ["list_chrome_windows"]


def test_safe_planner_error_redacts_urls_and_key_fragments():
    err = RuntimeError("Insufficient credits: https://openrouter.ai/settings/credits keys/secret123")
    msg = _safe_planner_error(err)
    assert "https://" not in msg
    assert "secret123" not in msg
    assert "Insufficient credits" in msg


def test_empty_flow_with_llm_quota_reason_does_not_suggest_node_url():
    routes = [{"uri": "adb://host/input/command/key", "node": "host", "safe": True}]
    reason = 'litellm.APIError: OpenrouterException - {"error":{"message":"Key limit exceeded","code":403}}'

    with pytest.raises(ValueError) as err:
        normalize_flow_or_explain(
            {"steps": []},
            {"adb://host/input/command/key"},
            routes=routes,
            selected_nodes=["host"],
            planner_reason=reason,
        )

    msg = str(err.value)
    assert "LLM planner/provider failed" in msg
    assert "--node-url" not in msg


def test_empty_flow_without_routes_keeps_node_url_hint():
    with pytest.raises(ValueError) as err:
        normalize_flow_or_explain(
            {"steps": []},
            set(),
            routes=[],
            selected_nodes=["laptop"],
            planner_reason="LLM disabled",
        )

    assert "--node-url" in str(err.value)


def test_normalize_rejects_unknown_result_reference_against_strict_schema():
    allowed = {"kvm://host/screen/query/capture"}
    routes = [{
        "uri": "kvm://host/screen/query/capture",
        "inputSchema": {
            "type": "object",
            "properties": {"monitor": {"type": "integer"}},
            "additionalProperties": False,
        },
    }]

    with pytest.raises(ValueError, match="Additional properties"):
        normalize_flow(
            {
                "steps": [{
                    "id": "capture",
                    "uri": "kvm://host/screen/query/capture",
                    "payload": {"foo_from": "list.result.value.foo"},
                }],
            },
            allowed,
            routes=routes,
        )


def test_normalize_strips_angle_brackets_from_result_reference_step_id():
    allowed = {
        "kvm://host/window/query/list",
        "kvm://host/screen/query/capture",
    }
    routes = [
        {
            "uri": "kvm://host/window/query/list",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": True},
        },
        {
            "uri": "kvm://host/screen/query/capture",
            "inputSchema": {
                "type": "object",
                "properties": {"monitor": {"type": "integer"}},
                "additionalProperties": False,
            },
        },
    ]

    normalized = normalize_flow(
        {
            "steps": [
                {"id": "list_windows", "uri": "kvm://host/window/query/list", "payload": {}},
                {
                    "id": "capture",
                    "uri": "kvm://host/screen/query/capture",
                    "payload": {"monitor_from": "<list_windows>.result.value.selected.monitor"},
                    "depends_on": ["list_windows"],
                },
            ],
        },
        allowed,
        routes=routes,
    )

    assert normalized["steps"][1]["payload"]["monitor_from"] == (
        "list_windows.result.value.selected.monitor"
    )


# ─── screenshot capture repair ──────────────────────────────────────────────

def test_screenshot_capture_bypasses_required_verify_from_recalled_episode():
    flow = {
        "steps": [
            {"id": "ready", "uri": "kvm://host/cdp/page/query/ready", "payload": {}, "depends_on": []},
            {
                "id": "verify",
                "uri": "kvm://host/ui/query/verify",
                "payload": {"expect": "LinkedIn", "required": True},
                "depends_on": ["ready"],
            },
            {
                "id": "capture_screen",
                "uri": "kvm://host/screen/query/capture",
                "payload": {"base64": True},
                "depends_on": ["verify"],
            },
        ]
    }
    repaired = prepare_screenshot_capture_flow(flow, "zrob zrzut ekranu", set())

    verify = repaired["steps"][1]
    capture = repaired["steps"][2]
    assert verify["optional"] is True
    assert verify["payload"]["required"] is False
    assert capture["depends_on"] == ["ready"]
    assert capture["payload"]["base64"] is True
    assert capture["payload"]["scope"] == "browser"


def test_injected_screenshot_capture_depends_on_step_before_required_verify():
    flow = {
        "steps": [
            {"id": "ready", "uri": "kvm://host/cdp/page/query/ready", "payload": {}, "depends_on": []},
            {
                "id": "verify",
                "uri": "kvm://host/ui/query/verify",
                "payload": {"expect": "LinkedIn", "required": True},
                "depends_on": ["ready"],
            },
        ]
    }
    repaired = prepare_screenshot_capture_flow(
        flow,
        "otworz linkedin i zrob screenshot",
        {"kvm://host/screen/query/capture"},
    )

    assert repaired["steps"][1]["optional"] is True
    assert repaired["steps"][2]["id"] == "capture_screen"
    assert repaired["steps"][2]["depends_on"] == ["ready"]
    assert repaired["steps"][2]["payload"] == {"scope": "browser"}


def test_all_monitors_prompt_sets_capture_scope_all():
    repaired = prepare_screenshot_capture_flow(
        {"steps": []},
        "zrob zrzut ekranu wszystkich monitorw",
        {"kvm://host/screen/query/capture"},
    )

    capture = repaired["steps"][0]
    assert capture["payload"] == {"scope": "all", "monitor": -1}


def test_existing_capture_gets_all_monitor_payload_without_losing_base64():
    flow = {"steps": [{
        "id": "capture_screen",
        "uri": "kvm://host/screen/query/capture",
        "payload": {"base64": True},
    }]}

    repaired = prepare_screenshot_capture_flow(flow, "screenshot all monitors", set())

    assert repaired["steps"][0]["payload"] == {"base64": True, "scope": "all", "monitor": -1}


def test_explicit_monitor_number_is_kept_one_based():
    repaired = prepare_screenshot_capture_flow(
        {"steps": []},
        "zrob zrzut ekranu monitor 2",
        {"kvm://host/screen/query/capture"},
    )

    assert repaired["steps"][0]["payload"] == {"monitor": 2}


def test_explicit_monitor_number_before_word_is_kept_one_based():
    repaired = prepare_screenshot_capture_flow(
        {"steps": []},
        "zrob zrzut ekranu 3 monitora",
        {"kvm://host/screen/query/capture"},
    )

    assert repaired["steps"][0]["payload"] == {"monitor": 3}


def test_explicit_monitor_number_after_numer_is_kept_one_based():
    repaired = prepare_screenshot_capture_flow(
        {"steps": []},
        "zrzut ekranu monitora numer 3",
        {"kvm://host/screen/query/capture"},
    )

    assert repaired["steps"][0]["payload"] == {"monitor": 3}


def test_monitor_index_via_ekran_phrasing_is_extracted():
    # "zrob zrzut 3 ekranu monitora" — the digit is separated from "monitor" by "ekranu",
    # so the bare-number patterns miss it; anchored on "monitor" so it cannot false-positive.
    repaired = prepare_screenshot_capture_flow(
        {"steps": []},
        "zrob zrzut 3 ekranu  monitora",
        {"kvm://host/screen/query/capture"},
    )

    assert repaired["steps"][0]["payload"] == {"monitor": 3}


def test_recalled_all_monitors_scope_is_preserved_for_the_env_enum_gate():
    # Canon (EXPERIENCE_RETRIEVAL.md): a recalled {scope: all} must NOT be silently re-bound
    # here. Its scope is preserved so the env-enum recall gate
    # (recall_env_enum_replan_required) detects the skipWhen and sends the flow to
    # retrieve→propose. This layer proposes; the gate admits.
    from urirun_flow.env_selection import recall_env_enum_replan_required

    recalled = {"steps": [{
        "id": "kvm_host_screen_query_capture",
        "uri": "kvm://host/screen/query/capture",
        "payload": {"monitor": -1, "scope": "all"},
    }]}

    repaired = prepare_screenshot_capture_flow(recalled, "zrob zrzut 3 ekranu monitora", set())

    # scope:all survives (not cleared) so the gate can still see it.
    assert repaired["steps"][0]["payload"].get("scope") == "all"

    routes = [{"uri": "kvm://host/screen/query/capture", "node": "host", "meta": {"contract": {
        "domains": {"monitor": {"type": "enum", "domain": "env:monitors.id",
                                "skipWhen": {"scope": ["all", "all-monitors", "desktop"]}}}}}}]
    inventories = {"host": {"domains": {"env:monitors.id": [{"value": 1}, {"value": 2}, {"value": 3}]}}}
    verdict = recall_env_enum_replan_required(repaired, routes, inventories)
    assert verdict["required"] is True
    assert verdict["reason"] == "skip-when"


def test_generic_screenshot_prompt_does_not_bind_a_monitor():
    # "zrob zrzut ekranu" is the everyday phrase for "take a screenshot" — it must not be
    # misread as a monitor index by the new ekran-anchored pattern.
    repaired = prepare_screenshot_capture_flow(
        {"steps": []},
        "zrob zrzut ekranu",
        {"kvm://host/screen/query/capture"},
    )

    assert "monitor" not in repaired["steps"][0]["payload"]


def test_heuristic_flow_uses_kvm_capture_for_screen_prompt_without_llm():
    routes = [{"uri": "kvm://host/screen/query/capture", "node": "host", "safe": True}]
    nodes = [{"name": "host", "reachable": True}]

    flow = heuristic_flow("zrob zrzut ekranu wszystkich monitorw", routes, nodes, use_llm=False)

    assert [step["uri"] for step in flow["steps"]] == ["kvm://host/screen/query/capture"]


def test_make_flow_no_llm_adds_all_monitor_payload_to_kvm_capture():
    mesh = {
        "nodes": [{"name": "host", "reachable": True}],
        "routes": [{"uri": "kvm://host/screen/query/capture", "node": "host", "safe": True}],
    }

    flow, generator = make_flow("zrob zrzut ekranu wszystkich monitorw", mesh, use_llm=False)

    assert generator["provider"] == "heuristic"
    assert flow["steps"][0]["uri"] == "kvm://host/screen/query/capture"
    assert flow["steps"][0]["payload"] == {"scope": "all", "monitor": -1}


def test_date_prompt_prefers_typed_time_query_over_shell_command():
    routes = [
        {"uri": "env://host/runtime/query/health", "node": "host", "safe": True},
        {"uri": "time://host/clock/query/now", "node": "host", "safe": True},
        {"uri": "shell://host/command/date", "node": "host", "safe": True},
    ]
    nodes = [{"name": "host", "reachable": True}]

    flow = heuristic_flow("jaka jest data?", routes, nodes, selected_nodes=["host"], use_llm=False)

    assert [step["uri"] for step in flow["steps"]] == ["time://host/clock/query/now"]


def test_date_prompt_keeps_shell_fallback_when_time_query_route_is_absent():
    routes = [
        {"uri": "env://lenovo/runtime/query/health", "node": "lenovo", "safe": True},
        {"uri": "shell://lenovo/command/date", "node": "lenovo", "safe": True},
    ]
    nodes = [{"name": "lenovo", "reachable": True}]

    flow = heuristic_flow(
        "jaka jest data na lenovo laptop",
        routes,
        nodes,
        selected_nodes=["lenovo"],
        use_llm=False,
    )

    assert [step["uri"] for step in flow["steps"]] == [
        "env://lenovo/runtime/query/health",
        "shell://lenovo/command/date",
    ]


def test_make_flow_llm_outage_degrades_to_heuristic_by_default(monkeypatch):
    # Inverted planner policy: the LLM leads, but a planner outage (here: no model configured)
    # DEGRADES to the deterministic heuristic by default — the operator gets a result, not a wall.
    mesh = {
        "nodes": [{"name": "host", "reachable": True}],
        "routes": [{"uri": "kvm://host/screen/query/capture", "node": "host", "safe": True}],
    }
    monkeypatch.delenv("URIRUN_LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("URIRUN_ALLOW_HEURISTIC_PLANNER_FALLBACK", raising=False)
    monkeypatch.delenv("URIRUN_STRICT_LLM_PLANNER", raising=False)

    flow, generator = make_flow("zrob zrzut ekranu", mesh, use_llm=True)
    assert generator["provider"] == "heuristic"
    assert generator["fallback"] is True


def test_make_flow_llm_outage_raises_only_in_strict_mode(monkeypatch):
    # Opt-in loud failure for callers that must not silently degrade (e.g. CI measuring the LLM).
    mesh = {
        "nodes": [{"name": "host", "reachable": True}],
        "routes": [{"uri": "kvm://host/screen/query/capture", "node": "host", "safe": True}],
    }
    monkeypatch.delenv("URIRUN_LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("URIRUN_STRICT_LLM_PLANNER", "1")

    with pytest.raises(RuntimeError, match="strict mode"):
        make_flow("zrob zrzut ekranu", mesh, use_llm=True)


def test_make_flow_llm_model_override_is_passed_to_provider(monkeypatch):
    mesh = {
        "nodes": [{"name": "host", "reachable": True}],
        "routes": [{"uri": "kvm://host/screen/query/capture", "node": "host", "safe": True}],
    }
    captured = {}

    class _Message:
        content = (
            '{"task":{"id":"shot"},"steps":[{"id":"cap",'
            '"uri":"kvm://host/screen/query/capture","payload":{},"depends_on":[]}]}'
        )

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]

    def fake_completion(*, model, messages, **kwargs):
        captured["model"] = model
        captured["messages"] = messages
        return _Response()

    monkeypatch.delenv("URIRUN_LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setattr(planner, "quiet_completion", fake_completion)

    flow, generator = make_flow("zrob zrzut ekranu", mesh, use_llm=True, llm_model="request/model")

    assert captured["model"] == "request/model"
    assert generator["model"] == "request/model"
    assert flow["steps"][0]["uri"] == "kvm://host/screen/query/capture"


def test_thin_plan_injects_inventory_beside_drift_for_kvm_steps():
    steps = [{"id": "cap", "uri": "kvm://host/screen/query/capture", "payload": {}}]

    plan = _build_thin_plan(steps, {"steps": steps}, execute=True, memory=object(), routes=[])

    assert [s["id"] for s in plan[:2]] == ["twin:drift:host", "twin:inventory:host"]
    assert plan[1]["uri"] == "twin://host/env/query/inventory"


def test_fetch_planner_environments_prefers_twin_profile_inventory(monkeypatch):
    calls = []

    def fake_local(uri, payload=None):
        calls.append(("local", uri, dict(payload or {})))
        if uri == "twin://host/environment/query/profile":
            return {"ok": True, "result": {"value": {
                "ok": True,
                "node": "host",
                "profile": {
                    "controlStrategies": {"cdp": True},
                    "best": "cdp",
                    "controllable": True,
                    "actionMatrix": {},
                },
                "surface": {"kind": "browser", "browser": {
                    "url": "https://linkedin.com/feed",
                    "title": "Feed",
                }},
                "constraints": [{"kind": "infeasible", "what": "web-auth", "surface": "cdp"}],
                "warnings": ["measured by twin"],
            }}}
        if uri == "twin://host/environment/query/inventory":
            return {"ok": True, "result": {"value": {
                "ok": True,
                "node": "host",
                "displays": [{"id": "DP-2", "primary": True}],
                "audioSinks": [],
                "cameras": [],
            }}}
        return None

    def fake_v2_call(uri, payload, registry, mode):
        calls.append(("v2", uri, dict(payload or {})))
        return {"ok": False, "result": {"value": {}}}

    monkeypatch.setattr(planner, "_local_inprocess_query", fake_local)
    monkeypatch.setattr(planner.v2_service, "call", fake_v2_call)

    envs = planner.fetch_planner_environments(["host"], registry={}, mesh={"serviceMap": {}})

    assert len(envs) == 1
    assert envs[0]["facts"]["bestSurface"] == "cdp"
    assert envs[0]["facts"]["foreground"]["url"] == "https://linkedin.com/feed"
    assert envs[0]["inventory"]["displays"][0]["id"] == "DP-2"
    assert any(c.get("what") == "web-auth" for c in envs[0]["constraints"])
    assert any("measured by twin" in g for g in envs[0]["guidance"])
    assert not any(uri.startswith("kvm://") for _, uri, _ in calls)


def test_env_inventory_builds_monitor_domain_from_display(monkeypatch):
    def fake_call(uri, payload, registry):
        if uri.endswith("/display/query/info"):
            return {
                "width": 5888,
                "height": 2889,
                "monitors": [
                    {"connector": "HDMI-1", "x": 0, "y": 1609,
                     "logicalWidth": 2048, "logicalHeight": 1280, "primary": True},
                    {"connector": "DP-2", "x": 2048, "y": 0,
                     "logicalWidth": 3840, "logicalHeight": 2160},
                ],
            }
        if uri.endswith("/browser/query/sessions"):
            return {"browsers": [{"browser": "chrome", "cdp_port": 9222, "profile": "/tmp/cdp",
                                  "running": True, "throwaway": True}]}
        if uri.endswith("/env/query/profile"):
            return {"platform": "linux-wayland", "wayland": True, "best": "cdp"}
        return {}

    monkeypatch.setattr("urirun_flow.flow._call_route_value", fake_call)

    inv = _build_env_inventory("host", {})

    assert inv["monitors"][1]["label"] == "DP-2"
    assert inv["domains"]["env:monitors.id"][1]["value"] == 2
    assert inv["domains"]["env:cdp_endpoints.id"][0]["value"] == "127.0.0.1:9222"


def test_make_flow_no_llm_uses_uri_targets_when_host_is_not_in_nodes():
    mesh = {
        "nodes": [],
        "routes": [{"uri": "kvm://host/screen/query/capture", "node": "host", "safe": True}],
    }

    flow, _ = make_flow("zrob zrzut ekranu", mesh, use_llm=False)

    assert [step["uri"] for step in flow["steps"]] == ["kvm://host/screen/query/capture"]


def test_screenshot_repair_keeps_required_verify_that_guards_later_command():
    flow = {
        "steps": [
            {"id": "ready", "uri": "kvm://host/cdp/page/query/ready", "payload": {}, "depends_on": []},
            {
                "id": "verify",
                "uri": "kvm://host/ui/query/verify",
                "payload": {"expect": "Publish", "required": True},
                "depends_on": ["ready"],
            },
            {
                "id": "click",
                "uri": "kvm://host/cdp/page/command/click",
                "payload": {"text": "Publish"},
                "depends_on": ["verify"],
            },
            {
                "id": "capture_screen",
                "uri": "kvm://host/screen/query/capture",
                "payload": {},
                "depends_on": ["click"],
            },
        ]
    }
    repaired = prepare_screenshot_capture_flow(flow, "zrob screenshot po publikacji", set())

    assert "optional" not in repaired["steps"][1]
    assert repaired["steps"][1]["payload"]["required"] is True
    assert repaired["steps"][3]["depends_on"] == ["click"]
    assert repaired["steps"][3]["payload"]["scope"] == "browser"


def test_desktop_screenshot_without_cdp_keeps_os_capture_scope():
    flow = {
        "steps": [
            {
                "id": "capture_screen",
                "uri": "kvm://host/screen/query/capture",
                "payload": {},
                "depends_on": [],
            },
        ]
    }
    repaired = prepare_screenshot_capture_flow(flow, "zrob zrzut ekranu pulpitu", set())

    assert "scope" not in repaired["steps"][0]["payload"]


def test_window_monitor_capture_focuses_window_before_screenshot_when_route_available():
    flow = {
        "steps": [
            {
                "id": "list_chrome_windows",
                "uri": "kvm://host/window/query/list",
                "payload": {"app": "chrome"},
                "depends_on": [],
            },
            {
                "id": "capture_chrome_screen",
                "uri": "kvm://host/screen/query/capture",
                "payload": {"monitor_from": "list_chrome_windows.result.value.selected.monitor"},
                "depends_on": ["list_chrome_windows"],
            },
        ]
    }
    repaired = prepare_screenshot_capture_flow(flow, "zrob zrzut monitora z chrome", {
        "kvm://host/window/query/list",
        "kvm://host/window/command/focus",
        "kvm://host/screen/query/capture",
    })

    assert [step["id"] for step in repaired["steps"]] == [
        "list_chrome_windows",
        "focus_list_chrome_windows",
        "capture_chrome_screen",
    ]
    assert repaired["steps"][1]["uri"] == "kvm://host/window/command/focus"
    assert repaired["steps"][1]["payload"] == {"title": "chrome"}
    assert repaired["steps"][2]["depends_on"] == ["focus_list_chrome_windows", "list_chrome_windows"]

    repaired_again = prepare_screenshot_capture_flow(repaired, "zrob zrzut monitora z chrome", {
        "kvm://host/window/query/list",
        "kvm://host/window/command/focus",
        "kvm://host/screen/query/capture",
    })
    assert [step["id"] for step in repaired_again["steps"]] == [
        "list_chrome_windows",
        "focus_list_chrome_windows",
        "capture_chrome_screen",
    ]


def test_window_monitor_capture_does_not_inject_focus_without_focus_route():
    flow = {
        "steps": [
            {
                "id": "list_chrome_windows",
                "uri": "kvm://host/window/query/list",
                "payload": {"app": "chrome"},
                "depends_on": [],
            },
            {
                "id": "capture_chrome_screen",
                "uri": "kvm://host/screen/query/capture",
                "payload": {"monitor_from": "list_chrome_windows.result.value.selected.monitor"},
                "depends_on": ["list_chrome_windows"],
            },
        ]
    }
    repaired = prepare_screenshot_capture_flow(flow, "zrob zrzut monitora z chrome", {
        "kvm://host/window/query/list",
        "kvm://host/screen/query/capture",
    })

    assert [step["id"] for step in repaired["steps"]] == ["list_chrome_windows", "capture_chrome_screen"]


def test_window_monitor_capture_collapses_duplicate_focus_steps_from_recall():
    flow = {
        "steps": [
            {"id": "list_chrome_windows", "uri": "kvm://host/window/query/list",
             "payload": {"app": "chrome"}, "depends_on": []},
            {"id": "focus_list_chrome_windows", "uri": "kvm://host/window/command/focus",
             "payload": {"title": "chrome"}, "depends_on": ["list_chrome_windows"]},
            {"id": "focus_list_chrome_windows_2", "uri": "kvm://host/window/command/focus",
             "payload": {"title": "chrome"}, "depends_on": ["list_chrome_windows"]},
            {"id": "capture_chrome_screen", "uri": "kvm://host/screen/query/capture",
             "payload": {"monitor_from": "list_chrome_windows.result.value.selected.monitor"},
             "depends_on": ["focus_list_chrome_windows", "focus_list_chrome_windows_2"]},
        ]
    }
    repaired = prepare_screenshot_capture_flow(flow, "zrob zrzut monitora z chrome", {
        "kvm://host/window/query/list",
        "kvm://host/window/command/focus",
        "kvm://host/screen/query/capture",
    })

    assert [step["id"] for step in repaired["steps"]] == [
        "list_chrome_windows",
        "focus_list_chrome_windows",
        "capture_chrome_screen",
    ]
    assert repaired["steps"][2]["depends_on"] == ["focus_list_chrome_windows", "list_chrome_windows"]


def test_normalize_wires_depends_on_for_clean_monitor_from():
    """Regression (found live): the LLM routinely emits a CLEAN monitor_from (no scope:all)
    but forgets depends_on. The normalizer must still declare the data dependency — a
    monitor_from reference requires its producer in depends_on, not luck of step order."""
    allowed = {"kvm://host/window/query/list", "kvm://host/screen/query/capture"}
    routes = [
        {"uri": "kvm://host/window/query/list",
         "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}},
                         "additionalProperties": False}},
        {"uri": "kvm://host/screen/query/capture",
         "inputSchema": {"type": "object", "properties": {"monitor": {"type": "integer"}},
                         "additionalProperties": False}},
    ]
    flow = {"task": {"id": "shot"}, "steps": [
        {"id": "list_chrome_windows", "uri": "kvm://host/window/query/list",
         "payload": {"app": "chrome"}, "depends_on": []},
        {"id": "capture_chrome_monitor", "uri": "kvm://host/screen/query/capture",
         "payload": {"monitor_from": "list_chrome_windows.result.value.selected.monitor"},
         "depends_on": []},
    ]}
    capture = normalize_flow(flow, allowed, routes=routes)["steps"][1]
    assert capture["depends_on"] == ["list_chrome_windows"]
    assert capture["payload"]["monitor_from"] == "list_chrome_windows.result.value.selected.monitor"


def test_heuristic_uses_window_inventory_for_named_monitor_anchor():
    routes = [
        {"uri": "kvm://host/window/query/list", "node": "host", "safe": True},
        {"uri": "kvm://host/screen/query/capture", "node": "host", "safe": True},
    ]
    nodes = [{"name": "host"}]
    environments = [{
        "node": "host",
        "windows": [{"app": "Google Chrome", "title": "LinkedIn - Google Chrome", "monitor": 2}],
    }]

    flow = heuristic_flow(
        "zrób zrzut ekranu monitora, na którym jest chrome",
        routes,
        nodes,
        selected_nodes=["host"],
        use_llm=False,
        environments=environments,
    )

    assert [s["uri"] for s in flow["steps"]] == [
        "kvm://host/window/query/list",
        "kvm://host/screen/query/capture",
    ]
    assert flow["steps"][0]["payload"] == {"app": "chrome"}
    assert flow["steps"][1]["payload"] == {
        "monitor_from": "kvm_host_window_query_list.result.value.selected.monitor"
    }
    assert flow["steps"][1]["depends_on"] == ["kvm_host_window_query_list"]


def test_heuristic_does_not_invent_window_anchor_when_inventory_has_no_match():
    routes = [
        {"uri": "kvm://host/window/query/list", "node": "host", "safe": True},
        {"uri": "kvm://host/screen/query/capture", "node": "host", "safe": True},
    ]
    nodes = [{"name": "host"}]
    environments = [{"node": "host", "windows": [{"app": "VSCodium", "title": "README.md"}]}]

    flow = heuristic_flow(
        "zrób zrzut ekranu monitora, na którym jest chrome",
        routes,
        nodes,
        selected_nodes=["host"],
        use_llm=False,
        environments=environments,
    )

    assert [s["uri"] for s in flow["steps"]] == ["kvm://host/screen/query/capture"]
    assert flow["steps"][0]["payload"] == {}


def test_fetch_kvm_query_prefers_local_inprocess_for_host(monkeypatch):
    calls = []

    def fake_local(uri, payload):
        calls.append(("local", uri, payload))
        return {"ok": True, "result": {"windows": [{"app": "Google Chrome", "monitor": 2}]}}

    def fake_mesh(*args, **kwargs):  # pragma: no cover - should not be called
        calls.append(("mesh", args, kwargs))
        return {"ok": False, "error": {"type": "transport"}}

    monkeypatch.setattr(planner, "_local_inprocess_query", fake_local)
    monkeypatch.setattr(planner.v2_service, "call", fake_mesh)

    value = planner._fetch_kvm_query(
        {"uri": "kvm://host/x"},
        {"routes": []},
        "window/query/list",
        "windows",
    )

    assert value == {"windows": [{"app": "Google Chrome", "monitor": 2}]}
    assert calls == [("local", "kvm://host/window/query/list", {})]


def test_env_domain_binding_is_table_driven():
    """Step-1 refactor: the window-list -> capture.monitor coupling is no longer hardcoded
    logic but ONE row in _ENV_DOMAIN_PRODUCERS. A new env-enum domain is a table row, not a
    code path — proven by registering a temp spec and seeing it wire with no new code."""
    from urirun_flow import flow_planner as fp
    assert any(s["domain"] == "env:monitors.id" for s in fp._ENV_DOMAIN_PRODUCERS)
    # monitor still wires exactly as before, via the generic table-driven binder
    steps = [
        {"id": "list_w", "uri": "kvm://host/window/query/list", "payload": {"app": "chrome"}},
        {"id": "cap", "uri": "kvm://host/screen/query/capture", "payload": {"monitor": -1, "scope": "all"}},
    ]
    cap = fp._bind_env_domain_producers(steps)[1]
    assert cap["payload"]["monitor_from"] == "list_w.result.value.selected.monitor"
    assert cap["depends_on"] == ["list_w"] and "scope" not in cap["payload"]
    # a brand-new env-enum domain wires generically with zero new logic
    saved = fp._ENV_DOMAIN_PRODUCERS
    try:
        fp._ENV_DOMAIN_PRODUCERS = (*saved, {
            "domain": "env:audio_sinks.id", "consumer_suffix": "/audio/command/play",
            "producer_suffix": "/audio/query/list", "param": "sink",
            "path": "result.value.selected.sink", "skip_scopes": ("all",)})
        s2 = [{"id": "list_s", "uri": "snd://host/audio/query/list", "payload": {}},
              {"id": "play", "uri": "snd://host/audio/command/play", "payload": {"scope": "all"}}]
        play = fp._bind_env_domain_producers(s2)[1]
        assert play["payload"]["sink_from"] == "list_s.result.value.selected.sink"
        assert play["depends_on"] == ["list_s"]
    finally:
        fp._ENV_DOMAIN_PRODUCERS = saved


def test_recall_is_planner_fallback_before_heuristic():
    """Step-4: a known-good retrieval candidate is the planner fallback BEFORE the hardcoded
    heuristic (recall is material, the heuristic is last resort). The no-retrieval path is
    unchanged, so the offline harness keeps measuring the same thing."""
    from urirun_flow.flow_planner import _flow_from_retrieval, make_flow
    assert _flow_from_retrieval(None) is None and _flow_from_retrieval({}) is None
    retr = {"flows": [{"steps": [{"id": "s1", "uri": "env://host/runtime/query/health", "payload": {}}]}],
            "episodes": []}
    assert _flow_from_retrieval(retr)["task"]["source"] == "recall-fallback"
    mesh = {"routes": [{"uri": "env://host/runtime/query/health", "kind": "query", "safe": True}],
            "nodes": [{"name": "host"}]}
    _, gen_recall = make_flow("sprawdz health", mesh, use_llm=False, retrieval=retr)
    _, gen_heur = make_flow("sprawdz health", mesh, use_llm=False, retrieval=None)
    assert gen_recall["provider"] == "recall"
    assert gen_heur["provider"] == "heuristic"
