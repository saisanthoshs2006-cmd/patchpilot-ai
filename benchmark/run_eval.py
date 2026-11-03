"""Run the benchmark and write benchmark/results.json.

    .venv/bin/python benchmark/run_eval.py              # live (records to cache)
    .venv/bin/python benchmark/run_eval.py --replay     # from recorded responses only
    .venv/bin/python benchmark/run_eval.py --tools-only # no AI, instant
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from patchpilot import benchmark


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--replay", action="store_true", help="use recorded LLM responses only")
    parser.add_argument("--tools-only", action="store_true", help="skip the AI configurations")
    args = parser.parse_args()

    configs = ("tools",) if args.tools_only else benchmark.CONFIGS
    results = benchmark.run(mode="replay" if args.replay else "auto", configs=configs)
    if not args.tools_only:
        benchmark.RESULTS_PATH.write_text(json.dumps(results, indent=1))
        print(f"\nwrote {benchmark.RESULTS_PATH}")
    print(f"\n{results['files']} files, {results['defects']} labeled defects, model {results['model']}\n")
    print(benchmark.markdown_table(results))
    for name, d in results["details"].items():
        print(f"\n{name} missed: {', '.join(d['missed']) or 'none'}")
        fps = [f"{j['file']}:{j['line']} {j['rule_id']} ({j['title'][:50]})" for j in d["judged"] if j["verdict"] == "FP"]
        print(f"{name} false positives:\n  " + ("\n  ".join(fps) or "none"))
    for name, s in results["configs"].items():
        if s["errors"]:
            print(f"\nWARNING {name}: {len(s['errors'])} LLM call(s) failed, its numbers are incomplete:")
            print("  " + "\n  ".join(e[:160] for e in s["errors"]))


if __name__ == "__main__":
    main()
