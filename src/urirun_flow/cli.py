"""urirun-flow CLI: validate flow YAML, or convert a typed Python flow to YAML."""
from __future__ import annotations
import argparse, importlib, sys
from . import Flow, FlowError


def _load_python_flow(target: str) -> Flow:
    mod_name, _, attr = target.partition(":")
    module = importlib.import_module(mod_name)
    obj = getattr(module, attr or "flow")
    return obj() if callable(obj) and not isinstance(obj, Flow) else obj


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="urirun-flow")
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate", help="validate a flow YAML (DAG, deps, URIs)")
    v.add_argument("path")
    t = sub.add_parser("to-yaml", help="import a Python flow object (module:attr) and emit YAML")
    t.add_argument("target")
    f = sub.add_parser("from-yaml", help="parse + re-emit a flow YAML (normalize / round-trip)")
    f.add_argument("path")
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
    except (FlowError, Exception) as exc:  # noqa: BLE001 - surface a clean message
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
