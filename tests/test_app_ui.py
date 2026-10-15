"""Interface wiring checks (no browser): the quiz controls exist and the theme toggle
targets the element Gradio 6 actually uses for dark mode."""
from __future__ import annotations

import course_assistant.app as app


def _components(demo, cls: str) -> list:
    return [b for b in demo.blocks.values() if b.__class__.__name__ == cls]


def test_quiz_controls_present(monkeypatch, lib):
    monkeypatch.setattr(app, "get_library", lambda: lib)
    demo = app.build_ui()
    buttons = {b.value for b in _components(demo, "Button")}
    assert {"Generate quiz", "Retry missed questions", "Clear missed questions",
            "Clear mastered questions"} <= buttons
    sliders = _components(demo, "Slider")
    assert any(s.label == "Number of questions" and s.maximum == 20 for s in sliders)
    labels = {b.label for b in _components(demo, "Textbox")} | {b.label for b in _components(demo, "Radio")}
    assert "Topic filter (optional)" in labels and "Difficulty" in labels
    assert len(_components(demo, "BrowserState")) == 2  # missed + mastered survive reloads
    tabs = {t.label for t in _components(demo, "Tab")}
    assert {"Ask", "Quiz", "Prior Questions", "Library"} <= tabs


def test_theme_toggle_targets_body():
    # Gradio 6 puts the `dark` class on <body>; toggling <html> has no effect when the
    # system is in dark mode (the bug this guards against).
    assert "document.body.classList.toggle('dark'" in app.THEME_TOGGLE_JS
    assert "localStorage" in app.THEME_TOGGLE_JS and "localStorage" in app.THEME_ON_LOAD_JS


def test_mastered_tracks_correct_answers_only(monkeypatch, lib, fake):
    import json

    from conftest import doc_id_by_name
    from course_assistant.quiz import generate_quiz, save_quiz
    from test_quiz import _quiz_reply

    monkeypatch.setattr(app, "get_library", lambda: lib)
    fake.chat_replies = [_quiz_reply(lib, n=2)]
    quiz = generate_quiz(lib, [doc_id_by_name(lib, "biology.md")], n=2, seed=2)
    save_quiz(quiz, lib.dir)
    q1, q2 = quiz.questions
    right = f"{app.LETTERS[q1.correct_index]}. x"
    out = app._finish(0, quiz.quiz_id, {}, right, False, [], [])
    prog, mastered, md, missed = out[0], out[5], out[6], out[7]
    assert [e["question"] for e in mastered] == [q1.question] and "1" in md and missed == []
    # Revealing the solution (even after picking the right answer) is a miss, not mastered.
    pick = f"{app.LETTERS[q2.correct_index]}. x"
    out = app._finish(1, quiz.quiz_id, prog, pick, True, mastered, missed)
    mastered, missed, missed_md, mastered_md = out[5], out[7], out[8], out[9]
    assert [e["question"] for e in mastered] == [q1.question]
    assert [e["question"] for e in missed] == [q2.question]
    assert q2.question in missed_md and q1.question in mastered_md
    json.dumps([mastered, missed])  # stored in the browser, so it must be JSON


def test_missed_question_moves_to_mastered_when_answered_right(monkeypatch, lib, fake):
    from conftest import doc_id_by_name
    from course_assistant.quiz import generate_quiz, retry_missed, save_quiz
    from test_quiz import _quiz_reply

    monkeypatch.setattr(app, "get_library", lambda: lib)
    fake.chat_replies = [_quiz_reply(lib, n=1)]
    quiz = generate_quiz(lib, [doc_id_by_name(lib, "biology.md")], n=1, seed=4)
    save_quiz(quiz, lib.dir)
    q = quiz.questions[0]
    wrong = f"{app.LETTERS[(q.correct_index + 1) % 4]}. x"
    out = app._finish(0, quiz.quiz_id, {}, wrong, False, [], [])
    mastered, missed = out[5], out[7]
    assert missed[0]["your_answer"].startswith(app.LETTERS[(q.correct_index + 1) % 4])
    retry = retry_missed(quiz, {q.qid: (q.correct_index + 1) % 4})
    save_quiz(retry, lib.dir)
    out = app._finish(0, retry.quiz_id, {}, f"{app.LETTERS[q.correct_index]}. x", False, mastered, missed)
    assert out[7] == [] and [e["question"] for e in out[5]] == [q.question]


def test_old_saved_lists_of_strings_still_work():
    assert app._as_entries(["What is RAG?"]) == [{"question": "What is RAG?"}]
    assert "What is RAG?" in app._prior_md(["What is RAG?"], "mastered")
