"""Answer a question from retrieved evidence, then validate the answer and its sources.

Pipeline: retrieve -> LLM returns JSON {found, answer, sources[{id, quote}]} ->
format check (pydantic) -> source checks (cited id was retrieved, quote really
appears on that page) -> support check (a second LLM call judges whether the
verified quotes back the answer). Anything that fails is dropped or flagged;
if nothing survives, the user is told the materials don't cover it.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .grounding import evidence_messages
from .library import Library
from .retrieval import Evidence, Mode, retrieve
from .services import ServiceError, extract_json, image_data_url

log = logging.getLogger(__name__)

NOT_FOUND_MESSAGE = "I couldn't find this in the selected course materials."

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "answer": {"type": "string"},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "quote": {"type": "string"}},
                "required": ["id", "quote"],
            },
        },
    },
    "required": ["found", "answer", "sources"],
}

SYSTEM_PROMPT = """You are a course assistant. Answer the student's question using ONLY the evidence provided from the course materials. Evidence items are labelled [S1], [S2], ...; some are original slide/page images (diagrams, charts, tables) that you should read carefully.

Rules:
- Use only facts stated or shown in the evidence. Do not use outside knowledge, and never invent facts, numbers or citations.
- If the evidence does not contain the answer, set "found" to false, say briefly what is missing in "answer", and return an empty "sources" list.
- If the evidence answers only part of the question, answer that part and say plainly which part the materials do not cover.
- Mark claims in the answer with their evidence ids, e.g. "... grows quadratically [S2]."
- For every id you cite, add an entry to "sources" with a "quote": a short passage (one sentence or bullet, under 300 characters) copied word for word from that evidence's text. For an image, copy words exactly as they appear on the page (e.g. the diagram labels you relied on). Do not paraphrase quotes or add arrows or symbols.
- Be concise: a few sentences or a short list.

Return JSON: {"found": true|false, "answer": "...", "sources": [{"id": "S1", "quote": "..."}]}"""

SUPPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["supported", "unsupported_claims"],
}

SUPPORT_PROMPT = """You check citations. Below is an answer and the source excerpts (and page images) it cites. Decide whether every factual claim in the answer is supported by these sources. Ignore wording differences; flag claims that add facts, numbers or conclusions not present in the sources. Statements that something is not covered by the materials are fine.

