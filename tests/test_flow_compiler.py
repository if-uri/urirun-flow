"""Owner tests for the declarative NL -> urirun.flow.v1 prompt compiler
(STARTER-132): fail-closed schema validation, injectable transport, and the
make_flow fallback chain (llm_flow -> recall -> flow-compiler -> offline
heuristic safe fallback)."""
from __future__ import annotations

import json

import pytest

from urirun_flow import flow_planner as planner
from urirun_flow.flow_compiler import (
    COMPILER_SYSTEM_PROMPT,
    MAX_STEPS,
    compile_flow,
    parse_flow_v1,
)
from urirun_flow.flow_planner import make_flow


class _Message:
    def __init__(self, content: str):
        self.content = content


class _Choice:
    def __init__(self, message):
        self.message = message


class _Response:
    def __init__(self, content: str):
        self.choices = [_Choice(_Message(content))]


VALID_FLOW = {
    "task": {"id": "shot", "title": "Screenshot"},
    "steps": [
        {"id": "cap", "uri": "kvm://host/screen/query/capture", "payload": {}, "depends_on": []},
    ],
}

ROUTES = [
    {"uri": "kvm://host/screen/query/capture", "node": "host", "safe": True},
    {"uri": "kvm://host/runtime/query/health", "node": "host", "safe": True},
]
NODES = [{"name": "host", "reachable": True}]
ALLOWED = {route["uri"] for route in ROUTES}


# ─── parse_flow_v1: fail-closed validation ──────────────────────────────────

def test_parse_flow_v1_accepts_valid_document():
    flow = parse_flow_v1(VALID_FLOW, ALLOWED)
    assert flow["task"]["id"] == "shot"
    assert flow["steps"][0]["uri"] in ALLOWED


@pytest.mark.parametrize("mutation,reason", [
    (lambda f: f.update(steps=[]), "empty"),
    (lambda f: f["steps"][0].update(uri="kvm://host/invented/route"), "not in allowedRoutes"),
    (lambda f: f["steps"][0].update(depends_on=["ghost"]), "not an earlier step"),
    (lambda f: f["steps"][0].update(depends_on=["cap"]), "not an earlier step"),
    (lambda f: f["steps"].append(dict(f["steps"][0])), "duplicate step id"),
    (lambda f: f["steps"][0].update(payload="text"), "payload is not an object"),
    (lambda f: f["steps"][0].pop("id"), "no id"),
])
def test_parse_flow_v1_rejects_violations(mutation, reason):
    document = json.loads(json.dumps(VALID_FLOW))
    mutation(document)
    with pytest.raises(ValueError, match="flow.v1"):
        parse_flow_v1(document, ALLOWED)


def test_parse_flow_v1_rejects_non_object_and_step_bound():
    with pytest.raises(ValueError, match="flow.v1"):
        parse_flow_v1([VALID_FLOW], ALLOWED)
    document = {
        "task": {"id": "many"},
        "steps": [
            {"id": f"s{i}", "uri": "kvm://host/runtime/query/health", "payload": {}, "depends_on": []}
            for i in range(MAX_STEPS + 1)
        ],
    }
    with pytest.raises(ValueError, match="exceed bound"):
        parse_flow_v1(document, ALLOWED)


def test_parse_flow_v1_allows_chain_and_defaults_optionals():
    document = {
        "task": {"id": "chain"},
        "steps": [
            {"id": "one", "uri": "kvm://host/runtime/query/health"},
            {"id": "two", "uri": "kvm://host/screen/query/capture", "depends_on": ["one"], "payload": None},
        ],
    }
    flow = parse_flow_v1(document, ALLOWED)
    assert flow["steps"][0]["payload"] == {}
    assert flow["steps"][1]["depends_on"] == ["one"]


# ─── compile_flow: declarative prompt + injectable transport ────────────────

def test_compile_flow_builds_schema_bound_prompt_and_parses_answer():
    captured = {}

    def fake_complete(**kwargs):
        captured.update(kwargs)
        return _Response(json.dumps(VALID_FLOW))

    flow = compile_flow("zrob zrzut ekranu", ROUTES, NODES, complete=fake_complete,
                        llm_model="test/model")

    assert flow["steps"][0]["uri"] == "kvm://host/screen/query/capture"
    system = captured["messages"][0]["content"]
    user = json.loads(captured["messages"][1]["content"])
    assert "allowedRoutes" in system
    assert user["request"] == "zrob zrzut ekranu"
    assert user["allowedRoutes"][0]["uri"] in ALLOWED
    assert captured["model"] == "test/model"
    assert captured["temperature"] == 0


def test_compile_flow_rejects_invented_uri_from_model():
    def fake_complete(**_):
        bad = json.loads(json.dumps(VALID_FLOW))
        bad["steps"][0]["uri"] = "kvm://host/invented/route"
        return _Response(json.dumps(bad))

    with pytest.raises(ValueError, match="not in allowedRoutes"):
        compile_flow("zrob zrzut ekranu", ROUTES, NODES, complete=fake_complete)


def test_compile_flow_propagates_transport_failure():
    def broken_complete(**_):
        raise RuntimeError("quota exhausted")

    with pytest.raises(RuntimeError, match="quota exhausted"):
        compile_flow("zrob zrzut ekranu", ROUTES, NODES, complete=broken_complete)


def test_compile_flow_fails_closed_without_safe_routes():
    unsafe = [{"uri": "kvm://host/screen/command/wipe", "node": "host", "safe": False}]
    with pytest.raises(ValueError, match="no safe allowed routes"):
        compile_flow("zrob zrzut ekranu", unsafe, NODES, complete=lambda **_: _Response("{}"))


