"""Gradio interface: Library, Ask, and Quiz tabs.

    uv run python -m course_assistant.app

Answer keys never leave the server until a student checks an answer or asks for
the solution: quizzes are stored server-side and the browser only holds a quiz id.
"""
from __future__ import annotations

import html
import logging
from pathlib import Path

import gradio as gr

from . import get_library
from .answer import AnswerResult, SourceRef, answer_question
from .config import ACCEPTED_FORMATS, ShareSettings, load_share_settings, redact, register_secret, setup_logging
from .quiz import Quiz, generate_quiz, grade, load_quiz, public_questions, save_quiz

log = logging.getLogger(__name__)

MAX_Q = 10
LETTERS = "ABCD"

STATUS_BADGE = {
    "answered": "✅ **Answered from the course materials**",
    "partially_supported": "⚠️ **Answer only partly verified** — see notes below",
    "not_found": "🔎 **Not found in the selected materials**",
    "error": "❗ **Could not answer**",
}


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


def add_files(files, progress=gr.Progress()):
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


def remove_doc(doc_id):
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
    responses = {qid: p.get("chosen") for qid, p in progress.items()}
    g = grade(quiz, responses)
    done = len(progress)
    revealed = sum(1 for p in progress.values() if p.get("revealed") and p.get("chosen") is None)
    txt = f"### Score: {g.score} / {g.total}  \n{done} of {g.total} questions finished"
    if revealed:
        txt += f" ({revealed} revealed without answering, counted as incorrect)"
    return txt


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


def make_quiz(doc_ids, topic, n, progress=gr.Progress()):
    lib = get_library()
    hidden = [gr.update(visible=False)] * MAX_Q
    blank = [gr.update(value="")] * MAX_Q
    try:
        if not doc_ids:
            raise ValueError("Choose at least one document for the quiz.")
        progress(0.2, desc="Retrieving material and writing questions")
        quiz = generate_quiz(lib, doc_ids, topic or "", int(n))
        save_quiz(quiz, lib.dir)
    except Exception as e:
        log.exception("quiz failed")
        return [None, {}, f"❗ {_safe_error(e)}", ""] + hidden + blank * 3 + [gr.update(value=None)] * MAX_Q

    pub = public_questions(quiz)  # no answers leave the server here
    header = f"**Quiz `{quiz.quiz_id}`** — {len(pub)} questions from {', '.join(quiz.doc_names)}"
    if quiz.topic:
        header += f" · topic: *{html.escape(quiz.topic)}*"
    if quiz.notes and len(pub) < int(n):
        header += f"\n\n_{len(pub)} of {int(n)} requested questions passed validation._"
    groups, qmd, radios, fb, imgs = [], [], [], [], []
    for i in range(MAX_Q):
        if i < len(pub):
            q = pub[i]
            groups.append(gr.update(visible=True))
            qmd.append(gr.update(value=f"**Q{i + 1}. {html.escape(q['question'])}**"))
            radios.append(gr.update(choices=[f"{LETTERS[j]}. {o}" for j, o in enumerate(q["options"])],
                                    value=None, interactive=True))
        else:
            groups.append(gr.update(visible=False))
            qmd.append(gr.update(value=""))
            radios.append(gr.update(choices=[], value=None))
        fb.append(gr.update(value=""))
        imgs.append(gr.update(value=None, visible=False))
    return [quiz.quiz_id, {}, header, _score_md(quiz, {})] + groups + qmd + radios + fb + imgs


def _finish(i: int, quiz_id, progress: dict, choice: str | None, reveal: bool):
    quiz = _quiz(quiz_id)
    if quiz is None or i >= len(quiz.questions):
        return progress, gr.update(), gr.update(), gr.update(), gr.update()
    q = quiz.questions[i]
    progress = dict(progress or {})
    if q.qid in progress:  # already finished: answers are final
        return progress, gr.update(), gr.update(), gr.update(), _score_md(quiz, progress)
    if not reveal and not choice:
        return progress, gr.update(value="Choose an answer first."), gr.update(), gr.update(), gr.update()
    chosen = LETTERS.index(choice[0]) if choice and not reveal else None
    if reveal and choice:  # revealing after picking still grades the pick
        chosen = LETTERS.index(choice[0])
    progress[q.qid] = {"chosen": chosen, "revealed": reveal}
    img = gr.update(value=q.source.image_path, visible=bool(q.source.image_path),
                    label=f"{q.source.doc_name} — {q.source.location}")
    return (progress, _feedback_md(quiz, i, chosen), gr.update(interactive=False), img,
            _score_md(quiz, progress))


