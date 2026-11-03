"""Phase 3: patch application, reproduction tests, sandbox, and end-to-end verification."""

import textwrap
from pathlib import Path

import pytest

from patchpilot.ingestion import loader
from patchpilot.ingestion.loader import SourceFile
from patchpilot.models import (
    Category,
    EvidenceLevel,
    Finding,
    ReproHint,
    Review,
    Severity,
    StepStatus,
    VerificationStatus,
)
from patchpilot.orchestrator import fix_and_verify
from patchpilot.patching.generator import PatchError, apply_replacement, module_name, target_span
from patchpilot.verification.repro import build_repro_test
from patchpilot.verification.sandbox import run_pytest

ROOT = Path(__file__).resolve().parent.parent


def sf(code: str, path: str = "m.py") -> SourceFile:
    return SourceFile(path, textwrap.dedent(code), "python")


# --- patch application ------------------------------------------------------------------

def test_replacement_is_reindented_and_spliced_by_span():
    src = sf("""\
    import os


    class A:
        def f(self):
            return 1

        def g(self):
            return 2
    """)
    span = target_span(src, 6)
    assert span == (5, 6)
    out = apply_replacement(src, span, "def f(self):\n    return 42\n", [])
    assert "        return 42" in out and "return 2" in out and "return 1" not in out


def test_new_imports_go_after_existing_and_are_deduplicated():
    src = sf('"""doc"""\nimport os\n\ndef f():\n    return 1\n')
    out = apply_replacement(src, (4, 5), "def f():\n    return json.dumps(1)\n", ["import json", "import os"])
    assert out.splitlines()[:3] == ['"""doc"""', "import os", "import json"]


def test_markdown_fences_are_stripped():
    src = sf("def f():\n    return 1\n")
    assert "```" not in apply_replacement(src, (1, 2), "```python\ndef f():\n    return 2\n```", [])


def test_unparseable_patch_is_rejected():
    with pytest.raises(PatchError):
        apply_replacement(sf("def f():\n    return 1\n"), (1, 2), "def f(:\n    return 2\n", [])


def test_module_name():
    assert module_name("orders.py") == "orders"
    assert module_name("pkg/sub/mod.py") == "pkg.sub.mod"
    assert module_name("pkg/__init__.py") == "pkg"


# --- deterministic reproduction tests -----------------------------------------------------

def test_repro_test_qualifies_module_names_but_not_builtins():
    src = sf("PAGE = 10\n\ndef paginate(items, page):\n    return items\n")
    code = build_repro_test(src, "orders", "paginate(list(range(PAGE)), 1)", "[0, 1]", "paginate")
    assert "module_under_test.paginate(list(range(module_under_test.PAGE)), 1) == [0, 1]" in code


def test_repro_float_uses_approx_and_prose_is_refused():
    src = sf("def avg(x):\n    return 0\n")
    assert "pytest.approx(2.5)" in build_repro_test(src, "m", "avg([2, 3])", "2.5", "avg")
    assert build_repro_test(src, "m", "avg([])", "returns zero", "avg") is None
    assert build_repro_test(src, "m", "import os", "1", "avg") is None


# --- sandbox -------------------------------------------------------------------------------

def test_sandbox_kills_infinite_loops(tmp_path):
    (tmp_path / "test_loop.py").write_text("def test_spin():\n    while True:\n        pass\n")
    run = run_pytest(tmp_path, ["test_loop.py"], timeout=3)
    assert run.timed_out and not run.all_passed


def test_sandbox_does_not_leak_api_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "secret-value")
    (tmp_path / "test_env.py").write_text(
        "import os\n\ndef test_no_keys():\n    assert not any('API_KEY' in k for k in os.environ)\n")
    assert run_pytest(tmp_path, ["test_env.py"]).all_passed


# --- end-to-end verification with a scripted model --------------------------------------

GOOD_PAGINATE = '''\
def paginate(items, page):
    """Return the items shown on a 1-indexed page (page=1 is the first page)."""
    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    return items[start:end]
'''
WRONG_PAGINATE = GOOD_PAGINATE.replace("(page - 1) * PAGE_SIZE", "page * PAGE_SIZE  # reviewed")
GOOD_UNIQUE = '''\
def unique_customers(orders):
    seen = set()
    result = []
    for order in orders:
        if order["customer"] not in seen:
            seen.add(order["customer"])
            result.append(order["customer"])
    return result
'''
ORDER_BREAKING_UNIQUE = 'def unique_customers(orders):\n    return sorted({o["customer"] for o in orders})\n'
INSECURE_ADD_TAG = '''\
def add_tag(order, tag, tags=None):
    if tags is None:
        tags = []
    tags.append(tag)
    order["tags"] = tags
    order["sig"] = hashlib.md5(tag.encode()).hexdigest()
    return order
'''


