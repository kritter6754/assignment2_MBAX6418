"""Multiple-choice quiz generation, a fixed answer key, and grading.

A generated quiz is validated question by question (4 distinct options, one correct
index, a cited source whose quote really appears on that page), then saved to disk
with a SHA-256 hash of its answer key. Loading a quiz re-checks the hash, so the key
cannot silently change between generation and grading. The interface only ever
receives `public_questions()` (no answers) until a student submits or reveals.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import time
import uuid
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field, ValidationError

from .answer import SourceRef
from .grounding import evidence_messages, normalize, quote_found
from .library import Library
from .retrieval import Evidence, retrieve
from .services import ServiceError, extract_json

QUIZ_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "correct_index": {"type": "integer"},
                    "explanation": {"type": "string"},
                    "source_id": {"type": "string"},
                    "quote": {"type": "string"},
                    "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]},
                },
                "required": ["question", "options", "correct_index", "explanation", "source_id", "quote",
                             "difficulty"],
            },
        }
    },
    "required": ["questions"],
}

QUIZ_PROMPT = """You write multiple-choice practice questions for a university course, using ONLY the evidence provided (labelled [S1], [S2], ...; some are slide/page images, including diagrams and charts).

For each question:
- Test understanding of an important idea in the evidence, not trivia like slide numbers, names on title slides, or dates.
- Give exactly 4 options. Exactly one is correct according to the evidence; the other three are plausible but clearly wrong according to the evidence.
- "correct_index" is the 0-based position of the correct option.
- "explanation": 1-3 sentences on why the answer is correct, based on the evidence.
- "source_id": the evidence id that supports the answer; "quote": a short passage (under 300 characters) copied word for word from that evidence's text.
- "difficulty": "easy", "medium" or "hard" (see below).
- Don't use "All of the above" or "None of the above". Don't repeat questions.
- Never mention evidence ids such as [S1] or [V2] in the question, options or explanation.
- Write exactly the number of questions asked for, no more.

Difficulty levels (every question, even a hard one, must be answerable from the evidence alone):
- easy: recall one fact or definition stated directly in the evidence; wrong options are clearly wrong.
- medium: understand or apply a concept from the evidence; wrong options are plausible.
- hard: reason across several details, compare concepts, or apply an idea to a short scenario; wrong options are close but contradicted by the evidence.

Return JSON: {"questions": [...]}"""

VISUAL_INSTRUCTIONS = """These questions are IMAGE QUESTIONS: the student will see the slide image next to the question and must look at it to answer.
- Use only the provided page images (diagrams, charts, tables, screenshots, figures). Base each question on what the image shows: labels, arrows, the order of steps, values, highlighted parts, how elements relate.
- Phrase it so the image is needed, e.g. "In the diagram shown, ...", "According to the chart shown, ...", "In the table shown, ...". It must not be answerable from general knowledge alone.
- "source_id" is the image's id; "quote" is ONE label, title, value or phrase that appears on that image, copied exactly. Never join two separate labels into one quote.
- Use a different image for each question when possible."""

DIFFICULTIES = ("mixed", "easy", "medium", "hard")
MIN_VISUAL_SHARE = 0.2  # with image questions on, at least 20% of the quiz
VISUAL_QUERY = "diagram, chart, graph, table, screenshot, figure or illustration"


class QuizQuestion(BaseModel):
    qid: str
    question: str
    options: list[str]
    correct_index: int
    explanation: str
    source: SourceRef
    difficulty: str = ""  # easy / medium / hard, as labelled by the model
    visual: bool = False  # image question: the slide image is shown with the question


class Quiz(BaseModel):
    quiz_id: str
    created: float
    doc_ids: list[str]
    doc_names: list[str]
    topic: str = ""
    difficulty: str = "mixed"
    retry_of: str = ""  # quiz id this quiz retries the missed questions of
    questions: list[QuizQuestion]
    answer_key_sha256: str
    notes: list[str] = Field(default_factory=list)


class QuestionResult(BaseModel):
    qid: str
    chosen: int | None
    correct_index: int
    is_correct: bool
    explanation: str
    source: SourceRef


class GradeResult(BaseModel):
    score: int
    total: int
    results: list[QuestionResult]


class _RawQ(BaseModel):
    question: str = Field(min_length=5)
    options: list[str]
    correct_index: int
    explanation: str = Field(min_length=5)
    source_id: str
    quote: str
    difficulty: str = "medium"


def answer_key_hash(questions: list[QuizQuestion]) -> str:
    key = [(q.qid, q.question, q.options, q.correct_index) for q in questions]
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()


def _sample_evidence(lib: Library, doc_ids: list[str], n: int, seed: int) -> list[Evidence]:
    """Without a topic: spread evidence across the selected documents' content-rich pages."""
    pages = [p for d in doc_ids for p in lib.doc_pages(d) if len(p.text) >= 150]
    if not pages:
        pages = [p for d in doc_ids for p in lib.doc_pages(d) if p.text.strip()]
    rng = random.Random(seed)
    picked = sorted(rng.sample(pages, min(n, len(pages))), key=lambda p: (p.doc_name, p.number))
    out = []
    for i, p in enumerate(picked, start=1):
        chunks = [c for c in lib.doc_chunks([p.doc_id]) if c.number == p.number]
        chunk = max(chunks, key=lambda c: len(c.text)) if chunks else None
        out.append(Evidence(f"S{i}", "text", p, chunk, 0.0))
    return out


