"""Declarative NL -> urirun.flow.v1 prompt compiler.

Replaces the two-phase heuristic fallback in flow_planner (boolean LLM intent
classification feeding the procedural ``_append_target_steps`` graph builder)
with a single schema-declared prompt compile: the model receives the allowed
route table plus the urirun.flow.v1 output contract and must answer with one
flow document. ``parse_flow_v1`` is fail-closed — any structural violation,
unknown URI, dangling dependency or bound overflow rejects the document so the
caller degrades to the offline safe fallback instead of executing a guess.

The lexical classifier (``_flow_intents_lexical``) and ``heuristic_flow``
remain in flow_planner ONLY as that offline safe fallback; they no longer
participate in the LLM planning path.
"""
from __future__ import annotations

import json
from collections.abc import Callable

from urirun_flow._flow_normalize import json_from_text
from urirun_connector_router.routing import route_target, safe_route, target_nodes

FLOW_SCHEMA_VERSION = "urirun.flow.v1"

# Fail-closed bounds for compiled documents (reject beyond these, never truncate).
MAX_STEPS = 32
MAX_STEP_ID_CHARS = 64
MAX_TASK_TITLE_CHARS = 200
MAX_PAYLOAD_ITEMS = 32
MAX_ROUTES_IN_PROMPT = 160

# Declarative output contract embedded in the prompt. This is the same shape
# flow_document()/normalize_flow() enforce downstream, declared up front so the
# model compiles against the schema instead of free-form prose.
FLOW_V1_SHAPE: dict = {
    "task": {"id": "short slug", "title": "human title"},
    "steps": [
        {
            "id": "unique step id",
            "uri": "one of allowedRoutes[].uri",
            "payload": "object matching the route input, {} when empty",
            "depends_on": ["ids of EARLIER steps this step needs, [] when none"],
        }
    ],
}

COMPILER_SYSTEM_PROMPT = (
    "You are a declarative flow compiler. Read the request and emit ONE strict "
    "JSON document, no prose, no markdown fences, matching this shape: "
    + json.dumps(FLOW_V1_SHAPE, ensure_ascii=False)
    + " ."
    " Hard rules:"
    " (1) Every step uri MUST be copied verbatim from allowedRoutes; never invent,"
    " merge or template URIs."
    " (2) depends_on MUST reference ids of EARLIER steps only; no forward or self"
    " references; keep chains linear unless the request needs real parallelism."
    " (3) Steps are atomic and ordered; prefer the fewest steps that satisfy the"
    " request; read-oriented queries before the mutations they justify."
    " (4) When the request cannot be satisfied with allowedRoutes, return"
    ' {"task":{"id":"uncovered","title":"request not coverable"},"steps":[]}'
    " — an honest empty plan, never a guess or a nearest-neighbour substitute."
    " (5) payloads are plain JSON objects with only parameters the route"
    " describes; {} when it needs none."
    " (6) Answer in the JSON document language of the request (ids/titles may"
    " reuse the request language); the document itself is always JSON."
)

Transport = Callable[..., object]


def _route_table(routes: list[dict], prompt: str, nodes: list[dict],
                 selected_nodes: list[str] | None) -> tuple[list[dict], set[str]]:
    """Compile the declarative route table: safe routes, selected-node filtered."""
    selected = target_nodes(prompt, nodes, selected_nodes)

    def selected_route(route: dict) -> bool:
        if not selected:
            return True
        if route.get("node"):
            return route.get("node") in selected
        try:
            return route_target(str(route.get("uri") or "")) in selected
        except Exception:  # noqa: BLE001
            return False

    table: list[dict] = []
    allowed: set[str] = set()
    for route in routes:
        uri = route.get("uri")
        if not safe_route(route) or not isinstance(uri, str) or not selected_route(route):
            continue
        allowed.add(uri)
        if len(table) < MAX_ROUTES_IN_PROMPT:
            table.append({
                "uri": uri,
                "node": route.get("node"),
                "kind": route.get("kind"),
                "title": route.get("title"),
            })
    return table, allowed


