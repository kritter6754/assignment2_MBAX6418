"""Gradio interface: Library, Ask, and Quiz tabs.

    uv run python -m course_assistant.app

Answer keys never leave the server until a student checks an answer or asks for
the solution: quizzes are stored server-side and the browser only holds a quiz id.
"""
from __future__ import annotations

import base64
import hmac
import html
import time
import logging
from pathlib import Path

import gradio as gr

from . import get_library
from .answer import AnswerResult, SourceRef, answer_question
from .config import (ACCEPTED_FORMATS, PROJECT_ROOT, ShareSettings, load_share_settings, redact,
                     register_secret, setup_logging)
from .quiz import Quiz, generate_quiz, load_quiz, public_questions, retry_missed, save_quiz

log = logging.getLogger(__name__)

MAX_Q = 20
LETTERS = "ABCD"

STATUS_BADGE = {
    "answered": "✅ **Answered from the course materials**",
    "partially_supported": "⚠️ **Answer only partly verified** — see notes below",
    "not_found": "🔎 **Not found in the selected materials**",
    "error": "❗ **Could not answer**",
}

# -- theme helper ----------------------------------------------------------------
# Gradio 6 marks dark mode with a `dark` class on <body> (set from the system setting on
# load), so that is the element to toggle. The choice is remembered in localStorage.
TO_LIGHT, TO_DARK = "☀️ Light mode", "🌙 Dark mode"

THEME_TOGGLE_JS = f"""
() => {{
    const dark = !document.body.classList.contains('dark');
    document.body.classList.toggle('dark', dark);
    document.documentElement.classList.remove('dark');
    try {{ localStorage.setItem('course-assistant-theme', dark ? 'dark' : 'light'); }} catch (e) {{}}
    return dark ? "{TO_LIGHT}" : "{TO_DARK}";
}}
"""

# -- branding ---------------------------------------------------------------------
APP_TITLE = "MBAX 6418 - Course Assistant"
APP_ICON = PROJECT_ROOT / "agentic_icon.png"


def _header_html() -> str:
    """Header with the app icon embedded inline (no file serving needed)."""
    icon = ""
    if APP_ICON.exists():
        data = base64.b64encode(APP_ICON.read_bytes()).decode()
        icon = f'<img src="data:image/png;base64,{data}" alt="" class="app-logo">'
    return (f'<div class="app-title-row">{icon}<h1>{html.escape(APP_TITLE)}</h1></div>'
            '<p>Ask questions about your course materials and practise with quizzes. '
            'Every answer cites the document and page/slide it came from.</p>')


# -- quiz loading timer -----------------------------------------------------------
# Shows only the elapsed time (e.g. "⏳ 4.2s") once, at the top of the quiz card, while a
# quiz is being generated. Runs in the browser before the request; the inputs are passed
# through unchanged. QUIZ_TIMER_STOP_JS runs after the request finishes.
QUIZ_TIMER_START_JS = """
(...args) => {
    const card = document.querySelector('.quiz-summary');
    if (window.__quizTimer) clearInterval(window.__quizTimer);
    document.querySelectorAll('.quiz-timer').forEach(e => e.remove());
    if (card) {
        const el = document.createElement('div');
        el.className = 'quiz-timer';
        card.prepend(el);
        const t0 = performance.now();
        const tick = () => { el.textContent = '⏳ ' + ((performance.now() - t0) / 1000).toFixed(1) + 's'; };
        tick();
        window.__quizTimer = setInterval(tick, 100);
    }
    return args;
}
"""

QUIZ_TIMER_STOP_JS = """
() => {
    if (window.__quizTimer) clearInterval(window.__quizTimer);
    window.__quizTimer = null;
    document.querySelectorAll('.quiz-timer').forEach(e => e.remove());
}
"""


# -- page styling ----------------------------------------------------------------
# Soft, rounded "card" look: white surfaces with blue accents in light mode, deep navy
# surfaces with the same blue accents in dark mode. Gradio applies the *_dark values
# whenever <body> has the `dark` class, which is what the mode toggle switches.
ACCENT = "#2F6BFF"
ACCENT_HOVER = "#1F57E6"
ACCENT_DARK_TEXT = "#8DB0FF"


