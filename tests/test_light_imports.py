"""Pure flow helper imports must stay independent of the hub runtime."""
from __future__ import annotations

import os
import subprocess
import sys


def test_pure_helpers_do_not_import_urirun_runtime():
    code = """
import sys
import urirun_flow._util, urirun_flow.envelope, urirun_flow.flow_thin, urirun_flow.flow_verify
assert "urirun" not in sys.modules, sorted(m for m in sys.modules if m == "urirun" or m.startswith("urirun."))[:20]
assert not any(m == "urirun_node" or m.startswith("urirun_node.") for m in sys.modules), sorted(sys.modules)[:20]
assert not any(m.startswith("urirun.node.routing") for m in sys.modules), sorted(sys.modules)[:20]
"""
    env = dict(os.environ)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