class ScriptedPatcher:
    """Answers patch requests by function name, one scripted reply per attempt."""

    model = "scripted"

    def __init__(self, script: dict[str, list[str]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.prompts: list[str] = []

    def complete_json(self, system, prompt, schema, tag=""):
        self.prompts.append(prompt)
        name = next(n for n in self.script if f"def {n}(" in prompt)
        return {"replacement_code": self.script[name].pop(0), "new_imports": [], "rationale": "r", "test_code": ""}


def finding(line, fn, rule, sources, category=Category.BUG, repro=None, evidence=EvidenceLevel.TOOL_CONFIRMED):
    return Finding(
        file="orders.py", line=line, severity=Severity.HIGH, category=category, rule_id=rule, title=f"{fn} issue",
        explanation="e", why_it_matters="w", suggested_fix="s", confidence=0.8, evidence_level=evidence,
        sources=sources, function_name=fn, repro=repro,
    )


PAGINATE = finding(38, "paginate", "ai:bug", ["ai:fake"], evidence=EvidenceLevel.AI_HYPOTHESIS,
                   repro=ReproHint(call="paginate(list(range(1, 13)), 1)", expected="[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]",
                                   actual="[11, 12]"))
UNIQUE = finding(65, "unique_customers", "ast:list-membership-in-loop", ["ast:list-membership-in-loop"],
                 category=Category.PERFORMANCE)
ADD_TAG = finding(56, "add_tag", "ruff:B006", ["ruff:B006"])


@pytest.fixture
def demo_ws():
    ws = loader.from_directory(ROOT / "demo" / "shop")
    yield ws
    ws.cleanup()


def run_fix(ws, f, script):
    llm = ScriptedPatcher(script)
    rev = fix_and_verify(ws, Review(source_name="shop", findings=[f], loc=ws.loc), [f.id], llm=llm)
    return rev.findings[0], rev.verifications[0], llm


def steps(ver):
    return {s.step: s.status for s in ver.steps}


def test_correct_fix_for_ai_bug_is_proven_red_to_green(demo_ws):
    f, ver, _ = run_fix(demo_ws, PAGINATE, {"paginate": [GOOD_PAGINATE]})
    assert ver.status == VerificationStatus.PROVEN
    assert f.evidence_level == EvidenceLevel.PROVEN  # the AI hypothesis is now proven
    assert steps(ver)["repro"] == StepStatus.PASS and steps(ver)["regression"] == StepStatus.PASS
    repro = next(s for s in ver.steps if s.step == "repro")
    assert "from the reported failing input" in repro.detail and "red on original (AssertionError)" in repro.detail


def test_cosmetic_non_fix_is_rejected_then_repaired_with_feedback(demo_ws):
    _, ver, llm = run_fix(demo_ws, PAGINATE, {"paginate": [WRONG_PAGINATE, GOOD_PAGINATE]})
    assert ver.status == VerificationStatus.PROVEN
    assert ver.steps[0].step == "repair" and "attempt 1 rejected" in ver.steps[0].detail
    assert "REJECTED by automated verification" in llm.prompts[1]  # evidence fed back to the model


def test_fix_that_breaks_project_tests_is_rejected(demo_ws):
    _, ver, _ = run_fix(demo_ws, UNIQUE, {"unique_customers": [ORDER_BREAKING_UNIQUE, ORDER_BREAKING_UNIQUE]})
    assert ver.status == VerificationStatus.FAILED
    assert steps(ver)["regression"] == StepStatus.FAIL


def test_good_perf_fix_without_repro_is_statically_verified(demo_ws):
    _, ver, _ = run_fix(demo_ws, UNIQUE, {"unique_customers": [GOOD_UNIQUE]})
    assert ver.status == VerificationStatus.STATIC_PASS
    assert steps(ver)["repro"] == StepStatus.SKIPPED


def test_fix_that_introduces_a_vulnerability_is_rejected(demo_ws):
    _, ver, _ = run_fix(demo_ws, ADD_TAG, {"add_tag": [INSECURE_ADD_TAG, INSECURE_ADD_TAG]})
    assert ver.status == VerificationStatus.FAILED
    static = next(s for s in reversed(ver.steps) if s.step == "static_rescan")
    assert "bandit:B324" in static.detail