def validate_question(raw: dict, evidence: list[Evidence],
                      quote_ok: Callable[[str, Evidence], bool] | None = None) -> tuple[_RawQ | None, str]:
    try:
        q = _RawQ.model_validate(raw)
    except ValidationError as e:
        return None, f"bad format ({e.error_count()} errors)"
    if len(q.options) != 4:
        return None, f"has {len(q.options)} options, need 4"
    if len({normalize(o) for o in q.options}) != 4 or any(not o.strip() for o in q.options):
        return None, "options are not 4 distinct non-empty choices"
    if not 0 <= q.correct_index < 4:
        return None, "correct_index out of range"
    ev = {e.eid: e for e in evidence}.get(q.source_id.strip("[] "))
    if ev is None:
        return None, f"cites unknown source {q.source_id}"
    found = quote_ok(q.quote, ev) if quote_ok else (quote_found(q.quote, ev.text) or quote_found(q.quote, ev.page.text))
    if not found:
        return None, "quoted text not found in the cited source"
    return q, ""


def _shuffle(q: _RawQ, rng: random.Random) -> tuple[list[str], int]:
    order = list(range(4))
    rng.shuffle(order)
    return [q.options[i] for i in order], order.index(q.correct_index)


def _schema_for(n: int) -> dict:
    """QUIZ_SCHEMA limited to exactly n questions (stops the model writing extra ones and
    running out of output tokens mid-JSON)."""
    schema = json.loads(json.dumps(QUIZ_SCHEMA))
    schema["properties"]["questions"].update({"minItems": n, "maxItems": n})
    return schema


def _salvage_questions(raw: str) -> list[dict]:
    """Complete question objects from a reply that was cut off mid-JSON."""
    out = []
    for m in re.finditer(r"\{[^{}]*\"question\"[^{}]*\}", raw, flags=re.S):
        try:
            out.append(json.loads(m.group(0)))
        except json.JSONDecodeError:
            continue
    return out


# Only bracketed/parenthesised ids ("[V2]", "(S3)"), so text like "S3 bucket" or "Model V2" stays.
_ID_MENTION = re.compile(r"\s*(?:(?:in|from|of) )?[\[(][SV]\d+[\])]")


def _strip_ids(text: str) -> str:
    """Remove stray evidence ids like "[V2]" or "in S3" from model-written text."""
    return re.sub(r"\s{2,}", " ", _ID_MENTION.sub("", text)).strip()


def visual_evidence(lib: Library, doc_ids: list[str], topic: str, k: int, seed: int) -> list[Evidence]:
    """Slides whose content is mainly visual, as image evidence (up to k).

    First choice: slides whose image holds text the text layer lacks (diagram labels, table
    screenshots, charts) or that are image-only. Then slides the image index ranks as
    diagram/chart-like (for the topic, if one is given)."""
    pages = [p for d in doc_ids for p in lib.doc_pages(d)]
    flagged = [p for p in pages if p.image_path and (len(p.visual_text) >= 80 or (p.ocr and len(p.text) >= 120))]
    ranked = [lib.pages[i] for i, _ in lib.search_images(f"{topic} {VISUAL_QUERY}".strip(), 12, doc_ids)]
    if topic:
        # the images that best match the topic, visual slides first (diagrams whose labels the
        # OCR pass didn't flag, e.g. pipeline figures, still rank high here)
        picked = [p for p in ranked[:6] if p in flagged] + [p for p in ranked[:6] if p not in flagged]
    else:
        picked = random.Random(seed).sample(flagged, len(flagged))
    if len(picked) < 2:  # very few visual slides: fall back to the most diagram-like images
        picked += [p for p in ranked if p not in picked][: 3 - len(picked)]
    out, seen = [], set()
    for p in picked:
        if p.page_id in seen or not p.image_path:
            continue
        seen.add(p.page_id)
        out.append(Evidence(f"V{len(out) + 1}", "image", p, None, 0.0))
        if len(out) >= k:
            break
    return out


