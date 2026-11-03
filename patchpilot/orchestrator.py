"""Runs the review pipeline. UI-agnostic: Streamlit, the CLI and the benchmark all call `review()`.

Stage 1: deterministic engines in parallel (Ruff, Bandit, AST rules, Radon).
Stage 2: AI reasoning, told what stage 1 found so it focuses on what tools can't see.
Stage 3: validate, merge, corroborate, score.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from patchpilot import config
from patchpilot.aggregation.aggregator import aggregate
from patchpilot.aggregation.scoring import compute_scores
from patchpilot.engines.ai_reasoner import AiReasonerEngine
from patchpilot.engines.ast_engine import AstEngine
from patchpilot.engines.base import Engine, EngineResult, run_engine
from patchpilot.engines.metrics_radon import RadonEngine, collect_metrics
from patchpilot.engines.security_bandit import BanditEngine
from patchpilot.engines.static_ruff import RuffEngine
from patchpilot.ingestion.loader import Workspace
from patchpilot.llm.client import JsonLLM, LLMError, make_client
from patchpilot.models import (
    EngineError,
    EvidenceLevel,
    Finding,
    Patch,
    RejectedClaim,
    Review,
    Severity,
    StepStatus,
    Verification,
    VerificationStatus,
    VerificationStep,
)
from patchpilot.patching.generator import PatchError, generate_patch
from patchpilot.verification.verifier import Baseline, verify

# (stage, message) -> None. Stages: "engine_done", "ai_start", "aggregate", "done".
ProgressFn = Callable[[str, str], None]


def default_engines() -> list[Engine]:
    return [RuffEngine(), BanditEngine(), AstEngine(), RadonEngine()]


def _status(res: EngineResult) -> str:
    return f"failed: {res.error.message}" if res.error else f"{len(res.findings)} findings"


def review(
    workspace: Workspace,
    engines: list[Engine] | None = None,
    progress: ProgressFn | None = None,
    mode: str | None = None,
    llm: JsonLLM | None = None,
) -> Review:
    """mode: "auto" | "replay" | "offline" (no AI). Defaults to config.LLM_MODE."""
    start = time.perf_counter()
    mode = mode or config.LLM_MODE
    engines = engines if engines is not None else default_engines()
    notify = progress or (lambda stage, msg: None)

    # Stage 1: engines are independent and mostly wait on subprocesses, so threads parallelize well.
    results: list[EngineResult] = []
    with ThreadPoolExecutor(max_workers=len(engines) or 1) as pool:
        for res in pool.map(lambda e: run_engine(e, workspace), engines):
            results.append(res)
            notify("engine_done", f"{res.engine}: {_status(res)} ({res.duration_ms} ms)")

    # Stage 2: AI reasoning with stage-1 context.
    errors: list[EngineError] = [r.error for r in results if r.error]
    rejected: list[RejectedClaim] = []
    metrics: dict[str, float] = {}
    if mode != "offline":
        client = llm or make_client(mode=mode)
        ai = AiReasonerEngine(client, known=[f for r in results for f in r.findings])
        notify("ai_start", f"AI reasoning ({client.model}, {mode} mode)…")
        res = run_engine(ai, workspace)
        results.append(res)
        notify("engine_done", f"ai: {_status(res)}, {len(ai.rejected)} rejected by quote check ({res.duration_ms} ms)")
        if res.error:
            errors.append(res.error)
        errors += [EngineError(engine="ai", message=m) for m in ai.partial_errors]
        rejected += [RejectedClaim(file=r.file, line=r.line, title=r.title, source="ai", reason=r.reason)
                     for r in ai.rejected]
        metrics["ai_claims"] = len(res.findings) + len(ai.rejected)
        metrics["ai_rejected"] = len(ai.rejected)
        for k, v in getattr(client, "stats", {}).items():
            metrics[f"llm_{k}"] = v

    # Stage 3
    notify("aggregate", "Validating, merging and scoring findings")
    raw = [f for r in results for f in r.findings]
    agg = aggregate(workspace, raw)
    rejected += [
        RejectedClaim(file=d.finding.file, line=d.finding.line, title=d.finding.title,
                      source=d.finding.sources[0].split(":")[0] if d.finding.sources else "?", reason=d.reason)
        for d in agg.dropped
    ]
    try:
        metrics.update(collect_metrics(workspace))
    except Exception as exc:  # noqa: BLE001 - metrics are optional, never fatal
        errors.append(EngineError(engine="metrics", message=str(exc)))
    metrics["raw_findings"] = agg.raw_count
    metrics["dropped_findings"] = len(rejected)
    metrics.update({f"ms_{r.engine}": r.duration_ms for r in results})

    rev = Review(
        id=uuid.uuid4().hex[:12],
        source_name=workspace.source_name,
        files=[f.path for f in workspace.source_files],
        loc=workspace.loc,
        mode=mode,
        findings=agg.findings,
        scores=compute_scores(agg.findings, workspace.loc, metrics.get("maintainability_index")),
        engine_errors=errors,
        rejected_claims=rejected,
        metrics=metrics,
        duration_ms=int((time.perf_counter() - start) * 1000),
    )
    notify("done", f"{len(rev.findings)} findings in {rev.duration_ms} ms")
    return rev


def default_fix_candidates(rev: Review, limit: int = config.MAX_FINDINGS_TO_PATCH) -> list[Finding]:
    """Worst first; skip info-level notes and pure size/complexity findings (no single fix)."""
    skip_rules = {"ast:function-too-long", "ast:too-many-parameters", "radon:high-complexity"}
    eligible = [f for f in rev.findings if f.severity != Severity.INFO and f.rule_id not in skip_rules]
    return eligible[:limit]


def fix_and_verify(
    workspace: Workspace,
    rev: Review,
    finding_ids: list[str] | None = None,
    progress: ProgressFn | None = None,
    mode: str | None = None,
    llm: JsonLLM | None = None,
) -> Review:
    """Stage 4: generate a patch per selected finding and verify each one independently."""
    notify = progress or (lambda stage, msg: None)
    client = llm or make_client(mode=mode or config.LLM_MODE, model=config.PATCH_MODEL, effort=config.PATCH_EFFORT)
    chosen = [f for f in rev.findings if f.id in finding_ids] if finding_ids else default_fix_candidates(rev)

    notify("baseline", "Measuring the unpatched baseline (rescan + project tests)…")
    baseline = Baseline.compute(workspace)

    def one(f: Finding) -> tuple[Finding, Patch | None, Verification]:
        feedback: str | None = None
        history: list[VerificationStep] = []
        patch, ver = None, None
        for attempt in range(1 + config.PATCH_REPAIR_ATTEMPTS):
            try:
                proposal = generate_patch(workspace.get(f.file), f, client, feedback)
            except (PatchError, LLMError) as exc:
                step = VerificationStep(step="generate", status=StepStatus.ERROR, detail=str(exc))
                if ver is None:  # nothing usable from an earlier attempt either
                    ver = Verification(finding_id=f.id, status=VerificationStatus.FAILED, steps=[*history, step])
                break
            patch, ver = proposal.patch, verify(workspace, f, proposal, baseline)
            ver = ver.model_copy(update={"steps": [*history, *ver.steps]})
            if ver.status != VerificationStatus.FAILED:
                break
            # Self-repair: feed the verifier's evidence back to the model and try again.
            failing = [s for s in ver.steps if s.status in (StepStatus.FAIL, StepStatus.ERROR)]
            feedback = "\n".join(f"- {s.step}: {s.detail}" + (f"\n{s.output[-800:]}" if s.output else "")
                                  for s in failing)
            history = [VerificationStep(step="repair", status=StepStatus.FAIL,
                                        detail=f"attempt {attempt + 1} rejected: " + "; ".join(s.detail for s in failing))]
        return f, patch, ver

    patches: dict[str, Patch] = {p.finding_id: p for p in rev.patches}
    verifications: dict[str, Verification] = {v.finding_id: v for v in rev.verifications}
    with ThreadPoolExecutor(max_workers=config.LLM_MAX_PARALLEL) as pool:
        for f, patch, ver in pool.map(one, chosen):
            if patch:
                patches[f.id] = patch
            verifications[f.id] = ver
            notify("verified", f"{f.location} {f.title}: {ver.status.value.replace('_', ' ')}")

    findings = []
    for f in rev.findings:
        ver = verifications.get(f.id)
        if ver:
            update = {"verification_status": ver.status}
            if ver.status == VerificationStatus.PROVEN:
                update |= {"evidence_level": EvidenceLevel.PROVEN, "confidence": max(f.confidence, 0.99)}
            f = f.model_copy(update=update)
        findings.append(f)

    # What the code would score with every verified fix applied (each verified independently).
    fixed = {VerificationStatus.PROVEN, VerificationStatus.STATIC_PASS}
    remaining = [f for f in findings if f.verification_status not in fixed]
    projected = compute_scores(remaining, rev.loc, rev.metrics.get("maintainability_index"))
    metrics = dict(rev.metrics)
    metrics["projected_overall"] = projected.overall
    for status in VerificationStatus:
        metrics[f"fix_{status.value}"] = sum(v.status == status for v in verifications.values())
    for k, v in getattr(client, "stats", {}).items():
        metrics[f"patch_llm_{k}"] = v
    notify("done", f"{len(chosen)} fixes attempted")
    return rev.model_copy(update={
        "findings": findings, "patches": list(patches.values()),
        "verifications": list(verifications.values()), "metrics": metrics,
    })
