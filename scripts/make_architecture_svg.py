"""Generate docs/architecture.svg, the architecture diagram embedded in README.md.

    uv run python scripts/make_architecture_svg.py

Edit the boxes below when the app changes, so the diagram keeps matching the code.
Plain SVG (no external fonts, scripts or foreignObject) so GitHub renders it.
"""
from __future__ import annotations

import html
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "architecture.svg"
W, H = 1400, 1010
FONT = "Helvetica, Arial, sans-serif"

# colour roles (legend)
APP = ("#EEF3FF", "#2F6BFF")      # Python code in the app container
MODEL = ("#FFF4E5", "#E07A00")    # call to a model on the class GPU server
STORE = ("#E9F7EF", "#1E8E4E")    # stored data
UI = ("#F3F0FF", "#6D4AFF")       # browser
NEUTRAL = ("#FFFFFF", "#9AA3B2")

parts: list[str] = []


def wrap(text: str, width_px: float, size: float) -> list[str]:
    max_chars = max(8, int(width_px / (size * 0.53)))
    lines: list[str] = []
    for para in text.split("\n"):
        line = ""
        for word in para.split():
            if line and len(line) + 1 + len(word) > max_chars:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        lines.append(line)
    return lines


def text(x, y, s, size=12, weight="normal", color="#1F2937", anchor="start"):
    parts.append(f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" '
                 f'fill="{color}" text-anchor="{anchor}">{html.escape(s)}</text>')


def box(x, y, w, h, title, body="", colors=APP, tag="", title_size=13, body_size=11, rx=10):
    fill, stroke = colors
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="1.6"/>')
    cy = y + 20
    for line in wrap(title, w - 16, title_size):
        text(x + 8, cy, line, title_size, "bold")
        cy += title_size + 3
    if tag:
        tw = min(w - 16, len(tag) * 6.1 + 12)
        parts.append(f'<rect x="{x + 8}" y="{cy - 9}" width="{tw}" height="17" rx="8" fill="{MODEL[1]}"/>')
        text(x + 14, cy + 3, tag, 10.5, "bold", "#FFFFFF")
        cy += 20
    for line in wrap(body, w - 16, body_size):
        text(x + 8, cy + 2, line, body_size, color="#374151")
        cy += body_size + 4


def arrow(x1, y1, x2, y2, color="#4B5563", dashed=False):
    dash = ' stroke-dasharray="5,4"' if dashed else ""
    parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="1.6"'
                 f'{dash} marker-end="url(#arrow)"/>')


def path_arrow(d, color="#4B5563", dashed=False):
    dash = ' stroke-dasharray="5,4"' if dashed else ""
    parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.6"{dash} marker-end="url(#arrow)"/>')


def region(x, y, w, h, label, colors=NEUTRAL, dashed=True):
    fill, stroke = colors
    dash = ' stroke-dasharray="7,5"' if dashed else ""
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{fill}" fill-opacity="0.35" '
                 f'stroke="{stroke}" stroke-width="1.5"{dash}/>')
    text(x + 14, y + 22, label, 14, "bold", stroke)


# ---------------------------------------------------------------- frame + title + legend
text(20, 34, "MBAX 6418 Course Assistant: architecture", 22, "bold")
text(20, 56, "Hybrid RAG over course files: separate keyword, text-vector and image-vector indexes, multimodal "
             "reranking, a vision LLM, and citation checks.", 12.5, color="#4B5563")
lx = 930
for i, (label, c) in enumerate([("App code (Python, in the container)", APP),
                                ("Model call to the class GPU server", MODEL),
                                ("Stored data", STORE), ("Browser", UI)]):
    yy = 22 + i * 17
    parts.append(f'<rect x="{lx}" y="{yy - 10}" width="14" height="12" rx="3" fill="{c[0]}" stroke="{c[1]}"/>')
    text(lx + 20, yy, label, 11.5)

# ---------------------------------------------------------------- where things run
region(16, 84, 186, 720, "Student's browser", UI)
region(214, 84, 852, 720, "Railway container (Docker): Gradio app, course_assistant package")
region(1078, 84, 306, 720, "Class GPU server: dobolyi.com", MODEL)

# browser column
box(28, 116, 162, 250, "Gradio web UI",
    "Tabs: Ask · Quiz · Prior Questions · Library\nLight/dark mode\nNumbered questions → one answer "
    "box each\nAnswers + quiz keys come from the server; the browser never sees the API key", UI)
box(28, 380, 162, 150, "Kept in the browser",
    "Missed / mastered quiz questions (Gradio BrowserState), theme choice", UI)
text(110, 572, "HTTPS", 12, "bold", "#6D4AFF", "middle")
parts.append('<line x1="190" y1="580" x2="226" y2="580" stroke="#6D4AFF" stroke-width="2" '
             'marker-end="url(#arrow)" marker-start="url(#arrow-rev)"/>')

# ---------------------------------------------------------------- A. document preparation
text(228, 128, "A. Document preparation (Library tab → Add)", 14, "bold", "#2F6BFF")
box(228, 140, 118, 132, "Course file", "PDF, PPTX, PPT, DOCX, DOC, MD, TXT, HTML, PNG/JPG")
box(358, 140, 150, 132, "Parse + render",
    "python-pptx, PyMuPDF, BeautifulSoup; LibreOffice → PDF → 1280-px page images; blank GIF/video areas filled in")
box(520, 140, 122, 132, "OCR every page image", "text the slide's text layer lacks → 'visual text'",
    MODEL, tag=":9005 OCR")
box(654, 140, 130, 132, "Chunk per page",
    "LangChain recursive splitter, 900 chars / 150 overlap; each chunk keeps doc, slide, offset")
for x1, x2 in [(346, 358), (508, 520), (642, 654)]:
    arrow(x1, 206, x2, 206)
