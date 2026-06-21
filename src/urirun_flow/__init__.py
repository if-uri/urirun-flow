"""urirun-flow — author urirun URI flows in typed Python, convert to/from YAML.

A urirun *flow* is an ordered DAG of URI steps (`query` reads, `command` mutates),
chaining prior results. This package gives that flow a typed, validated model
(Pydantic) — like Pydantic does for data — so you build flows in a typed language
with autocompletion and validation, then emit the canonical urirun flow YAML that
`run_flow.py` / the node runner executes.

    from urirun_flow import Flow

    flow = Flow(task={"title": "Web recon"})
    up    = flow.step("httpcheck://host/url/query/status", payload={"url": URL})
    read  = flow.step("browser://chrome/page/query/dom", payload={"url": URL}, after=[up])
    flow.step("log://host/run/command/write",
              payload={"event": "recon", "detail": read.ref("text")}, after=[read])

    print(flow.to_yaml())          # canonical urirun flow YAML
    Flow.from_yaml(text)           # parse + validate the YAML back into the model
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

__all__ = ["Flow", "Step", "FlowError"]

URI_RE = re.compile(r"^[a-z][a-z0-9+.-]*://")


class FlowError(ValueError):
    """Raised when a flow is structurally invalid (bad URI, cycle, dangling dep)."""


class Step(BaseModel):
    id: str
    uri: str
    operation: str | None = None
    kind: str | None = None  # query | command — derived from the URI tail if omitted
    payload: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("uri")
    @classmethod
    def _check_uri(cls, value: str) -> str:
        if not URI_RE.match(value):
            raise FlowError(f"not a URI: {value!r}")
        return value

    @model_validator(mode="after")
    def _derive_kind(self) -> "Step":
        if self.kind is None:
            segments = self.uri.split("://", 1)[1].split("/")
            for candidate in ("query", "command", "assertion"):
                if candidate in segments:
                    object.__setattr__(self, "kind", candidate)
                    break
        return self

    def ref(self, field: str = "") -> str:
        """A `<step-id>.<field>` reference, for chaining into a later step's payload."""
        return f"{self.id}.{field}" if field else self.id


class Flow(BaseModel):
    task: dict[str, Any] = Field(default_factory=dict)
    registry: str | None = None
    allow: list[str] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)

    # --- typed builder -------------------------------------------------------
    def step(self, uri: str, *, id: str | None = None, payload: dict | None = None,
             after: list[Any] | None = None, operation: str | None = None,
             kind: str | None = None) -> Step:
        """Append a step and return it (so later steps can `.ref()` its output)."""
        sid = id or f"s{len(self.steps) + 1}"
        deps = [a.id if isinstance(a, Step) else str(a) for a in (after or [])]
        st = Step(id=sid, uri=uri, payload=payload or {}, depends_on=deps,
                  operation=operation, kind=kind)
        self.steps.append(st)
        self._validate_graph()
        return st

    # --- validation ----------------------------------------------------------
    @model_validator(mode="after")
    def _validate(self) -> "Flow":
        self._validate_graph()
        return self

    def _validate_graph(self) -> None:
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise FlowError("duplicate step ids")
        known = set(ids)
        for s in self.steps:
            for dep in s.depends_on:
                if dep not in known:
                    raise FlowError(f"step {s.id!r} depends on unknown step {dep!r}")
        # cycle detection (DFS)
        graph = {s.id: list(s.depends_on) for s in self.steps}
        state: dict[str, int] = {}

        def visit(node: str) -> None:
            if state.get(node) == 1:
                raise FlowError(f"dependency cycle through {node!r}")
            if state.get(node) == 2:
                return
            state[node] = 1
            for nxt in graph[node]:
                visit(nxt)
            state[node] = 2

        for node in graph:
            visit(node)

    def order(self) -> list[Step]:
        """Steps in a dependency-respecting (topological) order."""
        by_id = {s.id: s for s in self.steps}
        out: list[Step] = []
        seen: set[str] = set()

        def emit(sid: str) -> None:
            if sid in seen:
                return
            for dep in by_id[sid].depends_on:
                emit(dep)
            seen.add(sid)
            out.append(by_id[sid])

        for s in self.steps:
            emit(s.id)
        return out

    # --- serialization (canonical urirun flow shape) -------------------------
    def to_dict(self) -> dict:
        out: dict[str, Any] = {}
        if self.task:
            out["task"] = self.task
        if self.registry:
            out["registry"] = self.registry
        if self.allow:
            out["allow"] = self.allow
        steps: list[dict] = []
        for s in self.steps:
            entry: dict[str, Any] = {"id": s.id, "uri": s.uri}
            if s.operation:
                entry["operation"] = s.operation
            if s.payload:
                entry["payload"] = s.payload
            if s.depends_on:
                entry["depends_on"] = s.depends_on
            steps.append(entry)
        out["steps"] = steps
        return out

    def to_yaml(self) -> str:
        import yaml

        return yaml.safe_dump(self.to_dict(), sort_keys=False, default_flow_style=False, allow_unicode=True)

    @classmethod
    def from_dict(cls, data: dict) -> "Flow":
        return cls(**data)

    @classmethod
    def from_yaml(cls, text: str) -> "Flow":
        import yaml

        return cls(**(yaml.safe_load(text) or {}))
