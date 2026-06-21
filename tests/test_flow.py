"""urirun-flow: typed authoring, validation and YAML round-trip."""
import pytest
from pydantic import ValidationError

from urirun_flow import Flow, Step, FlowError

# Pydantic wraps validator errors raised during construction in ValidationError;
# the builder path (flow.step) raises FlowError directly. Accept either.
INVALID = (FlowError, ValidationError)


def test_builder_chains_refs_and_derives_kind():
    flow = Flow(task={"title": "t"})
    a = flow.step("ocr://host/image/latest/query/text", id="read")
    flow.step("llm://host/vision/query/analyze", id="analyze",
              payload={"text": a.ref("text")}, after=[a])
    assert flow.steps[0].kind == "query"          # derived from the URI tail
    assert flow.steps[1].depends_on == ["read"]
    assert flow.steps[1].payload["text"] == "read.text"


def test_to_yaml_shape_matches_urirun_flow():
    flow = Flow(task={"title": "t"}, registry="r.json", allow=["ocr://*"])
    flow.step("ocr://host/image/latest/query/text", id="read", payload={"src": "x"})
    data = flow.to_dict()
    assert set(data) == {"task", "registry", "allow", "steps"}
    assert data["steps"][0] == {"id": "read", "uri": "ocr://host/image/latest/query/text",
                                "payload": {"src": "x"}}


def test_round_trip_is_stable():
    flow = Flow(task={"title": "t"})
    up = flow.step("httpcheck://host/url/query/status", id="up", payload={"url": "u"})
    flow.step("log://host/run/command/write", id="audit", after=[up], payload={"e": 1})
    text = flow.to_yaml()
    assert Flow.from_yaml(text).to_yaml() == text


def test_rejects_dangling_dependency():
    with pytest.raises(INVALID):
        Flow(steps=[Step(id="a", uri="x://h/r/query/op", depends_on=["nope"])])


def test_rejects_cycle():
    with pytest.raises(INVALID):
        Flow(steps=[
            Step(id="a", uri="x://h/r/query/op", depends_on=["b"]),
            Step(id="b", uri="x://h/r/query/op2", depends_on=["a"]),
        ])


def test_rejects_non_uri():
    with pytest.raises(INVALID):
        Step(id="a", uri="not-a-uri")


def test_order_respects_dependencies():
    flow = Flow()
    a = flow.step("x://h/r/query/a", id="a")
    b = flow.step("x://h/r/query/b", id="b", after=[a])
    flow.step("x://h/r/query/c", id="c", after=[b])
    assert [s.id for s in flow.order()] == ["a", "b", "c"]