box(800, 140, 252, 40, "Keyword index: bm25s (in memory)", colors=APP, title_size=12)
box(800, 186, 252, 44, "Text vectors: Chroma 'text_chunks'", colors=APP, tag=":9002 text embedding",
    title_size=12)
box(800, 238, 252, 44, "Image vectors: Chroma 'page_images'", colors=APP, tag=":9003 visual embedding",
    title_size=12)
arrow(784, 180, 800, 162)
arrow(784, 206, 800, 206)
path_arrow("M 433 272 L 433 292 L 790 292 L 790 262 L 800 262")
text(560, 305, "page images", 10.5, color="#4B5563")
# box heights grow with tags; keep regions spaced

# ---------------------------------------------------------------- B. answering
text(228, 340, "B. Answering (Ask tab)", 14, "bold", "#2F6BFF")
box(228, 352, 118, 134, "Question(s)", "'1. … 2. …' split into separate questions, answered in parallel")
box(358, 352, 150, 42, "Keyword search (BM25): top 20", title_size=11)
box(358, 398, 150, 42, "Text-vector search: top 20", title_size=11)
box(358, 444, 150, 42, "Image-vector search: top 8", title_size=11)
for y in (373, 419, 465):
    arrow(346, 419, 358, y)
box(520, 352, 100, 88, "RRF merge", "of the two text lists")
arrow(508, 373, 520, 385)
arrow(508, 419, 520, 405)
box(632, 352, 152, 150, "Multimodal rerank", "text chunks + slide images scored together → best 8 evidence "
    "items (≤ 3 images)", MODEL, tag=":9004 reranker")
arrow(620, 391, 632, 391)
path_arrow("M 508 465 L 570 465 L 570 480 L 632 480")
box(800, 352, 252, 112, "Answer generation (vision LLM)",
    "question + evidence text + slide images → JSON {found, answer, sources[{id, quote}]}", MODEL,
    tag=":9001 LLM")
arrow(784, 410, 800, 410)
box(800, 474, 252, 112, "Checks before showing",
    "format (pydantic, 1 retry) · each quote must appear on its cited page (text or OCR) · support "
    "check: 2nd LLM call reviews the claims · nothing verified → 'not found'")
arrow(926, 464, 926, 474)
box(632, 516, 152, 70, "Answer card", "answer, sources, excerpts, slide images", UI)
arrow(800, 551, 784, 551)

# ---------------------------------------------------------------- C. quizzes
text(228, 620, "C. Quizzes (Quiz tab)", 14, "bold", "#2F6BFF")
box(228, 632, 160, 160, "Pick evidence",
    "topic → hybrid retrieval (B); no topic → sampled slides; image questions → visual slides (diagrams, "
    "charts, screenshots)")
box(400, 632, 150, 160, "Write questions",
    "JSON, exactly n; difficulty; ≥ 20 % image questions when ticked; mastered ones avoided", MODEL,
    tag=":9001 LLM")
box(562, 632, 222, 160, "Validate each question",
    "4 distinct options · cited quote must be on the slide · labels OCR missed are checked on the "
    "image by the vision model · retries rotate slides")
box(796, 632, 256, 160, "Fixed answer key + grading",
    "options shuffled; quiz saved once with a SHA-256 hash of the key; answers sent only after "
    "Check / Show solution; live scoreboard; retry missed")
for x1, x2 in [(388, 400), (550, 562), (784, 796)]:
    arrow(x1, 712, x2, 712)

# ---------------------------------------------------------------- GPU server column
services = [
    (":9001 LLM", "Qwen3.6-35B-A3B (vision-language)", "answers, support checks, quiz questions, label checks"),
    (":9002 text embedding", "Nemotron-3-Embed-1B", "chunks + queries ('passage: ' / 'query: ' prefixes)"),
    (":9003 visual embedding", "Qwen3-VL-Embedding-2B", "slide images + text queries in one space"),
    (":9004 reranker", "Qwen3-VL-Reranker-2B", "scores text and images against the question"),
    (":9005 OCR", "dots.mocr", "text from slide images and scanned pages"),
]
text(1092, 128, "vLLM, OpenAI-compatible API", 11.5, color="#4B5563")
text(1092, 144, "key: .env locally / Railway variables", 11.5, color="#4B5563")
for i, (port, model, use) in enumerate(services):
    box(1090, 158 + i * 126, 282, 114, model, use, MODEL, tag=port)

# ---------------------------------------------------------------- storage + deploy
box(214, 822, 852, 74, "Railway volume  /data/library  (the course library; managed only on the website)",
    "slide images · pages + chunks JSON · Chroma indexes · saved quizzes · OCR cache. Not in GitHub: "
    "the repo is public and the slides are the instructor's. Backed up with scripts/backup_railway_library.sh.",
    STORE)
path_arrow("M 640 804 L 640 822", "#1E8E4E")
box(16, 912, 1368, 82, "Deploy and run",
    "GitHub main → Railway builds the Docker image (Python 3.12, uv, LibreOffice) on every push → "
    "https://course-assistant-production-3fbf.up.railway.app.  Local development runs the same app on a "
    "laptop (uv run course-assistant) with its own data/ folder and a read-only library. "
    "API settings come from .env (never committed).", NEUTRAL)

svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
       '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
       'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#4B5563"/></marker>'
       '<marker id="arrow-rev" viewBox="0 0 10 10" refX="1" refY="5" markerWidth="7" markerHeight="7" '
       'orient="auto"><path d="M10,0 L0,5 L10,10 z" fill="#6D4AFF"/></marker></defs>'
       f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>' + "".join(parts) + "</svg>\n")
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(svg)
print(f"wrote {OUT} ({len(svg) // 1024} KB)")