def make_theme() -> gr.themes.Base:
    return gr.themes.Soft(
        primary_hue=gr.themes.colors.blue,
        secondary_hue=gr.themes.colors.blue,
        neutral_hue=gr.themes.colors.slate,
        radius_size=gr.themes.sizes.radius_lg,
        font=[gr.themes.GoogleFont("Inter"), "system-ui", "sans-serif"],
    ).set(
        # page and text
        body_background_fill="#F3F6FC", body_background_fill_dark="#0B1020",
        body_text_color="#111111", body_text_color_dark="#FFFFFF",
        body_text_color_subdued="#6B7280", body_text_color_subdued_dark="#A9B3CC",
        block_title_text_color="#111111", block_label_text_color="#4B5563",
        link_text_color=ACCENT, link_text_color_dark=ACCENT_DARK_TEXT,
        # cards
        block_background_fill="#FFFFFF", block_background_fill_dark="#141A2E",
        background_fill_primary="#FFFFFF", background_fill_primary_dark="#141A2E",
        background_fill_secondary="#F7F9FD", background_fill_secondary_dark="#10162A",
        panel_background_fill="#FFFFFF", panel_background_fill_dark="#141A2E",
        block_border_width="0px", block_radius="18px", container_radius="18px",
        block_shadow="0 6px 24px rgba(30, 50, 110, 0.07)",
        block_shadow_dark="0 6px 24px rgba(0, 0, 0, 0.35)",
        block_label_background_fill="transparent", block_label_background_fill_dark="transparent",
        border_color_primary="#E6EBF5", border_color_primary_dark="#232B45",
        # inputs
        input_background_fill="#F7F9FD", input_background_fill_dark="#0F1528", input_radius="14px",
        # buttons
        button_primary_background_fill=ACCENT, button_primary_background_fill_dark=ACCENT,
        button_primary_background_fill_hover=ACCENT_HOVER, button_primary_text_color="#FFFFFF",
        button_secondary_background_fill="#EEF3FF", button_secondary_background_fill_dark="#1C2542",
        button_secondary_text_color=ACCENT, button_secondary_text_color_dark=ACCENT_DARK_TEXT,
        button_border_width="0px", button_large_radius="999px", button_small_radius="999px",
        # accents: checkboxes, radios, sliders
        color_accent=ACCENT, color_accent_soft="#EEF3FF", color_accent_soft_dark="#1C2542",
        slider_color=ACCENT, checkbox_background_color_selected=ACCENT,
        checkbox_label_background_fill="#F7F9FD", checkbox_label_border_width="1px",
        checkbox_label_background_fill_selected="#EEF3FF",
        checkbox_label_background_fill_selected_dark="#1C2542",
        checkbox_label_text_color_selected=ACCENT, checkbox_label_text_color_selected_dark="#FFFFFF",
        checkbox_label_border_color_selected=ACCENT,
        # tables and code
        table_even_background_fill="#FFFFFF", table_odd_background_fill="#F7F9FD",
        table_even_background_fill_dark="#141A2E", table_odd_background_fill_dark="#10162A",
        code_background_fill="#F1F4FB", code_background_fill_dark="#0F1528",
    )