def parse_flow_v1(payload: object, allowed_uris: set[str], *,
                  max_steps: int = MAX_STEPS) -> dict:
    """Validate one compiled urirun.flow.v1 candidate. Fail-closed.

    Returns {"task": ..., "steps": [...]} on success; raises ValueError with a
    precise reason otherwise — the caller treats any raise as "compiler
    unavailable" and degrades to the safe fallback.
    """
    if not isinstance(payload, dict):
        raise ValueError("flow.v1: document is not a JSON object")

    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("flow.v1: empty or missing steps")
    if len(raw_steps) > max_steps:
        raise ValueError(f"flow.v1: {len(raw_steps)} steps exceed bound {max_steps}")

    task = payload.get("task")
    if task is not None and not isinstance(task, dict):
        raise ValueError("flow.v1: task is not an object")
    if task is not None:
        for key in ("id", "title"):
            value = task.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"flow.v1: task.{key} is not a string")
        title = task.get("title")
        if isinstance(title, str) and len(title) > MAX_TASK_TITLE_CHARS:
            raise ValueError("flow.v1: task.title too long")

    seen: set[str] = set()
    steps: list[dict] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise ValueError(f"flow.v1: step {index} is not an object")
        step_id = raw.get("id")
        if not isinstance(step_id, str) or not step_id.strip():
            raise ValueError(f"flow.v1: step {index} has no id")
        if len(step_id) > MAX_STEP_ID_CHARS:
            raise ValueError(f"flow.v1: step {index} id too long")
        if step_id in seen:
            raise ValueError(f"flow.v1: duplicate step id {step_id!r}")
        uri = raw.get("uri")
        if not isinstance(uri, str) or uri not in allowed_uris:
            raise ValueError(f"flow.v1: step {step_id!r} uri not in allowedRoutes")
        payload_obj = raw.get("payload", {})
        if payload_obj is None:
            payload_obj = {}
        if not isinstance(payload_obj, dict):
            raise ValueError(f"flow.v1: step {step_id!r} payload is not an object")
        if len(payload_obj) > MAX_PAYLOAD_ITEMS:
            raise ValueError(f"flow.v1: step {step_id!r} payload too large")
        deps = raw.get("depends_on", [])
        if deps is None:
            deps = []
        if not isinstance(deps, list):
            raise ValueError(f"flow.v1: step {step_id!r} depends_on is not a list")
        for dep in deps:
            if not isinstance(dep, str) or dep not in seen:
                raise ValueError(
                    f"flow.v1: step {step_id!r} depends_on {dep!r} is not an earlier step"
                )
        seen.add(step_id)
        steps.append({
            "id": step_id,
            "uri": uri,
            "payload": payload_obj,
            "depends_on": list(deps),
        })

    return {
        "task": dict(task) if isinstance(task, dict) else {},
        "steps": steps,
    }


def compile_flow(prompt: str, routes: list[dict], nodes: list[dict],
                 selected_nodes: list[str] | None = None, *,
                 complete: Transport, llm_model: str | None = None) -> dict:
    """Compile NL into one validated urirun.flow.v1 flow document via prompt.

    ``complete`` is the transport (production: litellm ``quiet_completion``;
    tests: an injectable fake). Raises on transport failure, non-JSON answers
    or any ``parse_flow_v1`` violation — callers degrade to the safe fallback.
    """
    table, allowed = _route_table(routes, prompt, nodes, selected_nodes)
    if not table:
        raise ValueError("flow.v1: no safe allowed routes to compile against")

    messages = [
        {"role": "system", "content": COMPILER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "request": prompt,
                    "nodes": [{"name": node.get("name"), "reachable": node.get("reachable")}
                              for node in nodes],
                    "allowedRoutes": table,
                },
                ensure_ascii=False,
            ),
        },
    ]
    response = complete(model=llm_model, messages=messages, temperature=0,
                        response_format={"type": "json_object"})
    content = response.choices[0].message.content or ""
    flow = json_from_text(content)
    return parse_flow_v1(flow, allowed)
