"""AI engine tests with a scripted fake LLM: no network, fully deterministic."""

import json
from pathlib import Path

import pytest

from patchpilot.engines.ai_reasoner import build_chunks
from patchpilot.ingestion import loader
from patchpilot.llm.client import CacheMiss, ClaudeClient
from patchpilot.models import EvidenceLevel
from patchpilot.orchestrator import review

ROOT = Path(__file__).resolve().parent.parent


def claim(line, quote, category="bug", title="t", call="f()"):
    return {
        "function": "f", "line": line, "quoted_code": quote, "category": category, "severity": "high",
        "title": title, "explanation": "e", "why_it_matters": "w", "suggested_fix": "s",
        "failing_input": call, "expected_behavior": "x", "actual_behavior": "y", "confidence": 0.95,
    }


SCRIPTED = [
    # real bug, but cited one line off: must be relocated to 38 by the exact-quote search
    claim(37, "start = page * PAGE_SIZE", title="paginate skips the first page",
          call="paginate(list(range(25)), 1)"),
    # real bug, exact
    claim(53, "return stock > threshold", title="restock condition inverted", call="needs_restock(0, 5)"),
    # same defect a tool already found (same line + category): must merge into the tool finding
    claim(48, "return total / len(orders)", category="runtime_error", title="empty orders crash",
          call="average_order_value([])"),
    # different-category claim on a tool-flagged line: must become CORROBORATED
    claim(97, "except:", title="invalid quantity silently becomes None", call="parse_quantity('x')"),
    # fabricated: this code does not exist anywhere in the file
    claim(40, "return items[start:end + 1]", title="fabricated"),
    # line outside anything we showed the model
    claim(500, "x = 1", title="out of range"),
    # no failing input -> rejected
    claim(65, 'if order["customer"] not in seen:', title="vague", call=""),
]


class FakeLLM:
    model = "fake-model"

    def __init__(self, findings):
        self.findings = findings
        self.prompts = []

    def complete_json(self, system, prompt, schema, tag=""):
        self.prompts.append(prompt)
        return {"findings": self.findings}


@pytest.fixture(scope="module")
def ai_review():
    fake = FakeLLM(SCRIPTED)
    ws = loader.from_directory(ROOT / "demo" / "shop")
    try:
        yield review(ws, mode="auto", llm=fake), fake
    finally:
        ws.cleanup()


def by_title(rev, title):
    return next(f for f in rev.findings if f.title == title)


def test_prompt_has_real_line_numbers_context_and_known_findings(ai_review):
    _, fake = ai_review
    prompt = fake.prompts[0]
    assert "   9 | PAGE_SIZE = 10" in prompt  # module constant included for context
    assert "  38 |     start = page * PAGE_SIZE" in prompt
    assert "[bandit:B608]" in prompt  # deterministic findings passed as context
    assert "test_orders" not in prompt  # tests are not reviewed


def test_quote_gate_rejects_fabrications(ai_review):
    rev, _ = ai_review
    rejected = {c.title: c.reason for c in rev.rejected_claims}
    assert set(rejected) == {"fabricated", "out of range", "vague"}
    assert "does not match" in rejected["fabricated"]
    assert all(f.title not in rejected for f in rev.findings)


def test_line_drift_is_corrected_by_exact_quote(ai_review):
    rev, _ = ai_review
    f = by_title(rev, "paginate skips the first page")
    assert f.line == 38 and f.function_name == "paginate"


def test_ai_only_findings_are_capped_hypotheses_with_repro(ai_review):
    rev, _ = ai_review
    f = by_title(rev, "restock condition inverted")
    assert f.evidence_level == EvidenceLevel.AI_HYPOTHESIS
    assert f.confidence <= 0.8  # 0.95 claimed, capped
    assert f.repro.call == "needs_restock(0, 5)"


def test_ai_agreeing_with_tool_merges_and_keeps_repro(ai_review):
    rev, _ = ai_review
    hits = [f for f in rev.findings if f.line == 48]
    assert len(hits) == 1
    f = hits[0]
    assert f.evidence_level == EvidenceLevel.TOOL_CONFIRMED
    assert {s.split(":")[0] for s in f.sources} == {"ast", "ai"}
    assert f.repro.call == "average_order_value([])"
    assert f.confidence > 0.9


def test_ai_on_tool_flagged_line_is_corroborated(ai_review):
    rev, _ = ai_review
    f = by_title(rev, "invalid quantity silently becomes None")
    assert f.evidence_level == EvidenceLevel.CORROBORATED
    assert "ruff:E722" in f.sources


def test_metrics_report_claims_and_rejections(ai_review):
    rev, _ = ai_review
    assert rev.metrics["ai_claims"] == len(SCRIPTED)
    assert rev.metrics["ai_rejected"] == 3


def test_ai_failure_degrades_gracefully():
    class Broken:
        model = "broken"

        def complete_json(self, *a, **k):
            from patchpilot.llm.client import LLMError
            raise LLMError("No Anthropic credentials found.")

    ws = loader.from_directory(ROOT / "demo" / "shop")
    try:
        rev = review(ws, mode="auto", llm=Broken())
    finally:
        ws.cleanup()
    assert [e.engine for e in rev.engine_errors] == ["ai"]
    assert len(rev.findings) == 14  # all deterministic findings still delivered


def test_chunks_cover_outer_functions_once():
    ws = loader.from_text(
        "X = 1\n\ndef a():\n    def inner():\n        return 1\n    return inner()\n\ndef b():\n    return X\n",
        "m.py",
    )
    try:
        chunks = build_chunks(ws.get("m.py"))
        assert [[u.name for u in c.units] for c in chunks] == [["a", "b"]]
        assert "   1 | X = 1" in chunks[0].numbered_code()
    finally:
        ws.cleanup()


# --- record / replay cache ------------------------------------------------------------

def test_cache_records_then_replays_without_api(tmp_path, monkeypatch):
    calls = []
    client = ClaudeClient(model="m", mode="auto", cache_dir=tmp_path)
    monkeypatch.setattr(client, "_call", lambda *a: calls.append(1) or json.dumps({"findings": []}))
    assert client.complete_json("s", "p", {"type": "object"}, tag="t") == {"findings": []}

    replay = ClaudeClient(model="m", mode="replay", cache_dir=tmp_path)
    monkeypatch.setattr(replay, "_call", lambda *a: pytest.fail("replay must not call the API"))
    assert replay.complete_json("s", "p", {"type": "object"}, tag="t") == {"findings": []}
    assert len(calls) == 1 and replay.stats["cache_hits"] == 1


def test_replay_miss_is_an_error(tmp_path):
    with pytest.raises(CacheMiss):
        ClaudeClient(mode="replay", cache_dir=tmp_path).complete_json("s", "different", {}, tag="t")


def test_cache_key_changes_with_prompt_or_model(tmp_path):
    a = ClaudeClient(model="m1", cache_dir=tmp_path)
    b = ClaudeClient(model="m2", cache_dir=tmp_path)
    assert a._key("s", "p", {}) != a._key("s", "p2", {})
    assert a._key("s", "p", {}) != b._key("s", "p", {})
