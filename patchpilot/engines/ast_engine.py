"""Runs PatchPilot's own AST rules, and reports files that don't parse at all."""

from __future__ import annotations

import ast

from patchpilot.engines.ast_rules import RULES
from patchpilot.engines.ast_rules.base import RuleContext
from patchpilot.ingestion.loader import Workspace
from patchpilot.models import Category, EvidenceLevel, Finding, Severity


class AstEngine:
    name = "ast"

    def run(self, workspace: Workspace) -> list[Finding]:
        findings: list[Finding] = []
        for sf in workspace.python_files:
            if sf.syntax_error is not None and not sf.is_test:
                err = sf.syntax_error
                findings.append(Finding(
                    file=sf.path, line=max(err.lineno or 1, 1), column=err.offset,
                    severity=Severity.CRITICAL, category=Category.RUNTIME_ERROR,
                    rule_id="ast:syntax-error", title="File does not parse",
                    explanation=f"SyntaxError: {err.msg}",
                    why_it_matters="The module cannot be imported at all; nothing in it runs, and no other analysis is possible.",
                    suggested_fix="Fix the syntax error at the reported position.",
                    confidence=1.0, evidence_level=EvidenceLevel.TOOL_CONFIRMED, sources=["ast:syntax-error"],
                ))
        for sf in workspace.source_files:
            ctx = RuleContext(sf, ast.parse(sf.content, filename=sf.path))
            for check in RULES:
                findings.extend(check(ctx))
        return findings