Return JSON: {"supported": true|false, "unsupported_claims": ["..."]}"""


class LLMSource(BaseModel):
    id: str
    quote: str


class LLMAnswer(BaseModel):
    found: bool
    answer: str = Field(min_length=1)
    sources: list[LLMSource]


class SourceRef(BaseModel):
    """A source shown to the student."""

    eid: str
    doc_name: str
    location: str  # e.g. "Slide 12"
    page_id: str
    excerpt: str  # verified quote, or the start of the page text for image-only sources
    image_path: str | None
    kind: Literal["text", "image"]
    # False only for image evidence whose quote couldn't be matched to text on the page (e.g. the
    # model described a diagram's arrows). The image is then the evidence, and the support check
    # (which looks at the image) must pass.
    quote_verified: bool = True


class AnswerResult(BaseModel):
    answer: str
    sources: list[SourceRef]
    status: Literal["answered", "not_found", "partially_supported", "error"]
    notes: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)  # page_ids retrieved (for evaluation)


def _ask_llm(lib: Library, question: str, evidence: list[Evidence], feedback: str | None = None) -> LLMAnswer:
    user_content = [{"type": "text", "text": "Evidence from the course materials:"}]
    user_content += evidence_messages(evidence)
    user_content.append({"type": "text", "text": f"Question: {question}"})
    if feedback:
        user_content.append({"type": "text", "text": feedback})
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_content}]

    last_err = ""
    for _ in range(2):  # one retry if the output is malformed
        msgs = messages if not last_err else messages + [
            {"role": "user", "content": f"Your previous reply was invalid ({last_err}). Return only valid JSON."}]
        raw = lib.services.chat(msgs, schema=ANSWER_SCHEMA, max_tokens=1500)
        try:
            return LLMAnswer.model_validate(extract_json(raw))
        except (ValidationError, json.JSONDecodeError, ValueError) as e:
            last_err = type(e).__name__
    raise ServiceError("The model did not return a valid answer format after a retry.")


def check_sources(lib: Library, parsed: LLMAnswer, evidence: list[Evidence]) -> tuple[list[SourceRef], list[str]]:
    """Keep only sources whose id was retrieved and whose quote appears on that page."""
    by_id = {e.eid: e for e in evidence}
    kept: list[SourceRef] = []
    notes: list[str] = []
    seen: set[str] = set()
    for s in parsed.sources:
        ev = by_id.get(s.id.strip("[] "))
        if ev is None:
            notes.append(f"Dropped citation {s.id}: not one of the retrieved sources.")
            continue
        # A quote may come from the chunk, anywhere on the same page, or text visible in its image.
        verified = lib.quote_on_page(s.quote, ev.page, ev.text)
        if not verified and ev.shows_image:
            notes.append(f"{ev.page.doc_name}, {ev.page.label}: the excerpt is the model's reading of the "
                         "page image, not exact text; check it against the image.")
        elif not verified:
            note = (f"Dropped a citation of {ev.page.doc_name}, {ev.page.label}: "
                    "the quoted text was not found there.")
            if note not in notes:
                notes.append(note)
            continue
        if ev.eid in seen:
            continue
        seen.add(ev.eid)
        kept.append(SourceRef(eid=ev.eid, doc_name=ev.page.doc_name, location=ev.page.label,
                              page_id=ev.page_id, excerpt=s.quote.strip(),
                              image_path=ev.page.image_path, kind=ev.kind, quote_verified=verified))
    return kept, notes


def _strip_dropped_markers(answer: str, kept_ids: set[str]) -> str:
    def repl(m: re.Match) -> str:
        ids = [i.strip() for i in m.group(1).split(",")]
        ids = [i for i in ids if i in kept_ids]
        return f"[{', '.join(ids)}]" if ids else ""

    return re.sub(r"\[((?:S\d+\s*,?\s*)+)\]", repl, answer).replace("  ", " ")


def support_check(lib: Library, answer: str, sources: list[SourceRef]) -> tuple[bool, list[str]]:
    content: list[dict] = [{"type": "text", "text": f"Answer:\n{answer}\n\nCited sources:"}]
    for s in sources:
        content.append({"type": "text", "text": f"[{s.eid}] ({s.doc_name}, {s.location}): \"{s.excerpt}\""})
        if s.image_path and (s.kind == "image" or not s.quote_verified):
            content.append({"type": "image_url", "image_url": {"url": image_data_url(s.image_path, 768)}})
    raw = lib.services.chat([{"role": "system", "content": SUPPORT_PROMPT},
                             {"role": "user", "content": content}], schema=SUPPORT_SCHEMA, max_tokens=600)
    data = extract_json(raw)
    return bool(data.get("supported")), [str(c) for c in data.get("unsupported_claims", [])]


def answer_question(lib: Library, question: str, doc_ids: list[str] | None = None,
                    mode: Mode = "hybrid", check_support: bool = True) -> AnswerResult:
    question = question.strip()
    if not question:
        return AnswerResult(answer="Please enter a question.", sources=[], status="error")
    if not lib.docs:
        return AnswerResult(answer="The library is empty. Add course files first.", sources=[], status="error")

    evidence = retrieve(lib, question, doc_ids, mode=mode)
    ev_ids = [e.page_id for e in evidence]
    if not evidence:
        return AnswerResult(answer=NOT_FOUND_MESSAGE, sources=[], status="not_found", evidence_ids=ev_ids)

    parsed = _ask_llm(lib, question, evidence)
    if not parsed.found:
        msg = parsed.answer.strip()
        return AnswerResult(answer=f"{NOT_FOUND_MESSAGE} {msg}".strip(), sources=[], status="not_found",
                            evidence_ids=ev_ids)

    sources, notes = check_sources(lib, parsed, evidence)
    if not sources:
        # The model claimed an answer but couldn't point to real text: don't show it.
        return AnswerResult(
            answer=NOT_FOUND_MESSAGE + " (A draft answer was discarded because its citations "
                   "could not be verified against the materials.)",
            sources=[], status="not_found", notes=notes, evidence_ids=ev_ids)

    answer = _strip_dropped_markers(parsed.answer, {s.eid for s in sources})
    status: Literal["answered", "partially_supported"] = "answered"
    if check_support or not all(s.quote_verified for s in sources):
        ok, claims = support_check(lib, answer, sources)
        if not ok:
            # One regeneration with feedback, then re-check.
            feedback = ("A reviewer found these claims unsupported by the quoted sources: "
                        + "; ".join(claims) + ". Remove them or support them with exact quotes.")
            parsed2 = _ask_llm(lib, question, evidence, feedback=feedback)
            sources2, notes2 = check_sources(lib, parsed2, evidence)
            if parsed2.found and sources2:
                answer2 = _strip_dropped_markers(parsed2.answer, {s.eid for s in sources2})
                ok2, claims2 = support_check(lib, answer2, sources2)
                if ok2 or len(claims2) < len(claims):
                    answer, sources, notes, claims, ok = answer2, sources2, notes2, claims2, ok2
            if not ok:
                status = "partially_supported"
                notes.append("Some statements may not be fully supported by the cited sources: "
                             + "; ".join(claims))
    return AnswerResult(answer=answer, sources=sources, status=status, notes=notes, evidence_ids=ev_ids)
