"""Running analyzer CLIs as subprocesses: stable JSON contracts, no reliance on internal APIs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


class ToolError(RuntimeError):
    pass


def run_json_tool(module: str, args: list[str], cwd: Path, timeout: int = 60) -> Any:
    """`python -m <module> <args>` in cwd, parse stdout as JSON. Uses the venv's own interpreter."""
    proc = subprocess.run(
        [sys.executable, "-m", module, *args],
        cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no output"]
        raise ToolError(f"{module} exited {proc.returncode}: {detail[0]}") from exc
