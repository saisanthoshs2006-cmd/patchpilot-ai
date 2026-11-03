"""PatchPilot Streamlit dashboard.  Run:  .venv/bin/streamlit run app.py"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import streamlit as st

from patchpilot import benchmark, config
from patchpilot.ingestion import loader
from patchpilot.models import Category, EvidenceLevel, Review, Severity, VerificationStatus
from patchpilot.orchestrator import default_fix_candidates, fix_and_verify, review
from patchpilot.reporting.export import export
from patchpilot.storage import db
from ui.components.finding_card import render_finding

DEMO_DIR = Path(__file__).parent / "demo" / "shop"
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "groq": "GROQ_API_KEY"}

st.set_page_config(page_title="PatchPilot", page_icon="🛩️", layout="wide")


AI_MODES = {
    "Live (record to cache)": "auto",
    "Replay recorded (no network)": "replay",
    "Off (tools only)": "offline",
}


def ai_mode() -> str:
    return AI_MODES[st.session_state.ai_mode]


def run_review(make_workspace) -> None:
    try:
        ws = make_workspace()
    except loader.IngestionError as exc:
        st.error(str(exc))
        return
    # Keep the workspace alive for the fix step; drop the previous one.
    if old := st.session_state.pop("workspace", None):
        old.cleanup()
    st.session_state.workspace = ws
    with st.status("Reviewing…", expanded=True) as status:
        rev = review(ws, mode=ai_mode(), progress=lambda stage, msg: st.write(msg))
        status.update(label=f"Review complete: {len(rev.findings)} findings", state="complete", expanded=False)
    st.session_state.review = rev
    st.session_state.skipped = ws.skipped
    db.save(rev)


def history_page() -> None:
    st.header("Review history")
    rows = db.list_reviews(limit=100)
    if not rows:
        st.info("No reviews saved yet. Every review (and every fix run) is stored here.")
        return
    st.dataframe(
        [{"When": r.created_at[:19].replace("T", " "), "Source": r.source_name, "AI": r.mode, "LOC": r.loc,
          "Findings": r.n_findings, "Score": round(r.overall), "With fixes": None if r.projected is None else round(r.projected),
          "Proven fixes": r.n_proven, "id": r.id} for r in rows],
        hide_index=True, use_container_width=True,
    )
    sources = sorted({r.source_name for r in rows})
    src = st.selectbox("Score trend for", sources)
    trend = [r for r in reversed(rows) if r.source_name == src]
    if len(trend) > 1:
        st.line_chart({"Score": [r.overall for r in trend]})
    st.markdown("**Rules that keep coming back**")
    st.table([{"Rule": rule, "Occurrences": n, "Reviews": k} for rule, n, k in db.recurring_rules()])
    pick = st.selectbox("Open a past review", [r.id for r in rows],
                        format_func=lambda i: next(f"{r.created_at[:19]} · {r.source_name} · {r.n_findings} findings" for r in rows if r.id == i))
    if st.button("Open"):
        st.session_state.review = db.load(pick)
        st.session_state.pop("workspace", None)  # code isn't stored: fixes need a fresh review
        st.session_state.page = "Review"
        st.rerun()


def benchmark_page() -> None:
    st.header("Benchmark: does the hybrid actually beat an LLM alone?")
    res = benchmark.load_results()
    if res is None:
        st.info("No results yet. Run `.venv/bin/python benchmark/run_eval.py`.")
        return
    st.caption(
        f"{res['files']} Python files with {res['defects']} hand-labeled defects (labels written before any engine ran). "
        f"Model: `{res['model']}` via {res['provider']}. A finding counts as correct only if it lands in a labeled defect's "
        "line span with a matching category; findings in debatable spans are ignored."
    )
    for n, s in res["configs"].items():
        if s["errors"]:
            st.warning(f"{n}: {len(s['errors'])} LLM call(s) failed during this run, so its numbers are understated. "
                       "Rerun the benchmark (successful calls are cached).")
    names = {"tools": "Tools only (Ruff, Bandit, AST, Radon)", "ai_only": "LLM only (plain review prompt)",
             "hybrid": "PatchPilot hybrid"}
    st.dataframe(
        [{"Configuration": names.get(n, n), "Precision": f"{s['precision']:.0%}",
          "Recall": f"{s['recall']:.0%} ({s['found']}/{s['total_defects']})", "F1": round(s["f1"], 2),
          "False positives": s["fp"], "Claims rejected by quote gate": s["rejected_claims"],
          "Precision without the gate": f"{s['raw_precision']:.0%}" if n != "tools" else "—"} for n, s in res["configs"].items()],
        hide_index=True, use_container_width=True,
    )
    fams = sorted({f for s in res["configs"].values() for f in s["recall_by_family"]})
    st.markdown("**Recall by defect family**")
    st.dataframe(
        [{"Family": fam, **{names.get(n, n): "{}/{}".format(*s["recall_by_family"].get(fam, [0, 0]))
                            for n, s in res["configs"].items()}} for fam in fams],
        hide_index=True, use_container_width=True,
    )
    for n, d in res["details"].items():
        with st.expander(f"{names.get(n, n)}: misses and false positives"):
            st.markdown("**Missed:** " + (", ".join(f"`{m}`" for m in d["missed"]) or "none"))
            fps = [j for j in d["judged"] if j["verdict"] == "FP"]
            st.markdown("**False positives:**" + ("" if fps else " none"))
            for j in fps:
                st.markdown(f"- `{j['file']}:{j['line']}` `{j['rule_id']}` {j['title']}")


# --- Sidebar: input -------------------------------------------------------------------
with st.sidebar:
    st.title("🛩️ PatchPilot")
    st.caption("Hybrid code review: static analysis + AST + security + AI, with verified fixes. Python only.")
    page = st.radio("View", ["Review", "History", "Benchmark"], horizontal=True, key="page")
    st.radio("AI reasoning", list(AI_MODES), key="ai_mode")
    st.caption(f"Model: `{config.REASONING_MODEL}` via {config.LLM_PROVIDER}")
    if ai_mode() == "auto" and not os.environ.get(KEY_ENV[config.LLM_PROVIDER]):
        st.caption(f"⚠️ No {KEY_ENV[config.LLM_PROVIDER]} set: only previously recorded reviews will have AI findings.")
    st.divider()
    source = st.radio("Code to review", ["Demo project", "Paste code", "Upload .py / .zip"])
    if source == "Demo project":
        st.caption("A small shop module with seeded bugs and an existing test suite.")
        if st.button("Review demo", type="primary", use_container_width=True):
            run_review(lambda: loader.from_directory(DEMO_DIR))
    elif source == "Paste code":
        code = st.text_area("Python code", height=300, placeholder="def f(x):\n    ...")
        name = st.text_input("File name", "snippet.py")
        if st.button("Review", type="primary", use_container_width=True, disabled=not code.strip()):
            run_review(lambda: loader.from_text(code, name))
    else:
        up = st.file_uploader("Python file or zip archive", type=["py", "zip"])
        if st.button("Review", type="primary", use_container_width=True, disabled=up is None):
            run_review(lambda: loader.from_upload(up.name, up.getvalue()))

if page == "History":
    history_page()
    st.stop()
if page == "Benchmark":
    benchmark_page()
    st.stop()

rev: Review | None = st.session_state.get("review")
if rev is None:
    st.header("Every finding comes with evidence.")
    st.markdown(
        "Pick an input in the sidebar. PatchPilot runs **Ruff**, **Bandit**, **Radon** and its own "
        "**AST rules** in parallel, then asks an AI to find the logic bugs those tools can't see. "
        "Every finding is labeled by how strongly it is backed, and every fix is **verified by "
        "running tests** before it is offered."
    )
    st.stop()

# --- Summary ----------------------------------------------------------------------------
st.subheader(f"Review of `{rev.source_name}`")
st.caption(f"{len(rev.files)} file{'s' if len(rev.files) != 1 else ''} reviewed · {rev.loc} lines of code · {rev.duration_ms} ms · mode: {rev.mode}")
for err in rev.engine_errors:
    st.warning(f"Engine **{err.engine}** failed and was skipped: {err.message}")
if st.session_state.get("skipped"):
    st.info("Not reviewed (unsupported or not Python): " + ", ".join(st.session_state.skipped))

s = rev.scores
projected = rev.metrics.get("projected_overall")
cols = st.columns(6)
cols[0].metric("Overall", f"{s.overall:.0f}",
               delta=f"{projected - s.overall:+.0f} if fixed" if projected is not None else None,
               help="Projected score with every verified fix applied" if projected is not None else None)
for col, (label, value) in zip(cols[1:], [
    ("Security", s.security), ("Reliability", s.reliability), ("Performance", s.performance),
    ("Maintainability", s.maintainability), ("Style", s.style),
], strict=True):
    col.metric(label, f"{value:.0f}")

sev = Counter(f.severity for f in rev.findings)
ev = Counter(f.evidence_level for f in rev.findings)
st.markdown(
    " · ".join(f"**{sev.get(x, 0)}** {x.value}" for x in Severity)
    + "  \n" + " · ".join(f"**{ev.get(x, 0)}** {x.value.replace('_', '-')}" for x in EvidenceLevel)
    + f" · **{len(rev.rejected_claims)}** rejected as invalid"
)
if "ai_claims" in rev.metrics:
    m = rev.metrics
    st.caption(
        f"AI: {int(m['ai_claims'])} claims, {int(m['ai_rejected'])} failed the quote check · "
        f"{int(m.get('llm_api_calls', 0))} API calls, {int(m.get('llm_cache_hits', 0))} cache hits, "
        f"{int(m.get('llm_input_tokens', 0)):,} in / {int(m.get('llm_output_tokens', 0)):,} out tokens"
    )

dl = st.columns(3)
for col, (fmt, mime, label) in zip(dl, [("html", "text/html", "HTML report"), ("json", "application/json", "JSON"),
                                        ("sarif", "application/sarif+json", "SARIF (GitHub code scanning)")], strict=True):
    col.download_button(f"⬇ {label}", export(rev, fmt), file_name=f"patchpilot-{rev.id}.{fmt}", mime=mime,
                        use_container_width=True)

# --- Fixes ------------------------------------------------------------------------------
with st.container(border=True):
    st.markdown("#### 🛠 Generate & verify fixes")
    ws = st.session_state.get("workspace")
    fixable = {f.id: f"{f.location} — {f.title}" for f in rev.findings if f.severity != Severity.INFO}
    already = {v.finding_id for v in rev.verifications}
    defaults = [f.id for f in default_fix_candidates(rev) if f.id not in already]
    chosen = st.multiselect("Findings to fix", list(fixable), default=defaults, format_func=fixable.get)
    if ai_mode() == "offline":
        st.caption("Fix generation needs the AI: switch AI reasoning to Live or Replay.")
    st.caption(
        "Each fix is applied to a throwaway copy, then checked four ways: it parses · the tool no longer flags it "
        "and nothing new appears · a reproduction test fails before and passes after · the project's own tests still pass. "
        "A rejected fix gets one retry with the verifier's evidence."
    )
    if ws is None:
        st.caption("Loaded from history: run the review again to generate fixes.")
    if st.button("Generate & verify fixes", type="primary", disabled=ws is None or not chosen or ai_mode() == "offline"):
        with st.status("Fixing and verifying…", expanded=True) as status:
            rev = fix_and_verify(ws, rev, chosen, progress=lambda stage, msg: st.write(msg), mode=ai_mode())
            status.update(label="Verification complete", state="complete", expanded=False)
        st.session_state.review = rev
        db.save(rev)
        st.rerun()
    if rev.verifications:
        counts = Counter(v.status for v in rev.verifications)
        st.markdown(
            f"✅ **{counts[VerificationStatus.PROVEN]}** proven · ☑️ **{counts[VerificationStatus.STATIC_PASS]}** verified by rescan · "
            f"❔ **{counts[VerificationStatus.INCONCLUSIVE]}** inconclusive · ❌ **{counts[VerificationStatus.FAILED]}** rejected"
        )

# --- Findings ---------------------------------------------------------------------------
f1, f2, f3 = st.columns(3)
sev_filter = f1.multiselect("Severity", [x.value for x in Severity], default=[x.value for x in Severity])
present_cats = sorted({f.category.value for f in rev.findings})
cat_filter = f2.multiselect("Category", [c.value for c in Category if c.value in present_cats], default=present_cats)
file_filter = f3.multiselect("File", rev.files, default=rev.files)

patches = {p.finding_id: p for p in rev.patches}
verifications = {v.finding_id: v for v in rev.verifications}
shown = [
    f for f in rev.findings
    if f.severity.value in sev_filter and f.category.value in cat_filter and f.file in file_filter
]
verified = [f for f in shown if f.evidence_level != EvidenceLevel.AI_HYPOTHESIS]
hypotheses = [f for f in shown if f.evidence_level == EvidenceLevel.AI_HYPOTHESIS]

st.markdown(f"#### Confirmed findings ({len(verified)})")
for f in verified:
    render_finding(f, patches.get(f.id), verifications.get(f.id))
if hypotheses:
    st.markdown(f"#### AI hypotheses — not yet proven ({len(hypotheses)})")
    st.caption("Suspected by the AI only. Treat as leads to investigate, not as facts. Fixing them runs a reproduction test.")
    for f in hypotheses:
        render_finding(f, patches.get(f.id), verifications.get(f.id))
if rev.rejected_claims:
    with st.expander(f"🚫 {len(rev.rejected_claims)} claims rejected by validation (not shown above)"):
        st.caption("Claims whose cited line or quoted code didn't match the real file. This is how fabricated findings are kept out.")
        st.table([c.model_dump() for c in rev.rejected_claims])
