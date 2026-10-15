from __future__ import annotations

import json

import pytest
from conftest import doc_id_by_name

from course_assistant.quiz import (generate_quiz, grade, load_quiz, public_questions, save_quiz,
                                   validate_question)
from course_assistant.retrieval import retrieve


def _q(source_id, quote="It happens in the mitochondria.", options=None, correct=0, question=None):
    return {"question": question or "Where does cellular respiration take place?",
            "options": options or ["Mitochondria", "Chloroplasts", "Nucleus", "Ribosomes"],
            "correct_index": correct, "explanation": "The notes say it happens in the mitochondria.",
            "source_id": source_id, "quote": quote}


def _quiz_reply(lib, n=3):
    def reply(messages):
        parts = [p["text"] for p in messages[1]["content"]
                 if p.get("type") == "text" and p["text"].startswith("[S")]
        part = next((t for t in parts if "It happens in the mitochondria." in t), parts[0])
        sid = part.split("]")[0].strip("[")
        quote = ("It happens in the mitochondria." if "It happens in the mitochondria." in part
                 else part.split("\n")[1].strip()[:60])
        qs = [_q(sid, quote, question=f"Question {i} about the notes?",
                 options=[f"right {i}", f"wrong a{i}", f"wrong b{i}", f"wrong c{i}"]) for i in range(n)]
        qs.insert(0, _q("S99"))  # invalid source -> rejected
        return json.dumps({"questions": qs})
    return reply


@pytest.fixture
def evidence(lib):
    return retrieve(lib, "Where does cellular respiration happen?")


def test_validate_question_rules(lib, evidence):
    sid = next(e.eid for e in evidence if "mitochondria" in e.text)
    assert validate_question(_q(sid), evidence)[0] is not None
    assert "4" in validate_question(_q(sid, options=["a", "b", "c"]), evidence)[1]
    assert "distinct" in validate_question(_q(sid, options=["A", "a", "b", "c"]), evidence)[1]
    assert "range" in validate_question(_q(sid, correct=4), evidence)[1]
    assert "unknown" in validate_question(_q("S42"), evidence)[1]
    assert "quoted" in validate_question(_q(sid, quote="Respiration occurs in the Golgi body."), evidence)[1]


def test_generate_save_load_and_hidden_answers(lib, fake):
    bio = doc_id_by_name(lib, "biology.md")
    fake.chat_replies = [_quiz_reply(lib)]
    quiz = generate_quiz(lib, [bio], topic="cellular respiration", n=3, seed=7)
    assert len(quiz.questions) == 3
    assert any("unknown source" in n for n in quiz.notes)
    # options are shuffled but the key still points at the right text
    for q in quiz.questions:
        assert q.options[q.correct_index].startswith("right")
        assert q.source.doc_name == "biology.md"

    pub = public_questions(quiz)
    blob = json.dumps(pub)
    assert "correct" not in blob and "explanation" not in blob and "source" not in blob

    save_quiz(quiz, lib.dir)
    again = load_quiz(quiz.quiz_id, lib.dir)
    assert [q.correct_index for q in again.questions] == [q.correct_index for q in quiz.questions]
    with pytest.raises(FileExistsError):
        save_quiz(quiz, lib.dir)  # answer keys are never overwritten


def test_tampered_answer_key_is_detected(lib, fake):
    bio = doc_id_by_name(lib, "biology.md")
    fake.chat_replies = [_quiz_reply(lib, n=2)]
    quiz = generate_quiz(lib, [bio], n=2, seed=1)
    path = save_quiz(quiz, lib.dir)
    data = json.loads(path.read_text())
    data["questions"][0]["correct_index"] = (data["questions"][0]["correct_index"] + 1) % 4
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="integrity"):
        load_quiz(quiz.quiz_id, lib.dir)


def test_grading(lib, fake):
    bio = doc_id_by_name(lib, "biology.md")
    fake.chat_replies = [_quiz_reply(lib, n=3)]
    quiz = generate_quiz(lib, [bio], n=3, seed=3)
    q1, q2, q3 = quiz.questions
    g = grade(quiz, {q1.qid: q1.correct_index, q2.qid: (q2.correct_index + 1) % 4})
    assert (g.score, g.total) == (1, 3)
    assert [r.is_correct for r in g.results] == [True, False, False]
    assert g.results[2].chosen is None


def test_requires_documents(lib):
    with pytest.raises(ValueError):
        generate_quiz(lib, [], n=3)


def _prompt_text(fake) -> str:
    return " ".join(p.get("text", "") for p in fake.chat_calls[-1][1]["content"] if p.get("type") == "text")


def test_difficulty_is_requested_and_labelled(lib, fake):
    bio = doc_id_by_name(lib, "biology.md")
    fake.chat_replies = [_quiz_reply(lib, n=2)]
    quiz = generate_quiz(lib, [bio], n=2, seed=1, difficulty="hard")
    assert "Every question must be hard" in _prompt_text(fake)
    assert quiz.difficulty == "hard" and all(q.difficulty == "hard" for q in quiz.questions)

    fake.chat_replies = [_quiz_reply(lib, n=2)]
    generate_quiz(lib, [bio], n=2, seed=1, difficulty="mixed")
    assert "mix of easy, medium and hard" in _prompt_text(fake)
    with pytest.raises(ValueError, match="Difficulty"):
        generate_quiz(lib, [bio], n=2, difficulty="impossible")


def test_mastered_questions_are_not_asked_again(lib, fake):
    bio = doc_id_by_name(lib, "biology.md")
    fake.chat_replies = [_quiz_reply(lib, n=3), _quiz_reply(lib, n=3)]
    quiz = generate_quiz(lib, [bio], n=3, seed=1, exclude_questions=["Question 0 about the notes?"])
    assert "Question 0 about the notes?" in _prompt_text(fake)  # the model is told to avoid it
    assert "Question 0 about the notes?" not in [q.question for q in quiz.questions]  # and it's filtered


