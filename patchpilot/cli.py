"""Command line / CI entry point.

    python -m patchpilot review demo/shop
    python -m patchpilot review src/ --ai offline --fail-on high          # CI gate, no network
    python -m patchpilot review app.py --fix --format html --out report.html

Exit codes: 0 = passed, 1 = a confirmed finding at or above --fail-on, 2 = usage/input error.
AI hypotheses never fail the build: only findings backed by a tool, two engines or a test do.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from patchpilot import __version__
from patchpilot.ingestion import loader
from patchpilot.models import EvidenceLevel, Review, Severity
from patchpilot.orchestrator import fix_and_verify, review
from patchpilot.reporting.export import FORMATS, export
from patchpilot.storage import db


def gate_failures(rev: Review, fail_on: str) -> list:
    if fail_on == "never":
        return []
    threshold = Severity(fail_on).rank
    return [f for f in rev.findings
            if f.severity.rank >= threshold and f.evidence_level != EvidenceLevel.AI_HYPOTHESIS]


def _text_report(rev: Review) -> str:
    out = [(f"PatchPilot {__version__} · {rev.source_name} · {len(rev.files)} files, {rev.loc} LOC, "
           f"{rev.duration_ms} ms · AI {rev.mode}"),
           f"Score {rev.scores.overall:.0f}/100"
           + (f" ({rev.metrics['projected_overall']:.0f} with verified fixes)" if "projected_overall" in rev.metrics else ""),
           ""]
    for f in rev.findings:
        tag = "hypothesis" if f.evidence_level == EvidenceLevel.AI_HYPOTHESIS else f.evidence_level.value
        fix = f" [fix {f.verification_status.value}]" if f.verification_status.value != "unverified" else ""
        out.append(f"{f.file}:{f.line}: {f.severity.value.upper():8} {f.rule_id:28} {f.title} ({tag}){fix}")
    sev = Counter(f.severity.value for f in rev.findings)
    out += ["", " · ".join(f"{sev.get(s.value, 0)} {s.value}" for s in Severity)
            + f" · {len(rev.rejected_claims)} AI claims rejected"]
    out += [f"engine {e.engine} failed: {e.message}" for e in rev.engine_errors]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="patchpilot", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("review", help="review a .py file, a directory or a .zip")
    r.add_argument("path", type=Path)
    r.add_argument("--ai", choices=["auto", "replay", "offline"], default="auto",
                   help="auto: live with cache; replay: recorded responses only; offline: tools only")
    r.add_argument("--fix", action="store_true", help="generate and verify fixes (needs AI)")
    r.add_argument("--format", choices=["text", *FORMATS], default="text")
    r.add_argument("--out", type=Path, help="write the report here instead of stdout")
    r.add_argument("--fail-on", choices=[*(s.value for s in Severity), "never"], default="high",
                   help="exit 1 if a confirmed finding is at least this severe (default: high)")
    r.add_argument("--no-history", action="store_true", help="don't save to the review history")
    args = parser.parse_args(argv)

    try:
        if args.path.is_dir():
            ws = loader.from_directory(args.path)
        elif args.path.suffix == ".zip":
            ws = loader.from_upload(args.path.name, args.path.read_bytes())
        else:
            ws = loader.from_text(args.path.read_text(), args.path.name)
    except (OSError, loader.IngestionError) as exc:
        print(f"patchpilot: {exc}", file=sys.stderr)
        return 2

    def progress(stage: str, msg: str) -> None:
        print(f"  {msg}", file=sys.stderr)

    try:
        rev = review(ws, mode=args.ai, progress=progress)
        if args.fix and args.ai != "offline":
            rev = fix_and_verify(ws, rev, progress=progress, mode=args.ai)
    finally:
        ws.cleanup()
    if not args.no_history:
        db.save(rev)

    report = _text_report(rev) if args.format == "text" else export(rev, args.format)
    if args.out:
        args.out.write_text(report)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(report)

    failures = gate_failures(rev, args.fail_on)
    if failures:
        print(f"FAILED: {len(failures)} confirmed finding(s) at or above {args.fail_on}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
