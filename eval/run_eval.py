"""Benchmark: compare retrieval designs and check end-to-end answers.

    uv run python eval/run_eval.py                 # retrieval + end-to-end
    uv run python eval/run_eval.py --retrieval-only

Questions live in eval/questions.jsonl. Each names the document (a substring such as
"Week 5") and the slide numbers that answer it; `type` is `text` (answer is in the slide
text), `visual` (answer is only in a diagram/screenshot/image) or `unanswerable`.

Retrieval metrics (answerable questions only), per mode:
  hit@k   - a gold slide is among the top-k evidence pages
  MRR     - mean of 1/rank of the first gold slide (0 if absent)
End-to-end metrics, for the hybrid design and the text-vector-only baseline:
  answered_correct_source - answered AND at least one shown source is a gold slide
  abstained_correctly     - unanswerable question AND the app said it couldn't find it
  false_answers           - unanswerable question that got an answer anyway
Results go to results/eval_results.json and results/summary.md.
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

from course_assistant import get_library, setup_logging  # noqa: E402
from course_assistant.answer import answer_question  # noqa: E402
from course_assistant.retrieval import MODES, retrieve  # noqa: E402

E2E_MODES = ("hybrid", "text_vector")


def load_questions() -> list[dict]:
    return [json.loads(l) for l in (ROOT / "eval" / "questions.jsonl").read_text().splitlines() if l.strip()]


def gold_ids(lib, q: dict) -> set[str]:
    if q["type"] == "unanswerable":
        return set()
    matches = [d for d in lib.docs.values() if q["doc"] in d.name]
    if len(matches) != 1:
        raise SystemExit(f"{q['id']}: doc '{q['doc']}' matches {len(matches)} documents")
    return {f"{matches[0].doc_id}:{n}" for n in q["slides"]}


def page_ranking(evidence) -> list[str]:
    seen: list[str] = []
    for e in evidence:
        if e.page_id not in seen:
            seen.append(e.page_id)
    return seen


def eval_retrieval(lib, questions) -> dict:
    out = {}
    answerable = [q for q in questions if q["type"] != "unanswerable"]
    for mode in MODES:
        rows = []
        for q in answerable:
            gold = gold_ids(lib, q)
            ranked = page_ranking(retrieve(lib, q["question"], mode=mode))
            rank = next((i + 1 for i, p in enumerate(ranked) if p in gold), None)
            rows.append({"id": q["id"], "type": q["type"], "rank": rank})

        def summarize(rs):
            n = len(rs) or 1
            return {"n": len(rs),
                    "hit@1": sum(1 for r in rs if r["rank"] == 1) / n,
                    "hit@3": sum(1 for r in rs if r["rank"] and r["rank"] <= 3) / n,
                    "hit@8": sum(1 for r in rs if r["rank"]) / n,
                    "mrr": sum(1 / r["rank"] for r in rs if r["rank"]) / n}

        out[mode] = {"all": summarize(rows),
                     "text": summarize([r for r in rows if r["type"] == "text"]),
                     "visual": summarize([r for r in rows if r["type"] == "visual"]),
                     "per_question": rows}
        print(f"retrieval {mode:17s} hit@3={out[mode]['all']['hit@3']:.2f} "
              f"visual hit@3={out[mode]['visual']['hit@3']:.2f} mrr={out[mode]['all']['mrr']:.2f}")
    return out


def eval_end_to_end(lib, questions) -> dict:
    out = {}
    for mode in E2E_MODES:
        rows = []
        for q in questions:
            gold = gold_ids(lib, q)
            t = time.time()
            r = answer_question(lib, q["question"], mode=mode)
            rows.append({
                "id": q["id"], "type": q["type"], "status": r.status, "seconds": round(time.time() - t, 1),
                "sources": [f"{s.doc_name} | {s.location}" for s in r.sources],
                "cites_gold": any(s.page_id in gold for s in r.sources),
                "dropped_citations": sum(1 for n in r.notes if n.startswith("Dropped")),
                "answer": r.answer,
            })
            print(f"  {mode} {q['id']}: {r.status}{' (gold cited)' if rows[-1]['cites_gold'] else ''}")
        ans = [r for r in rows if r["type"] != "unanswerable"]
        una = [r for r in rows if r["type"] == "unanswerable"]
        vis = [r for r in ans if r["type"] == "visual"]
        answered = lambda r: r["status"] in ("answered", "partially_supported")  # noqa: E731
        out[mode] = {
            "answered_correct_source": sum(1 for r in ans if answered(r) and r["cites_gold"]) / len(ans),
            "visual_answered_correct_source": (sum(1 for r in vis if answered(r) and r["cites_gold"]) / len(vis))
            if vis else None,
            "answerable_marked_not_found": sum(1 for r in ans if r["status"] == "not_found") / len(ans),
            "partially_supported": sum(1 for r in ans if r["status"] == "partially_supported"),
            "abstained_correctly": (sum(1 for r in una if r["status"] == "not_found") / len(una)) if una else None,
            "false_answers": sum(1 for r in una if answered(r)),
            "dropped_citations_total": sum(r["dropped_citations"] for r in rows),
            "median_seconds": statistics.median(r["seconds"] for r in rows),
            "per_question": rows,
        }
        m = out[mode]
        print(f"e2e {mode}: correct-source answers={m['answered_correct_source']:.2f} "
              f"abstained={m['abstained_correctly']} false_answers={m['false_answers']}")
    return out


def write_summary(results: dict) -> str:
    lines = ["# Evaluation summary", "",
             f"Generated {results['timestamp']} on {results['n_questions']} questions "
             f"({results['counts']}).", "", "## Retrieval (answerable questions)", "",
             "| Mode | hit@1 | hit@3 | hit@8 | MRR | visual hit@3 | text hit@3 |",
             "|---|---|---|---|---|---|---|"]
    for mode, r in results["retrieval"].items():
        a, v, t = r["all"], r["visual"], r["text"]
        lines.append(f"| {mode} | {a['hit@1']:.2f} | {a['hit@3']:.2f} | {a['hit@8']:.2f} | {a['mrr']:.2f} "
                     f"| {v['hit@3']:.2f} | {t['hit@3']:.2f} |")
    if "end_to_end" in results:
        lines += ["", "## End-to-end answers", "",
                  "| Mode | answered + cited a gold slide | visual: same | answerable marked not found "
                  "| partly supported | unanswerable correctly refused | false answers | dropped citations "
                  "| median s |", "|---|---|---|---|---|---|---|---|---|"]
        for mode, m in results["end_to_end"].items():
            vis = "n/a" if m["visual_answered_correct_source"] is None else f"{m['visual_answered_correct_source']:.2f}"
            lines.append(f"| {mode} | {m['answered_correct_source']:.2f} | {vis} "
                         f"| {m['answerable_marked_not_found']:.2f} | {m['partially_supported']} "
                         f"| {m['abstained_correctly']:.2f} | {m['false_answers']} "
                         f"| {m['dropped_citations_total']} | {m['median_seconds']} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--retrieval-only", action="store_true")
    args = ap.parse_args()
    setup_logging()
    lib = get_library()
    questions = load_questions()
    counts = {t: sum(1 for q in questions if q["type"] == t) for t in ("text", "visual", "unanswerable")}
    results = {"timestamp": time.strftime("%Y-%m-%d %H:%M"), "n_questions": len(questions),
               "counts": counts, "retrieval": eval_retrieval(lib, questions)}
    if not args.retrieval_only:
        results["end_to_end"] = eval_end_to_end(lib, questions)
    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    (out / "eval_results.json").write_text(json.dumps(results, indent=1))
    (out / "summary.md").write_text(write_summary(results))
    print((out / "summary.md").read_text())


if __name__ == "__main__":
    main()
