"""Security rules that follow data into a dangerous sink, complementing Bandit's pattern matching.

Bandit B608 flags a string that *looks like* SQL. This rule independently checks that a
formatted string actually *reaches* cursor.execute(), so when both fire on the same line
the aggregator treats it as two independent pieces of evidence.
"""

from __future__ import annotations

import ast

from patchpilot.ast_tools.units import iter_functions, walk_scope
from patchpilot.engines.ast_rules.base import RuleContext, make_finding, rule
from patchpilot.models import Category, Severity

_SQL_SINKS = {"execute", "executemany", "executescript", "raw"}


def _is_formatted(v: ast.AST) -> bool:
    """f-string with placeholders, "..." + x, "..." % x, or "...".format(x)."""
    if isinstance(v, ast.JoinedStr):
        return any(isinstance(p, ast.FormattedValue) for p in v.values)
    if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add | ast.Mod):
        return any(isinstance(s, ast.Constant) and isinstance(s.value, str) for s in (v.left, v.right)) or (
            _is_formatted(v.left) or _is_formatted(v.right)
        )
    return isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "format"


@rule
def sql_built_from_string_formatting(ctx: RuleContext):
    for func in iter_functions(ctx.tree):
        scope = list(walk_scope(func))
        formatted: dict[str, ast.Assign] = {}
        for n in scope:
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                if _is_formatted(n.value):
                    formatted[n.targets[0].id] = n
                else:
                    formatted.pop(n.targets[0].id, None)
        for n in scope:
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _SQL_SINKS):
                continue
            if not n.args:
                continue
            query = n.args[0]
            # Report on the line where the string is built, so it lines up with Bandit's B608.
            if _is_formatted(query):
                origin = query
            elif isinstance(query, ast.Name) and query.id in formatted:
                origin = formatted[query.id]
            else:
                continue
            yield make_finding(
                ctx, origin, rule_id="sql-string-to-execute",
                severity=Severity.CRITICAL, category=Category.SECURITY,
                title="SQL injection via string-built query",
                explanation=(
                    f"A string built with formatting/concatenation is passed to `.{n.func.attr}()` on "
                    f"line {n.lineno}; any variable inside it becomes SQL code."
                ),
                why="Attacker-controlled input can read, modify or delete any data in the database (CWE-89).",
                fix=f"Use placeholders and pass values separately: `.{n.func.attr}('... = ?', (value,))`.",
                confidence=0.9,
            )
