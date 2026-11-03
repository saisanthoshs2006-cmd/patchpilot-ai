"""Run pytest on a workspace copy in a separate, constrained process.

What this protects against: crashes, infinite loops, runaway output/disk use, and
user code reading our secrets (the child gets a minimal environment with no API keys),
plus damage to the user's files (it only ever runs in a throwaway copy).

What it does NOT do: it is not a security sandbox. Uploaded code runs as the current
user and can reach the network. Docker isolation is a P2 item; say so honestly.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from patchpilot import config

OUTPUT_LIMIT = 4000
# Exceptions that mean "the test itself is broken", not "the bug was reproduced".
BAD_TEST_EXCEPTIONS = {"ImportError", "ModuleNotFoundError", "NameError", "SyntaxError", "IndentationError",
                       "fixture", "collection"}


@dataclass
class CaseResult:
    name: str
    outcome: str  # "passed" | "failed" | "error" | "skipped"
    exc_type: str | None = None
    message: str = ""

    @property
    def broken_test(self) -> bool:
        return self.outcome == "error" or (self.exc_type or "") in BAD_TEST_EXCEPTIONS


@dataclass
class PytestRun:
    cases: list[CaseResult] = field(default_factory=list)
    timed_out: bool = False
    collection_error: bool = False
    output: str = ""
    duration_ms: int = 0

    @property
    def passed(self) -> int:
        return sum(c.outcome == "passed" for c in self.cases)

    @property
    def failed(self) -> int:
        return sum(c.outcome in ("failed", "error") for c in self.cases)

    @property
    def all_passed(self) -> bool:
        return bool(self.cases) and self.failed == 0 and not self.timed_out and not self.collection_error


# Sets resource limits inside the child, then hands over to pytest. (preexec_fn would do the
# same but is unsafe when the parent is multithreaded, which the verifier is.)
_LAUNCHER = (
    "import resource, runpy, sys\n"
    "cpu = int(sys.argv[1])\n"
    "resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))\n"
    "resource.setrlimit(resource.RLIMIT_FSIZE, (50_000_000, 50_000_000))\n"
    "sys.argv = ['pytest', *sys.argv[2:]]\n"
    "runpy.run_module('pytest', run_name='__main__', alter_sys=True)\n"
)


def _child_env(root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root),
        "PYTHONPATH": str(root),
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
    }


def _exc_type(message: str) -> str:
    if message.startswith(("assert", "AssertionError")):
        return "AssertionError"
    if message.startswith("Failed: DID NOT RAISE"):
        return "DidNotRaise"
    m = re.match(r"([A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt))\b", message)
    if m:
        return m.group(1).rsplit(".", 1)[-1]
    if "fixture" in message:
        return "fixture"
    return "Exception"


def run_pytest(root: Path, targets: list[str], timeout: int = config.SANDBOX_TIMEOUT_S) -> PytestRun:
    report = Path(tempfile.mkstemp(prefix="pp-junit-", suffix=".xml")[1])
    cmd = [sys.executable, "-c", _LAUNCHER, str(timeout + 5), "-q", "-p", "no:cacheprovider", "--no-header",
           f"--junitxml={report}", "--rootdir", str(root), *targets]
    start = time.perf_counter()
    proc = subprocess.Popen(
        cmd, cwd=root, env=_child_env(root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True,
    )
    run = PytestRun()
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)  # kill the whole group, including anything it spawned
        out, _ = proc.communicate()
        run.timed_out = True
    run.output = (out or "")[-OUTPUT_LIMIT:]
    run.duration_ms = int((time.perf_counter() - start) * 1000)

    try:
        tree = ET.parse(report).getroot()
    except (ET.ParseError, FileNotFoundError):
        run.collection_error = not run.timed_out
        return run
    finally:
        report.unlink(missing_ok=True)

    for tc in tree.iter("testcase"):
        name = tc.get("name", "?")
        child = next(iter(tc), None)
        if child is None or child.tag in ("system-out", "system-err", "properties"):
            run.cases.append(CaseResult(name, "passed"))
        elif child.tag == "skipped":
            run.cases.append(CaseResult(name, "skipped"))
        else:
            message = child.get("message") or ""
            if child.tag == "error" and message == "collection failure":
                run.collection_error = True
            run.cases.append(CaseResult(
                name, "failed" if child.tag == "failure" else "error",
                exc_type=_exc_type(message) if child.tag == "failure" else "collection" if "collection" in message else "fixture",
                message=message[:500],
            ))
    return run
