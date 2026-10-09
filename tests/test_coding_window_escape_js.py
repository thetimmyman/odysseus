"""Exercise the real shared window/Escape modules with a synthetic DOM in Node."""

import shutil
import subprocess
from pathlib import Path


def test_coding_window_escape_behavior():
    repo = Path(__file__).resolve().parent.parent
    node = shutil.which("node")
    assert node, "Node is required for the shared window behavior regression suite"
    result = subprocess.run(
        [node, "--experimental-vm-modules", "--test",
         str(repo / "tests/navigation/window-escape.test.mjs")],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"Window/Escape behavior failed:\n{result.stdout}\n{result.stderr}"
    )
