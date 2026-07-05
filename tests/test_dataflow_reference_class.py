"""Metamorphic coverage for the ``*_from`` data-flow reference CLASS.

The live LLM track produced a real structural defect: ``monitor_from`` referenced ``list_chrome_windows``
but ``depends_on`` did not declare it. That is one member of a class, not a Chrome incident — an LLM
omits or garbles the producer edge in several ways. ``normalize_flow`` must handle the whole class
generically (over every ``*_from``, not just ``monitor_from``):

  mode 1 — ref to an EARLIER step, missing depends_on   -> ADD the dependency edge (recoverable)
  mode 2 — ref to a NONEXISTENT step                    -> REJECT (unsatisfiable; would silently default)
  mode 3 — ref to a LATER step (producer after consumer)-> REJECT (impossible data flow)
"""
from __future__ import annotations
import pytest as _pytest_guard  # noqa: E402
_pytest_guard.importorskip("jsonschema")  # integration tests need the full urirun runtime (not in the isolated package-test env)

import pytest

from urirun_flow.flow_planner import normalize_flow

ALLOWED = {"kvm://host/window/query/list", "kvm://host/screen/query/capture"}
ROUTES = [
    {"uri": "kvm://host/window/query/list",
     "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}},
                     "additionalProperties": False}},
    {"uri": "kvm://host/screen/query/capture",
     "inputSchema": {"type": "object",
                     "properties": {"monitor": {"type": "integer"}, "output": {"type": "string"}},
                     "additionalProperties": False}},
]


def _flow(cap_payload, *, capture_first=False):
    lst = {"id": "lst", "uri": "kvm://host/window/query/list", "payload": {"app": "chrome"}, "depends_on": []}
    cap = {"id": "cap", "uri": "kvm://host/screen/query/capture", "payload": cap_payload, "depends_on": []}
    steps = [cap, lst] if capture_first else [lst, cap]
    return {"task": {"id": "x"}, "steps": steps}


def test_mode1_missing_dependency_edge_is_added():
    flow = _flow({"monitor_from": "lst.result.value.selected.monitor"})
    cap = [s for s in normalize_flow(flow, ALLOWED, routes=ROUTES)["steps"] if "capture" in s["uri"]][0]
    assert cap["depends_on"] == ["lst"]


def test_mode2_reference_to_nonexistent_step_is_rejected():
    flow = _flow({"monitor_from": "GHOST.result.value.selected.monitor"})
    with pytest.raises(ValueError, match="no step 'GHOST' exists"):
        normalize_flow(flow, ALLOWED, routes=ROUTES)


def test_mode3_reference_to_later_step_is_rejected():
    flow = _flow({"monitor_from": "lst.result.value.selected.monitor"}, capture_first=True)
    with pytest.raises(ValueError, match="not earlier"):
        normalize_flow(flow, ALLOWED, routes=ROUTES)


def test_class_is_generic_not_monitor_specific():
    # A different *_from param exercises the SAME gate — the rule is over the reference class,
    # not hard-coded to monitor_from / Chrome.
    flow = _flow({"output_from": "GHOST.result.value.path"})
    with pytest.raises(ValueError, match="no step 'GHOST' exists"):
        normalize_flow(flow, ALLOWED, routes=ROUTES)
