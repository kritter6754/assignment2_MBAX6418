"""Multiple-choice quiz generation, a fixed answer key, and grading.

A generated quiz is validated question by question (4 distinct options, one correct
index, a cited source whose quote really appears on that page), then saved to disk
with a SHA-256 hash of its answer key. Loading a quiz re-checks the hash, so the key
cannot silently change between generation and grading. The interface only ever
receives `public_questions()` (no answers) until a student submits or reveals.
"""
from __future__ import annotations
import os
import threading
import hashlib
import json
import random
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
                },
                "required": ["question", "options", "correct_index", "explanation", "source_id", "quote"],
            },
        }
    },
    "required": ["questions"],
}

QUIZ_PROMPT = """You write multiple-choice practice questions for a university course, using ONLY the evidence provided (labelled [S1], [S2], ...; some are slide/page images, including diagrams and charts).
# Maximum number of stored questions per topic
MAX_BANK_SIZE = 30

# Protect question-bank updates from simultaneous requests
_BANK_LOCK = threading.Lock()
For each question:
- Test understanding of an important idea in the evidence, not trivia like slide numbers, names on title slides, or dates.
- Give exactly 4 options. Exactly one is correct according to the evidence; the other three are plausible but clearly wrong according to the evidence.
- "correct_index" is the 0-based position of the correct option.
- "explanation": 1-3 sentences on why the answer is correct, based on the evidence.
- "source_id": the evidence id that supports the answer; "quote": a short passage (under 300 characters) copied word for word from that evidence's text.
- Don't use "All of the above" or "None of the above". Don't repeat questions.

Return JSON: {"questions": [...]}"""


class QuizQuestion(BaseModel):
    qid: str
    question: str
    options: list[str]
    correct_index: int
    explanation: str
    source: SourceRef


class Quiz(BaseModel):
    quiz_id: str
    created: float
    doc_ids: list[str]
    doc_names: list[str]
    topic: str = ""
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


def generate_quiz(lib: Library, doc_ids: list[str], topic: str = "", n: int = 5,
                  seed: int | None = None) -> Quiz:
    if not doc_ids:
        raise ValueError("Choose at least one document.")
    n = max(1, min(n, 15))
    seed = seed if seed is not None else int(time.time())
    topic = topic.strip()

        # Load previously validated questions for this topic
    with _BANK_LOCK:
        bank_questions = load_question_bank(lib.dir, doc_ids, topic)

    # Randomly select questions when the bank has enough
    if len(bank_questions) >= n:
        rng = random.Random(seed)
        selected = rng.sample(bank_questions, n)

        questions = []

        for i, stored_question in enumerate(selected, start=1):
            questions.append(
                stored_question.model_copy(update={"qid": f"q{i}"})
            )

        return Quiz(
            quiz_id=uuid.uuid4().hex[:10],
            created=time.time(),
            doc_ids=list(doc_ids),
            doc_names=[
                lib.docs[d].name for d in doc_ids if d in lib.docs
            ],
            topic=topic,
            questions=questions,
            answer_key_sha256=answer_key_hash(questions),
            notes=["Questions selected from the saved question bank."],
        )

    if topic:
        evidence = retrieve(lib, topic, doc_ids, max_evidence=min(4 + n, 12), max_images=3)
    else:
        evidence = _sample_evidence(lib, doc_ids, min(4 + n, 12), seed)
    if not evidence:
        raise ValueError("No usable content found in the selected documents"
                         + (f" for topic '{topic}'." if topic else "."))

    accepted: list[tuple[_RawQ, Evidence]] = []
    notes: list[str] = []
    seen_q: set[str] = set()
    for attempt in range(2):  # second attempt only tops up missing questions
        need = n - len(accepted)
        if need <= 0:
            break
        ask = f"Write {need} questions" + (f" about: {topic}" if topic else " covering different evidence items")
        if seen_q:
            ask += ". Do not repeat these questions: " + " | ".join(q.question for q, _ in accepted)
        content = [{"type": "text", "text": "Evidence from the course materials:"}]
        content += evidence_messages(evidence, image_max_side=896)
        content.append({"type": "text", "text": ask + "."})
        raw = lib.services.chat([{"role": "system", "content": QUIZ_PROMPT},
                                 {"role": "user", "content": content}],
                                schema=QUIZ_SCHEMA, max_tokens=900 * need + 500)
        try:
            items = extract_json(raw).get("questions", [])
        except (json.JSONDecodeError, AttributeError):
            notes.append(f"Attempt {attempt + 1}: model output was not valid JSON.")
            continue
        by_id = {e.eid: e for e in evidence}
        for item in items:
            q, why = validate_question(item, evidence, lambda qt, e: lib.quote_on_page(qt, e.page, e.text))
            if q is None:
                notes.append(f"Rejected a generated question: {why}.")
                continue
            if normalize(q.question) in seen_q:
                continue
            seen_q.add(normalize(q.question))
            accepted.append((q, by_id[q.source_id.strip("[] ")]))
            if len(accepted) >= n:
                break
    if not accepted:
        raise ServiceError("Could not generate valid questions from these materials. Try another topic.")

    rng = random.Random(seed)
    questions = []
    for i, (q, ev) in enumerate(accepted, start=1):
        options, correct = _shuffle(q, rng)
        questions.append(QuizQuestion(
            qid=f"q{i}", question=q.question.strip(), options=[o.strip() for o in options],
            correct_index=correct, explanation=q.explanation.strip(),
            source=SourceRef(eid=ev.eid, doc_name=ev.page.doc_name, location=ev.page.label,
                             page_id=ev.page_id, excerpt=q.quote.strip(),
                             image_path=ev.page.image_path, kind=ev.kind)))
       if len(questions) < n:
        notes.append(f"Only {len(questions)} of {n} questions passed validation.")

    # Save newly generated questions into the reusable question bank.
    with _BANK_LOCK:
        existing_questions = load_question_bank(lib.dir, doc_ids, topic)

        existing_texts = {
            normalize(q.question) for q in existing_questions
        }

        for question in questions:
            question_text = normalize(question.question)

            if question_text not in existing_texts:
                if len(existing_questions) >= MAX_BANK_SIZE:
                    break

                existing_questions.append(question)
                existing_texts.add(question_text)

        save_question_bank(
            lib.dir,
            doc_ids,
            topic,
            existing_questions,
        )

    return Quiz(
        quiz_id=uuid.uuid4().hex[:10],
        created=time.time(),
        doc_ids=list(doc_ids),
        doc_names=[lib.docs[d].name for d in doc_ids if d in lib.docs],
        topic=topic,
        questions=questions,
        answer_key_sha256=answer_key_hash(questions),
        notes=notes,
    )