CUSTOM_CSS = f"""
.gradio-container {{ max-width: 1280px !important; margin: 0 auto !important;
                     overflow: clip !important; }}  /* clip (not hidden) so the scoreboard can stick */

/* header card: white in light mode, navy in dark mode */
#app-header {{
    background: var(--block-background-fill);
    border-radius: 22px;
    padding: 18px 24px;
    align-items: center;
    box-shadow: var(--block-shadow);
}}
#app-header .block, #app-header .prose, #app-header .md {{
    background: transparent !important; border: none !important; box-shadow: none !important;
}}
#app-header .app-title-row {{ display: flex; align-items: center; gap: 14px; }}
#app-header h1 {{ margin: 0; font-size: 1.75rem; font-weight: 700; color: var(--body-text-color); }}
#app-header p {{ margin: 4px 0 0; color: var(--body-text-color-subdued); }}
#app-header .app-logo {{ width: 52px; height: 52px; flex-shrink: 0; }}

#theme-toggle-btn {{ min-width: 150px; }}

/* centred, wide tab bar on its own pill-shaped track */
[role="tablist"] {{
    display: flex !important; justify-content: center; gap: 8px;
    width: fit-content; max-width: 100%; margin: 8px auto 4px !important;
    background: var(--block-background-fill); border-radius: 999px; padding: 6px !important;
    box-shadow: var(--block-shadow); border: none !important;
}}
button[role="tab"] {{
    border-radius: 999px !important; padding: 12px 20px !important; min-width: 180px;
    font-size: 1.05rem !important; font-weight: 600 !important; justify-content: center;
    border: none !important; color: var(--body-text-color-subdued) !important;
}}
@media (max-width: 640px) {{
    [role="tablist"] {{ width: 100%; }}
    button[role="tab"] {{ min-width: 0; flex: 1; padding: 10px 8px !important; }}
}}
button[role="tab"][aria-selected="true"] {{
    background: {ACCENT} !important; color: #FFFFFF !important;
    box-shadow: 0 6px 16px rgba(47, 107, 255, 0.30);
}}
button[role="tab"][aria-selected="true"]::after {{ display: none !important; }}
button[role="tab"]:not([aria-selected="true"]):hover {{ background: var(--color-accent-soft) !important; }}

/* softer, bubblier controls */
button.lg, button.md {{ font-weight: 600; }}
button.primary {{ box-shadow: 0 6px 16px rgba(47, 107, 255, 0.30); }}
label.selected, .wrap label {{ border-radius: 999px !important; }}
.gr-group, .group {{ border-radius: 18px !important; }}
.card {{
    background: var(--block-background-fill); border-radius: 18px; padding: 20px 22px !important;
    box-shadow: var(--block-shadow);
}}
.card .block, .card .prose {{ background: transparent !important; box-shadow: none !important; }}
.q-card {{ gap: 12px !important; }}
.q-card p, .q-card strong {{ line-height: 1.5; }}
.q-actions {{ gap: 10px !important; justify-content: flex-start; }}
.q-slide, .q-slide .image-container, .q-slide .image-frame {{
    border: none !important; background: transparent !important; box-shadow: none !important;
}}
.q-slide img {{ border-radius: 14px; box-shadow: var(--block-shadow); }}
.quiz-summary {{ gap: 6px !important; position: sticky; top: 8px; z-index: 1000; isolation: isolate; }}
/* question text: own padding so the rounded container doesn't clip it */
.q-title, .q-title .prose {{ overflow: visible !important; border-radius: 0 !important; }}
.q-title {{ padding: 4px 6px 0 !important; }}
.q-title p, .q-title strong {{ font-size: 0.93rem !important; line-height: 1.5; }}
/* quiz loading timer */
/* Gradio still draws its status text for show_progress="hidden" events; hide it. */
.quiz-summary .wrap.hidden, .quiz-summary .wrap.hidden .progress-text {{ display: none !important; }}
.quiz-timer {{
    align-self: flex-start; width: fit-content; padding: 6px 14px; border-radius: 999px; font-weight: 600;
    background: var(--color-accent-soft); color: {ACCENT}; font-variant-numeric: tabular-nums;
}}
.dark .quiz-timer {{ color: {ACCENT_DARK_TEXT}; }}
/* live scoreboard */
.scoreboard {{ display: flex; flex-wrap: wrap; gap: 10px; }}
.score-pill {{
    display: flex; align-items: baseline; gap: 8px; padding: 8px 16px; border-radius: 999px;
    background: var(--color-accent-soft); color: var(--body-text-color); font-size: 0.9rem;
}}
.score-pill b {{ font-size: 1.05rem; }}
.score-pill.main {{ background: {ACCENT}; color: #FFFFFF; }}
.score-pill.ok b {{ color: #16A34A; }}
.score-pill.bad b {{ color: #DC2626; }}
.dark .score-pill.ok b {{ color: #4ADE80; }}
.dark .score-pill.bad b {{ color: #F87171; }}
.score-note {{ margin: 8px 4px 0; color: var(--body-text-color-subdued); font-size: 0.9rem; }}
/* Prior Questions: one card split down the middle */
.prior-split {{ gap: 0 !important; align-items: stretch; }}
.prior-split .prior-col {{ padding: 4px 24px !important; min-width: 0; }}
.prior-split .prior-right {{ border-left: 2px solid var(--border-color-primary); }}
.prior-split h2 {{ margin: 0 0 8px; font-size: 1.35rem; font-weight: 700; }}
@media (max-width: 760px) {{
    .prior-split .prior-right {{ border-left: none; border-top: 2px solid var(--border-color-primary);
                                padding-top: 16px !important; }}
}}
/* Text must never touch a rounded edge: Gradio's text blocks hide overflow and have
   rounded corners, which clipped the first letters of headings and lines. Applies to every
   Markdown/HTML block, label and table title in the app. */
.block.hide-container, .block:has(> .prose), .block:has(.md.prose) {{
    overflow: visible !important; border-radius: 0 !important;
}}
.prose, .md {{ padding-left: 3px !important; padding-right: 3px !important; overflow: visible !important; }}
.prose h1, .prose h2, .prose h3 {{ overflow: visible; }}
[data-testid="block-info"], .block-title, .block-label {{ padding-left: 3px; overflow: visible !important; }}
.table-wrap, .table-container {{ overflow-x: auto; }}
table td, table th, table input {{ font-family: var(--font) !important; }}
img[alt^="[S"] {{ border-radius: 14px !important; border: none !important; box-shadow: var(--block-shadow); }}
"""


THEME_ON_LOAD_JS = f"""
() => {{
    let saved = null;
    try {{ saved = localStorage.getItem('course-assistant-theme'); }} catch (e) {{}}
    if (saved === 'dark' || saved === 'light') document.body.classList.toggle('dark', saved === 'dark');
    return document.body.classList.contains('dark') ? "{TO_LIGHT}" : "{TO_DARK}";
}}
"""

def _safe_error(e: Exception) -> str:
    return redact(f"{type(e).__name__}: {e}")


# -- library helpers -----------------------------------------------------------
def _doc_choices() -> list[tuple[str, str]]:
    lib = get_library()
    return sorted(((d.name, d.doc_id) for d in lib.docs.values()), key=lambda x: x[0])


def _doc_table() -> list[list]:
    lib = get_library()
    rows = []
    for d in sorted(lib.docs.values(), key=lambda d: d.name):
        rows.append([d.name, d.ext, d.num_pages, d.kind + "s",
                     "yes" if d.has_images else "no", "; ".join(d.warnings)])
    return rows


def accepted_formats_md() -> str:
    lines = ["**Accepted formats** (e.g. files downloaded from Canvas):", ""]
    lines += [f"- `{ext}` — {desc}" for ext, desc in ACCEPTED_FORMATS.items()]
    lines += ["", "Other files (e.g. `.xlsx`, `.zip`, videos) are rejected with a message. "
              "Slides and pages are stored with their original images so answers can show them."]
    return "\n".join(lines)


