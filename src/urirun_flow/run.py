"""Execute a typed flow through urirun — the same semantics as the YAML runner, so a
typed flow runs identically to its YAML form. Requires `urirun` to be installed.

    from urirun_flow import Flow
    from urirun_flow.run import run_flow
    results = run_flow(flow, base_dir=".", execute=True)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import Flow


def run_flow(flow: Flow, base_dir: str | Path = ".", *, execute: bool = False,
             allow: list[str] | None = None, secret_allow: list[str] | None = None) -> dict[str, Any]:
    """Run each step in dependency order, chaining `<key>_from` references through
    prior results, gated by the flow's `allow` policy. Returns {step_id: envelope}."""
    try:
        from urirun import v2, _runtime
        from urirun.node import mesh
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("`urirun-flow run` needs urirun installed: pip install urirun") from exc

    data = flow.to_dict()
    if not data.get("registry"):
        raise ValueError("flow has no `registry`; set Flow(registry=...) to run it")
    registry = v2.load_registry_arg(str(Path(base_dir) / data["registry"]))
    policy = _runtime.build_policy(
        None,
        list(allow or data.get("allow") or []),
        None,
        list(secret_allow or data.get("secretAllow") or []),
    )

    results: dict[str, Any] = {}
    for step in flow.order():
        missing = [d for d in step.depends_on if d not in results]
        if missing:
            raise ValueError(f"step {step.id!r} runs before its dependencies {missing}")
        payload = mesh.resolve_step_payload(step.payload or {}, results)
        env = v2.run(step.uri, registry, payload,
                     mode="execute" if execute else "dry-run", policy=policy)
        results[step.id] = env
    return results
