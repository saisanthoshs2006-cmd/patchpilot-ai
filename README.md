# PatchPilot

**Hybrid, evidence-backed code review for Python.** Static analysis, AST rules, security
scanning and an LLM work together. Every finding is labeled by how strongly it is backed,
and every suggested fix is **verified by running tests** before it is offered.

Built for Neural Storm 2026, challenge FT-05 (AI-Powered Automated Code Review).
Python only: we don't claim support for other languages.

## Why not just ask an LLM?

On our benchmark (below), a plain "review this code" prompt does well: 81% recall. But
every one of its findings is a guess, and you can't tell which ones are right. PatchPilot:

- **Finds more:** 91% recall, because tools and the AI catch different defects.
- **Shows its evidence:** each finding is *tool-confirmed*, *corroborated* (two engines),
  *proven* (a test fails before the fix and passes after) or an *AI hypothesis*, kept in a
  separate list.
- **Rejects fabrications:** every AI claim must quote its line verbatim. Claims whose quote
  doesn't match the real file are dropped and counted, never shown.
- **Verifies fixes:** a patch is accepted only if it parses, the tool stops flagging it with
  nothing new introduced, a reproduction test goes red → green, and the project's own
  tests still pass.

## Quick start

```bash
uv venv --python 3.12 .venv && uv pip install -r requirements.txt   # or: python -m venv .venv && pip install ...
echo "GROQ_API_KEY=..." > .env            # or ANTHROPIC_API_KEY=... (optional: replay/offline need no key)
.venv/bin/streamlit run app.py            # http://localhost:8501
```

In the sidebar pick an **AI mode**:

| Mode | What it does |
|---|---|
| Live | Calls the LLM and records every response to `demo/llm_cache/` |
| Replay | Uses recorded responses only. No network, no key, identical results. Use this to present. |
| Off | Deterministic engines only (Ruff, Bandit, Radon, AST rules) |

Then review the **demo project**, paste code or upload a `.py`/`.zip`, and click
**Generate & verify fixes**. Reports download as HTML, JSON or SARIF. **History** keeps
every review in SQLite; **Benchmark** shows the evaluation below.

### Public mode and deployment

Public mode is **on by default** (`PATCHPILOT_PUBLIC=1`). It starts in Replay, and fix
verification runs only on the built-in demo project, because verifying a fix executes the
project's tests and the sandbox is not a security boundary. Uploaded and pasted code still get
the full review. For local development, put `PATCHPILOT_PUBLIC=0` in `.env`.

Deploy anywhere that runs Docker; the `Dockerfile` pins Python 3.12, runs as an unprivileged user and
listens on `$PORT`. On [Render](https://render.com): New → Web Service → this repo → Docker →
Free. No secrets are needed: the demo replays from `demo/llm_cache`. Adding `GROQ_API_KEY` as an
environment variable enables Live AI reviews for new code. Locally:
`docker build -t patchpilot . && docker run -p 8501:8501 patchpilot`.

### Command line / CI

```bash
.venv/bin/python -m patchpilot review demo/shop --ai replay --fix --format html --out report.html
.venv/bin/python -m patchpilot review src/ --ai offline --fail-on high      # CI gate
```

Exit codes: `0` pass, `1` a confirmed finding at or above `--fail-on`, `2` bad input.
AI hypotheses never fail a build. `--format sarif` plugs into GitHub code scanning.

## How it works

```
 ingest ──▶ Stage 1 (parallel)          ──▶ Stage 2         ──▶ Stage 3          ──▶ Stage 4 (on request)
 .py/.zip   Ruff · Bandit · Radon ·         AI reasoner          aggregate:           patch → verify:
 limits,    AST rules (9 custom)            told what tools      quote gate,          syntax · static rescan ·
 zip-slip                                   found; reviews       merge, corroborate,  repro red→green ·
 guards                                     function chunks      score                regression · 1 self-repair
```

- **Deterministic first.** The AI sees the tools' findings and spends its effort on logic
  bugs the tools can't see.
- **Real line numbers.** Code goes to the model function by function with its real line
  numbers. A cited line can drift by at most 3 lines, and only if the quote matches there.
- **Capped AI confidence** (0.8) until a test proves the claim.
- **Deterministic scores** (0–100 per category), never computed by the LLM. The UI also
  shows the projected score with verified fixes applied.
- **Providers:** Anthropic (`claude-opus-5-5`, structured outputs) or Groq
  (`openai/gpt-oss-120b`, strict JSON schema), chosen by which key is set. Groq schema
  failures are retried at lower reasoning effort.

## Benchmark

8 Python files with 32 hand-labeled defects (correctness, security, performance, quality),
labels written **before** any engine was run on them (`benchmark/labels.json`). A finding is
correct only if it lands inside a labeled defect's line span with a matching category.
Findings in debatable spans are ignored; info-level notes are excluded.

| Configuration | Precision | Recall | F1 | False positives |
|---|---|---|---|---|
| Tools only (Ruff, Bandit, AST, Radon) | 90% | 59% (19/32) | 0.72 | 2 |
| LLM only (plain review prompt) | 90% | 81% (26/32) | 0.85 | 3 |
| **PatchPilot hybrid** | 88% | **91% (29/32)** | **0.89** | 4 |

Model `openai/gpt-oss-120b` via Groq. Reproduce offline with
`.venv/bin/python benchmark/run_eval.py --replay`, or live without `--replay`.

What the numbers say, honestly:
- The tools miss nearly every logic bug (inverted checks, off-by-ones). The LLM alone misses
  easy-to-detect issues (a hardcoded secret, a bare `except`, quadratic loops). The hybrid gets both.
- Precision is about the same across all three; the hybrid trades 2 points of precision for 10 of recall.
- The LLM-only findings are all unproven; 19 of the hybrid's 29 hits are tool-confirmed.
- This is a small benchmark that we wrote ourselves. Treat it as evidence, not proof.

**Demo project fixes** (`demo/shop`, 8 fixes attempted): 6 proven by a red→green test,
1 verified by rescan only (the model's test was broken), 1 rejected (the shell-injection
fix introduced a new insecure `/tmp` path and was refused, even after self-repair).
Score 58 → 78 with the verified fixes applied.

## Limits

- **Python only.**
- **The verification sandbox is not a security boundary.** Patched code and generated tests run
  in a subprocess with a temp dir, CPU/file-size limits, a timeout and an environment without
  API keys. Do not review hostile code; container isolation is future work.
- A fix that is *proven* is proven against one reproduction test plus your existing tests,
  not against every possible input.
- AI recall depends on the model; numbers above are for one model on one run.

## Project layout

```
patchpilot/
  ingestion/      load text, uploads, directories (size limits, zip-slip guards)
  engines/        ruff, bandit, radon, ast_rules/, ai_reasoner (quote gate)
  aggregation/    merge, corroboration, scoring
  llm/            Claude & Groq clients with record/replay cache, prompts, schemas
  patching/       patch generation and safe application
  verification/   sandbox, repro tests, verifier
  storage/db.py   SQLite review history
  reporting/      HTML (self-contained), JSON, SARIF
  benchmark.py    evaluation harness       cli.py   command line / CI
app.py            Streamlit UI             benchmark/   cases, labels, results
demo/shop         demo project with seeded bugs and its own tests
tests/            67 tests
```

Run the tests: `.venv/bin/python -m pytest -q`. Lint: `.venv/bin/ruff check .`
