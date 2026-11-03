"""Function-level code units.

The AI engine reasons about one function at a time (cheaper, more precise than whole
files), the aggregator labels findings with their enclosing function, and the patcher
rewrites one function at a time. All three share this extraction.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass

from patchpilot.ingestion.loader import SourceFile


@dataclass(frozen=True)
class FunctionUnit:
    file: str
    name: str
    qualname: str  # "Class.method" or "outer.inner"
    lineno: int  # first line, including decorators
    end_lineno: int
    source: str
    params: tuple[str, ...]
    docstring: str | None

    def contains(self, line: int) -> bool:
        return self.lineno <= line <= self.end_lineno


def iter_functions(tree: ast.AST) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def walk_scope(func: ast.AST) -> Iterator[ast.AST]:
    """Like ast.walk over a function body, but does not descend into nested functions,
    lambdas or classes: their variables are a different scope."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            stack.extend(ast.iter_child_nodes(node))


def extract_units(sf: SourceFile, tree: ast.Module | None = None) -> list[FunctionUnit]:
    tree = tree or ast.parse(sf.content)
    lines = sf.lines
    units: list[FunctionUnit] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                start = min([d.lineno for d in child.decorator_list] + [child.lineno])
                end = child.end_lineno or child.lineno
                a = child.args
                params = tuple(p.arg for p in [*a.posonlyargs, *a.args, *a.kwonlyargs])
                units.append(FunctionUnit(
                    file=sf.path, name=child.name, qualname=prefix + child.name,
                    lineno=start, end_lineno=end, source="\n".join(lines[start - 1:end]),
                    params=params, docstring=ast.get_docstring(child),
                ))
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return units


def enclosing_unit(units: list[FunctionUnit], line: int) -> FunctionUnit | None:
    """Innermost function containing the line."""
    hits = [u for u in units if u.contains(line)]
    return min(hits, key=lambda u: u.end_lineno - u.lineno) if hits else None
