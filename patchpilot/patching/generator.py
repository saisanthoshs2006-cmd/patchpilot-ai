"""Generate a fix for one finding and apply it to a copy of the source.

The fix target is the smallest self-contained block around the finding: its enclosing
function, or the top-level statement for module-level issues. The model rewrites only
that block; we splice it back by line range, so it cannot touch anything else.
"""

from __future__ import annotations

import ast
import difflib
import textwrap
from dataclasses import dataclass
from pathlib import PurePosixPath

from patchpilot.ast_tools.units import enclosing_unit, extract_units
from patchpilot.ingestion.loader import SourceFile
from patchpilot.llm.client import JsonLLM
from patchpilot.llm.prompts import PATCH_SYSTEM, patch_prompt
from patchpilot.llm.schemas import PATCH_SCHEMA
from patchpilot.models import Finding, Patch


class PatchError(ValueError):
    pass


@dataclass
class PatchProposal:
    patch: Patch
    test_code: str  # model-written regression test ("" if none)
    span: tuple[int, int]  # original lines replaced (1-based, inclusive)


def module_name(path: str) -> str:
    p = PurePosixPath(path).with_suffix("")
    parts = list(p.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def target_span(sf: SourceFile, line: int) -> tuple[int, int]:
    tree = ast.parse(sf.content)
    unit = enclosing_unit(extract_units(sf, tree), line)
    if unit:
        # Fix the whole outermost function, so nested helpers stay consistent.
        outer = [u for u in extract_units(sf, tree) if u.lineno <= unit.lineno and unit.end_lineno <= u.end_lineno]
        top = min(outer, key=lambda u: u.lineno)
        return top.lineno, top.end_lineno
    for node in tree.body:
        if node.lineno <= line <= (node.end_lineno or node.lineno):
            return node.lineno, node.end_lineno or node.lineno
    return line, line


def _module_context(sf: SourceFile, span: tuple[int, int]) -> str:
    tree = ast.parse(sf.content)
    lines = []
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom | ast.Assign | ast.AnnAssign) and not (
            span[0] <= node.lineno <= span[1]
        ):
            lines.extend(sf.lines[node.lineno - 1:node.end_lineno])
    return "\n".join(lines) or "# (none)"


def _strip_fences(code: str) -> str:
    code = code.strip("\n")
    if code.lstrip().startswith("```"):
        code = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("```"))
    return code


def _reindent(code: str, indent: str) -> str:
    return textwrap.indent(textwrap.dedent(_strip_fences(code)), indent, lambda line: bool(line.strip()))


def _add_imports(source: str, imports: list[str]) -> str:
    tree = ast.parse(source)
    existing = {ast.unparse(n) for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)}
    new = []
    for imp in imports:
        imp = imp.strip()
        try:
            parsed = ast.parse(imp).body
        except SyntaxError:
            continue
        if len(parsed) == 1 and isinstance(parsed[0], ast.Import | ast.ImportFrom) and ast.unparse(parsed[0]) not in existing:
            new.append(ast.unparse(parsed[0]))
    if not new:
        return source
    lines = source.splitlines(keepends=True)
    # After the last top-level import (or the docstring, or at the top).
    anchor = 0
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom) or anchor == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            anchor = node.end_lineno or node.lineno
    insert = "".join(f"{s}\n" for s in new)
    return "".join(lines[:anchor]) + insert + "".join(lines[anchor:])


def apply_replacement(sf: SourceFile, span: tuple[int, int], replacement: str, imports: list[str]) -> str:
    start, end = span
    first = sf.lines[start - 1]
    indent = first[: len(first) - len(first.lstrip())]
    new_block = _reindent(replacement, indent)
    lines = sf.content.splitlines(keepends=True)
    patched = "".join(lines[: start - 1]) + new_block.rstrip("\n") + "\n" + "".join(lines[end:])
    try:
        ast.parse(patched)
    except SyntaxError as exc:
        raise PatchError(f"patched file does not parse: {exc.msg} (line {exc.lineno})") from exc
    return _add_imports(patched, imports)


def unified_diff(path: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True), f"a/{path}", f"b/{path}", n=3,
    ))


def generate_patch(sf: SourceFile, finding: Finding, llm: JsonLLM, feedback: str | None = None) -> PatchProposal:
    span = target_span(sf, finding.line)
    block = "\n".join(sf.lines[span[0] - 1: span[1]])
    repro = f"{finding.repro.call} → expected {finding.repro.expected}, got {finding.repro.actual}" if finding.repro else None
    prompt = patch_prompt(
        sf.path, module_name(sf.path), _module_context(sf, span), block,
        finding.title, finding.explanation, finding.suggested_fix, repro, feedback,
    )
    data = llm.complete_json(PATCH_SYSTEM, prompt, PATCH_SCHEMA, tag=f"patch-{finding.id}")
    replacement = data.get("replacement_code", "")
    if not replacement.strip():
        raise PatchError("model returned an empty fix")
    patched = apply_replacement(sf, span, replacement, list(data.get("new_imports") or []))
    if patched == sf.content:
        raise PatchError("model returned the code unchanged")
    return PatchProposal(
        patch=Patch(
            finding_id=finding.id, file=sf.path, original_code=sf.content, patched_code=patched,
            diff=unified_diff(sf.path, sf.content, patched), rationale=data.get("rationale", ""),
            generated_by=llm.model,
        ),
        test_code=_strip_fences(data.get("test_code", "")),
        span=span,
    )
