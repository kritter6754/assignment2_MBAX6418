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
    assert {"Generate quiz", "Retry missed questions", "Reset mastered questions"} <= buttons
    labels = {b.label for b in _components(demo, "Textbox")} | {b.label for b in _components(demo, "Radio")}
    assert "Topic filter (optional)" in labels and "Difficulty" in labels
    assert _components(demo, "BrowserState")  # mastered questions survive reloads


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
    prog, *_, mastered, md = app._finish(0, quiz.quiz_id, {}, right, False, [])
    assert mastered == [q1.question] and "1" in md
    # Revealing the solution (even after picking the right answer) does not count as mastered.
    pick = f"{app.LETTERS[q2.correct_index]}. x"
    _, *_, mastered, _ = app._finish(1, quiz.quiz_id, prog, pick, True, mastered)
    assert mastered == [q1.question]
    json.dumps(mastered)  # stored in the browser, so it must be JSON
