## Inspiration

Code review is one of the most valuable things a team does, and one of the slowest. Reviewers have to catch security holes, logic bugs and maintainability problems at the same time, often in code they didn't write.

Tools already help, but each covers only part of the job. Linters and security scanners like Ruff and Bandit are fast and precise, but they only know patterns: they can't tell that `return stock > threshold` should have been `<=` when the docstring says "at or below". LLMs can reason about intent, but they produce a confident list with no way to tell a real bug from a guess. Sometimes they cite a line that doesn't exist, and nobody tests the fixes they suggest.

We wanted to combine the two so that each covers the other's blind spots, and to make the result **trustworthy**: every finding should show what backs it, and a suggested fix should be checked before anyone relies on it.

## What it does

PatchPilot AI reviews Python code (pasted code, a `.py` file, a `.zip`, or a folder from the command line) and reports each finding with its **file, exact line, severity, category, explanation, why it matters, suggested fix, confidence and verification status.**

- **Static analysis:** Ruff (correctness and simplification rules) and Radon (complexity and maintainability metrics).
- **Security scanning:** Bandit (SQL injection, shell injection, weak hashes, unsafe deserialization, hardcoded secrets…).
- **AST analysis:** 9 rules we wrote using Python's `ast` module: inconsistent returns, unreachable code, unchecked `Optional` values, unguarded division by `len()`, SQL built from string formatting, list membership and string concatenation inside loops, overly long functions, and too many parameters.
- **AI reasoning:** an LLM reviews each function to find logic bugs the tools can't see. Every claim must include a concrete failing input.
- **Evidence levels:** each finding is labeled *tool-confirmed*, *corroborated* (two engines agree), *proven* (a test fails before the fix and passes after) or *AI hypothesis*. AI-only findings are shown in a separate list.
- **Severity classification** (critical → info) and **0–100 quality scores** for security, reliability, performance, maintainability and style, computed deterministically.
- **Suggested fixes, with automated checks:** on request, the AI writes a patch, and PatchPilot checks it on a throwaway copy:
  1. the patched code parses;
  2. the original issue is gone on rescan and no new medium-or-worse issue appears;
  3. a reproduction test fails on the original and passes on the patch;
  4. the project's existing tests still pass.
- **Reports and history:** downloadable HTML, JSON and SARIF reports, and every review saved to SQLite with score trends.
- **CLI with a CI gate:** `python -m patchpilot review src/ --fail-on high` exits with code 1 only for confirmed findings; AI hypotheses never fail a build.
- **Record/replay mode:** AI responses are cached, so a review can be replayed with no network and identical results.

## How we built it

PatchPilot AI is a Python 3.12 application with a Streamlit UI and a four-stage pipeline:

1. **Deterministic engines (in parallel):** Ruff, Bandit and Radon run as subprocesses with JSON output, alongside our AST rules. Every engine produces the same Pydantic `Finding` model, and one engine crashing never stops the review.
2. **AI reasoning:** the code is split into function-sized chunks sent *with real line numbers*, plus the module's imports and constants for context. The model is also told what the tools already found, so it focuses on logic rather than repeating lint. Responses use a strict JSON schema. We support Groq (`openai/gpt-oss-120b`, the one we ran) and Anthropic Claude.
3. **Aggregation:** every AI claim passes a **quote gate**. It must quote its line verbatim, and we check the quote against the actual file; a claim may move by at most 3 lines, and only if the quote matches there. Failing claims are rejected and listed, never shown as findings. Findings from different engines at the same location are merged; their confidences combine with a noisy-OR, \\( c = 1 - \prod_i (1 - c_i) \\), and the finding is marked *corroborated*. AI-only confidence is capped at 0.8.
4. **Fix and verify:** the patch replaces one function, and verification runs pytest in a subprocess with CPU and file-size limits, a timeout and no API keys in its environment. The reproduction test is built from the AI's claimed failing input where possible. If a patch is rejected, the model gets one retry with the verifier's evidence.

**Scoring** is never done by the LLM. Each category's penalty \\( P \\) is a severity-weighted sum (AI hypotheses count half), normalized by a budget \\( B \\) that grows with lines of code:

$$
\text{score} = 100 \cdot e^{-P/B}
$$

The UI also shows a projected score assuming the verified fixes are applied. HTML reports are rendered with Jinja2; history is stored in SQLite.

## Challenges we ran into

