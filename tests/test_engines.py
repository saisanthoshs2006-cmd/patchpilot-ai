import ast
import json
import textwrap
from pathlib import Path

import pytest

from patchpilot.aggregation.aggregator import aggregate
from patchpilot.aggregation.scoring import compute_scores
from patchpilot.engines.ast_rules import RULES
from patchpilot.engines.ast_rules.base import RuleContext
from patchpilot.ingestion import loader
from patchpilot.ingestion.loader import SourceFile
from patchpilot.models import Category, EvidenceLevel, Severity
from patchpilot.orchestrator import review
from tests.test_foundation import make_finding

ROOT = Path(__file__).resolve().parent.parent


def ast_rule_ids(code: str) -> list[tuple[str, int]]:
    """Run every AST rule on a snippet; return (rule_id, line) pairs."""
    code = textwrap.dedent(code)
    ctx = RuleContext(SourceFile("t.py", code, "python"), ast.parse(code))
    return sorted((f.rule_id.removeprefix("ast:"), f.line) for check in RULES for f in check(ctx))


# --- End to end on the demo: measured against ground truth --------------------------

@pytest.fixture(scope="module")
def demo_review():
    ws = loader.from_directory(ROOT / "demo" / "shop")
    try:
        yield review(ws, mode="offline")
    finally:
        ws.cleanup()


def test_demo_has_no_engine_errors(demo_review):
    assert demo_review.engine_errors == []


def test_demo_recall_for_deterministic_engines(demo_review):
    labels = json.loads((ROOT / "demo" / "shop_labels.json").read_text())
    found = {(f.line, f.category.value) for f in demo_review.findings}
    expected = [i for i in labels["issues"] if i["expected_detector"] in ("tool", "ast")]
    missed = [i["id"] for i in expected if (i["line"], i["category"]) not in found]
    assert missed == []


def test_demo_ai_only_bugs_are_not_claimed_by_tools(demo_review):
    """Honesty check: logic bugs only an AI can find must not appear yet (no fake coverage)."""
    labels = json.loads((ROOT / "demo" / "shop_labels.json").read_text())
    ai_lines = {i["line"] for i in labels["issues"] if i["expected_detector"] == "ai"}
    assert not ai_lines & {f.line for f in demo_review.findings}


def test_demo_negative_controls_are_clean(demo_review):
    labels = json.loads((ROOT / "demo" / "shop_labels.json").read_text())
    noisy = [f for f in demo_review.findings if f.function_name in labels["negative_controls"]]
    assert noisy == []


def test_demo_tests_folder_not_reviewed(demo_review):
    assert all(not f.file.startswith("tests/") for f in demo_review.findings)


def test_demo_sql_injection_corroborated_by_two_engines(demo_review):
    sqli = next(f for f in demo_review.findings if f.line == 19)
    assert {s.split(":")[0] for s in sqli.sources} == {"bandit", "ast"}
    assert sqli.severity == Severity.CRITICAL and sqli.confidence > 0.9


def test_every_finding_has_required_fields(demo_review):
    for f in demo_review.findings:
        assert f.file and f.line >= 1 and f.title and f.explanation and f.why_it_matters and f.suggested_fix
        assert f.code_snippet, f"no snippet for {f.rule_id}"


# --- AST rules: positives AND negatives ------------------------------------------------

def test_list_membership_flagged_only_for_lists():
    bad = """
    def f(xs):
        seen = []
        for x in xs:
            if x not in seen:
                seen.append(x)
    """
    good = bad.replace("seen = []", "seen = set()").replace("append", "add")
    assert ("list-membership-in-loop", 5) in ast_rule_ids(bad)
    assert not any(r == "list-membership-in-loop" for r, _ in ast_rule_ids(good))


def test_str_concat_only_for_strings():
    assert ("str-concat-in-loop", 5) in ast_rule_ids("""
    def f(xs):
        out = ""
        for x in xs:
            out += x
        return out
    """)
    assert ast_rule_ids("""
    def f(xs):
        total = 0
        for x in xs:
            total += x
        return total
    """) == []


