"""Helpers shared by answering and quizzes: evidence formatting and quote checks."""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from .retrieval import Evidence
from .services import image_data_url, protect_special

QUOTE_MATCH_THRESHOLD = 85  # rapidfuzz partial_ratio, 0-100
MIN_QUOTE_CHARS = 8


def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[‘’“”\"'`•▪●◦–—\-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def quote_found(quote: str, source_text: str) -> bool:
    """True if `quote` appears (near-)verbatim in `source_text`.

    Allows small differences (whitespace, bullets, line breaks, quote marks); a
    paraphrase or invented text scores well below the threshold. A quote that
    skips text with an ellipsis ("A ... B") passes only if every fragment is found.
    """
    s = normalize(source_text)
    if not s:
        return False
    fragments = [normalize(f) for f in re.split(r"\.\.\.+|…", quote)]
    fragments = [f for f in fragments if f]
    if not fragments or sum(len(f) for f in fragments) < MIN_QUOTE_CHARS:
        return False
    for f in fragments:
        if len(f) < MIN_QUOTE_CHARS:
            continue  # too short to check on its own (e.g. "1."); others must carry it
        if f not in s and fuzz.partial_ratio(f, s) < QUOTE_MATCH_THRESHOLD:
            return False
    return any(len(f) >= MIN_QUOTE_CHARS for f in fragments)


MAX_IMAGES = 5  # page images per LLM request (keeps latency down)


def evidence_messages(evidence: list[Evidence], image_max_side: int = 1024) -> list[dict]:
    """Chat content parts for the evidence: text blocks plus page images (each page's
    image is sent at most once, in evidence order, up to MAX_IMAGES)."""
    parts: list[dict] = []
    sent: set[str] = set()

    def add_image(ev: Evidence) -> None:
        if ev.page_id in sent or len(sent) >= MAX_IMAGES:
            return
        sent.add(ev.page_id)
        parts.append({"type": "text", "text": f"(image of {ev.page.doc_name}, {ev.page.label} for [{ev.eid}])"})
        parts.append({"type": "image_url",
                      "image_url": {"url": image_data_url(ev.page.image_path, max_side=image_max_side)}})

    for ev in evidence:
        where = f"{ev.page.doc_name}, {ev.page.label}"
        if ev.kind == "text":
            label = " text read from the page image" if ev.chunk and ev.chunk.field == "visual_text" else ""
            parts.append({"type": "text", "text": protect_special(f"[{ev.eid}] ({where}){label}\n{ev.text}\n")})
            if ev.shows_image:
                add_image(ev)
        else:
            caption = f"[{ev.eid}] ({where}) original page image"
            page_text = "\n".join(t for t in (ev.page.text, ev.page.visual_text) if t)
            if page_text:
                caption += f". Text extracted from this page:\n{page_text[:2000]}"
            parts.append({"type": "text", "text": protect_special(caption)})
            add_image(ev)
    return parts
