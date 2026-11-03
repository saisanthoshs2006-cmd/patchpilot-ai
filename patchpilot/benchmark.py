"""Measure PatchPilot against hand-labeled ground truth (benchmark/cases + benchmark/labels.json).

Three configurations, same files, same scoring:
- tools:   Ruff + Bandit + AST rules + Radon, no AI
- ai_only: a plain "review this code" LLM prompt, no tools (the baseline most teams ship).
           Its claims still pass through the quote gate; `raw_precision` also counts the
           claims the gate rejected, i.e. what you'd show without validation.
- hybrid:  the full PatchPilot pipeline (tools, then AI told what the tools found)

Scoring: a finding is a true positive if its line falls in a labeled defect's span and its
category family is accepted for that defect. Findings in an "ignore" span (debatable
issues) count as neither. Info-severity notes are excluded. Recall counts labeled defects
found at least once.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field

from patchpilot import config
from patchpilot.aggregation.aggregator import aggregate
from patchpilot.engines.ai_reasoner import AiReasonerEngine
from patchpilot.ingestion import loader
from patchpilot.llm.client import JsonLLM, make_client
from patchpilot.llm.prompts import GENERALIST_SYSTEM
from patchpilot.models import Category, Finding, Severity
from patchpilot.orchestrator import review

BENCH_DIR = config.PROJECT_ROOT / "benchmark"
RESULTS_PATH = BENCH_DIR / "results.json"
CONFIGS = ("tools", "ai_only", "hybrid")

FAMILY = {
    Category.BUG: "correctness", Category.RUNTIME_ERROR: "correctness", Category.SECURITY: "security",
    Category.PERFORMANCE: "performance", Category.CODE_SMELL: "quality", Category.MAINTAINABILITY: "quality",
    Category.BEST_PRACTICE: "quality", Category.STYLE: "quality",
}
_NEIGHBOURS = {"correctness": {"quality"}, "quality": {"correctness"}}


def accepted_families(defect: dict) -> set[str]:
    if "accept" in defect:
        return set(defect["accept"])
    return {defect["family"], *_NEIGHBOURS.get(defect["family"], set())}


@dataclass
class Judged:
    file: str
    line: int
    rule_id: str
    title: str
    verdict: str  # defect id, "FP" or "ignored"


@dataclass
class ConfigResult:
    name: str
    tp: int = 0
    fp: int = 0
    found: int = 0
    total_defects: int = 0
    rejected_claims: int = 0
    duration_ms: int = 0
    tokens: int = 0
    errors: list[str] = field(default_factory=list)
    recall_by_family: dict[str, list[int]] = field(default_factory=dict)  # family -> [found, total]
    judged: list[Judged] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def raw_precision(self) -> float:
        n = self.tp + self.fp + self.rejected_claims
        return self.tp / n if n else 0.0

    @property
    def recall(self) -> float:
        return self.found / self.total_defects if self.total_defects else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def summary(self) -> dict:
        return {
            "precision": round(self.precision, 3), "recall": round(self.recall, 3), "f1": round(self.f1, 3),
            "raw_precision": round(self.raw_precision, 3), "tp": self.tp, "fp": self.fp,
            "found": self.found, "total_defects": self.total_defects, "rejected_claims": self.rejected_claims,
            "duration_ms": self.duration_ms, "tokens": self.tokens, "errors": self.errors,
            "recall_by_family": self.recall_by_family,
        }


def judge(result: ConfigResult, file: str, findings: list[Finding], case: dict) -> None:
    defects = case["defects"]
    hit: set[str] = set()
    for f in findings:
        if f.severity == Severity.INFO:
            continue
        fam = FAMILY[f.category]
        candidates = [d for d in defects if d["lines"][0] <= f.line <= d["lines"][1] and fam in accepted_families(d)]
        # Most specific defect first: exact family, then not yet found, then narrowest span.
        candidates.sort(key=lambda d: (d["family"] != fam, d["id"] in hit, d["lines"][1] - d["lines"][0]))
        match = candidates[0] if candidates else None
        if match:
            result.tp += 1
            hit.add(match["id"])
            verdict = match["id"]
        elif any(a <= f.line <= b for a, b in case.get("ignore", [])):
            verdict = "ignored"
        else:
            result.fp += 1
            verdict = "FP"
        result.judged.append(Judged(file, f.line, f.rule_id, f.title, verdict))
    result.total_defects += len(defects)
    result.found += len(hit)
    for d in defects:
        found_total = result.recall_by_family.setdefault(d["family"], [0, 0])
        found_total[1] += 1
        if d["id"] in hit:
            found_total[0] += 1
        else:
            result.missed.append(f"{file}:{d['id']}")


def _ai_only(ws, llm: JsonLLM) -> tuple[list[Finding], int, list[str]]:
    engine = AiReasonerEngine(llm, known=[], system=GENERALIST_SYSTEM, require_failing_input=False)
    try:
        raw = engine.run(ws)
    except Exception as exc:  # noqa: BLE001 - recorded, the benchmark continues
        return [], 0, [str(exc)]
    agg = aggregate(ws, raw)
    return agg.findings, len(engine.rejected) + len(agg.dropped), engine.partial_errors


def run(mode: str = "auto", configs: tuple[str, ...] = CONFIGS, progress=print) -> dict:
    labels = json.loads((BENCH_DIR / "labels.json").read_text())["cases"]
    results = {name: ConfigResult(name) for name in configs}
    llm = make_client(mode=mode) if {"ai_only", "hybrid"} & set(configs) else None
    for file, case in labels.items():
        code = (BENCH_DIR / "cases" / file).read_text()
        for name in configs:
            ws = loader.from_text(code, file)
            tokens_before = sum(getattr(llm, "stats", {}).get(k, 0) for k in ("input_tokens", "output_tokens"))
            start = time.perf_counter()
            try:
                if name == "tools":
                    findings, rejected, errors = review(ws, mode="offline").findings, 0, []
                elif name == "ai_only":
                    findings, rejected, errors = _ai_only(ws, llm)
                else:
                    rev = review(ws, mode=mode, llm=llm)
                    findings = rev.findings
                    rejected = len(rev.rejected_claims)
                    errors = [e.message for e in rev.engine_errors]
            finally:
                ws.cleanup()
            r = results[name]
            r.duration_ms += int((time.perf_counter() - start) * 1000)
            r.tokens += sum(getattr(llm, "stats", {}).get(k, 0) for k in ("input_tokens", "output_tokens")) - tokens_before
            r.rejected_claims += rejected
            r.errors += [f"{file}: {e}" for e in errors]
            judge(r, file, findings, case)
            progress(f"{file:14} {name:8} tp={r.tp:<3} fp={r.fp:<3} found={r.found}/{r.total_defects}")
    out = {
        "model": getattr(llm, "model", None), "provider": config.LLM_PROVIDER, "mode": mode,
        "files": len(labels), "defects": sum(len(c["defects"]) for c in labels.values()),
        "configs": {n: r.summary() for n, r in results.items()},
        "details": {n: {"judged": [asdict(j) for j in r.judged], "missed": r.missed} for n, r in results.items()},
    }
    return out


def load_results() -> dict | None:
    return json.loads(RESULTS_PATH.read_text()) if RESULTS_PATH.exists() else None


def markdown_table(results: dict) -> str:
    rows = ["| Config | Precision | Recall | F1 | False positives | Rejected claims | Time |",
            "|---|---|---|---|---|---|---|"]
    for name, s in results["configs"].items():
        rows.append(
            f"| {name} | {s['precision']:.0%} | {s['recall']:.0%} ({s['found']}/{s['total_defects']}) | "
            f"{s['f1']:.2f} | {s['fp']} | {s['rejected_claims']} | {s['duration_ms'] / 1000:.1f}s |"
        )
    return "\n".join(rows)
