"""Rule registry and helpers for PatchPilot's own AST rules.

Each rule is a function `(ctx) -> Iterable[Finding]` decorated with @rule. Rules target
what Ruff/Bandit do NOT already cover, and are tuned for precision over recall: when a
pattern is ambiguous, a rule stays quiet (the AI engine covers the fuzzy cases).
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from patchpilot.ingestion.loader import SourceFile
from patchpilot.models import Category, EvidenceLevel, Finding, Severity


@dataclass(frozen=True)
class RuleContext:
    file: SourceFile
    tree: ast.Module


RuleFn = Callable[[RuleContext], Iterable[Finding]]
RULES: list[RuleFn] = []


def rule(fn: RuleFn) -> RuleFn:
    RULES.append(fn)
    return fn


def make_finding(
    ctx: RuleContext, node: ast.AST, *, rule_id: str, severity: Severity, category: Category,
    title: str, explanation: str, why: str, fix: str, confidence: float, end_line: int | None = None,
) -> Finding:
    return Finding(
        file=ctx.file.path,
        line=node.lineno,
        end_line=end_line or getattr(node, "end_lineno", None),
        column=getattr(node, "col_offset", None),
        severity=severity,
        category=category,
        rule_id=f"ast:{rule_id}",
        title=title,
        explanation=explanation,
        why_it_matters=why,
        suggested_fix=fix,
        confidence=confidence,
        evidence_level=EvidenceLevel.TOOL_CONFIRMED,
        sources=[f"ast:{rule_id}"],
    )


LOOPS = (ast.For, ast.AsyncFor, ast.While)


def names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def guarded_names(scope_nodes: Iterable[ast.AST]) -> set[str]:
    """Names that appear in any condition (if/while/assert/ternary/boolean op) in the scope.
    Deliberately generous: any check at all counts as a guard, to keep false positives low."""
    out: set[str] = set()
    for node in scope_nodes:
        if isinstance(node, ast.If | ast.While | ast.IfExp | ast.Assert):
            out |= names_in(node.test)
        elif isinstance(node, ast.BoolOp):
            out |= names_in(node)
        elif isinstance(node, ast.comprehension):
            for cond in node.ifs:
                out |= names_in(cond)
    return out
