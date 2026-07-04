from __future__ import annotations

from urirun_flow.flow_thin import FlowEnvelope, _thin_driver


def _dispatch_ok(uri, payload=None):
    return {"ok": True, "next": {"kind": "continue"}}


def test_timeline_entries_carry_step_duration_ms():
    steps = [
        {"id": "a", "uri": "kvm://host/display/query/info", "payload": {}},
        {"id": "b", "uri": "kvm://host/screen/query/capture", "payload": {}, "optional": True},
    ]
    result = _thin_driver(steps, FlowEnvelope(flow_id="t"), _dispatch_ok, {}, execute=True)
    timeline = result["timeline"]
    stepped = [e for e in timeline if e.get("id") in {"a", "b"}]
    assert len(stepped) == 2
    for entry in stepped:
        # per-step wall-clock is the data for "where does execute time go";
        # both the normal and the optional-step paths must stamp it
        assert isinstance(entry.get("ms"), float)
        assert entry["ms"] >= 0
