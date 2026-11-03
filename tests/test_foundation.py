import io
import json
import zipfile
from pathlib import Path

import pytest

from patchpilot.engines.base import run_engine
from patchpilot.ingestion import loader
from patchpilot.models import Category, EvidenceLevel, Finding, Severity

ROOT = Path(__file__).resolve().parent.parent


def make_finding(**overrides) -> Finding:
    base = {
        "file": "a.py", "line": 3, "severity": Severity.HIGH, "category": Category.BUG, "rule_id": "ast:x",
        "title": "t", "explanation": "e", "why_it_matters": "w", "suggested_fix": "f",
        "confidence": 0.9, "evidence_level": EvidenceLevel.TOOL_CONFIRMED,
    }
    return Finding(**{**base, **overrides})


def _zip(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in entries.items():
            zf.writestr(name, text)
    return buf.getvalue()


# --- models -------------------------------------------------------------------

def test_finding_id_is_stable_and_location_specific():
    assert make_finding().id == make_finding(title="different text").id
    assert make_finding().id != make_finding(line=4).id


def test_finding_rejects_invalid_line_and_confidence():
    with pytest.raises(ValueError):
        make_finding(line=0)
    with pytest.raises(ValueError):
        make_finding(confidence=1.5)


def test_finding_normalizes_windows_paths():
    assert make_finding(file="pkg\\mod.py").file == "pkg/mod.py"


def test_severity_and_evidence_ordering():
    assert Severity.CRITICAL.rank > Severity.HIGH.rank > Severity.INFO.rank
    assert EvidenceLevel.PROVEN.rank > EvidenceLevel.TOOL_CONFIRMED.rank > EvidenceLevel.AI_HYPOTHESIS.rank


# --- ingestion ----------------------------------------------------------------

def test_from_text_writes_workspace():
    ws = loader.from_text("x = 1\n", "demo.py")
    try:
        assert ws.abspath("demo.py").read_text() == "x = 1\n"
        assert ws.reviewable_files[0].path == "demo.py"
    finally:
        ws.cleanup()


def test_syntax_error_file_is_kept_but_not_reviewable():
    ws = loader.from_text("def broken(:\n", "bad.py")
    try:
        f = ws.get("bad.py")
        assert f.syntax_error is not None and not f.reviewable
    finally:
        ws.cleanup()


def test_zip_strips_common_root_and_skips_unsupported():
    ws = loader.from_upload("p.zip", _zip({"proj/src/a.py": "a = 1\n", "proj/README.md": "# hi"}))
    try:
        assert [f.path for f in ws.python_files] == ["src/a.py"]
        assert ws.skipped == ["README.md"]
        assert ws.abspath("README.md").exists()  # copied for tests/fixtures
    finally:
        ws.cleanup()


def test_zip_slip_entries_are_ignored():
    ws = loader.from_upload("p.zip", _zip({"../evil.py": "x=1", "ok.py": "y=2"}))
    try:
        assert [f.path for f in ws.files] == ["ok.py"]
        assert not (ws.root.parent / "evil.py").exists()
    finally:
        ws.cleanup()


def test_non_python_upload_is_rejected_honestly():
    with pytest.raises(loader.IngestionError, match="Python only"):
        loader.from_upload("main.js", b"console.log(1)")


def test_workspace_copy_is_independent():
    ws = loader.from_text("x = 1\n", "m.py")
    clone = ws.copy()
    try:
        clone.write("m.py", "x = 2\n")
        assert ws.abspath("m.py").read_text() == "x = 1\n"
        assert ws.get("m.py").content == "x = 1\n"
        assert clone.get("m.py").content == "x = 2\n"
    finally:
        ws.cleanup()
        clone.cleanup()


def test_demo_project_loads():
    ws = loader.from_directory(ROOT / "demo" / "shop")
    try:
        assert {f.path for f in ws.reviewable_files} == {"orders.py", "tests/test_orders.py"}
    finally:
        ws.cleanup()


def test_demo_labels_point_at_real_lines():
    labels = json.loads((ROOT / "demo" / "shop_labels.json").read_text())
    n_lines = len((ROOT / "demo" / "shop" / labels["file"]).read_text().splitlines())
    assert all(1 <= issue["line"] <= n_lines for issue in labels["issues"])


# --- engine isolation ---------------------------------------------------------

def test_crashing_engine_becomes_engine_error():
    class Boom:
        name = "boom"

        def run(self, workspace):
            raise RuntimeError("kaput")

    result = run_engine(Boom(), workspace=None)
    assert result.findings == [] and result.error.engine == "boom" and "kaput" in result.error.message