def test_compiler_system_prompt_declares_the_v1_shape():
    assert '"task"' in COMPILER_SYSTEM_PROMPT
    assert "allowedRoutes" in COMPILER_SYSTEM_PROMPT
    assert "EARLIER" in COMPILER_SYSTEM_PROMPT


# ─── make_flow fallback chain ───────────────────────────────────────────────

def test_make_flow_uses_flow_compiler_after_llm_planner_failure(monkeypatch):
    mesh = {"nodes": NODES, "routes": ROUTES}
    monkeypatch.setenv("URIRUN_LLM_MODEL", "test/model")

    def broken_llm_flow(*args, **kwargs):
        raise RuntimeError("primary planner outage")

    monkeypatch.setattr(planner, "llm_flow", broken_llm_flow)
    monkeypatch.setattr(planner, "quiet_completion", lambda **_: _Response(json.dumps(VALID_FLOW)))

    flow, generator = make_flow("zrob zrzut ekranu", mesh, use_llm=True)

    assert generator["provider"] == "flow-compiler"
    assert generator["fallback"] is True
    assert generator["model"] == "test/model"
    assert generator["reason"].find("primary planner outage") != -1
    assert flow["steps"][0]["uri"] == "kvm://host/screen/query/capture"


def test_make_flow_compiler_rejection_degrades_to_offline_heuristic(monkeypatch):
    mesh = {"nodes": NODES, "routes": ROUTES}
    monkeypatch.setenv("URIRUN_LLM_MODEL", "test/model")

    def broken_llm_flow(*args, **kwargs):
        raise RuntimeError("primary planner outage")

    def hallucinating_complete(**_):
        bad = json.loads(json.dumps(VALID_FLOW))
        bad["steps"][0]["uri"] = "kvm://host/invented/route"
        return _Response(json.dumps(bad))

    monkeypatch.setattr(planner, "llm_flow", broken_llm_flow)
    monkeypatch.setattr(planner, "quiet_completion", hallucinating_complete)

    _flow, generator = make_flow("zrob zrzut ekranu", mesh, use_llm=True)

    assert generator["provider"] == "heuristic"
    assert generator["fallback"] is True


def test_make_flow_native_app_request_still_fails_closed_when_compiler_omits_launch(monkeypatch):
    mesh = {
        "nodes": [{"name": "laptop", "reachable": True}],
        "routes": [
            {"uri": "app://laptop/desktop/command/launch", "node": "laptop", "safe": True},
            {"uri": "kvm://laptop/screen/query/capture", "node": "laptop", "safe": True},
        ],
    }
    monkeypatch.setenv("URIRUN_LLM_MODEL", "test/model")

    def broken_llm_flow(*args, **kwargs):
        raise RuntimeError("primary planner outage")

    def launch_less_complete(**_):
        return _Response(json.dumps({
            "task": {"id": "shot", "title": "query only"},
            "steps": [{"id": "cap", "uri": "kvm://laptop/screen/query/capture",
                       "payload": {}, "depends_on": []}],
        }))

    monkeypatch.setattr(planner, "llm_flow", broken_llm_flow)
    monkeypatch.setattr(planner, "quiet_completion", launch_less_complete)

    with pytest.raises(RuntimeError, match="cannot safely synthesize app launch"):
        make_flow("otworz ONLYOFFICE i wpisz tekst", mesh, use_llm=True)


def test_make_flow_native_app_request_accepts_compiled_launch_flow(monkeypatch):
    mesh = {
        "nodes": [{"name": "laptop", "reachable": True}],
        "routes": [
            {"uri": "app://laptop/desktop/command/launch", "node": "laptop", "safe": True},
            {"uri": "kvm://laptop/screen/query/capture", "node": "laptop", "safe": True},
        ],
    }
    monkeypatch.setenv("URIRUN_LLM_MODEL", "test/model")

    def broken_llm_flow(*args, **kwargs):
        raise RuntimeError("primary planner outage")

    def launching_complete(**_):
        return _Response(json.dumps({
            "task": {"id": "app-shot", "title": "open and capture"},
            "steps": [
                {"id": "launch", "uri": "app://laptop/desktop/command/launch",
                 "payload": {"app": "onlyoffice"}, "depends_on": []},
                {"id": "cap", "uri": "kvm://laptop/screen/query/capture",
                 "payload": {}, "depends_on": ["launch"]},
            ],
        }))

    monkeypatch.setattr(planner, "llm_flow", broken_llm_flow)
    monkeypatch.setattr(planner, "quiet_completion", launching_complete)

    flow, generator = make_flow("otworz ONLYOFFICE i wpisz tekst", mesh, use_llm=True)

    assert generator["provider"] == "flow-compiler"
    uris = [step["uri"] for step in flow["steps"]]
    assert "app://laptop/desktop/command/launch" in uris


def test_make_flow_without_model_skips_compiler_and_uses_heuristic(monkeypatch):
    mesh = {"nodes": NODES, "routes": ROUTES}
    monkeypatch.delenv("URIRUN_LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    def broken_llm_flow(*args, **kwargs):
        raise RuntimeError("unreachable without a model")

    monkeypatch.setattr(planner, "llm_flow", broken_llm_flow)

    _flow, generator = make_flow("zrob zrzut ekranu", mesh, use_llm=True)

    assert generator["provider"] == "heuristic"
    assert generator["fallback"] is True