# When the app is public without a login, adding/removing documents needs this password.
_EDIT_PASSWORD: str | None = None


WRONG_PASSWORD = "Wrong library password. Adding or removing documents needs the library password."


def _password_ok(password: str | None) -> bool:
    """True when no library password is set, or the given one matches. On a mismatch shows a
    non-blocking warning (a raised error would lock the controls behind an error overlay)."""
    if not _EDIT_PASSWORD or hmac.compare_digest((password or "").encode(), _EDIT_PASSWORD.encode()):
        return True
    gr.Warning(WRONG_PASSWORD)
    return False


def add_files(files, password=None, progress=gr.Progress()):
    if not _password_ok(password):
        return (f"✗ {WRONG_PASSWORD}",) + (gr.update(),) * 5
    lib = get_library()
    msgs = []
    for f in files or []:
        path = Path(f if isinstance(f, str) else f.name)
        name = path.name
        if path.suffix.lower() not in ACCEPTED_FORMATS:
            msgs.append(f"✗ {name}: unsupported format ({path.suffix or 'no extension'})")
            continue
        try:
            progress(0, desc=f"Processing {name}")
            info = lib.add_file(path, name=name, progress=lambda m: progress(0.5, desc=m))
            msgs.append(f"✓ {info.name}: {info.num_pages} {info.kind}s")
            msgs += [f"   warning: {w}" for w in info.warnings]
        except Exception as e:
            log.exception("ingest failed")
            msgs.append(f"✗ {name}: {_safe_error(e)}")
    choices = _doc_choices()
    return ("\n".join(msgs) or "No files selected.", _doc_table(),
            gr.update(choices=choices), gr.update(choices=choices), gr.update(choices=choices),
            gr.update(choices=choices))


def remove_doc(doc_id, password=None):
    if not _password_ok(password):
        return (gr.update(),) * 5
    if doc_id:
        get_library().remove(doc_id)
    choices = _doc_choices()
    return (_doc_table(), gr.update(choices=choices, value=None), gr.update(choices=choices),
            gr.update(choices=choices), gr.update(choices=choices, value=None))


def show_page(doc_id, number):
    lib = get_library()
    if not doc_id:
        return None, "Choose a document."
    pages = lib.doc_pages(doc_id)
    if not pages:
        return None, "No pages."
    number = int(min(max(1, number or 1), len(pages)))
    p = pages[number - 1]
    header = f"{p.doc_name} — {p.label} of {len(pages)}" + (" (text from OCR)" if p.ocr else "")
    body = p.text or "(no text)"
    if p.visual_text:
        body += "\n\n--- Text read from the page image (OCR) ---\n" + p.visual_text
    return p.image_path, f"{header}\n\n{body}"


# -- ask -------------------------------------------------------------------------
def _image_html(path: str | None, alt: str) -> str:
    if not path:
        return ""
    src = "/gradio_api/file=" + str(Path(path).resolve())  # served from allowed_paths (the data dir)
    return (f'\n\n<a href="{src}" target="_blank"><img src="{src}" alt="{html.escape(alt)}" '
            f'style="width:100%;max-width:640px;height:auto;max-height:none;border:1px solid #ccc;border-radius:6px"></a>')


def _source_md(s: SourceRef) -> str:
    where = f"[{s.eid}] {s.doc_name} — {s.location}"
    quote = html.escape(s.excerpt).replace("\n", " ")
    if not s.quote_verified:
        text = (f"**{where}** (visual evidence: see the original below)\n\n"
                f"> _Model's reading of the image:_ {quote}")
    else:
        text = f"**{where}**\n\n> {quote}"
    return text + _image_html(s.image_path, where)


def render_answer(r: AnswerResult) -> tuple[str, str]:
    answer_md = f"{STATUS_BADGE[r.status]}\n\n{r.answer}"
    if r.sources:
        sources_md = "### Sources\n\n" + "\n\n".join(_source_md(s) for s in r.sources)
    else:
        sources_md = "_No sources: nothing in the materials supports an answer._"
    if r.notes:
        sources_md += "\n\n<details><summary>Checks and notes</summary>\n\n" + \
            "\n".join(f"- {html.escape(n)}" for n in r.notes) + "\n</details>"
    return answer_md, sources_md


def ask(question, doc_ids):
    try:
        r = answer_question(get_library(), question, doc_ids or None)
    except Exception as e:
        log.exception("ask failed")
        return f"{STATUS_BADGE['error']}\n\n{_safe_error(e)}", ""
    return render_answer(r)

# -- quiz --------------------------------------------------------------------------
def _quiz(quiz_id: str | None) -> Quiz | None:
    if not quiz_id:
        return None
    return load_quiz(quiz_id, get_library().dir)


