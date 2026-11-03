"""Export a Review as JSON (lossless), SARIF 2.1.0 (GitHub code scanning, IDEs) or a
self-contained HTML report (no external assets, safe to email or attach to a PR)."""

from __future__ import annotations

import json
from collections import Counter

from jinja2 import Environment, PackageLoader

from patchpilot import __version__
from patchpilot.models import EvidenceLevel, Review, Severity, VerificationStatus

FORMATS = ("json", "sarif", "html")
_SARIF_LEVEL = {
    Severity.CRITICAL: "error", Severity.HIGH: "error", Severity.MEDIUM: "warning",
    Severity.LOW: "note", Severity.INFO: "note",
}
_env = Environment(loader=PackageLoader("patchpilot.reporting", "templates"), autoescape=True)  # reviewed code is untrusted input


def to_json(rev: Review) -> str:
    return rev.model_dump_json(indent=2)


def to_sarif(rev: Review) -> str:
    rules: dict[str, dict] = {}
    results = []
    for f in rev.findings:
        rules.setdefault(f.rule_id, {
            "id": f.rule_id, "shortDescription": {"text": f.title},
            "help": {"text": f.suggested_fix},
            "properties": {"category": f.category.value},
        })
        hypothesis = f.evidence_level == EvidenceLevel.AI_HYPOTHESIS
        results.append({
            "ruleId": f.rule_id,
            # Unproven AI claims never surface as errors: they are leads, not facts.
            "level": "note" if hypothesis else _SARIF_LEVEL[f.severity],
            "message": {"text": f"{f.title}. {f.explanation} Why it matters: {f.why_it_matters}"},
            "locations": [{"physicalLocation": {
                "artifactLocation": {"uri": f.file},
                "region": {"startLine": f.line, **({"endLine": f.end_line} if f.end_line else {})},
            }}],
            "partialFingerprints": {"patchpilot/v1": f.id},
            "properties": {
                "severity": f.severity.value, "category": f.category.value,
                "confidence": f.confidence, "evidence": f.evidence_level.value,
                "verification": f.verification_status.value, "sources": f.sources,
                "suggestedFix": f.suggested_fix,
            },
        })
    return json.dumps({
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "PatchPilot", "version": __version__,
                "informationUri": "https://github.com/", "rules": list(rules.values()),
            }},
            "results": results,
        }],
    }, indent=2)


def to_html(rev: Review) -> str:
    patches = {p.finding_id: p for p in rev.patches}
    verifications = {v.finding_id: v for v in rev.verifications}
    confirmed = [f for f in rev.findings if f.evidence_level != EvidenceLevel.AI_HYPOTHESIS]
    hypotheses = [f for f in rev.findings if f.evidence_level == EvidenceLevel.AI_HYPOTHESIS]
    fixes = Counter(v.status for v in rev.verifications)
    return _env.get_template("report.html.j2").render(
        rev=rev, confirmed=confirmed, hypotheses=hypotheses, patches=patches,
        verifications=verifications, severities=list(Severity),
        sev_counts=Counter(f.severity for f in rev.findings),
        fix_counts={s.value: fixes.get(s, 0) for s in VerificationStatus if s != VerificationStatus.UNVERIFIED},
        version=__version__,
    )


def export(rev: Review, fmt: str) -> str:
    return {"json": to_json, "sarif": to_sarif, "html": to_html}[fmt](rev)
