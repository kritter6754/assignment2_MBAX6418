"""Design-choice check: does the text embedder need "query: " / "passage: " prefixes?

Embeds every chunk and every answerable benchmark question with and without the
prefixes (in memory; the library is not changed) and reports text-vector-only hit@k.

    uv run python eval/prefix_ablation.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from course_assistant import get_library  # noqa: E402
from course_assistant.chunking import index_text  # noqa: E402

VARIANTS = {"no prefixes": ("", ""), "query:/passage: prefixes": ("query: ", "passage: ")}


def main() -> None:
    lib = get_library()
    s = lib.services.s
    questions = [json.loads(l) for l in (ROOT / "eval" / "questions.jsonl").read_text().splitlines()
                 if l.strip() and json.loads(l)["type"] != "unanswerable"]
    ids = list(lib.chunks)

    def embed(texts):  # raw call: bypass the configured prefixes
        m = np.array(lib.services._embed_batch(s.text_embed_url, s.text_embed_model, texts))
        return m / np.linalg.norm(m, axis=1, keepdims=True)

    results = {}
    for name, (qp, pp) in VARIANTS.items():
        chunks = embed([pp + index_text(lib.chunks[i]) for i in ids])
        queries = embed([qp + q["question"] for q in questions])
        hit = {1: 0, 3: 0, 8: 0}
        for qi, q in enumerate(questions):
            doc = next(d for d in lib.docs.values() if q["doc"] in d.name)
            gold = {f"{doc.doc_id}:{n}" for n in q["slides"]}
            pages: list[str] = []
            for j in np.argsort(-(chunks @ queries[qi])):
                pid = lib.chunks[ids[j]].page_id
                if pid not in pages:
                    pages.append(pid)
                if len(pages) == 8:
                    break
            for k in hit:
                hit[k] += any(p in gold for p in pages[:k])
        results[name] = {f"hit@{k}": round(v / len(questions), 3) for k, v in hit.items()}
        print(f"{name:28s} {results[name]}")
    out = ROOT / "results" / "prefix_ablation.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"n_questions": len(questions), "text_vector_only": results}, indent=1))


if __name__ == "__main__":
    main()
