"""Owner unit tests for the pure flow-planning helpers (url/text parsing, URI segmentation +
template matching, JSON extraction). These live with the flow package — they need only
``urirun_flow``, no hub runtime. Moved here from the monorepo hub so the package owns its own
helper coverage (the hub keeps the integration tests that exercise execution through the shim)."""
from __future__ import annotations

from urirun_flow.flow import (
    first_url,
    json_from_text,
    nl_key,
    requested_folder_path,
    _uri_matches_template,
    _uri_segments,
)
from urirun_flow.flow_planner import prepare_screenshot_capture_flow


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
