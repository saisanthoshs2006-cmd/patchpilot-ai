"""Turn raw engine output into the final, trustworthy finding list.

1. Validate: the file must be one we reviewed, the line must exist. Anything else is
   dropped and counted, never shown (this is where fabricated AI findings die).
2. Enrich: attach the source snippet and enclosing function.
3. Merge: findings at the same file + line + category from different engines become one
   finding with combined sources and higher confidence.
4. Corroborate: AI hypotheses on a line a tool also flagged are upgraded one evidence level.
5. Sort: worst first.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass, field

from patchpilot.ast_tools.units import FunctionUnit, enclosing_unit, extract_units
from patchpilot.ingestion.loader import Workspace
from patchpilot.models import EvidenceLevel, Finding

SNIPPET_MAX_LINES = 6


@dataclass
class DroppedFinding:
    finding: Finding
    reason: str


@dataclass
class AggregationResult:
    findings: list[Finding] = field(default_factory=list)
    dropped: list[DroppedFinding] = field(default_factory=list)
    raw_count: int = 0


def _noisy_or(confidences: list[float]) -> float:
    """Independent signals: P(at least one is right). Capped below 1 unless something was 1."""
    miss = 1.0
    for c in confidences:
        miss *= 1.0 - c
    combined = 1.0 - miss
    return round(combined if max(confidences) == 1.0 else min(combined, 0.99), 3)


def _merge(group: list[Finding]) -> Finding:
    primary = max(group, key=lambda f: (f.evidence_level.rank, f.severity.rank, f.confidence))
    if len(group) == 1:
        return primary
    sources = list(dict.fromkeys(s for f in group for s in f.sources))
    engines = {s.split(":", 1)[0] for s in sources}
    # Only *different engines* agreeing adds evidence; one engine repeating itself does not.
    confidence = _noisy_or([f.confidence for f in group]) if len(engines) > 1 else primary.confidence
    return primary.model_copy(update={
        "severity": max((f.severity for f in group), key=lambda s: s.rank),
        "end_line": max((f.end_line or f.line) for f in group),
        "confidence": confidence,
        "sources": sources,
        "repro": next((f.repro for f in group if f.repro), None),
    })


def _is_ai(f: Finding) -> bool:
    return all(s.startswith("ai:") for s in f.sources)


def _corroborate(findings: list[Finding]) -> list[Finding]:
    """An AI hypothesis on a line that a deterministic engine also flagged (for a different
    reason, otherwise they would already have merged) gets CORROBORATED: two independent
    methods both say something is wrong here. Its confidence is not raised: the tool
    supports the location, not the AI's specific explanation."""
    tool_sources: dict[tuple[str, int], list[str]] = defaultdict(list)
    for f in findings:
        if not _is_ai(f):
            tool_sources[(f.file, f.line)].extend(f.sources)
    out = []
    for f in findings:
        support = tool_sources.get((f.file, f.line))
        if f.evidence_level == EvidenceLevel.AI_HYPOTHESIS and support:
            f = f.model_copy(update={
                "evidence_level": EvidenceLevel.CORROBORATED,
                "sources": list(dict.fromkeys([*f.sources, *support])),
            })
        out.append(f)
    return out


def aggregate(workspace: Workspace, raw: list[Finding]) -> AggregationResult:
    result = AggregationResult(raw_count=len(raw))
    units_by_file: dict[str, list[FunctionUnit]] = {}
    for sf in workspace.source_files:
        units_by_file[sf.path] = extract_units(sf, ast.parse(sf.content))

    valid: list[Finding] = []
    for f in raw:
        sf = workspace.get(f.file)
        if sf is None:
            result.dropped.append(DroppedFinding(f, f"unknown file {f.file!r}"))
            continue
        n_lines = len(sf.lines)
        if f.line > max(n_lines, 1):
            result.dropped.append(DroppedFinding(f, f"line {f.line} beyond end of file ({n_lines} lines)"))
            continue
        end = min(max(f.end_line or f.line, f.line), n_lines or 1)
        snippet_end = min(end, f.line + SNIPPET_MAX_LINES - 1)
        unit = enclosing_unit(units_by_file.get(f.file, []), f.line)
        valid.append(f.model_copy(update={
            "end_line": end,
            "code_snippet": "\n".join(sf.lines[f.line - 1:snippet_end]),
            "function_name": unit.qualname if unit else None,
        }))

    groups: dict[tuple[str, int, str], list[Finding]] = defaultdict(list)
    for f in valid:
        groups[(f.file, f.line, f.category.value)].append(f)
    merged = _corroborate([_merge(g) for g in groups.values()])

    merged.sort(key=lambda f: (-f.severity.rank, -f.evidence_level.rank, -f.confidence, f.file, f.line))
    result.findings = merged
    return result
