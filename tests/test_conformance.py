"""Every language flow emitter agrees with the Python reference."""
import shutil, subprocess, sys, pathlib
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_flow_conformance_all_emitters_agree():
    r = subprocess.run([sys.executable, str(ROOT / "conformance.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0 error(s)" in r.stdout
