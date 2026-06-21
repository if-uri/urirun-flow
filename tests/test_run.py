"""urirun-flow run: a typed flow executes through urirun with `_from` chaining."""
import json, pathlib
import pytest

pytest.importorskip("urirun")
from urirun import v2
from urirun_flow import Flow
from urirun_flow.run import run_flow


def _registry(tmp_path):
    doc = {"version": v2.VERSION, "bindings": {
        "demo://host/clock/query/now": {"uri": "demo://host/clock/query/now", "kind": "command",
            "adapter": "argv-template", "argv": ["date", "+%FT%TZ"],
            "policy": {"allowExecute": True}},
        "demo://host/echo/command/say": {"uri": "demo://host/echo/command/say", "kind": "command",
            "adapter": "argv-template", "argv": ["printf", "%s", "{text}"],
            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
            "policy": {"allowExecute": True}}}}
    (tmp_path / "reg.json").write_text(json.dumps(v2.compile_registry(doc)))


def test_typed_flow_runs_and_chains(tmp_path):
    _registry(tmp_path)
    flow = Flow(registry="reg.json", allow=["demo://*"])
    clock = flow.step("demo://host/clock/query/now", id="clock")
    flow.step("demo://host/echo/command/say", id="echo",
              payload={"text_from": clock.ref("result.stdout")}, after=[clock])
    results = run_flow(flow, tmp_path, execute=True)
    assert results["clock"]["ok"] and results["echo"]["ok"]
    # the echo step received the clock's stdout (the chain resolved)
    assert results["clock"]["result"]["stdout"].strip() in results["echo"]["result"]["stdout"]
