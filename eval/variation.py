"""Issue #5: how much do benchmark results vary between identical runs?

Runs the full benchmark (retrieval + end-to-end answers) several times and reports the
mean and range of every metric, plus the questions whose outcome changed between runs.

    uv run python eval/variation.py            # 3 runs (default)
    uv run python eval/variation.py --runs 5

Writes results/variation.json and results/variation.md.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from course_assistant import get_library, setup_logging  # noqa: E402
from run_eval import eval_end_to_end, eval_retrieval, load_questions  # noqa: E402

RETRIEVAL_KEYS = ("hit@1", "hit@3", "mrr")
E2E_KEYS = ("answered_correct_source", "visual_answered_correct_source", "answerable_marked_not_found",
            "abstained_correctly", "false_answers", "partially_supported", "median_seconds")


def summarize(values: list[float]) -> dict:
    return {"mean": round(statistics.mean(values), 3), "min": round(min(values), 3),
            "max": round(max(values), 3), "runs": [round(v, 3) for v in values]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    setup_logging()
    lib = get_library()
    questions = load_questions()

    runs = []
    for i in range(args.runs):
        t = time.time()
        print(f"=== run {i + 1}/{args.runs}")
        runs.append({"retrieval": eval_retrieval(lib, questions), "end_to_end": eval_end_to_end(lib, questions),
                     "seconds": round(time.time() - t)})

    report: dict = {"timestamp": time.strftime("%Y-%m-%d %H:%M"), "n_runs": args.runs,
                    "n_questions": len(questions), "retrieval": {}, "end_to_end": {}, "unstable_questions": {}}
    for mode in runs[0]["retrieval"]:
        report["retrieval"][mode] = {k: summarize([r["retrieval"][mode]["all"][k] for r in runs])
                                     for k in RETRIEVAL_KEYS}
    for mode in runs[0]["end_to_end"]:
        report["end_to_end"][mode] = {k: summarize([r["end_to_end"][mode][k] for r in runs])
                                      for k in E2E_KEYS if runs[0]["end_to_end"][mode][k] is not None}
        # questions whose status or "cited a correct slide" changed between runs
        changed = []
        for qi, q in enumerate(runs[0]["end_to_end"][mode]["per_question"]):
            outcomes = [(r["end_to_end"][mode]["per_question"][qi]["status"],
                         r["end_to_end"][mode]["per_question"][qi]["cites_gold"]) for r in runs]
            if len(set(outcomes)) > 1:
                changed.append({"id": q["id"], "type": q["type"],
                                "outcomes": [f"{s}{' +gold' if g else ''}" for s, g in outcomes]})
        report["end_to_end"][mode]["unstable_questions"] = changed

    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    (out / "variation.json").write_text(json.dumps(report, indent=1))

    fmt = lambda s: f"{s['mean']:.2f} ({s['min']:.2f}–{s['max']:.2f})"  # noqa: E731
    md = [f"# Run-to-run variation ({args.runs} runs, {len(questions)} questions, {report['timestamp']})", "",
          "Mean (min–max) across identical runs.", "", "## Retrieval", "",
          "| Mode | hit@1 | hit@3 | MRR |", "|---|---|---|---|"]
    for mode, m in report["retrieval"].items():
        md.append(f"| {mode} | {fmt(m['hit@1'])} | {fmt(m['hit@3'])} | {fmt(m['mrr'])} |")
    md += ["", "## End-to-end", "",
           "| Mode | answered + correct slide | visual: same | answerable marked not found | unanswerable refused "
           "| false answers | partly supported | median s |", "|---|---|---|---|---|---|---|---|"]
    for mode, m in report["end_to_end"].items():
        g = lambda k: fmt(m[k]) if k in m else "n/a"  # noqa: E731
        md.append(f"| {mode} | {g('answered_correct_source')} | {g('visual_answered_correct_source')} "
                  f"| {g('answerable_marked_not_found')} | {g('abstained_correctly')} | {g('false_answers')} "
                  f"| {g('partially_supported')} | {g('median_seconds')} |")
    md += ["", "## Questions whose outcome changed between runs", ""]
    for mode, m in report["end_to_end"].items():
        if not m["unstable_questions"]:
            md.append(f"- **{mode}:** none")
        for u in m["unstable_questions"]:
            md.append(f"- **{mode} {u['id']}** ({u['type']}): " + " → ".join(u["outcomes"]))
    (out / "variation.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
