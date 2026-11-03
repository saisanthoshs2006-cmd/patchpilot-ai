# Demo script (3 minutes)

**Before going on stage**
- `.venv/bin/streamlit run app.py`, open http://localhost:8501, zoom the browser to 125%.
- Sidebar: **AI reasoning → Replay recorded (no network)**. Everything below is recorded,
  so it runs identically with no Wi-Fi and no API key.
- Click **Review demo** once to warm up, then reload the page.
- Have `report.html` (generated below) and the backup screen recording open in other tabs.

## 0:00 – 0:20 · The problem
> "Most AI code reviewers just paste your code into an LLM. You get a confident list and
> no way to tell which items are real. We built a reviewer where every finding shows its
> evidence, and every fix is proven by running tests."

## 0:20 – 1:00 · Review
Click **Review demo** (a small shop module with seeded bugs and its own test suite).
- Point at the live stages: Ruff, Bandit, Radon and our AST rules run in parallel, then the AI.
- Scores: **58 overall**, security lowest.
- **Confirmed findings**: open the **SQL injection** card (line 19). Badges:
  severity, *tool confirmed*, the exact line, why it matters, the fix. Sources: **Bandit and our
  own AST rule** both caught it and were merged into one finding.
- Scroll to **AI hypotheses**: `paginate` (line 38) and `needs_restock` (line 53).
  > "No linter can see these: the code is valid, it's just wrong. The AI found them, and
  > it had to quote the line and give a failing input, which we check against the real file."

## 1:00 – 2:00 · Fix and verify (the money shot)
Click **Generate & verify fixes** (8 findings preselected).
- Result: **6 proven · 1 verified by rescan · 1 rejected**. Score **58 → 78 if fixed**.
- Open `paginate` → **Verification** tab: the generated test **fails on the original code
  and passes on the patch**. That's proof, not an opinion.
- Open the **shell injection** card: **rejected**.
  > "The model's fix introduced a new insecure /tmp path. Bandit caught it on rescan, it got
  > one retry with that evidence, failed again, so we refused to offer it. We'd rather show
  > you a rejected fix than a wrong one."

## 2:00 – 2:40 · Numbers
Sidebar **View → Benchmark**.
> "32 labeled defects, labels written before we ran anything. Tools alone: 59% recall.
> A plain LLM prompt: 81%. PatchPilot: 91%, at the same precision, and two thirds of
> our hits are backed by a deterministic tool rather than a guess."

## 2:40 – 3:00 · It fits real workflows
Back on the review: download **HTML / SARIF**. Mention the CLI:
`python -m patchpilot review src/ --fail-on high` as a CI gate; AI hypotheses never fail a build.
> "Hybrid analysis, honest evidence, verified fixes."

## Likely questions
- **"Is running fixes safe?"** Subprocess, temp copy, CPU and file-size limits, timeout,
  no API keys in its environment. It is *not* a security sandbox; don't review hostile code.
  Containers are the next step.
- **"Doesn't the AI hallucinate?"** Every claim must quote its line verbatim; mismatches are
  rejected and counted (the "rejected" expander). AI confidence is capped at 0.8 until a test proves it.
- **"Isn't the benchmark biased?"** We wrote it, so yes, it's small and ours. Labels were
  fixed before any run; when the benchmark exposed bugs in our rules we fixed the rules, not
  the labels. It reruns offline with `benchmark/run_eval.py --replay`.
- **"Other languages?"** Python only, by design. We didn't want to claim what we haven't tested.

## If something breaks
1. Replay mode fails → switch to **Off (tools only)**: the deterministic half still works.
2. The app won't start → play the backup recording, then open `report.html`.
3. Regenerate the report: `.venv/bin/python -m patchpilot review demo/shop --ai replay --fix --format html --out report.html --no-history`
