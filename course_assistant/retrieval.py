"""Hybrid retrieval: keyword + text-vector + visual-vector search, fused, then reranked.

The three indexes stay separate. Their ranked lists are merged with reciprocal rank
fusion (RRF: each item scores sum(1 / (60 + rank)) across lists), and the merged
text chunks and page images are then rescored together by the multimodal reranker.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from .chunking import index_text
from .library import Library
from .models import Chunk, Page
from .services import RerankDoc

log = logging.getLogger(__name__)

Mode = Literal["hybrid", "hybrid_no_rerank", "text_vector", "keyword"]
MODES: tuple[Mode, ...] = ("hybrid", "hybrid_no_rerank", "text_vector", "keyword")


@dataclass
class Evidence:
    eid: str  # S1, S2, ... as shown to the LLM
    kind: Literal["text", "image"]
    page: Page
    chunk: Chunk | None
    score: float

    @property
    def text(self) -> str:
        return self.chunk.text if self.chunk else self.page.text

    @property
    def page_id(self) -> str:
        return self.page.page_id

    @property
    def shows_image(self) -> bool:
        """Send the original page image with this evidence: image hits, and text that was
        read off the image (OCR'd diagrams/tables), where the image is the real source."""
        if not self.page.image_path:
            return False
        return self.kind == "image" or (self.chunk is not None and (self.chunk.field == "visual_text" or self.page.ocr))


def rrf(ranked_lists: list[list[str]], k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, item in enumerate(lst):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return scores


def retrieve(lib: Library, query: str, doc_ids: list[str] | None = None, mode: Mode = "hybrid",
             max_evidence: int = 8, max_images: int = 3, candidates: int = 20) -> list[Evidence]:
    kw = lib.search_keyword(query, candidates, doc_ids) if mode in ("hybrid", "hybrid_no_rerank", "keyword") else []
    dense = lib.search_text(query, candidates, doc_ids) if mode in ("hybrid", "hybrid_no_rerank", "text_vector") else []
    visual = lib.search_images(query, 8, doc_ids) if mode in ("hybrid", "hybrid_no_rerank") else []

    # Fuse the two text lists; keep the visual list separate until reranking.
    text_fused = rrf([[i for i, _ in kw], [i for i, _ in dense]])
    text_ids = sorted(text_fused, key=text_fused.get, reverse=True)[:candidates]
    img_ids = [i for i, _ in visual if i in lib.pages][:6]

    cands: list[tuple[str, str]] = [("text", i) for i in text_ids] + [("image", i) for i in img_ids]
    if not cands:
        return []

    if mode == "hybrid":
        docs = [RerankDoc(text=index_text(lib.chunks[i])) if t == "text"
                else RerankDoc(image_path=lib.pages[i].image_path) for t, i in cands]
        scores = lib.services.rerank(query, docs)
    else:
        # No reranker: order everything by RRF over all lists (visual included).
        fused = rrf([[i for i, _ in kw], [i for i, _ in dense], [i for i, _ in visual]])
        scores = [fused.get(i, 0.0) for _, i in cands]

    order = sorted(range(len(cands)), key=lambda j: scores[j], reverse=True)
    picked: list[Evidence] = []
    n_img = 0
    for j in order:
        t, i = cands[j]
        if t == "image":
            if n_img >= max_images:
                continue
            n_img += 1
            ev = Evidence("", "image", lib.pages[i], None, scores[j])
        else:
            c = lib.chunks[i]
            ev = Evidence("", "text", lib.pages[c.page_id], c, scores[j])
        picked.append(ev)
        if len(picked) >= max_evidence:
            break
    for n, ev in enumerate(picked, start=1):
        ev.eid = f"S{n}"
    return picked
