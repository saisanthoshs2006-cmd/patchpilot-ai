"""The interface every analysis engine implements.

An engine takes a Workspace and returns Findings. It must not raise for bad input
code: the orchestrator wraps `run` and turns any exception into an EngineError so
one broken engine never sinks the whole review.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from patchpilot.ingestion.loader import Workspace
from patchpilot.models import EngineError, Finding


@runtime_checkable
class Engine(Protocol):
    name: str  # short id used in Finding.sources, e.g. "ruff", "bandit", "ast", "ai"

    def run(self, workspace: Workspace) -> list[Finding]: ...


@dataclass
class EngineResult:
    engine: str
    findings: list[Finding] = field(default_factory=list)
    error: EngineError | None = None
    duration_ms: int = 0


def run_engine(engine: Engine, workspace: Workspace) -> EngineResult:
    start = time.perf_counter()
    try:
        findings = engine.run(workspace)
        error = None
    except Exception as exc:  # noqa: BLE001 - isolation boundary by design
        findings, error = [], EngineError(engine=engine.name, message=f"{type(exc).__name__}: {exc}")
    return EngineResult(engine.name, findings, error, int((time.perf_counter() - start) * 1000))
