"""Deterministic quality scores (0-100, higher is better). The LLM never sets a score.

    penalty(dim) = sum over findings in dim of  SEVERITY_WEIGHT * confidence * evidence_factor
    score(dim)   = 100 * exp(-penalty / budget),  budget = BASE_BUDGET * max(1, loc / 200)

- exp() gives diminishing returns: the 10th low-severity nit hurts less than the first
  critical. A single critical (weight 10) on a small file costs ~40 points.
- The budget scales with code size, so a 2,000-line project isn't punished for having
  more findings than a 50-line snippet with the same density.
- Unproven AI hypotheses count at half weight.
- Maintainability also blends in Radon's maintainability index.
"""

from __future__ import annotations

import math

from patchpilot.models import Category, EvidenceLevel, Finding, Scores, Severity

SEVERITY_WEIGHT = {
    Severity.CRITICAL: 10.0, Severity.HIGH: 6.0, Severity.MEDIUM: 3.0, Severity.LOW: 1.0, Severity.INFO: 0.2,
}
EVIDENCE_FACTOR = {
    EvidenceLevel.PROVEN: 1.0, EvidenceLevel.TOOL_CONFIRMED: 1.0,
    EvidenceLevel.CORROBORATED: 0.9, EvidenceLevel.AI_HYPOTHESIS: 0.5,
}
DIMENSION_OF = {
    Category.SECURITY: "security",
    Category.BUG: "reliability",
    Category.RUNTIME_ERROR: "reliability",
    Category.PERFORMANCE: "performance",
    Category.MAINTAINABILITY: "maintainability",
    Category.CODE_SMELL: "maintainability",
    Category.BEST_PRACTICE: "style",
    Category.STYLE: "style",
}
OVERALL_WEIGHTS = {"security": 0.30, "reliability": 0.30, "performance": 0.15, "maintainability": 0.15, "style": 0.10}
BASE_BUDGET = 20.0


def compute_scores(findings: list[Finding], loc: int, maintainability_index: float | None = None) -> Scores:
    budget = BASE_BUDGET * max(1.0, loc / 200)
    penalty = dict.fromkeys(OVERALL_WEIGHTS, 0.0)
    for f in findings:
        penalty[DIMENSION_OF[f.category]] += (
            SEVERITY_WEIGHT[f.severity] * f.confidence * EVIDENCE_FACTOR[f.evidence_level]
        )
    dims = {d: 100.0 * math.exp(-p / budget) for d, p in penalty.items()}
    if maintainability_index is not None:
        dims["maintainability"] = 0.7 * dims["maintainability"] + 0.3 * max(0.0, min(100.0, maintainability_index))
    overall = sum(dims[d] * w for d, w in OVERALL_WEIGHTS.items())
    return Scores(overall=round(overall, 1), **{d: round(v, 1) for d, v in dims.items()})
