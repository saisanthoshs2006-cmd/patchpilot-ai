"""AI semantic reasoning: the defects deterministic tools can't see (logic, intent, edge cases).

How it stays honest:
- It runs *after* the deterministic engines and is told what they found, so it spends its
  effort on logic instead of re-reporting lint.
- Code is sent with real line numbers, function by function (grouped into small chunks),
  plus the module's imports/constants so it knows what names like PAGE_SIZE mean.
- Every claim must cite a line, quote that line verbatim, and give a concrete failing input.
  The quote gate below checks the quote against the real file; a claim that doesn't match
  is rejected and counted, never shown.
- Output is an AI_HYPOTHESIS with capped confidence until something else backs it.
"""

from __future__ import annotations

import ast
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from patchpilot import config
from patchpilot.ast_tools.units import FunctionUnit, extract_units
from patchpilot.ingestion.loader import SourceFile, Workspace
from patchpilot.llm.client import JsonLLM, LLMError
from patchpilot.llm.prompts import REVIEW_SYSTEM, review_prompt
from patchpilot.llm.schemas import REVIEW_SCHEMA
from patchpilot.models import Category, EvidenceLevel, Finding, ReproHint, Severity

MODULE_CONTEXT_MAX_LINES = 40
QUOTE_SEARCH_RADIUS = 3  # tolerate small line-number drift, but only if the quote matches exactly nearby


@dataclass
class Chunk:
    file: SourceFile
    units: list[FunctionUnit]
    context_lines: list[int]  # module-level lines (imports, constants) included for context

    @property
    def shown_lines(self) -> set[int]:
        lines = set(self.context_lines)
        for u in self.units:
            lines.update(range(u.lineno, u.end_lineno + 1))
        return lines

    @property
    def tag(self) -> str:
        first = self.units[0].name if self.units else "module"
        return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{self.file.path}-{first}")

    def numbered_code(self) -> str:
        out, prev = [], None
        for n in sorted(self.shown_lines):
            if prev is not None and n != prev + 1:
                out.append("   ...")
            out.append(f"{n:>4} | {self.file.lines[n - 1]}")
            prev = n
        return "\n".join(out)


@dataclass
class Rejected:
    file: str
    line: int
    title: str
    reason: str


def _norm(s: str) -> str:
    return " ".join(s.split())


def _module_context(sf: SourceFile, tree: ast.Module) -> list[int]:
    lines: list[int] = []
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom | ast.Assign | ast.AnnAssign):
            lines.extend(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return lines[:MODULE_CONTEXT_MAX_LINES]


def build_chunks(sf: SourceFile, max_lines: int = config.AI_CHUNK_MAX_LINES) -> list[Chunk]:
    tree = ast.parse(sf.content)
    units = extract_units(sf, tree)
    # Only outermost functions: nested ones are already inside their parent's source.
    outer = [u for u in units if not any(o is not u and o.lineno <= u.lineno and u.end_lineno <= o.end_lineno
                                         for o in units)]
    context = _module_context(sf, tree)
    chunks: list[Chunk] = []
    current: list[FunctionUnit] = []
    size = 0
    for u in sorted(outer, key=lambda u: u.lineno):
        n = u.end_lineno - u.lineno + 1
        if current and size + n > max_lines:
            chunks.append(Chunk(sf, current, context))
            current, size = [], 0
        current.append(u)
        size += n
    if current:
        chunks.append(Chunk(sf, current, context))
    return chunks


def validate_claim(raw: dict[str, Any], chunk: Chunk, require_input: bool = True) -> tuple[int | None, str]:
    """Return (verified_line, "") or (None, rejection reason). The anti-hallucination gate."""
    lines = chunk.file.lines
    line = raw.get("line")
    quote = _norm(str(raw.get("quoted_code", "")))
    if not isinstance(line, int) or not quote:
        return None, "missing line or quote"
    if require_input and not str(raw.get("failing_input", "")).strip():
        return None, "no concrete failing input"

    def matches(n: int) -> bool:
        if not 1 <= n <= len(lines) or n not in chunk.shown_lines:
            return False
        actual = _norm(lines[n - 1])
        return actual == quote or (len(quote) >= 8 and quote in actual)

    if matches(line):
        return line, ""
    for delta in range(1, QUOTE_SEARCH_RADIUS + 1):
        for n in (line - delta, line + delta):
            if matches(n):
                return n, ""
    if line not in chunk.shown_lines:
        return None, f"cited line {line} was not in the code shown"
    return None, f"quoted code does not match line {line}"


class AiReasonerEngine:
    name = "ai"

    def __init__(
        self, llm: JsonLLM, known: list[Finding] | None = None, system: str = REVIEW_SYSTEM,
        require_failing_input: bool = True,
    ):
        self.llm = llm
        self.known = known or []
        self.system = system  # the benchmark swaps in a generalist prompt for its LLM-only baseline
        self.require_failing_input = require_failing_input
        self.rejected: list[Rejected] = []
        self.partial_errors: list[str] = []

    def _known_for(self, chunk: Chunk) -> list[str]:
        shown = chunk.shown_lines
        return [
            f"line {f.line}: [{f.rule_id}] {f.title}"
            for f in sorted(self.known, key=lambda f: f.line)
            if f.file == chunk.file.path and f.line in shown
        ]

    def _review_chunk(self, chunk: Chunk) -> list[Finding]:
        prompt = review_prompt(chunk.file.path, chunk.numbered_code(), self._known_for(chunk))
        data = self.llm.complete_json(self.system, prompt, REVIEW_SCHEMA, tag=chunk.tag)
        findings = []
        for raw in data.get("findings", []):
            line, reason = validate_claim(raw, chunk, self.require_failing_input)
            if line is None:
                self.rejected.append(Rejected(chunk.file.path, raw.get("line") or 0, raw.get("title", ""), reason))
                continue
            try:
                conf = max(0.0, min(float(raw.get("confidence", 0.5)), 1.0))
                findings.append(Finding(
                    file=chunk.file.path,
                    line=line,
                    severity=Severity(raw["severity"]),
                    category=Category(raw["category"]),
                    rule_id=f"ai:{raw['category']}",
                    title=raw["title"][:120],
                    explanation=raw["explanation"],
                    why_it_matters=raw["why_it_matters"],
                    suggested_fix=raw["suggested_fix"],
                    confidence=round(min(conf, config.AI_CONFIDENCE_CAP), 2),
                    evidence_level=EvidenceLevel.AI_HYPOTHESIS,
                    sources=[f"ai:{self.llm.model}"],
                    repro=ReproHint(call=raw["failing_input"], expected=raw["expected_behavior"],
                                    actual=raw["actual_behavior"]),
                ))
            except (KeyError, ValueError, TypeError) as exc:  # weaker models occasionally bend the schema
                self.rejected.append(Rejected(chunk.file.path, line, str(raw.get("title", "")), f"malformed claim: {exc}"))
        return findings

    def run(self, workspace: Workspace) -> list[Finding]:
        chunks = [c for sf in workspace.source_files for c in build_chunks(sf)]
        if not chunks:
            return []
        findings: list[Finding] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=config.LLM_MAX_PARALLEL) as pool:
            futures = [(c, pool.submit(self._review_chunk, c)) for c in chunks]
            for chunk, fut in futures:
                try:
                    findings.extend(fut.result())
                except LLMError as exc:
                    errors.append(f"{chunk.tag}: {exc}")
        if errors and len(errors) == len(chunks):
            raise LLMError(errors[0].split(": ", 1)[1])  # nothing worked: report as an engine failure
        self.partial_errors = errors
        return findings
