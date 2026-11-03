"""Phase 4: history, reports, CLI gate, benchmark scoring, and the dead-code rule fix."""

import json

from patchpilot import benchmark
from patchpilot.cli import gate_failures, main
from patchpilot.models import Category, EvidenceLevel, Review, Scores, Severity
from patchpilot.reporting.export import to_html, to_json, to_sarif
from patchpilot.storage import db
from tests.test_engines import ast_rule_ids
from tests.test_foundation import make_finding


def sample_review() -> Review:
    return Review(
        id="r1", source_name="shop", files=["a.py"], loc=40, mode="offline",
        findings=[
            make_finding(line=3, title="SQL <injection>", severity=Severity.CRITICAL, category=Category.SECURITY,
                         rule_id="bandit:B608"),
            make_finding(line=9, title="Off by one", severity=Severity.HIGH, rule_id="ai:bug",
                         evidence_level=EvidenceLevel.AI_HYPOTHESIS, confidence=0.7),
        ],
        scores=Scores(overall=61.5), metrics={"projected_overall": 80.0},
    )


# --- History --------------------------------------------------------------------------

def test_db_roundtrip_and_resave(tmp_path):
    path = tmp_path / "h.sqlite3"
    rev = sample_review()
    db.save(rev, path)
    db.save(rev, path)  # saving again (after fixes) replaces, never duplicates
    rows = db.list_reviews(path=path)
    assert len(rows) == 1 and rows[0].n_findings == 2 and rows[0].projected == 80.0
    assert db.load("r1", path) == rev
    assert db.load("missing", path) is None
    assert {r[0] for r in db.recurring_rules(path=path)} == {"bandit:B608", "ai:bug"}


# --- Reports --------------------------------------------------------------------------

def test_json_report_is_lossless():
    rev = sample_review()
    assert Review.model_validate_json(to_json(rev)) == rev


def test_sarif_shape_and_hypotheses_never_errors():
    sarif = json.loads(to_sarif(sample_review()))
    assert sarif["version"] == "2.1.0"
    results = sarif["runs"][0]["results"]
    by_rule = {r["ruleId"]: r for r in results}
    assert by_rule["bandit:B608"]["level"] == "error"
    assert by_rule["ai:bug"]["level"] == "note"  # unproven AI claim stays a note even at high severity
    loc = by_rule["bandit:B608"]["locations"][0]["physicalLocation"]
    assert loc == {"artifactLocation": {"uri": "a.py"}, "region": {"startLine": 3}}
    assert by_rule["ai:bug"]["properties"]["evidence"] == "ai_hypothesis"


def test_html_report_escapes_and_separates_hypotheses():
    html = to_html(sample_review())
    assert "SQL &lt;injection&gt;" in html and "<injection>" not in html
    assert html.index("Confirmed findings (1)") < html.index("SQL &lt;injection") < html.index("AI hypotheses")
    assert "a.py:9" in html and "80 with verified fixes" in html


# --- CLI gate -------------------------------------------------------------------------

def test_gate_ignores_ai_hypotheses():
    rev = sample_review()
    assert [f.rule_id for f in gate_failures(rev, "high")] == ["bandit:B608"]
    assert gate_failures(rev, "never") == []
    hypothesis_only = rev.model_copy(update={"findings": rev.findings[1:]})
    assert gate_failures(hypothesis_only, "low") == []


def test_cli_exit_codes(tmp_path, capsys):
    clean = tmp_path / "clean.py"
    clean.write_text("def add(a: int, b: int) -> int:\n    return a + b\n")
    bad = tmp_path / "bad.py"
    bad.write_text("import subprocess\n\n\ndef run(cmd):\n    return subprocess.call(cmd, shell=True)\n")
    common = ["--ai", "offline", "--no-history"]
    assert main(["review", str(clean), *common]) == 0
    assert main(["review", str(bad), *common, "--fail-on", "high"]) == 1
    assert main(["review", str(bad), *common, "--fail-on", "never"]) == 0
    assert main(["review", str(tmp_path / "nope.py"), *common]) == 2
    out = tmp_path / "r.sarif"
    assert main(["review", str(bad), *common, "--format", "sarif", "--out", str(out), "--fail-on", "never"]) == 0
    assert json.loads(out.read_text())["runs"][0]["results"]


# --- Benchmark scoring ----------------------------------------------------------------

CASE = {
    "defects": [
        {"id": "returns-none", "lines": [10, 14], "family": "correctness"},
        {"id": "bare-except", "lines": [13, 14], "family": "quality"},
        {"id": "sqli", "lines": [20, 20], "family": "security"},
    ],
    "ignore": [[30, 31]],
}


def judged(*findings):
    r = benchmark.ConfigResult("t")
    benchmark.judge(r, "x.py", list(findings), CASE)
    return r


def test_judge_prefers_most_specific_defect():
    r = judged(make_finding(line=13, category=Category.CODE_SMELL, severity=Severity.MEDIUM))
    assert r.judged[0].verdict == "bare-except"  # exact family beats the wider overlapping span
    # A bug-category finding on the same line credits the bug defect, never the code-smell one.
    r = judged(make_finding(line=13, severity=Severity.MEDIUM), make_finding(line=14, severity=Severity.MEDIUM))
    assert [j.verdict for j in r.judged] == ["returns-none", "returns-none"] and r.found == 1


def test_judge_counts_fp_ignore_and_family_mismatch():
    r = judged(
        make_finding(line=20, category=Category.PERFORMANCE),  # right line, wrong family -> FP
        make_finding(line=30),                                 # ignore span -> neither
        make_finding(line=20, category=Category.SECURITY),     # TP
        make_finding(line=50, severity=Severity.INFO),         # info -> excluded
    )
    assert (r.tp, r.fp, r.found, r.total_defects) == (1, 1, 1, 3)
    assert [j.verdict for j in r.judged] == ["FP", "ignored", "sqli"]
    assert r.precision == 0.5 and round(r.recall, 3) == 0.333


# --- Dead code after an if/else that always returns (found by the benchmark) ---------

def test_dead_code_after_exhaustive_if_else():
    rules = ast_rule_ids("""
        def is_adult(age):
            if age >= 18:
                return True
            else:
                return False
            print("checked")
    """)
    assert ("unreachable-code", 7) in rules
    assert not any(r == "inconsistent-return" for r, _ in rules)
