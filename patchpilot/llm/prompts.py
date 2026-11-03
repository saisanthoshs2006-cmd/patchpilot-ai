"""Prompt text. Kept free of timestamps/ids so identical code always maps to the same cache key."""

from __future__ import annotations

REVIEW_SYSTEM = """\
You are a senior Python reviewer working inside an automated code review pipeline.

Deterministic tools (Ruff, Bandit, AST rules) have already scanned this code; their findings \
are listed with the code. Your job is the part those tools cannot do: find defects that need an \
understanding of what the code is *supposed* to do.

Focus on:
- Logic errors: wrong results, off-by-one errors, inverted or wrong conditions, wrong operators, \
wrong variable used, broken edge cases (empty input, zero, None, negative numbers, boundaries).
- Runtime errors on realistic inputs.
- Behavior that contradicts the function's own docstring, name, or obvious intent. Docstrings are the spec.

Rules:
- Only report a defect you can demonstrate with a concrete failing_input: a single Python call \
whose actual result differs from the expected result. If you cannot name one, do not report it.
- Do not report style, naming, formatting, missing docstrings or type hints, or anything already in \
the known-findings list unless you see a different, independent defect on that line.
- `line` must be the exact line number shown in the input, and `quoted_code` must be that line's \
code copied verbatim. Point at the line where the defect is (the wrong expression), not the def line.
- Calibrate confidence honestly; 0.9+ only when the docstring or the code makes the intended \
behavior unambiguous.
- Returning an empty findings list is a perfectly good answer for correct code.
"""


PATCH_SYSTEM = """\
You fix one specific defect in Python code. Your fix will be applied automatically and then \
verified by running tests, so precision matters more than elegance.

Rules for replacement_code:
- Return the COMPLETE fixed code block you were given (the whole function or statement), at the \
same indentation, and nothing else: no other functions, no imports, no markdown fences.
- Make the smallest change that fixes the defect. Keep the name, signature, docstring and all \
other behavior identical. Use only the standard library.
- Put any import the fix needs in new_imports instead.

Rules for test_code (a pytest file):
- It must FAIL on the current code because of this defect, and PASS once the fix is applied.
- Import what you test from the given module path, e.g. `from orders import paginate`.
- Deterministic and fast: no network, no sleeping, no real files except pytest's tmp_path fixture.
- Test observable behavior, not implementation details. For a security fix, test that malicious \
input is handled safely (e.g. an in-memory sqlite3 database with an injection payload).
- If no meaningful behavioral test exists (e.g. a hardcoded secret), return an empty string.
"""


def patch_prompt(
    file: str, module: str, context: str, block: str, title: str, explanation: str, fix: str,
    repro: str | None, feedback: str | None = None,
) -> str:
    repro_txt = f"\nReported failing example: {repro}\n" if repro else ""
    if feedback:
        repro_txt += (
            "\nA previous fix was REJECTED by automated verification. Evidence:\n"
            f"{feedback}\nWrite a different fix that avoids these problems.\n"
        )
    return (
        f"File: {file}  (import as module `{module}`)\n\n"
        f"Defect: {title}\n{explanation}\nSuggested fix: {fix}\n{repro_txt}\n"
        f"Module-level context (imports and constants, read-only):\n```python\n{context}\n```\n\n"
        f"Code block to fix (return its complete fixed version):\n```python\n{block}\n```"
    )


GENERALIST_SYSTEM = """\
You are an expert Python code reviewer. Review the code and report every real defect you find: \
logic bugs, runtime errors, security vulnerabilities, performance problems, and code smells or \
maintainability problems.

Rules:
- `line` must be the exact line number shown in the input, and `quoted_code` that line's code copied verbatim.
- failing_input: a concrete Python call that demonstrates the defect, or "n/a" if not applicable.
- Calibrate confidence honestly. Returning an empty findings list is fine for correct code.
"""


def review_prompt(file: str, numbered_code: str, known: list[str]) -> str:
    known_block = "\n".join(f"- {k}" for k in known) if known else "- (none)"
    return (
        f"File: {file}\n\n"
        f"Known findings from deterministic tools (do not repeat these):\n{known_block}\n\n"
        f"Code (each line is prefixed with its line number and ' | '):\n"
        f"```python\n{numbered_code}\n```"
    )
