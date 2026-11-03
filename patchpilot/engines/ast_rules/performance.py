"""Performance rules: algorithmic traps that linters don't see."""

from __future__ import annotations

import ast

from patchpilot.ast_tools.units import iter_functions, walk_scope
from patchpilot.engines.ast_rules.base import LOOPS, RuleContext, make_finding, rule
from patchpilot.models import Category, Severity


def _is_list_value(v: ast.AST) -> bool:
    return isinstance(v, ast.List | ast.ListComp) or (
        isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "list"
    )


def _is_str_value(v: ast.AST) -> bool:
    return isinstance(v, ast.JoinedStr) or (isinstance(v, ast.Constant) and isinstance(v.value, str))


def _names_always_assigned(func: ast.AST, predicate) -> set[str]:
    """Local names whose every plain assignment in the scope satisfies the predicate."""
    good: set[str] = set()
    bad: set[str] = set()
    for node in walk_scope(func):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    (good if predicate(node.value) else bad).add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            (good if predicate(node.value) else bad).add(node.target.id)
    return good - bad


def _loop_body_nodes(func: ast.AST):
    """(loop, node) for every node inside a loop body in this function's scope."""
    for loop in walk_scope(func):
        if isinstance(loop, LOOPS):
            for stmt in loop.body:
                for node in ast.walk(stmt):
                    yield loop, node


@rule
def list_membership_in_loop(ctx: RuleContext):
    for func in iter_functions(ctx.tree):
        lists = _names_always_assigned(func, _is_list_value)
        if not lists:
            continue
        seen_lines: set[int] = set()
        for _loop, node in _loop_body_nodes(func):
            if not isinstance(node, ast.Compare) or node.lineno in seen_lines:
                continue
            for op, comp in zip(node.ops, node.comparators, strict=True):
                if isinstance(op, ast.In | ast.NotIn) and isinstance(comp, ast.Name) and comp.id in lists:
                    seen_lines.add(node.lineno)
                    yield make_finding(
                        ctx, node, rule_id="list-membership-in-loop",
                        severity=Severity.LOW, category=Category.PERFORMANCE,
                        title=f"O(n²) membership test on list `{comp.id}` inside a loop",
                        explanation=(
                            f"`... in {comp.id}` scans the whole list on every iteration of the loop, "
                            "so the total cost grows quadratically with input size."
                        ),
                        why=(
                            "Fine for 100 items, painful for 100,000: 10,000 items means ~50 million "
                            "comparisons instead of 10,000."
                        ),
                        fix=(
                            f"Track membership in a set (`seen = set()`, `seen.add(x)`); keep `{comp.id}` "
                            "as a list only if order matters."
                        ),
                        confidence=0.9,
                    )
                    break


@rule
def str_concat_in_loop(ctx: RuleContext):
    for func in iter_functions(ctx.tree):
        strings = _names_always_assigned(func, _is_str_value)
        if not strings:
            continue
        seen: set[str] = set()
        for _loop, node in _loop_body_nodes(func):
            if (
                isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add)
                and isinstance(node.target, ast.Name) and node.target.id in strings
                and node.target.id not in seen
            ):
                seen.add(node.target.id)
                yield make_finding(
                    ctx, node, rule_id="str-concat-in-loop",
                    severity=Severity.LOW, category=Category.PERFORMANCE,
                    title=f"String `{node.target.id}` built with += inside a loop",
                    explanation=(
                        "Python strings are immutable, so each += may copy the whole string built so "
                        "far, which is quadratic in the worst case."
                    ),
                    why="Building large reports, CSV or HTML this way gets dramatically slower as output grows.",
                    fix="Collect the pieces in a list and return `''.join(parts)`.",
                    confidence=0.85,
                )
