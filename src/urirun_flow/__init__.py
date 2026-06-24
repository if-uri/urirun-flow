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
    kind: str | None = None  # query | command | assertion — derived from the URI tail if omitted
    payload: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    # --- resilience policy (all optional; absent = today's behaviour) --------------------------
    # retry: {max:int, backoff_ms:int, on:[error categories]} — only retry RETRYABLE categories.
    retry: dict[str, Any] | None = None
    # fallback: an alternative URI to run (same payload) when the step still fails after retries.
    fallback: str | None = None
    # catch: what to do when the step ultimately fails — "continue" (default; dependents skip)
    # or "abort" (stop the rest of the flow).
    catch: str | None = None
    # timeout_ms: per-step deadline (overrides the policy default).
    timeout_ms: int | None = None
    # degrade: id of an earlier step whose last-good result is served (re-tagged live=False,
    # degraded=True) when THIS step fails after retries+fallback — so a failing live widget
    # falls back to the last frozen artifact instead of breaking the chain. Distinct from
    # `fallback` (which makes another live call); degrade serves known-good data, no new call.
    degrade: str | None = None

    @field_validator("uri", "fallback")
    @classmethod
    def _check_uri(cls, value: str | None) -> str | None:
        if value is not None and not URI_RE.match(value):
            raise FlowError(f"not a URI: {value!r}")
        return value

    @field_validator("catch")
    @classmethod
    def _check_catch(cls, value: str | None) -> str | None:
        if value is not None and value not in ("continue", "abort"):
            raise FlowError(f"catch must be 'continue' or 'abort', got {value!r}")
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
             kind: str | None = None, retry: dict | None = None, fallback: str | None = None,
             catch: str | None = None, timeout_ms: int | None = None,
             degrade: "str | Step | None" = None) -> Step:
        """Append a step and return it (so later steps can `.ref()` its output)."""
        sid = id or f"s{len(self.steps) + 1}"
        deps = [a.id if isinstance(a, Step) else str(a) for a in (after or [])]
        st = Step(id=sid, uri=uri, payload=payload or {}, depends_on=deps,
                  operation=operation, kind=kind, retry=retry, fallback=fallback,
                  catch=catch, timeout_ms=timeout_ms,
                  degrade=degrade.id if isinstance(degrade, Step) else degrade)
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
            if s.degrade is not None and s.degrade not in known:
                raise FlowError(f"step {s.id!r} degrades to unknown step {s.degrade!r}")
        # cycle detection (DFS) — degrade is also an ordering edge (its source must run first)
        graph = {s.id: list(s.depends_on) + ([s.degrade] if s.degrade else []) for s in self.steps}
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
            if by_id[sid].degrade:  # the degrade source must be ordered before this step
                emit(by_id[sid].degrade)
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
            if s.retry:
                entry["retry"] = s.retry
            if s.fallback:
                entry["fallback"] = s.fallback
            if s.catch:
                entry["catch"] = s.catch
            if s.timeout_ms:
                entry["timeout_ms"] = s.timeout_ms
            if s.degrade:
                entry["degrade"] = s.degrade
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
