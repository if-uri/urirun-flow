#!/usr/bin/env python3
"""Flow SDK conformance: build the same reference flow in every language and assert
each emits the identical flow contract (the dict the YAML serializes). Mirrors the
urirun connector `make conformance`."""
import json, subprocess, sys, pathlib
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))
from urirun_flow import Flow  # noqa: E402

URL = "https://example.com"

def python_flow() -> dict:
    f = Flow(task={"title": "Web recon"}, registry="tools.bindings.json",
             allow=["httpcheck://*", "browser://*", "log://*"])
    up = f.step("httpcheck://host/url/query/status", id="up", payload={"url": URL})
    read = f.step("browser://chrome/page/query/dom", id="read", payload={"url": URL}, after=[up])
    f.step("log://host/run/command/write", id="audit",
           payload={"detail": read.ref("text")}, after=[read])
    return f.to_dict()

EMITTERS = {
    "python": python_flow,
    "js": lambda: json.loads(subprocess.run(["node", str(HERE / "conformance/build_flow.js")],
                                            capture_output=True, text=True, check=True).stdout),
}

def main() -> int:
    ref = json.dumps(EMITTERS["python"](), sort_keys=True)
    errors = 0
    for name, emit in EMITTERS.items():
        try:
            got = json.dumps(emit(), sort_keys=True)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {name}: {exc}"); errors += 1; continue
        if got == ref:
            print(f"ok   {name}: emits the reference flow contract")
        else:
            print(f"FAIL {name}: differs from python\n  python: {ref}\n  {name}: {got}"); errors += 1
    print(f"\nflow conformance: {len(EMITTERS) - errors}/{len(EMITTERS)} emitters agree, {errors} error(s)")
    return 1 if errors else 0

if __name__ == "__main__":
    sys.exit(main())
