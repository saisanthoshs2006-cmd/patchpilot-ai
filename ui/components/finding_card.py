"""Renders one Finding with every required field, plus its fix and verification evidence."""

from __future__ import annotations

import ast

import streamlit as st

from patchpilot.models import (
    EvidenceLevel,
    Finding,
    Patch,
    Severity,
    StepStatus,
    Verification,
    VerificationStatus,
    VerificationStep,
)

SEVERITY_ICON = {
    Severity.CRITICAL: "🟥", Severity.HIGH: "🟧", Severity.MEDIUM: "🟨", Severity.LOW: "🟦", Severity.INFO: "⬜",
}
EVIDENCE_BADGE = {
    EvidenceLevel.PROVEN: ":green-badge[PROVEN]",
    EvidenceLevel.TOOL_CONFIRMED: ":blue-badge[TOOL-CONFIRMED]",
    EvidenceLevel.CORROBORATED: ":orange-badge[CORROBORATED]",
    EvidenceLevel.AI_HYPOTHESIS: ":gray-badge[AI HYPOTHESIS]",
}
FIX_LABEL = {
    VerificationStatus.PROVEN: ("✅", ":green[**Fix proven**: reproduction test red → green, nothing else broke]"),
    VerificationStatus.STATIC_PASS: ("☑️", ":blue[**Fix verified by rescan**: the tool no longer reports it, nothing else broke]"),
    VerificationStatus.INCONCLUSIVE: ("❔", ":orange[**Fix not verified**: checks could not decide; review it by hand]"),
    VerificationStatus.FAILED: ("❌", ":red[**Fix rejected** by verification: not safe to apply]"),
}
STEP_ICON = {
    StepStatus.PASS: "✅", StepStatus.FAIL: "❌", StepStatus.INCONCLUSIVE: "❔", StepStatus.ERROR: "⚠️",
    StepStatus.SKIPPED: "➖",
}


def _function_source(code: str, qualname: str | None) -> str | None:
    if not qualname:
        return None
    name = qualname.split(".")[-1]
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(code, node)
    return None


def _repro_step(v: Verification) -> VerificationStep | None:
    return next((s for s in reversed(v.steps) if s.step == "repro" and s.test_code), None)


def _render_fix(f: Finding, patch: Patch | None, ver: Verification) -> None:
    st.markdown(FIX_LABEL[ver.status][1])
    st.table([{"check": s.step, "result": f"{STEP_ICON[s.status]} {s.status.value}", "evidence": s.detail}
              for s in ver.steps])
    if patch is None:
        return
    if patch.rationale:
        st.caption(f"Fix rationale ({patch.generated_by}): {patch.rationale}")
    before, after = _function_source(patch.original_code, f.function_name), _function_source(
        patch.patched_code, f.function_name)
    tabs = st.tabs(["Before / after", "Diff", "Verification test"])
    with tabs[0]:
        if before and after:
            left, right = st.columns(2)
            left.caption("Before")
            left.code(before, language="python")
            right.caption("After")
            right.code(after, language="python")
        else:
            st.code(patch.diff, language="diff")
    with tabs[1]:
        st.code(patch.diff, language="diff")
    with tabs[2]:
        step = _repro_step(ver)
        if step:
            st.caption(step.detail)
            st.code(step.test_code, language="python")
        else:
            st.caption("No behavioral test was available for this fix.")
    dl1, dl2 = st.columns(2)
    dl1.download_button("Download patched file", patch.patched_code, file_name=patch.file.split("/")[-1],
                        key=f"dl-file-{f.id}", use_container_width=True)
    step = _repro_step(ver)
    if step:
        dl2.download_button("Download test", step.test_code, file_name=f"test_{(f.function_name or 'fix').split('.')[-1]}_fix.py",
                            key=f"dl-test-{f.id}", use_container_width=True)


def render_finding(f: Finding, patch: Patch | None = None, ver: Verification | None = None) -> None:
    fix_icon = f"{FIX_LABEL[ver.status][0]} " if ver else ""
    header = (
        f"{fix_icon}{SEVERITY_ICON[f.severity]} **{f.severity.value.upper()}** · {f.category.value.replace('_', ' ')} · "
        f"`{f.location}` — {f.title}"
    )
    with st.expander(header):
        st.markdown(
            f"{EVIDENCE_BADGE[f.evidence_level]} &nbsp; confidence **{f.confidence:.0%}** &nbsp; "
            f"verification: **{f.verification_status.value.replace('_', ' ')}**"
        )
        if f.code_snippet:
            # st.code numbers from 1; we want the real file line numbers.
            numbered = "\n".join(f"{f.line + i:>4} │ {text}" for i, text in enumerate(f.code_snippet.splitlines()))
            st.code(numbered, language="python")
        left, right = st.columns(2)
        with left:
            st.markdown(f"**What's wrong**  \n{f.explanation}")
            st.markdown(f"**Why it matters**  \n{f.why_it_matters}")
        with right:
            st.markdown(f"**Suggested fix**  \n{f.suggested_fix}")
            where = f"`{f.function_name}()` in " if f.function_name else ""
            st.markdown(f"**Location**  \n{where}`{f.file}`, line {f.line}" + (
                f"–{f.end_line}" if f.end_line and f.end_line != f.line else ""))
        if f.repro:
            proven = f.evidence_level == EvidenceLevel.PROVEN
            st.markdown(
                f"**{'Reproduced' if proven else 'Claimed failing input (not yet proven)'}**  \n"
                f"`{f.repro.call}` → expected {f.repro.expected}; actually {f.repro.actual}"
            )
        st.caption("Evidence from: " + ", ".join(f"`{s}`" for s in f.sources))
        if ver:
            st.divider()
            _render_fix(f, patch, ver)
