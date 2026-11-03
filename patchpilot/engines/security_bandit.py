"""Bandit: Python security analysis."""

from __future__ import annotations

from pathlib import PurePosixPath

from patchpilot.engines._subprocess import ToolError, run_json_tool
from patchpilot.engines.rule_kb import KB, fallback_why
from patchpilot.ingestion.loader import Workspace
from patchpilot.models import Category, EvidenceLevel, Finding, Severity

# B101 (assert used) is noise outside tests; every assert would be flagged.
SKIP = "B101"

_SEVERITY = {"HIGH": Severity.HIGH, "MEDIUM": Severity.MEDIUM, "LOW": Severity.LOW}
# Bandit's own confidence is about the pattern match, which is exactly what we want here.
_CONFIDENCE = {"HIGH": 0.9, "MEDIUM": 0.75, "LOW": 0.6}


class BanditEngine:
    name = "bandit"

    def run(self, workspace: Workspace) -> list[Finding]:
        files = [f.path for f in workspace.source_files]
        if not files:
            return []
        raw = run_json_tool("bandit", ["-q", "-f", "json", "--skip", SKIP, *files], cwd=workspace.root)
        if raw.get("errors"):
            # Bandit reports per-file parse errors here; surface the first one.
            err = raw["errors"][0]
            raise ToolError(f"bandit could not analyze {err.get('filename')}: {err.get('reason')}")

        findings = []
        for item in raw["results"]:
            rule_id = f"bandit:{item['test_id']}"
            info = KB.get(rule_id)
            category = info.category if info else Category.SECURITY
            severity = info.severity if info else _SEVERITY.get(item["issue_severity"], Severity.MEDIUM)
            cwe = item.get("issue_cwe") or {}
            explanation = item["issue_text"] + (f" (CWE-{cwe['id']})" if cwe.get("id") else "")
            line_range = item.get("line_range") or [item["line_number"]]
            findings.append(Finding(
                file=PurePosixPath(item["filename"]).as_posix().removeprefix("./"),
                line=item["line_number"],
                end_line=max(line_range),
                column=item.get("col_offset"),
                severity=severity,
                category=category,
                rule_id=rule_id,
                title=info.title if info else item["test_name"].replace("_", " ").capitalize(),
                explanation=explanation,
                why_it_matters=info.why if info else fallback_why(category),
                suggested_fix=info.fix if info else f"See {item.get('more_info', 'the Bandit docs')}.",
                confidence=_CONFIDENCE.get(item["issue_confidence"], 0.6),
                evidence_level=EvidenceLevel.TOOL_CONFIRMED,
                sources=[rule_id],
            ))
        return findings
