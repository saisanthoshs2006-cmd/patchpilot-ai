"""The frozen data contract shared by every engine, the aggregator, the UI and storage.

Change this file only with the whole team's agreement: every module codes against it.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        """Higher is worse. Use for sorting and --fail-on thresholds."""
        return {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}[self.value]


class Category(str, Enum):
    BUG = "bug"  # logical error: wrong result
    RUNTIME_ERROR = "runtime_error"  # will raise / crash on some input
    SECURITY = "security"
    PERFORMANCE = "performance"
    BEST_PRACTICE = "best_practice"
    CODE_SMELL = "code_smell"
    MAINTAINABILITY = "maintainability"
    STYLE = "style"


class EvidenceLevel(str, Enum):
    """How much we can back a finding. Never collapse these in the UI."""

    PROVEN = "proven"  # repro test failed on original, passed on patch
    TOOL_CONFIRMED = "tool_confirmed"  # deterministic analyzer (ruff/bandit/ast rule)
    CORROBORATED = "corroborated"  # AI + a deterministic signal at the same location
    AI_HYPOTHESIS = "ai_hypothesis"  # AI only, unproven

    @property
    def rank(self) -> int:
        return {"proven": 3, "tool_confirmed": 2, "corroborated": 1, "ai_hypothesis": 0}[self.value]


class VerificationStatus(str, Enum):
    UNVERIFIED = "unverified"  # no patch attempted / not checked yet
    STATIC_PASS = "static_pass"  # patch parses, finding gone on re-scan, nothing new introduced
    PROVEN = "proven"  # repro test red on original -> green on patch (+ regression pass)
    INCONCLUSIVE = "inconclusive"  # checks could not decide (bad repro test, timeout...)
    FAILED = "failed"  # patch rejected: syntax error, new findings, or tests broke


class StepStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    ERROR = "error"
    SKIPPED = "skipped"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ReproHint(BaseModel):
    """A concrete input claimed to trigger the defect. Phase 3 turns it into a real test."""

    call: str  # e.g. "paginate(list(range(25)), 1)"
    expected: str
    actual: str


class Finding(BaseModel):
    """One issue at one location. Every engine emits exactly this shape."""

    file: str  # path relative to the workspace root, posix separators
    line: int = Field(ge=1)
    end_line: int | None = None
    column: int | None = None
    severity: Severity
    category: Category
    rule_id: str  # e.g. "ruff:B006", "bandit:B608", "ast:open-without-with", "ai:logic"
    title: str  # one line, shown on the card header
    explanation: str  # what is wrong
    why_it_matters: str  # concrete consequence
    suggested_fix: str  # prose; the actual diff lives in Patch
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_level: EvidenceLevel
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    sources: list[str] = Field(default_factory=list)  # engines that reported it: ["ruff", "ai"]
    code_snippet: str = ""  # the offending source line(s), filled by the aggregator
    function_name: str | None = None  # enclosing function, if any
    repro: ReproHint | None = None  # AI findings only: claimed failing input, to be proven by a test

    @field_validator("file")
    @classmethod
    def _posix_path(cls, v: str) -> str:
        return v.replace("\\", "/")

    @property
    def id(self) -> str:
        """Stable id: same issue at same place from the same rule hashes the same across runs."""
        raw = f"{self.file}:{self.line}:{self.rule_id}"
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}"


class Patch(BaseModel):
    finding_id: str
    file: str
    original_code: str  # full file before
    patched_code: str  # full file after
    diff: str  # unified diff, for display and export
    rationale: str = ""
    generated_by: str = ""  # model id, or "rule" for deterministic fixes


class VerificationStep(BaseModel):
    step: str  # "syntax" | "static_rescan" | "repro" | "regression"
    status: StepStatus
    detail: str = ""  # one-line human summary
    test_code: str | None = None  # generated repro test, if any
    output: str = ""  # captured stdout/stderr (truncated)
    duration_ms: int = 0


class Verification(BaseModel):
    finding_id: str
    status: VerificationStatus
    steps: list[VerificationStep] = Field(default_factory=list)


class EngineError(BaseModel):
    """An engine failed; the review continues without it and the UI says so."""

    engine: str
    message: str


class RejectedClaim(BaseModel):
    """A finding that failed validation and was NOT shown. Kept for transparency and metrics."""

    file: str
    line: int
    title: str
    source: str  # engine that made the claim
    reason: str


class Scores(BaseModel):
    """0-100, higher is better. Computed deterministically, never by the LLM."""

    overall: float = 100.0
    security: float = 100.0
    reliability: float = 100.0
    performance: float = 100.0
    maintainability: float = 100.0
    style: float = 100.0


class Review(BaseModel):
    id: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    source_name: str
    files: list[str] = Field(default_factory=list)
    loc: int = 0
    mode: str = "live"  # live | replay | offline
    findings: list[Finding] = Field(default_factory=list)
    patches: list[Patch] = Field(default_factory=list)
    verifications: list[Verification] = Field(default_factory=list)
    scores: Scores = Field(default_factory=Scores)
    engine_errors: list[EngineError] = Field(default_factory=list)
    rejected_claims: list[RejectedClaim] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)  # e.g. avg_complexity, ai_findings_dropped
    duration_ms: int = 0