def _score_md(quiz: Quiz, progress: dict) -> str:
    """Scoreboard, updated on every Check answer / Show solution. A question counts as
    correct only if it was answered correctly before revealing the solution."""
    key = {q.qid: q.correct_index for q in quiz.questions}
    total = len(quiz.questions)
    finished = len(progress)
    correct = sum(1 for qid, p in progress.items()
                  if not p.get("revealed") and p.get("chosen") is not None and p["chosen"] == key.get(qid))
    incorrect = finished - correct
    revealed = sum(1 for p in progress.values() if p.get("revealed"))

    def pill(label: str, value: str, kind: str = "") -> str:
        return f'<div class="score-pill {kind}"><span>{label}</span><b>{value}</b></div>'

    board = ('<div class="scoreboard">'
             + pill("Score", f"{correct} / {total}", "main")
             + pill("Correct", str(correct), "ok")
             + pill("Incorrect", str(incorrect), "bad")
             + pill("Questions finished", f"{finished} / {total}")
             + "</div>")
    if revealed:
        board += (f'<p class="score-note">{revealed} solution{"s" if revealed != 1 else ""} revealed, '
                  "counted as incorrect.</p>")
    if total and finished == total:
        board += f'<p class="score-note">🎉 Quiz complete: {correct} of {total} correct.</p>'
    return board


def _feedback_md(quiz: Quiz, i: int, chosen: int | None) -> str:
    q = quiz.questions[i]
    correct = f"**{LETTERS[q.correct_index]}. {html.escape(q.options[q.correct_index])}**"
    if chosen is None:
        head = f"Solution: {correct}"
    elif chosen == q.correct_index:
        head = f"✅ Correct — {correct}"
    else:
        head = f"❌ Incorrect. You chose {LETTERS[chosen]}; the answer is {correct}"
    s = q.source
    return (f"{head}\n\n{html.escape(q.explanation)}\n\n"
            f"**Source: {s.doc_name} — {s.location}**\n> {html.escape(s.excerpt)}")


# -- prior questions (kept in the browser) -----------------------------------------
def _as_entries(items: list | None) -> list[dict]:
    """Saved entries are dicts; lists saved by older versions hold plain question strings."""
    return [i if isinstance(i, dict) else {"question": str(i)} for i in (items or [])]


def _entry(q, chosen: int | None) -> dict:
    return {
        "question": q.question,
        "answer": f"{LETTERS[q.correct_index]}. {q.options[q.correct_index]}",
        "your_answer": f"{LETTERS[chosen]}. {q.options[chosen]}" if chosen is not None else "",
        "source": f"{q.source.doc_name} — {q.source.location}",
        "date": time.strftime("%Y-%m-%d"),
    }


def _upsert(items: list[dict], entry: dict) -> list[dict]:
    """Add `entry`, replacing an older entry for the same question."""
    return [e for e in items if e["question"] != entry["question"]] + [entry]


def _without(items: list[dict], question: str) -> list[dict]:
    return [e for e in items if e["question"] != question]


def _prior_md(items: list | None, kind: str) -> str:
    entries = _as_entries(items)
    if not entries:
        return ("_No missed questions. Questions you get wrong or reveal will show up here._"
                if kind == "missed" else
                "_No mastered questions yet. Questions you answer correctly will show up here._")
    blocks = [f"_{len(entries)} question{'s' if len(entries) != 1 else ''}, newest first_"]
    for n, e in enumerate(reversed(entries), start=1):
        lines = [f"**{n}. {html.escape(e['question'])}**"]
        if e.get("answer"):
            lines.append(f"✅ Answer: {html.escape(e['answer'])}")
        if kind == "missed":
            lines.append(f"❌ Your answer: {html.escape(e['your_answer'])}" if e.get("your_answer")
                         else "👁️ Solution revealed without answering")
        if e.get("source"):
            lines.append(f"📄 {html.escape(e['source'])}")
        blocks.append("  \n".join(lines))
    return "\n\n".join(blocks)


def _prior_views(missed, mastered) -> tuple[str, str]:
    return _prior_md(missed, "missed"), _prior_md(mastered, "mastered")


def _mastered_md(mastered: list | None) -> str:
    n = len(mastered or [])
    if not n:
        return "_Mastered questions: none yet. Questions you answer correctly won't be asked again._"
    return f"_Mastered questions: **{n}**. New quizzes won't repeat them._"


