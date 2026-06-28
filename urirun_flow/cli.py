"""urirun-flow CLI: validate, convert (Python<->YAML) and run urirun flows."""
from __future__ import annotations
import argparse, importlib, json, sys
from pathlib import Path
from . import Flow, FlowError


def _load_python_flow(target: str) -> Flow:
    mod_name, _, attr = target.partition(":")
    module = importlib.import_module(mod_name)
    obj = getattr(module, attr or "flow")
    return obj() if callable(obj) and not isinstance(obj, Flow) else obj


def _load_flow(target: str) -> tuple[Flow, Path]:
    """A YAML path or a `module:attr` Python flow; returns (flow, base_dir)."""
    if ":" in target and not Path(target).exists():
        return _load_python_flow(target), Path.cwd()
    path = Path(target)
    return Flow.from_yaml(path.read_text(encoding="utf-8")), path.resolve().parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="urirun-flow")
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate", help="validate a flow YAML (DAG, deps, URIs)")
    v.add_argument("path")
    t = sub.add_parser("to-yaml", help="import a Python flow object (module:attr) and emit YAML")
    t.add_argument("target")
    f = sub.add_parser("from-yaml", help="parse + re-emit a flow YAML (normalize / round-trip)")
    f.add_argument("path")
    r = sub.add_parser("run", help="execute a flow (YAML path or module:attr) through urirun")
    r.add_argument("target")
    r.add_argument("--execute", action="store_true", help="actually run (default: dry-run)")
    r.add_argument("--allow", action="append", default=[], metavar="GLOB")
    r.add_argument("--base-dir", default=None, help="resolve registry relative to this dir")
    args = parser.parse_args(argv)

    try:
        if args.command == "validate":
            Flow.from_yaml(open(args.path, encoding="utf-8").read())
            print(f"ok: {args.path} is a valid urirun flow")
            return 0
        if args.command == "to-yaml":
            sys.stdout.write(_load_python_flow(args.target).to_yaml())
            return 0
        if args.command == "from-yaml":
            sys.stdout.write(Flow.from_yaml(open(args.path, encoding="utf-8").read()).to_yaml())
            return 0
        if args.command == "run":
            from .run import run_flow
            flow, base = _load_flow(args.target)
            results = run_flow(flow, Path(args.base_dir) if args.base_dir else base,
                               execute=args.execute, allow=args.allow)
            for sid, env in results.items():
                print(f"[{sid}] ok={env.get('ok')} {json.dumps(env.get('result') or env.get('error') or {})[:120]}")
            return 0 if all(e.get("ok") for e in results.values()) else 1
    except (FlowError, Exception) as exc:  # noqa: BLE001 - surface a clean message
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
