"""Maintainability rules: size limits (complexity itself is measured by Radon)."""

from __future__ import annotations

from patchpilot import config
from patchpilot.ast_tools.units import iter_functions
from patchpilot.engines.ast_rules.base import RuleContext, make_finding, rule
from patchpilot.models import Category, Severity


@rule
def function_too_long(ctx: RuleContext):
    for func in iter_functions(ctx.tree):
        length = (func.end_lineno or func.lineno) - func.lineno + 1
        if length > config.FUNCTION_MAX_LINES:
            yield make_finding(
                ctx, func, rule_id="function-too-long",
                severity=Severity.LOW, category=Category.MAINTAINABILITY,
                title=f"`{func.name}` is {length} lines long",
                explanation=f"Over the {config.FUNCTION_MAX_LINES}-line limit; it likely does several jobs.",
                why="Long functions are hard to test in isolation and changes in one part break another.",
                fix="Split it into smaller functions, each with one responsibility and a descriptive name.",
                confidence=1.0, end_line=func.lineno,
            )


@rule
def too_many_parameters(ctx: RuleContext):
    for func in iter_functions(ctx.tree):
        a = func.args
        params = [p.arg for p in [*a.posonlyargs, *a.args, *a.kwonlyargs] if p.arg not in ("self", "cls")]
        if len(params) > config.FUNCTION_MAX_PARAMS:
            yield make_finding(
                ctx, func, rule_id="too-many-parameters",
                severity=Severity.LOW, category=Category.MAINTAINABILITY,
                title=f"`{func.name}` takes {len(params)} parameters",
                explanation=f"Over the limit of {config.FUNCTION_MAX_PARAMS}; callers easily pass arguments in the wrong order.",
                why="Long parameter lists cause argument-order bugs and signal the function does too much.",
                fix="Group related parameters into a dataclass, or make the rarely-used ones keyword-only.",
                confidence=1.0, end_line=func.lineno,
            )
