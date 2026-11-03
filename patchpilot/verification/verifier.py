"""Decide whether a patch is proven, statically verified, inconclusive, or rejected.

For one finding + one patch, on throwaway copies of the workspace:
  1. syntax         the patched file parses (checked when the patch is applied)
  2. static_rescan  re-run Ruff/Bandit/AST: the flagged rule is gone from that function,
                    and the patch introduced no new medium-or-worse finding
  3. repro          a test for the defect FAILS on the original code (for the right reason,
                    not an ImportError) and PASSES on the patched code: red -> green
  4. regression     the project's own tests do no worse than before the patch

PROVEN needs 3 to go red->green and nothing to fail. STATIC_PASS needs 2 to confirm the
tool finding is gone. Anything that fails is FAILED; the rest is INCONCLUSIVE.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from patchpilot.aggregation.aggregator import aggregate
from patchpilot.engines.ast_engine import AstEngine
from patchpilot.engines.base import run_engine
from patchpilot.engines.security_bandit import BanditEngine
from patchpilot.engines.static_ruff import RuffEngine
from patchpilot.ingestion.loader import Workspace
from patchpilot.models import (
    Finding,
    Severity,
    StepStatus,
    Verification,
    VerificationStatus,
    VerificationStep,
)
from patchpilot.patching.generator import PatchProposal, module_name
from patchpilot.verification.repro import build_repro_test
from patchpilot.verification.sandbox import PytestRun, run_pytest

REPRO_FILE = "test_patchpilot_repro.py"
Tag = tuple[str, str | None]  # (engine:rule, enclosing function)


def deterministic_scan(ws: Workspace) -> list[Finding]:
    raw = []
    for engine in (RuffEngine(), BanditEngine(), AstEngine()):
        res = run_engine(engine, ws)
        if res.error:
            raise RuntimeError(f"rescan failed: {res.error.message}")
        raw += res.findings
    return aggregate(ws, raw).findings


def _tags(findings: list[Finding], file: str) -> tuple[Counter[Tag], dict[Tag, Severity]]:
    counts: Counter[Tag] = Counter()
    sev: dict[Tag, Severity] = {}
    for f in findings:
        if f.file != file:
            continue
        for s in f.sources:
            if not s.startswith("ai:"):
                counts[(s, f.function_name)] += 1
                sev[(s, f.function_name)] = f.severity
    return counts, sev


@dataclass
class Baseline:
    """Computed once per fix run on the unpatched code, so every patch is compared fairly."""

    findings: list[Finding]
    test_targets: list[str]
    tests: PytestRun | None

    @classmethod
    def compute(cls, ws: Workspace) -> Baseline:
        targets = [f.path for f in ws.python_files if f.is_test]
        tests = None
        if targets:
            copy = ws.copy()
            try:
                tests = run_pytest(copy.root, targets)
            finally:
                copy.cleanup()
        return cls(deterministic_scan(ws), targets, tests)


def _static_step(finding: Finding, baseline: Baseline, patched: Workspace) -> tuple[VerificationStep, bool | None, bool]:
    """Returns (step, target_gone or None if not applicable, introduced_serious)."""
    before, _ = _tags(baseline.findings, finding.file)
    after, after_sev = _tags(deterministic_scan(patched), finding.file)
    targets = {(s, finding.function_name) for s in finding.sources if not s.startswith("ai:")}
    gone = all(t not in after for t in targets) if targets else None
    introduced = after - before
    serious = [t for t in introduced if after_sev[t].rank >= Severity.MEDIUM.rank]
    parts = []
    if gone is True:
        parts.append(f"{', '.join(sorted(s for s, _ in targets))} no longer reported")
    elif gone is False:
        parts.append("the original tool finding is still reported")
    else:
        parts.append("no tool rule to re-check (AI finding)")
    if introduced:
        parts.append("new: " + ", ".join(f"{s} ({after_sev[(s, fn)].value})" for s, fn in introduced))
    else:
        parts.append("no new findings")
    status = StepStatus.FAIL if serious or gone is False else StepStatus.PASS
    return VerificationStep(step="static_rescan", status=status, detail="; ".join(parts)), gone, bool(serious)


def _repro_step(
    finding: Finding, proposal: PatchProposal, original: Workspace, patched: Workspace,
) -> tuple[VerificationStep, bool | None]:
    """Returns (step, proven): True red->green, False reproduced but fix didn't make it pass,
    None when we couldn't reproduce or had no usable test."""
    sf = original.get(finding.file)
    module = module_name(finding.file)
    candidates: list[tuple[str, str]] = []
    if finding.repro:
        det = build_repro_test(sf, module, finding.repro.call, finding.repro.expected, finding.function_name or "defect")
        if det:
            candidates.append(("from the reported failing input", det))
    if proposal.test_code.strip():
        candidates.append(("written by the model", proposal.test_code))
    if not candidates:
        return VerificationStep(step="repro", status=StepStatus.SKIPPED, detail="no behavioral test available"), None

    last = None
    for origin, code in candidates:
        for ws in (original, patched):
            (ws.root / REPRO_FILE).write_text(code)
        before = run_pytest(original.root, [REPRO_FILE])
        if before.timed_out or before.collection_error or any(c.broken_test for c in before.cases) or not before.cases:
            why = "timed out" if before.timed_out else "the test itself is broken"
            last = VerificationStep(step="repro", status=StepStatus.INCONCLUSIVE, test_code=code,
                                    detail=f"test {origin}: {why} on the original code", output=before.output)
            continue
        if before.all_passed:
            last = VerificationStep(step="repro", status=StepStatus.INCONCLUSIVE, test_code=code,
                                    detail=f"test {origin}: passes on the original code, defect not reproduced",
                                    output=before.output)
            continue
        failure = next(c for c in before.cases if c.outcome != "passed")
        after = run_pytest(patched.root, [REPRO_FILE])
        red = f"red on original ({failure.exc_type})"
        if after.all_passed:
            return VerificationStep(step="repro", status=StepStatus.PASS, test_code=code,
                                    detail=f"test {origin}: {red} → green on patch",
                                    output=after.output, duration_ms=before.duration_ms + after.duration_ms), True
        return VerificationStep(step="repro", status=StepStatus.FAIL, test_code=code,
                                detail=f"test {origin}: {red}, still failing after the patch",
                                output=after.output), False
    return last, None