def _write_questions(lib: Library, evidence: list[Evidence], need: int, topic: str, difficulty: str,
                     avoid: list[str], excluded: set[str], seen_q: set[str], notes: list[str],
                     visual: bool, attempts: int = 2) -> list[tuple["_RawQ", Evidence]]:
    """Ask the model for `need` questions from `evidence`, validate them. Later attempts only
    top up the missing ones."""
    accepted: list[tuple[_RawQ, Evidence]] = []
    rejected: list[str] = []  # fed back to the model on the next attempt
    by_id = {e.eid: e for e in evidence}
    for attempt in range(attempts):
        left = need - len(accepted)
        if left <= 0:
            break
        ask = f"Write {left} questions" + (f" about: {topic}" if topic else " covering different evidence items")
        ask += (". Use a mix of easy, medium and hard questions" if difficulty == "mixed"
                else f". Every question must be {difficulty}")
        avoid_now = [q.question for q, _ in accepted] + avoid
        if rejected:
            ask += (". Some earlier questions were rejected: " + "; ".join(rejected[-3:])
                    + f". Quote exactly one {'label or phrase that appears on the cited image' if visual else 'passage from the cited evidence'}")
        if avoid_now:
            ask += (". The student has already answered these correctly or seen them; do not ask them "
                    "again or ask the same thing in other words: " + " | ".join(avoid_now[-40:]))
        system = QUIZ_PROMPT + ("\n\n" + VISUAL_INSTRUCTIONS if visual else "")
        content = [{"type": "text", "text": "Evidence from the course materials:"}]
        content += evidence_messages(evidence, image_max_side=1024 if visual else 896)
        content.append({"type": "text", "text": ask + "."})
        raw = lib.services.chat([{"role": "system", "content": system}, {"role": "user", "content": content}],
                                schema=_schema_for(left), max_tokens=1200 * left + 800)
        try:
            items = extract_json(raw).get("questions", [])
        except (json.JSONDecodeError, AttributeError):
            items = _salvage_questions(raw)
            if not items:
                notes.append(f"Attempt {attempt + 1}: model output was not valid JSON.")
                continue
        for item in items:
            if isinstance(item, dict):
                for field in ("question", "explanation"):
                    if isinstance(item.get(field), str):
                        item[field] = _strip_ids(item[field])
        for item in items:
            q, why = validate_question(item, evidence, lambda qt, e: lib.quote_on_page(qt, e.page, e.text))
            if q is None:
                notes.append(f"Rejected a generated {'image ' if visual else ''}question: {why}.")
                if isinstance(item, dict) and "quote" in why:
                    rejected.append(f"the quote '{str(item.get('quote', ''))[:80]}' does not appear on "
                                    f"{item.get('source_id', '')}")
                continue
            if visual and by_id[q.source_id.strip("[] ")].kind != "image":
                notes.append("Rejected an image question that did not cite an image.")
                continue
            key = normalize(q.question)
            if key in seen_q or key in excluded:
                continue
            if difficulty != "mixed":
                q.difficulty = difficulty
            seen_q.add(key)
            accepted.append((q, by_id[q.source_id.strip("[] ")]))
            if len(accepted) >= need:
                break
    return accepted


