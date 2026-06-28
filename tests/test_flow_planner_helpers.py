"""Owner unit tests for the pure flow-planning helpers (url/text parsing, URI segmentation +
template matching, JSON extraction). These live with the flow package — they need only
``urirun_flow``, no hub runtime. Moved here from the monorepo hub so the package owns its own
helper coverage (the hub keeps the integration tests that exercise execution through the shim)."""
from __future__ import annotations

import pytest

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
from urirun_flow.flow_planner import heuristic_flow, make_flow, normalize_flow, prepare_screenshot_capture_flow


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

    normalized = normalize_flow(flow, allowed)
    capture = normalized["steps"][1]

    assert capture["depends_on"] == ["list_windows"]
    assert capture["payload"]["monitor_from"] == "list_windows.result.value.selected.monitor"
    assert capture["payload"]["output"] == "chrome_monitor.png"
    assert "scope" not in capture["payload"]
    assert "monitor" not in capture["payload"]


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


def test_recalled_all_monitors_flow_rebinds_to_specific_monitor():
    # The recall bug: a remembered episode replayed {monitor: -1, scope: "all"} verbatim and
    # captured ALL monitors even though the new prompt asked for monitor 3. The fresh monitor
    # index must win, and the conflicting all-desktop scope must be cleared (the kvm contract
    # skips `monitor` when scope is all/all-monitors/desktop).
    recalled = {"steps": [{
        "id": "kvm_host_screen_query_capture",
        "uri": "kvm://host/screen/query/capture",
        "payload": {"monitor": -1, "scope": "all"},
    }]}

    repaired = prepare_screenshot_capture_flow(recalled, "zrob zrzut 3 ekranu monitora", set())

    assert repaired["steps"][0]["payload"] == {"monitor": 3, "scope": ""}


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


def test_make_flow_llm_mode_does_not_silently_fallback_to_heuristics(monkeypatch):
    mesh = {
        "nodes": [{"name": "host", "reachable": True}],
        "routes": [{"uri": "kvm://host/screen/query/capture", "node": "host", "safe": True}],
    }
    monkeypatch.delenv("URIRUN_LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("URIRUN_ALLOW_HEURISTIC_PLANNER_FALLBACK", raising=False)

    with pytest.raises(RuntimeError, match="heuristic fallback is disabled"):
        make_flow("zrob zrzut ekranu", mesh, use_llm=True)


def test_thin_plan_injects_inventory_beside_drift_for_kvm_steps():
    steps = [{"id": "cap", "uri": "kvm://host/screen/query/capture", "payload": {}}]

    plan = _build_thin_plan(steps, {"steps": steps}, execute=True, memory=object(), routes=[])

    assert [s["id"] for s in plan[:2]] == ["twin:drift:host", "twin:inventory:host"]
    assert plan[1]["uri"] == "twin://host/env/query/inventory"


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