def _render_quiz(quiz: Quiz | None, message: str = "", requested: int | None = None) -> list:
    """Outputs for: quiz_id, progress, header, score, then per-slot groups, text, radios,
    feedback, images. Only questions and options are sent; answers stay on the server."""
    if quiz is None:
        return ([None, {}, message, ""] + [gr.update(visible=False)] * MAX_Q
                + [gr.update(value="")] * MAX_Q + [gr.update(choices=[], value=None)] * MAX_Q
                + [gr.update(value="")] * MAX_Q + [gr.update(value=None, visible=False)] * MAX_Q)
    pub = public_questions(quiz)  # no answers leave the server here
    if quiz.retry_of:
        header = f"**Retry of quiz `{quiz.retry_of}`**: {len(pub)} missed question(s)"
    else:
        header = f"**Quiz `{quiz.quiz_id}`**: {len(pub)} questions from {', '.join(quiz.doc_names)}"
        header += f" · difficulty: *{quiz.difficulty}*"
        if quiz.topic:
            header += f" · topic: *{html.escape(quiz.topic)}*"
    if requested and len(pub) < requested:
        header += f"\n\n_{len(pub)} of {requested} requested questions passed validation._"
    if message:
        header += f"\n\n{message}"
    groups, qmd, radios, fb, imgs = [], [], [], [], []
    for i in range(MAX_Q):
        if i < len(pub):
            q = pub[i]
            level = quiz.questions[i].difficulty
            tag = f" <small>({level})</small>" if level else ""
            groups.append(gr.update(visible=True))
            qmd.append(gr.update(value=f"**Q{i + 1}. {html.escape(q['question'])}**{tag}"))
            radios.append(gr.update(choices=[f"{LETTERS[j]}. {o}" for j, o in enumerate(q["options"])],
                                    value=None, interactive=True))
        else:
            groups.append(gr.update(visible=False))
            qmd.append(gr.update(value=""))
            radios.append(gr.update(choices=[], value=None))
        fb.append(gr.update(value=""))
        imgs.append(gr.update(value=None, visible=False))
    return [quiz.quiz_id, {}, header, _score_md(quiz, {})] + groups + qmd + radios + fb + imgs


def make_quiz(doc_ids, topic, difficulty, n, mastered):
    lib = get_library()
    try:
        if not doc_ids:
            raise ValueError("Choose at least one document for the quiz.")
        quiz = generate_quiz(lib, doc_ids, topic or "", int(n), difficulty=difficulty,
                             exclude_questions=[e["question"] for e in _as_entries(mastered)])
        save_quiz(quiz, lib.dir)
    except Exception as e:
        log.exception("quiz failed")
        return _render_quiz(None, f"❗ {_safe_error(e)}")
    return _render_quiz(quiz, requested=int(n))


def redo_missed(quiz_id, progress):
    quiz = _quiz(quiz_id)
    if quiz is None:
        return [gr.update()] * 4 + [gr.update()] * (5 * MAX_Q)
    responses = {qid: p.get("chosen") for qid, p in (progress or {}).items()}
    retry = retry_missed(quiz, responses)
    if retry is None:
        return [gr.update(), gr.update(), gr.update(), _score_md(quiz, progress or {})
                + '<p class="score-note">🎉 Nothing to retry: every question was answered correctly.</p>'] + \
            [gr.update()] * (5 * MAX_Q)
    save_quiz(retry, get_library().dir)
    return _render_quiz(retry)


def clear_missed(mastered):
    return [], *_prior_views([], mastered)


def clear_mastered(missed):
    return [], _mastered_md([]), *_prior_views(missed, [])


def _finish(i: int, quiz_id, progress: dict, choice: str | None, reveal: bool,
            mastered: list | None, missed: list | None):
    """Outputs: progress, feedback, radio, image, score, mastered, mastered count,
    missed, missed list, mastered list."""
    quiz = _quiz(quiz_id)
    mastered, missed = _as_entries(mastered), _as_entries(missed)
    keep = [gr.update()] * 3
    if quiz is None or i >= len(quiz.questions):
        return progress, gr.update(), gr.update(), gr.update(), gr.update(), mastered, gr.update(), missed, *keep[:2]
    q = quiz.questions[i]
    progress = dict(progress or {})
    if q.qid in progress:  # already finished: answers are final
        return (progress, gr.update(), gr.update(), gr.update(), _score_md(quiz, progress), mastered,
                gr.update(), missed, *keep[:2])
    if not reveal and not choice:
        return (progress, gr.update(value="Choose an answer first."), gr.update(), gr.update(), gr.update(),
                mastered, gr.update(), missed, *keep[:2])
    chosen = LETTERS.index(choice[0]) if choice else None  # revealing after picking still grades the pick
    progress[q.qid] = {"chosen": chosen, "revealed": reveal}
    # Mastered = answered correctly before seeing the solution; anything else is missed.
    if not reveal and chosen == q.correct_index:
        mastered = _upsert(mastered, _entry(q, chosen))
        missed = _without(missed, q.question)
    else:
        missed = _upsert(missed, _entry(q, chosen))
    img = gr.update(value=q.source.image_path, visible=bool(q.source.image_path),
                    label=f"{q.source.doc_name} — {q.source.location}")
    return (progress, _feedback_md(quiz, i, chosen), gr.update(interactive=False), img,
            _score_md(quiz, progress), mastered, _mastered_md(mastered), missed, *_prior_views(missed, mastered))