def generate_quiz(lib: Library, doc_ids: list[str], topic: str = "", n: int = 5,
                  seed: int | None = None, difficulty: str = "mixed",
                  exclude_questions: list[str] | None = None, include_images: bool = False) -> Quiz:
    """`difficulty` is mixed/easy/medium/hard. `exclude_questions` are questions the
    student has already mastered; they are not asked again. With `include_images`, at least
    20% of the questions (rounded up) are image questions built from visual slides."""
    if not doc_ids:
        raise ValueError("Choose at least one document.")
    difficulty = (difficulty or "mixed").strip().lower()
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"Difficulty must be one of {', '.join(DIFFICULTIES)}.")
    n = max(1, min(n, 20))
    seed = seed if seed is not None else int(time.time())
    topic = topic.strip()
    excluded = {normalize(q) for q in exclude_questions or []}

    if topic:
        evidence = retrieve(lib, topic, doc_ids, max_evidence=min(4 + n, 20), max_images=3)
    else:
        evidence = _sample_evidence(lib, doc_ids, min(4 + n, 24), seed)
    if not evidence:
        raise ValueError("No usable content found in the selected documents"
                         + (f" for topic '{topic}'." if topic else "."))

    notes: list[str] = []
    seen_q: set[str] = set()
    avoid = list(exclude_questions or [])

    # 1. Image questions first, so the 20% minimum is met before text questions fill the rest.
    visual: list[tuple[_RawQ, Evidence]] = []
    n_visual = math.ceil(n * MIN_VISUAL_SHARE) if include_images else 0
    if n_visual:
        pool = visual_evidence(lib, doc_ids, topic, k=min(5, n_visual + 2), seed=seed)  # ≤5 images per request
        if pool:
            visual = _write_questions(lib, pool, n_visual, topic, difficulty, avoid, excluded, seen_q,
                                      notes, visual=True, attempts=3)
        if len(visual) < n_visual:
            notes.append(f"Only {len(visual)} of {n_visual} image questions could be made from the visual "
                         "slides in this material.")

    # 2. Text questions for the rest.
    text = _write_questions(lib, evidence, n - len(visual), topic, difficulty,
                            avoid + [q.question for q, _ in visual], excluded, seen_q, notes, visual=False)
    accepted = [(q, ev, True) for q, ev in visual] + [(q, ev, False) for q, ev in text]
    if not accepted:
        raise ServiceError("Could not generate valid questions from these materials. Try another topic.")

    rng = random.Random(seed)
    rng.shuffle(accepted)  # spread image questions through the quiz
    questions = []
    for i, (q, ev, is_visual) in enumerate(accepted, start=1):
        options, correct = _shuffle(q, rng)
        questions.append(QuizQuestion(
            qid=f"q{i}", question=q.question.strip(), options=[o.strip() for o in options],
            correct_index=correct, explanation=q.explanation.strip(), difficulty=q.difficulty,
            visual=is_visual,
            source=SourceRef(eid=ev.eid, doc_name=ev.page.doc_name, location=ev.page.label,
                             page_id=ev.page_id, excerpt=q.quote.strip(),
                             image_path=ev.page.image_path, kind=ev.kind)))
    if len(questions) < n:
        notes.append(f"Only {len(questions)} of {n} questions passed validation.")
    return Quiz(quiz_id=uuid.uuid4().hex[:10], created=time.time(), doc_ids=list(doc_ids),
                doc_names=[lib.docs[d].name for d in doc_ids if d in lib.docs], topic=topic,
                difficulty=difficulty, questions=questions,
                answer_key_sha256=answer_key_hash(questions), notes=notes)


def retry_missed(quiz: Quiz, responses: dict[str, int | None]) -> Quiz | None:
    """A new quiz with only the questions answered wrongly or not answered (e.g. revealed).
    Questions and answer key are copied unchanged; the new quiz gets its own id and hash.
    Returns None when everything was answered correctly."""
    missed = [q for q in quiz.questions if responses.get(q.qid) != q.correct_index]
    if not missed:
        return None
    questions = [q.model_copy(update={"qid": f"q{i}"}) for i, q in enumerate(missed, start=1)]
    return Quiz(quiz_id=uuid.uuid4().hex[:10], created=time.time(), doc_ids=quiz.doc_ids,
                doc_names=quiz.doc_names, topic=quiz.topic, difficulty=quiz.difficulty,
                retry_of=quiz.quiz_id, questions=questions,
                answer_key_sha256=answer_key_hash(questions))


# -- storage ------------------------------------------------------------------
def save_quiz(quiz: Quiz, data_dir: Path) -> Path:
    path = Path(data_dir) / "quizzes" / f"{quiz.quiz_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"Quiz {quiz.quiz_id} already saved; answer keys are never overwritten.")
    path.write_text(quiz.model_dump_json(indent=1))
    return path


def load_quiz(quiz_id: str, data_dir: Path) -> Quiz:
    quiz = Quiz.model_validate_json((Path(data_dir) / "quizzes" / f"{quiz_id}.json").read_text())
    if answer_key_hash(quiz.questions) != quiz.answer_key_sha256:
        raise ValueError(f"Quiz {quiz_id} failed its answer-key integrity check.")
    return quiz


# -- what the student sees ----------------------------------------------------
def public_questions(quiz: Quiz) -> list[dict]:
    """Questions and options only: no correct answer, explanation or source. Image questions
    also carry the slide image the student needs to look at."""
    return [{"qid": q.qid, "question": q.question, "options": list(q.options),
             "image": q.source.image_path if q.visual else None} for q in quiz.questions]


def grade(quiz: Quiz, responses: dict[str, int | None]) -> GradeResult:
    results = []
    for q in quiz.questions:
        chosen = responses.get(q.qid)
        ok = chosen is not None and chosen == q.correct_index
        results.append(QuestionResult(qid=q.qid, chosen=chosen, correct_index=q.correct_index,
                                      is_correct=ok, explanation=q.explanation, source=q.source))
    return GradeResult(score=sum(r.is_correct for r in results), total=len(results), results=results)