def test_retry_missed_keeps_only_missed_with_its_own_fixed_key(lib, fake):
    from course_assistant.quiz import retry_missed

    bio = doc_id_by_name(lib, "biology.md")
    fake.chat_replies = [_quiz_reply(lib, n=3)]
    quiz = generate_quiz(lib, [bio], n=3, seed=3)
    q1, q2, q3 = quiz.questions
    retry = retry_missed(quiz, {q1.qid: q1.correct_index, q2.qid: (q2.correct_index + 1) % 4})
    assert [q.question for q in retry.questions] == [q2.question, q3.question]  # wrong + unanswered
    assert [q.qid for q in retry.questions] == ["q1", "q2"] and retry.retry_of == quiz.quiz_id
    assert [q.correct_index for q in retry.questions] == [q2.correct_index, q3.correct_index]
    save_quiz(retry, lib.dir)
    assert load_quiz(retry.quiz_id, lib.dir).answer_key_sha256 == retry.answer_key_sha256
    all_right = {q.qid: q.correct_index for q in quiz.questions}
    assert retry_missed(quiz, all_right) is None


OCR_DIAGRAM = ("Pipeline diagram: Documents flow into the Parser, then the Chunker, then the Indexer, "
               "which feeds the Keyword Index and the Vector Store before Retrieval and Reranking.")


def _generic_reply(prefix: str, quote: str, n: int):
    """Fake model: n valid questions citing the first evidence item whose id starts with prefix."""
    def reply(messages):
        parts = [p["text"] for p in messages[1]["content"]
                 if p.get("type") == "text" and p["text"].startswith(f"[{prefix}")]
        sid = parts[0].split("]")[0].strip("[")
        return json.dumps({"questions": [
            {"question": f"{prefix} question {i} about the shown material?",
             "options": [f"right {prefix}{i}", f"wrong a{prefix}{i}", f"wrong b{prefix}{i}", f"wrong c{prefix}{i}"],
             "correct_index": 0, "explanation": "Shown in the material.", "source_id": sid,
             "quote": quote, "difficulty": "medium"} for i in range(n)]})
    return reply


def _visual_library(tmp_path, fake, pages=3):
    import pymupdf

    from course_assistant.library import Library

    pdf = tmp_path / "slides.pdf"
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"Slide {i + 1}: the retrieval pipeline overview for this course.")
    doc.save(pdf)
    fake.ocr_text = OCR_DIAGRAM  # adds diagram words the text layer lacks -> visual_text
    library = Library(tmp_path / "data", fake)
    library.add_file(pdf, progress=lambda m: None)
    return library


@pytest.mark.parametrize("n, n_visual", [(5, 1), (10, 2), (1, 1)])
def test_image_questions_make_up_at_least_20_percent(tmp_path, fake, n, n_visual):
    library = _visual_library(tmp_path, fake)
    doc = next(iter(library.docs))
    assert all(p.visual_text for p in library.pages.values())
    fake.chat_replies = [_generic_reply("V", "Documents flow into the Parser", n_visual),
                         _generic_reply("S", "the retrieval pipeline overview", n)]
    quiz = generate_quiz(library, [doc], n=n, seed=3, include_images=True)
    visual = [q for q in quiz.questions if q.visual]
    assert len(visual) >= n_visual and len(visual) / len(quiz.questions) >= 0.2
    image_call = fake.chat_calls[0]
    assert "IMAGE QUESTIONS" in image_call[0]["content"]
    assert any(p.get("type") == "image_url" for p in image_call[1]["content"])  # model sees the slides
    for q in visual:
        assert q.source.kind == "image" and q.source.image_path
    pub = public_questions(quiz)
    assert all((p["image"] is not None) == q.visual for p, q in zip(pub, quiz.questions))


def test_no_images_unless_asked(tmp_path, fake):
    library = _visual_library(tmp_path, fake)
    fake.chat_replies = [_generic_reply("S", "the retrieval pipeline overview", 3)]
    quiz = generate_quiz(library, [next(iter(library.docs))], n=3, seed=1)
    assert not any(q.visual for q in quiz.questions) and len(fake.chat_calls) == 1


def test_image_questions_explain_when_material_has_no_visual_slides(lib, fake):
    bio = doc_id_by_name(lib, "biology.md")  # Markdown: no slide images at all
    fake.chat_replies = [_quiz_reply(lib, n=5)]
    quiz = generate_quiz(lib, [bio], n=5, seed=2, include_images=True)
    assert not any(q.visual for q in quiz.questions)
    assert any("image questions could be made" in n for n in quiz.notes)


def test_truncated_reply_is_salvaged_and_ids_stripped():
    from course_assistant.quiz import _salvage_questions, _schema_for, _strip_ids

    raw = ('{"questions": [{"question": "Q one?", "options": ["a","b","c","d"], "correct_index": 0, '
           '"explanation": "e", "source_id": "V1", "quote": "q"}, {"question": "Q two cut')
    assert [q["question"] for q in _salvage_questions(raw)] == ["Q one?"]
    assert _schema_for(3)["properties"]["questions"]["maxItems"] == 3
    assert _strip_ids("In the chart shown in [V2], which event happened?") == "In the chart shown, which event happened?"
    assert _strip_ids("Per [S1] the answer is clear (S3).") == "Per the answer is clear."
    assert _strip_ids("Store it in an S3 bucket with Model V2.") == "Store it in an S3 bucket with Model V2."