def build_ui(read_only: bool = False, edit_password: str | None = None) -> gr.Blocks:
    """read_only=True leaves out upload and remove entirely, so visitors can't change the library."""
    global _EDIT_PASSWORD
    _EDIT_PASSWORD = edit_password or None
    lib = get_library()
    choices = _doc_choices()
    
    with gr.Blocks(title=APP_TITLE) as demo:
        with gr.Row(elem_id="app-header"):
            gr.HTML(_header_html())
            theme_btn = gr.Button("🌓 Dark / light mode", elem_id="theme-toggle-btn", scale=0)
            theme_btn.click(fn=None, inputs=None, outputs=theme_btn, js=THEME_TOGGLE_JS)
            demo.load(fn=None, inputs=None, outputs=theme_btn, js=THEME_ON_LOAD_JS)
        # Questions answered correctly, kept in this browser across reloads. Defined outside
        # the tabs: Gradio only mounts a tab's components when it is opened, and a
        # BrowserState that isn't mounted yet is read as its default ([]) on page load.
        mastered = gr.BrowserState([], storage_key="course-assistant-mastered")
        missed = gr.BrowserState([], storage_key="course-assistant-missed")

        with gr.Tab("Ask"):
            with gr.Row():
                with gr.Column(scale=3):
                    question = gr.Textbox(label="Your question", lines=2,
                                          placeholder="e.g. Why does RAG use both keyword and vector search?")
                    ask_docs = gr.CheckboxGroup(choices=choices, label="Search in (leave empty for all documents)")
                    ask_btn = gr.Button("Ask", variant="primary")
                with gr.Column(scale=4, elem_classes=["card"]):
                    answer_md = gr.Markdown("_Your answer and its sources will appear here._")
                    sources_md = gr.Markdown()
            ask_btn.click(ask, [question, ask_docs], [answer_md, sources_md])
            question.submit(ask, [question, ask_docs], [answer_md, sources_md])

        with gr.Tab("Quiz") as quiz_tab:
            quiz_id = gr.State(None)
            quiz_progress = gr.State({})

            with gr.Row():
                quiz_docs = gr.CheckboxGroup(choices=choices, label="Material for the quiz")
                with gr.Column():
                    topic = gr.Textbox(label="Topic filter (optional)", placeholder="e.g. KV cache")
                    difficulty = gr.Radio(choices=[("Mixed", "mixed"), ("Easy", "easy"), ("Medium", "medium"),
                                                   ("Hard", "hard")], value="mixed", label="Difficulty")
                    n_q = gr.Slider(1, MAX_Q, value=5, step=1, label="Number of questions")
                    gen_btn = gr.Button("Generate quiz", variant="primary")
                    redo_btn = gr.Button("Retry missed questions", size="sm")
                    mastered_md = gr.Markdown(_mastered_md([]))
            with gr.Column(elem_classes=["card", "quiz-summary"]):
                quiz_header = gr.Markdown("_Choose material and press **Generate quiz** to start._")
                score_md = gr.HTML()
            groups, qmds, radios, fbs, imgs = [], [], [], [], []
            finish_outs = []
            for i in range(MAX_Q):
                with gr.Column(visible=False, elem_classes=["card", "q-card"]) as g:
                    qmd = gr.Markdown(elem_classes=["q-title"])
                    radio = gr.Radio(choices=[], label="Your answer")
                    with gr.Row(elem_classes=["q-actions"]):
                        check = gr.Button("Check answer", size="sm", variant="primary", scale=0, min_width=150)
                        reveal = gr.Button("Show solution", size="sm", scale=0, min_width=150)
                    fb = gr.Markdown()
                    img = gr.Image(visible=False, interactive=False, height=340, show_label=False,
                                   elem_classes=["q-slide"])
                groups.append(g); qmds.append(qmd); radios.append(radio); fbs.append(fb); imgs.append(img)
                finish_outs.append((check, reveal, fb, radio, img))
            quiz_outs = [quiz_id, quiz_progress, quiz_header, score_md] + groups + qmds + radios + fbs + imgs
            # Loading indicator: only our own elapsed-time pill (no Gradio bar or status text).
            gen_btn.click(make_quiz, [quiz_docs, topic, difficulty, n_q, mastered], quiz_outs,
                          show_progress="hidden", js=QUIZ_TIMER_START_JS
                          ).then(fn=None, js=QUIZ_TIMER_STOP_JS)
            redo_btn.click(redo_missed, [quiz_id, quiz_progress], quiz_outs, show_progress="hidden")
            demo.load(_mastered_md, [mastered], [mastered_md])
            quiz_tab.select(_mastered_md, [mastered], [mastered_md])

        with gr.Tab("Prior Questions") as prior_tab:
            with gr.Row(elem_classes=["card", "prior-split"], equal_height=False):
                with gr.Column(elem_classes=["prior-col"]):
                    gr.Markdown("## Missed Questions")
                    missed_list = gr.Markdown(_prior_md([], "missed"))
                    clear_missed_btn = gr.Button("Clear missed questions", size="sm", scale=0, min_width=200)
                with gr.Column(elem_classes=["prior-col", "prior-right"]):
                    gr.Markdown("## Mastered Questions")
                    mastered_list = gr.Markdown(_prior_md([], "mastered"))
                    clear_mastered_btn = gr.Button("Clear mastered questions", size="sm", scale=0, min_width=200)
            prior_tab.select(_prior_views, [missed, mastered], [missed_list, mastered_list])
            demo.load(_prior_views, [missed, mastered], [missed_list, mastered_list])
            clear_missed_btn.click(clear_missed, [mastered], [missed, missed_list, mastered_list])
            clear_mastered_btn.click(clear_mastered, [missed], [mastered, mastered_md, missed_list, mastered_list])

        # Check / reveal buttons update the quiz card, the score, and both prior-question lists.
        for i, (check, reveal, fb, radio, img) in enumerate(finish_outs):
            outs = [quiz_progress, fb, radio, img, score_md, mastered, mastered_md, missed,
                    missed_list, mastered_list]
            ins = [quiz_id, quiz_progress, radio, mastered, missed]
            check.click(lambda qid, prog, ch, m, ms, i=i: _finish(i, qid, prog, ch, False, m, ms), ins, outs)
            reveal.click(lambda qid, prog, ch, m, ms, i=i: _finish(i, qid, prog, ch, True, m, ms), ins, outs)

        with gr.Tab("Library"):
            if read_only:
                gr.Markdown("_This shared copy is read-only: browse the documents below; "
                            "adding or removing files is turned off._")
            else:
                with gr.Row():
                    with gr.Column():
                        upload = gr.File(label="Add course files", file_count="multiple",
                                         file_types=list(ACCEPTED_FORMATS))
                        add_btn = gr.Button("Add to library", variant="primary")
                        add_log = gr.Textbox(label="Status", lines=4, interactive=False)
                    with gr.Column(elem_classes=["card"]):
                        gr.Markdown(accepted_formats_md())
            table = gr.Dataframe(value=_doc_table(), label=f"Library ({len(lib.docs)} documents at start-up)",
                                 headers=["File", "Type", "Pages", "Unit", "Images", "Warnings"],
                                 interactive=False, wrap=True)
            if not read_only:
                with gr.Row():
                    rm_doc = gr.Dropdown(choices=choices, value=None, label="Remove a document")
                    rm_btn = gr.Button("Remove", variant="stop")
                if edit_password:
                    lib_pw = gr.Textbox(label="Library password (needed to add or remove documents)",
                                        type="password", placeholder="Library password")
                else:
                    lib_pw = gr.State(None)
            gr.Markdown("### Browse a document\nSee exactly what was extracted from each page or slide.")
            with gr.Row():
                view_doc = gr.Dropdown(choices=choices, value=None, label="Document")
                view_n = gr.Number(value=1, precision=0, minimum=1, label="Page / slide")
            with gr.Row():
                view_img = gr.Image(interactive=False, label="Original", height=360)
                view_txt = gr.Textbox(label="Extracted text", lines=16, max_lines=30, interactive=False)
            if not read_only:
                add_btn.click(add_files, [upload, lib_pw], [add_log, table, ask_docs, quiz_docs, rm_doc, view_doc])
                rm_btn.click(remove_doc, [rm_doc, lib_pw], [table, rm_doc, ask_docs, quiz_docs, view_doc])
            view_doc.change(show_page, [view_doc, view_n], [view_img, view_txt])
            view_n.change(show_page, [view_doc, view_n], [view_img, view_txt])
            
    return demo


