"""Reliability rules: code paths that crash or silently return the wrong thing."""

from __future__ import annotations

import ast

from patchpilot.ast_tools.units import iter_functions, walk_scope
from patchpilot.engines.ast_rules.base import RuleContext, guarded_names, make_finding, rule
from patchpilot.models import Category, Severity

_TERMINAL = (ast.Return, ast.Raise, ast.Continue, ast.Break)


def _always_terminates(stmts: list[ast.stmt]) -> bool:
    """True if every path through `stmts` ends in return/raise. A block terminates as soon as
    any one of its statements does (whatever follows it is dead code)."""
    return any(_stmt_terminates(s) for s in stmts)


def _stmt_terminates(last: ast.stmt) -> bool:
    if isinstance(last, ast.Return | ast.Raise):
        return True
    if isinstance(last, ast.If):
        return _always_terminates(last.body) and _always_terminates(last.orelse)
    if isinstance(last, ast.Try | ast.TryStar):
        if _always_terminates(last.finalbody):
            return True
        body_ok = _always_terminates(last.body) or _always_terminates(last.orelse)
        return body_ok and all(_always_terminates(h.body) for h in last.handlers)
    if isinstance(last, ast.With | ast.AsyncWith):
        return _always_terminates(last.body)
    if isinstance(last, ast.While):
        infinite = isinstance(last.test, ast.Constant) and bool(last.test.value)
        return infinite and not any(isinstance(n, ast.Break) for n in ast.walk(last))
    if isinstance(last, ast.Match):
        return all(_always_terminates(c.body) for c in last.cases)
    if isinstance(last, ast.Expr) and isinstance(last.value, ast.Call):
        f = last.value.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        return name in {"exit", "_exit", "abort", "fail", "skip"}  # sys.exit, os._exit, pytest.fail...
    return False


def _returns_value(r: ast.Return) -> bool:
    return r.value is not None and not (isinstance(r.value, ast.Constant) and r.value.value is None)


@rule
def inconsistent_return(ctx: RuleContext):
    for func in iter_functions(ctx.tree):
        scope = list(walk_scope(func))
        if any(isinstance(n, ast.Yield | ast.YieldFrom) for n in scope):
            continue  # generators: bare return is normal
        returns = [n for n in scope if isinstance(n, ast.Return)]
        valued = [r for r in returns if _returns_value(r)]
        if not valued:
            continue
        bare = [r for r in returns if r.value is None]
        falls_through = not _always_terminates(func.body)
        if not (bare or falls_through):
            continue
        how = "can reach the end of the function" if falls_through else f"has a bare `return` (line {bare[0].lineno})"
        yield make_finding(
            ctx, func, rule_id="inconsistent-return",
            severity=Severity.LOW, category=Category.CODE_SMELL,
            title=f"`{func.name}` sometimes returns a value, sometimes None",
            explanation=(
                f"`{func.name}` returns a value on line {valued[0].lineno} but {how}, "
                "where it implicitly returns None."
            ),
            why=(
                "Callers expect a value and get None on the edge-case path, which surfaces later as a "
                "confusing TypeError/AttributeError far from the real cause."
            ),
            fix="Return an explicit value (or raise) on every path, and document the edge case.",
            confidence=0.9, end_line=func.lineno,
        )


@rule
def unreachable_code(ctx: RuleContext):
    for node in ast.walk(ctx.tree):
        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(node, field, None)
            if not isinstance(stmts, list):
                continue
            for i, stmt in enumerate(stmts[:-1]):
                if isinstance(stmt, _TERMINAL) or _stmt_terminates(stmt):
                    dead = stmts[i + 1]
                    kind = type(stmt).__name__.lower()
                    what = f"`{kind}`" if isinstance(stmt, _TERMINAL) else f"the `{kind}` block (every path through it exits)"
                    yield make_finding(
                        ctx, dead, rule_id="unreachable-code",
                        severity=Severity.MEDIUM, category=Category.BUG,
                        title=f"Unreachable code after {what.split(' (')[0]}",
                        explanation=f"Line {dead.lineno} can never run: {what} on line {stmt.lineno} always exits first.",
                        why="Dead code usually means a misplaced return or break: the logic the author intended never executes.",
                        fix="Move the exit below the code that should run, or delete the dead code.",
                        confidence=0.95, end_line=stmts[-1].end_lineno,
                    )
                    break