# -- question bank ------------------------------------------------------------

def _bank_path(data_dir: Path, doc_ids: list[str], topic: str) -> Path:
    """Create a unique bank filename for each topic and document selection."""
    key = json.dumps(
        {
            "doc_ids": sorted(doc_ids),
            "topic": topic.strip().casefold(),
        },
        sort_keys=True,
    )
    bank_id = hashlib.sha256(key.encode()).hexdigest()[:20]
    return Path(data_dir) / "question_banks" / f"{bank_id}.json"


def load_question_bank(
    data_dir: Path, doc_ids: list[str], topic: str
) -> list[QuizQuestion]:
    """Load previously validated questions for this topic and document set."""
    path = _bank_path(data_dir, doc_ids, topic)

    if not path.exists():
        return []

    data = json.loads(path.read_text(encoding="utf-8"))
    questions = [QuizQuestion.model_validate(q) for q in data["questions"]]

    if answer_key_hash(questions) != data["answer_key_sha256"]:
        raise ValueError("Question bank failed its answer-key integrity check.")

    return questions[:MAX_BANK_SIZE]


def save_question_bank(
    data_dir: Path,
    doc_ids: list[str],
    topic: str,
    questions: list[QuizQuestion],
) -> Path:
    """Save up to 30 questions with an integrity-protected answer key."""
    path = _bank_path(data_dir, doc_ids, topic)
    path.parent.mkdir(parents=True, exist_ok=True)

    unique = []
    seen = set()

    for question in questions:
        key = normalize(question.question)
        if key not in seen:
            unique.append(question)
            seen.add(key)

        if len(unique) >= MAX_BANK_SIZE:
            break

    data = {
        "questions": [q.model_dump() for q in unique],
        "answer_key_sha256": answer_key_hash(unique),
    }

    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temporary_path, path)

    return path
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
    """Questions and options only: no correct answer, explanation or source."""
    return [{"qid": q.qid, "question": q.question, "options": list(q.options)} for q in quiz.questions]


def grade(quiz: Quiz, responses: dict[str, int | None]) -> GradeResult:
    results = []
    for q in quiz.questions:
        chosen = responses.get(q.qid)
        ok = chosen is not None and chosen == q.correct_index
        results.append(QuestionResult(qid=q.qid, chosen=chosen, correct_index=q.correct_index,
                                      is_correct=ok, explanation=q.explanation, source=q.source))
    return GradeResult(score=sum(r.is_correct for r in results), total=len(results), results=results)