def build_ui(read_only: bool = False) -> gr.Blocks:
    """read_only=True leaves out upload and remove entirely, so visitors can't change the library."""
    lib = get_library()
    choices = _doc_choices()
    with gr.Blocks(title="Course Assistant") as demo:
        gr.Markdown("# 📚 Course Assistant\nAsk questions about your course materials and practise "
                    "with quizzes. Every answer cites the document and page/slide it came from.")
        with gr.Tab("Ask"):
            with gr.Row():
                with gr.Column(scale=3):
                    question = gr.Textbox(label="Your question", lines=2,
                                          placeholder="e.g. Why does RAG use both keyword and vector search?")
                    ask_docs = gr.CheckboxGroup(choices=choices, label="Search in (leave empty for all documents)")
                    ask_btn = gr.Button("Ask", variant="primary")
                with gr.Column(scale=4):
                    answer_md = gr.Markdown()
                    sources_md = gr.Markdown()
            ask_btn.click(ask, [question, ask_docs], [answer_md, sources_md])
            question.submit(ask, [question, ask_docs], [answer_md, sources_md])

        with gr.Tab("Quiz"):
            quiz_id = gr.State(None)
            quiz_progress = gr.State({})
            with gr.Row():
                quiz_docs = gr.CheckboxGroup(choices=choices, label="Material for the quiz")
                with gr.Column():
                    topic = gr.Textbox(label="Topic filter (optional)", placeholder="e.g. KV cache")
                    n_q = gr.Slider(1, MAX_Q, value=5, step=1, label="Number of questions")
                    gen_btn = gr.Button("Generate quiz", variant="primary")
            quiz_header = gr.Markdown()
            score_md = gr.Markdown()
            groups, qmds, radios, fbs, imgs = [], [], [], [], []
            for i in range(MAX_Q):
                with gr.Group(visible=False) as g:
                    qmd = gr.Markdown()
                    radio = gr.Radio(choices=[], label="Your answer")
                    with gr.Row():
                        check = gr.Button("Check answer", size="sm", variant="primary")
                        reveal = gr.Button("Show solution", size="sm")
                    fb = gr.Markdown()
                    img = gr.Image(visible=False, interactive=False, height=320, show_label=True)
                groups.append(g); qmds.append(qmd); radios.append(radio); fbs.append(fb); imgs.append(img)
                outs = [quiz_progress, fb, radio, img, score_md]
                check.click(lambda qid, prog, ch, i=i: _finish(i, qid, prog, ch, False),
                            [quiz_id, quiz_progress, radio], outs)
                reveal.click(lambda qid, prog, ch, i=i: _finish(i, qid, prog, ch, True),
                             [quiz_id, quiz_progress, radio], outs)
            gen_btn.click(make_quiz, [quiz_docs, topic, n_q],
                          [quiz_id, quiz_progress, quiz_header, score_md] + groups + qmds + radios + fbs + imgs)

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
                    with gr.Column():
                        gr.Markdown(accepted_formats_md())
            table = gr.Dataframe(value=_doc_table(), label=f"Library ({len(lib.docs)} documents at start-up)",
                                 headers=["File", "Type", "Pages", "Unit", "Images", "Warnings"],
                                 interactive=False, wrap=True)
            if not read_only:
                with gr.Row():
                    rm_doc = gr.Dropdown(choices=choices, value=None, label="Remove a document")
                    rm_btn = gr.Button("Remove", variant="stop")
            gr.Markdown("### Browse a document\nSee exactly what was extracted from each page or slide.")
            with gr.Row():
                view_doc = gr.Dropdown(choices=choices, value=None, label="Document")
                view_n = gr.Number(value=1, precision=0, minimum=1, label="Page / slide")
            with gr.Row():
                view_img = gr.Image(interactive=False, label="Original", height=360)
                view_txt = gr.Textbox(label="Extracted text", lines=16, max_lines=30, interactive=False)
            if not read_only:
                add_btn.click(add_files, [upload], [add_log, table, ask_docs, quiz_docs, rm_doc, view_doc])
                rm_btn.click(remove_doc, [rm_doc], [table, rm_doc, ask_docs, quiz_docs, view_doc])
            view_doc.change(show_page, [view_doc, view_n], [view_img, view_txt])
            view_n.change(show_page, [view_doc, view_n], [view_img, view_txt])
    return demo


def launch_options(s: ShareSettings) -> dict:
    """Keyword arguments for launch(). A public link without a password is refused."""
    if s.share and not s.password:
        raise SystemExit("SHARE=true needs APP_PASSWORD in .env: a public link without a "
                         "password would let anyone use the class API through your key.")
    opts: dict = {"server_name": "127.0.0.1", "share": s.share, "show_error": False}
    if s.password:
        opts["auth"] = (s.username, s.password)
        opts["auth_message"] = "MBAX 6418 Course Assistant: ask your team for the password."
    return opts


def main() -> None:
    setup_logging()
    share = load_share_settings()
    register_secret(share.password)
    opts = launch_options(share)
    lib = get_library()
    demo = build_ui(read_only=share.read_only)
    if share.share:
        log.info("Creating a public link (up to a week, only while this computer runs the app); "
                 "read-only=%s", share.read_only)
    demo.queue(default_concurrency_limit=4).launch(allowed_paths=[str(lib.dir)], **opts)


if __name__ == "__main__":
    main()
