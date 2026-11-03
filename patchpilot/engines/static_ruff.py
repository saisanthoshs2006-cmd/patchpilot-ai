"""Ruff: correctness and best-practice lint.

Security (S) rules are deliberately NOT enabled: they reimplement Bandit, and two copies
of the same rule must not count as two independent pieces of evidence. Complexity (C90)
is left to Radon. Pure formatting rules are off: style nits drown out real findings.
"""

from __future__ import annotations

from pathlib import Path

from patchpilot.engines._subprocess import run_json_tool
from patchpilot.engines.rule_kb import KB, fallback_why
from patchpilot.ingestion.loader import Workspace
from patchpilot.models import Category, EvidenceLevel, Finding, Severity

SELECT = "E4,E7,E9,F,B,SIM,PERF"

# Prefix -> (category, severity) when the rule isn't in the KB. Longest prefix wins.
_PREFIX_DEFAULTS: dict[str, tuple[Category, Severity]] = {
    "E9": (Category.RUNTIME_ERROR, Severity.CRITICAL),
    "E7": (Category.BEST_PRACTICE, Severity.LOW),
    "E4": (Category.STYLE, Severity.INFO),
    "F": (Category.CODE_SMELL, Severity.LOW),
    "F82": (Category.RUNTIME_ERROR, Severity.HIGH),  # undefined names
    "B": (Category.BUG, Severity.MEDIUM),
    "SIM": (Category.STYLE, Severity.LOW),
    "PERF": (Category.PERFORMANCE, Severity.LOW),
}


def _classify(code: str) -> tuple[Category, Severity]:
    for prefix in sorted(_PREFIX_DEFAULTS, key=len, reverse=True):
        if code.startswith(prefix):
            return _PREFIX_DEFAULTS[prefix]
    return Category.CODE_SMELL, Severity.LOW


class RuffEngine:
    name = "ruff"

    def run(self, workspace: Workspace) -> list[Finding]:
        files = [f.path for f in workspace.source_files]
        if not files:
            return []
        raw = run_json_tool(
            "ruff",
            ["check", "--isolated", "--select", SELECT, "--output-format", "json", "--exit-zero", *files],
            cwd=workspace.root,
        )
        root = workspace.root.resolve()
        findings = []
        for item in raw:
            code = item["code"] or "E999"  # syntax errors come back with code None
            rule_id = f"ruff:{code}"
            info = KB.get(rule_id)
            category, severity = (info.category, info.severity) if info else _classify(code)
            fix_msg = (item.get("fix") or {}).get("message")
            findings.append(Finding(
                file=Path(item["filename"]).resolve().relative_to(root).as_posix(),
                line=item["location"]["row"],
                end_line=item["end_location"]["row"],
                column=item["location"]["column"],
                severity=severity,
                category=category,
                rule_id=rule_id,
                title=info.title if info else item["message"],
                explanation=item["message"],
                why_it_matters=info.why if info else fallback_why(category),
                suggested_fix=(info.fix if info else fix_msg or f"See {item.get('url') or 'the Ruff docs'}."),
                confidence=0.95,  # ruff rules are precise; false positives are rare
                evidence_level=EvidenceLevel.TOOL_CONFIRMED,
                sources=[rule_id],
            ))
        return findings
