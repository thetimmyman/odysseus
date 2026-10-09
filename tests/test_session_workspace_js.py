"""Run the browser-module workspace and Terminal behavior regressions."""

import shutil
import subprocess
from pathlib import Path


def test_session_workspace_behavior():
    repo = Path(__file__).resolve().parent.parent
    node = shutil.which("node")
    assert node, "Node is required for the session workspace behavior tests"
    result = subprocess.run(
        [
            node,
            "--experimental-vm-modules",
            "--test",
            str(repo / "tests/js/session_workspace.test.cjs"),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"Session workspace behavior tests failed:\n{result.stdout}\n{result.stderr}"
    )
