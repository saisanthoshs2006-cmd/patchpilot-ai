"""Radon: cyclomatic complexity per function and the maintainability index per file.

Emits findings only for functions over the complexity threshold; the raw numbers go to
Review.metrics for scoring and the dashboard.
"""

from __future__ import annotations

from radon.complexity import cc_visit
from radon.metrics import mi_visit

from patchpilot import config
from patchpilot.ingestion.loader import Workspace
from patchpilot.models import Category, EvidenceLevel, Finding, Severity


def collect_metrics(workspace: Workspace) -> dict[str, float]:
    complexities: list[int] = []
    mis: list[float] = []
    for sf in workspace.source_files:
        complexities += [b.complexity for b in cc_visit(sf.content) if b.letter in ("F", "M")]
        mis.append(mi_visit(sf.content, multi=True))
    return {
        "functions": len(complexities),
        "avg_complexity": round(sum(complexities) / len(complexities), 2) if complexities else 0.0,
        "max_complexity": max(complexities, default=0),
        "maintainability_index": round(sum(mis) / len(mis), 1) if mis else 100.0,  # 0-100, >20 is fine
        "loc": workspace.loc,
    }


class RadonEngine:
    name = "radon"

    def run(self, workspace: Workspace) -> list[Finding]:
        findings = []
        for sf in workspace.source_files:
            for block in cc_visit(sf.content):
                if block.letter not in ("F", "M") or block.complexity <= config.COMPLEXITY_WARN:
                    continue
                very = block.complexity > 2 * config.COMPLEXITY_WARN
                findings.append(Finding(
                    file=sf.path,
                    line=block.lineno,
                    end_line=block.endline,
                    severity=Severity.MEDIUM if very else Severity.LOW,
                    category=Category.MAINTAINABILITY,
                    rule_id="radon:high-complexity",
                    title=f"High cyclomatic complexity ({block.complexity}) in `{block.name}`",
                    explanation=(
                        f"`{block.name}` has {block.complexity} independent paths "
                        f"(threshold {config.COMPLEXITY_WARN}, radon rank {block.rank})."
                    ),
                    why_it_matters=(
                        "Each path needs its own test; complex functions are where untested edge-case "
                        "bugs hide, and they are risky to change."
                    ),
                    suggested_fix="Extract the independent branches into small, named helper functions.",
                    confidence=1.0,  # it's a measurement, not a guess
                    evidence_level=EvidenceLevel.TOOL_CONFIRMED,
                    sources=["radon:cc"],
                ))
        return findings
