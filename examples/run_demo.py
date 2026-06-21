"""A typed flow you can RUN: clock (query) -> echo the time (command), chaining the
clock's stdout into the echo via a `_from` reference. Builds its own registry.

    cd examples && python run_demo.py            # builds reg + runs (execute)
    urirun-flow run run_demo:flow --execute --allow 'demo://*'
"""
import json, pathlib
from urirun import v2
from urirun_flow import Flow

HERE = pathlib.Path(__file__).resolve().parent

# a registry whose two demo routes are allowed to execute
_doc = {"version": v2.VERSION, "bindings": {
    "demo://host/clock/query/now": {"uri": "demo://host/clock/query/now", "kind": "command",
        "adapter": "argv-template", "argv": ["date", "+%FT%TZ"],
        "policy": {"allowExecute": True}, "meta": {"label": "now"}},
    "demo://host/echo/command/say": {"uri": "demo://host/echo/command/say", "kind": "command",
        "adapter": "argv-template", "argv": ["printf", "the time is %s", "{text}"],
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
        "policy": {"allowExecute": True}, "meta": {"label": "say"}}}}
json.dump(v2.compile_registry(_doc), open(HERE / "demo.registry.json", "w"))

flow = Flow(task={"title": "clock then echo"}, registry="demo.registry.json", allow=["demo://*"])
clock = flow.step("demo://host/clock/query/now", id="clock")
flow.step("demo://host/echo/command/say", id="echo",
          payload={"text_from": clock.ref("result.stdout")}, after=[clock])  # chain stdout

if __name__ == "__main__":
    from urirun_flow.run import run_flow
    for sid, env in run_flow(flow, HERE, execute=True).items():
        print(f"[{sid}] ok={env.get('ok')} stdout={ (env.get('result') or {}).get('stdout')!r }")