def test_inconsistent_return():
    assert ("inconsistent-return", 2) in ast_rule_ids("""
    def f(v):
        try:
            return int(v)
        except ValueError:
            pass
    """)
    assert ast_rule_ids("""
    def f(v):
        if v:
            return 1
        else:
            raise ValueError
    def gen():
        yield 1
        return
    def g(v):
        if v:
            return None
        return
    """) == []


def test_unreachable_code():
    assert ("unreachable-code", 4) in ast_rule_ids("""
    def f():
        return 1
        print("never")
    """)


def test_division_by_len_guard_suppresses():
    assert ("division-by-len-unguarded", 3) in ast_rule_ids("""
    def avg(xs):
        return sum(xs) / len(xs)
    """)
    assert ast_rule_ids("""
    def avg(xs):
        if not xs:
            return 0
        return sum(xs) / len(xs)
    """) == []


def test_unchecked_optional_regex_but_not_requests_get():
    assert ("unchecked-optional", 5) in ast_rule_ids("""
    import re
    def f(s):
        m = re.match(r"(\\d+)", s)
        return m.group(1)
    """)
    assert ("unchecked-optional", 3) in ast_rule_ids("""
    def f(s):
        return re.search("x", s).group()
    """)
    assert ast_rule_ids("""
    import re, requests
    def f(s, url):
        m = re.match(r"x", s)
        if m:
            return m.group()
        return requests.get(url).json()
    """) == []


def test_sql_formatting_vs_parameterized():
    assert ("sql-string-to-execute", 3) in ast_rule_ids("""
    def f(cur, name):
        q = "SELECT * FROM t WHERE n = '%s'" % name
        cur.execute(q)
    """)
    assert ast_rule_ids("""
    def f(cur, name):
        cur.execute("SELECT * FROM t WHERE n = ?", (name,))
    """) == []


def test_syntax_error_file_reported(tmp_path):
    ws = loader.from_text("def broken(:\n    pass\n", "bad.py")
    try:
        rev = review(ws, mode="offline")
        assert [f.rule_id for f in rev.findings] == ["ast:syntax-error"]
    finally:
        ws.cleanup()


# --- Aggregator ---------------------------------------------------------------------------

def test_aggregator_drops_fabricated_locations():
    ws = loader.from_text("x = 1\ny = 2\n", "a.py")
    try:
        result = aggregate(ws, [
            make_finding(file="a.py", line=2), make_finding(file="a.py", line=99),
            make_finding(file="ghost.py", line=1),
        ])
        assert [f.line for f in result.findings] == [2]
        assert len(result.dropped) == 2
    finally:
        ws.cleanup()


def test_aggregator_merges_across_engines_but_not_within_one():
    ws = loader.from_text("x = 1\n", "a.py")
    try:
        cross = aggregate(ws, [
            make_finding(line=1, confidence=0.6, sources=["bandit:B1"]),
            make_finding(line=1, confidence=0.6, sources=["ast:r"], rule_id="ast:r"),
        ]).findings
        same = aggregate(ws, [
            make_finding(line=1, confidence=0.6, sources=["ruff:A"]),
            make_finding(line=1, confidence=0.6, sources=["ruff:B"], rule_id="ruff:B"),
        ]).findings
        assert len(cross) == 1 and cross[0].confidence == pytest.approx(0.84)
        assert len(same) == 1 and same[0].confidence == 0.6
    finally:
        ws.cleanup()


# --- Scoring ------------------------------------------------------------------------------

def test_scores_are_deterministic_and_monotonic():
    clean = compute_scores([], loc=100)
    one = compute_scores([make_finding(category=Category.SECURITY, severity=Severity.CRITICAL)], loc=100)
    two = compute_scores([make_finding(category=Category.SECURITY, severity=Severity.CRITICAL)] * 2, loc=100)
    assert clean.overall == 100 and clean.security > one.security > two.security
    assert one == compute_scores([make_finding(category=Category.SECURITY, severity=Severity.CRITICAL)], loc=100)


def test_ai_hypotheses_penalize_less_than_confirmed():
    confirmed = compute_scores([make_finding()], loc=100)
    hypo = compute_scores([make_finding(evidence_level=EvidenceLevel.AI_HYPOTHESIS)], loc=100)
    assert hypo.reliability > confirmed.reliability