_RE_OPTIONAL = {"match", "search", "fullmatch"}


def _optional_call(v: ast.AST, dicts: set[str]) -> str | None:
    """Describe the call if it can return None: re.match/search/fullmatch, or .get(key) on a
    variable known to be a dict (not requests.get!)."""
    if not (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)):
        return None
    recv, attr = v.func.value, v.func.attr
    if attr in _RE_OPTIONAL and isinstance(recv, ast.Name) and recv.id == "re":
        return f"re.{attr}(...)"
    if attr == "get" and len(v.args) == 1 and not v.keywords:
        if isinstance(recv, ast.Name) and recv.id in dicts:
            return f"{recv.id}.get(...)"
        if isinstance(recv, ast.Attribute) and recv.attr == "environ":
            return "os.environ.get(...)"
    return None


def _known_dicts(func: ast.AST) -> set[str]:
    out = set()
    for n in walk_scope(func):
        if isinstance(n, ast.Assign) and (
            isinstance(n.value, ast.Dict | ast.DictComp)
            or (isinstance(n.value, ast.Call) and getattr(n.value.func, "id", None) == "dict")
        ):
            out |= {t.id for t in n.targets if isinstance(t, ast.Name)}
    return out


def _none_finding(ctx, node, call_desc, var=None):
    target = f"`{var}`" if var else "the result"
    return make_finding(
        ctx, node, rule_id="unchecked-optional",
        severity=Severity.MEDIUM, category=Category.RUNTIME_ERROR,
        title=f"Possible None dereference of {call_desc}",
        explanation=f"{call_desc} returns None when nothing matches, and {target} is used without a None check.",
        why="On the no-match input this raises AttributeError/TypeError and crashes the request.",
        fix=f"Check {target} before using it: `if {var or 'result'} is None: ...` (handle or raise a clear error).",
        confidence=0.8,
    )


@rule
def unchecked_optional(ctx: RuleContext):
    for func in iter_functions(ctx.tree):
        scope = list(walk_scope(func))
        dicts = _known_dicts(func)
        guarded = guarded_names(scope)
        optional_vars: dict[str, tuple[int, str]] = {}
        for n in scope:
            # direct chaining: re.match(...).group(1)
            if isinstance(n, ast.Attribute | ast.Subscript) and (desc := _optional_call(n.value, dicts)):
                yield _none_finding(ctx, n, desc)
            if (
                isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                and (desc := _optional_call(n.value, dicts))
            ):
                optional_vars[n.targets[0].id] = (n.lineno, desc)
        reported: set[str] = set()
        for n in sorted(scope, key=lambda x: getattr(x, "lineno", 0)):
            if not isinstance(n, ast.Attribute | ast.Subscript) or not isinstance(n.value, ast.Name):
                continue
            var = n.value.id
            if var in optional_vars and var not in guarded and var not in reported and n.lineno >= optional_vars[var][0]:
                reported.add(var)
                yield _none_finding(ctx, n, optional_vars[var][1], var)


def _len_arg(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len"
        and len(node.args) == 1 and isinstance(node.args[0], ast.Name)
    ):
        return node.args[0].id
    return None


@rule
def division_by_len_unguarded(ctx: RuleContext):
    for func in iter_functions(ctx.tree):
        scope = list(walk_scope(func))
        guarded = guarded_names(scope)
        for n in scope:
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div | ast.FloorDiv | ast.Mod):
                seq = _len_arg(n.right)
                if seq and seq not in guarded:
                    yield make_finding(
                        ctx, n, rule_id="division-by-len-unguarded",
                        severity=Severity.MEDIUM, category=Category.RUNTIME_ERROR,
                        title=f"ZeroDivisionError when `{seq}` is empty",
                        explanation=f"Divides by `len({seq})` and nothing in `{func.name}` checks that `{seq}` is non-empty.",
                        why="An empty input (no orders, no rows, a new user) crashes the call instead of returning a sensible value.",
                        fix=f"Guard the empty case first: `if not {seq}: return 0` (or whatever the domain default is).",
                        confidence=0.85,
                    )