def _regression_step(baseline: Baseline, patched: Workspace) -> VerificationStep:
    if not baseline.test_targets or baseline.tests is None:
        return VerificationStep(step="regression", status=StepStatus.SKIPPED, detail="project has no test suite")
    after = run_pytest(patched.root, baseline.test_targets)
    b = baseline.tests
    ok = not after.timed_out and not after.collection_error and after.failed <= b.failed
    detail = f"project tests: {b.passed} passed / {b.failed} failed before, {after.passed} / {after.failed} after"
    return VerificationStep(step="regression", status=StepStatus.PASS if ok else StepStatus.FAIL, detail=detail,
                            output="" if ok else after.output, duration_ms=after.duration_ms)


def verify(workspace: Workspace, finding: Finding, proposal: PatchProposal, baseline: Baseline) -> Verification:
    steps = [VerificationStep(step="syntax", status=StepStatus.PASS, detail="patched file parses")]
    original, patched = workspace.copy(), workspace.copy()
    try:
        patched.write(finding.file, proposal.patch.patched_code)
        static, gone, serious = _static_step(finding, baseline, patched)
        repro, proven = _repro_step(finding, proposal, original, patched)
        regression = _regression_step(baseline, patched)
    finally:
        original.cleanup()
        patched.cleanup()
    steps += [static, repro, regression]

    if serious or gone is False or proven is False or regression.status == StepStatus.FAIL:
        status = VerificationStatus.FAILED
    elif proven:
        status = VerificationStatus.PROVEN
    elif gone:
        status = VerificationStatus.STATIC_PASS
    else:
        status = VerificationStatus.INCONCLUSIVE
    return Verification(finding_id=finding.id, status=status, steps=steps)