- **Accurate source locations.** Early AI findings pointed at the wrong lines. Sending numbered code chunks and adding the quote gate made locations reliable, and turned "the model made it up" into a counted, visible rejection.
- **Combining engines without duplicates.** Bandit and our own AST rule both flag the same SQL injection; Ruff and Bandit both flag a bare `except`. We merge on file, line and category, and only boost confidence when *different* engines agree.
- **False positives in our own rules.** Building a labeled test set exposed a bug in our unreachable-code rule: it only checked the last statement of a block, so it missed dead code after an `if/else` that always returns and raised a false "inconsistent return" instead. We fixed the rule, not the labels.
- **AI engine failures.** Groq's free tier gave us 429 rate limits, 413 errors (the max-tokens setting counts against the per-minute limit) and schema failures where the model used its whole token budget reasoning before writing any JSON. One failed run made the LLM-only baseline look far worse than it is. We tuned parallelism and retries, retry schema failures at lower reasoning effort, and now show a warning whenever a benchmark run had API errors.
- **Confirmed findings vs AI hypotheses.** We kept these separate everywhere: the UI, the reports, SARIF (AI hypotheses are always "note" level) and the CI gate.
- **Meaningful scores.** A raw count punishes large files. An exponential decay over a size-scaled budget gave scores that move sensibly, with AI hypotheses weighted at half.
- **Running generated code.** Python's `preexec_fn` isn't thread-safe, so resource limits are set in a small launcher script instead. Tests the model writes are sometimes broken themselves, so we build reproduction tests deterministically where we can, and mark a test that errors for the wrong reason as *inconclusive* instead of failing the fix.
- **A security bug in our own report.** Our HTML template ends in `.j2`, so Jinja's autoescape never switched on: reviewed code could have injected HTML into the report. A test caught it, and escaping is now always on.

## Accomplishments that we're proud of

- **It does more than wrap an LLM.** Deterministic tools run first and are the source of truth; the AI is told what they found, every claim is checked against the file, and scores are never computed by the LLM.
- **Fixes are checked by actually running tests.** On our demo project, 8 fixes were attempted: **6 passed all four checks** including a red → green reproduction test, 1 passed the rescan only (the model's test was broken), and **1 was rejected**. That rejected one was the model's fix for a shell injection: it removed `shell=True` but introduced a hardcoded `/tmp` path. Bandit caught it on rescan, the retry failed too, and PatchPilot refused it. The demo replays this offline with the same results.
- **We measured instead of guessing.** We wrote 8 Python files with 32 hand-labeled defects, labeling them before running any engine. In one run with `openai/gpt-oss-120b`:

| Configuration | Precision | Recall | F1 |
|---|---|---|---|
| Tools only (Ruff, Bandit, Radon, AST rules) | 90% | 59% (19/32) | 0.72 |
| LLM only (plain review prompt) | 90% | 81% (26/32) | 0.85 |
| **PatchPilot AI (hybrid)** | 88% | **91% (29/32)** | **0.89** |

  Tools found only 6 of 17 logic bugs; the LLM alone missed things like a hardcoded secret and a bare `except`. The hybrid caught both kinds, and 19 of its 29 hits are backed by a deterministic tool. This is a small benchmark we wrote ourselves, so we treat it as evidence, not proof. It reruns offline with `benchmark/run_eval.py --replay`.
- **67 automated tests** cover ingestion (including zip-slip protection), every engine, the quote gate, patch application, the verifier, history, reports, the CLI exit codes and the benchmark scorer.

## What we learned

- **Static analysis** is precise but blind to intent: valid code that does the wrong thing passes every linter.
- **Python's AST** makes custom rules surprisingly approachable, but control flow is subtle. Deciding when a block "always returns" took us two attempts.
- **Security scanning** tools are excellent at known patterns, and they made a great safety net for checking *AI-written fixes*, not just human code.
- **LLM integration** is mostly engineering around the model: strict schemas, caching, retries, rate limits, and checking every claim against the source.
- **Automated testing** of our own tool paid off: tests caught an XSS bug in our report, and the benchmark caught bugs in our rules.
- **Software architecture:** freezing one shared `Finding` model early let every engine, the aggregator, the UI and the reports be built independently.
- **Reliable AI tools** need evidence more than confidence. Labeling what backs each finding, and being willing to reject a fix, made the output easier to trust than any prompt change did.

## What's next for PatchPilot AI

*All of the following are planned, not implemented:*

- **Container isolation** for running fixes. Today's sandbox (subprocess, resource limits, timeout) is not a security boundary, so PatchPilot shouldn't be run on untrusted code yet.
- **A larger, independent benchmark**, run across several models and repeated runs.
- **A GitHub App** that comments on pull requests with confirmed findings and verified fixes.
- **Testing the Claude provider live**; it's implemented but we only ran Groq during the hackathon.
- **More languages**, only once we can test them as thoroughly as Python.