def launch_options(s: ShareSettings) -> dict:
    """Keyword arguments for launch(). A public link without a password is refused."""
    if s.public and s.require_login and not s.password:
        raise SystemExit("A public app (SHARE=true or HOST=0.0.0.0) needs APP_PASSWORD, or "
                         "REQUIRE_LOGIN=false to deliberately run it without a login.")
    opts: dict = {"server_name": s.host, "server_port": s.port, "share": s.share, "show_error": False}
    if s.public and s.require_login and s.password:  # local runs never need a login
        opts["auth"] = (s.username, s.password)
        opts["auth_message"] = "MBAX 6418 Course Assistant: ask your team for the password."
    return opts


def main() -> None:
    setup_logging()
    share = load_share_settings()
    register_secret(share.password)
    opts = launch_options(share)
    lib = get_library()
    # Public without a login: library edits are protected by the app password instead.
    edit_password = share.password if (share.public and "auth" not in opts and not share.read_only) else None
    if share.public and "auth" not in opts and not share.read_only and not edit_password:
        raise SystemExit("A public app without a login can only edit the library with APP_PASSWORD set "
                         "(or set READ_ONLY=true).")
    demo = build_ui(read_only=share.read_only, edit_password=edit_password)
    if share.share:
        log.info("Creating a public link (up to a week, only while this computer runs the app); "
                 "read-only=%s", share.read_only)
    log.info("Serving on %s:%s (read-only=%s, login=%s, documents=%d)", share.host, share.port,
             share.read_only, "auth" in opts, len(lib.docs))
    if share.public and "auth" not in opts:
        log.warning("Public app without a login (REQUIRE_LOGIN=false): anyone with the link can use it.")
    if not lib.docs:
        log.warning("The library at %s is empty. On Railway, upload it with scripts/deploy_railway.sh.",
                    lib.dir)
    demo.queue(default_concurrency_limit=4).launch(
        allowed_paths=[str(lib.dir)], theme=make_theme(), css=CUSTOM_CSS,
        favicon_path=str(APP_ICON) if APP_ICON.exists() else None, **opts)


if __name__ == "__main__":
    main()
