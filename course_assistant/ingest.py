"""Turn a course file into Page records: extracted text plus the original page image.

- PDF: text per page (OCR when a page has almost no text) + a rendered PNG per page.
- PPTX: slide text and speaker notes via python-pptx; slide images via LibreOffice -> PDF.
- PPT / DOC / DOCX: converted to PDF with LibreOffice, then handled like a PDF.
  (DOCX falls back to heading-based sections without images if LibreOffice is missing.)
- MD / TXT / HTML: split into sections by heading; no images.
- PNG / JPG: the image itself plus OCR'd text.
"""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf

from .config import ACCEPTED_FORMATS
from .models import DocInfo, Kind, Page
from .services import Services

log = logging.getLogger(__name__)

RENDER_WIDTH = 1280  # px; page images are stored at this width
MIN_TEXT_CHARS = 40  # pages with less text than this use their OCR text as the page text
MIN_NOVEL_WORDS = 12  # OCR must add this many new words to be kept as visual_text

_SOFFICE_LOCK = threading.Lock()

# Embedded videos in slides trigger harmless 'Screen annotation' warnings.
pymupdf.TOOLS.mupdf_display_errors(False)


class UnsupportedFormat(ValueError):
    pass


def accepted_extensions() -> list[str]:
    return sorted(ACCEPTED_FORMATS)


def file_doc_id(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:12]


def find_soffice() -> str | None:
    for cand in (shutil.which("soffice"), shutil.which("libreoffice"),
                 "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if cand and Path(cand).exists():
            return cand
    return None


def convert_to_pdf(src: Path, out_dir: Path) -> Path:
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError("LibreOffice (soffice) is not installed")
    out_dir.mkdir(parents=True, exist_ok=True)
    with _SOFFICE_LOCK, tempfile.TemporaryDirectory() as profile:
        # A private profile avoids clashes with a running LibreOffice instance.
        subprocess.run(
            [soffice, f"-env:UserInstallation=file://{profile}", "--headless",
             "--convert-to", "pdf", "--outdir", str(out_dir), str(src)],
            check=True, capture_output=True, timeout=600,
        )
    pdf = out_dir / (src.stem + ".pdf")
    if not pdf.exists():
        raise RuntimeError(f"LibreOffice did not produce a PDF for {src.name}")
    return pdf


def _clean(text: str) -> str:
    text = text.replace("\x00", "").replace("\u000b", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def clean_ocr(text: str) -> str:
    """OCR output uses HTML tables and Markdown emphasis; turn both into plain text."""
    text = re.sub(r"</t[dh]>\s*<t[dh][^>]*>", " | ", text)
    text = re.sub(r"</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\*\*|__|^#+\s*", "", text, flags=re.M)
    return _clean(text)


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip(" •-–\t")
        if len(line) > 2:
            return line[:100]
    return ""


def render_pdf_pages(pdf_path: Path, img_dir: Path) -> list[str]:
    img_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    with pymupdf.open(pdf_path) as doc:
        for i, page in enumerate(doc, start=1):
            zoom = RENDER_WIDTH / max(page.rect.width, 1)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            out = img_dir / f"p{i:04d}.png"
            pix.save(out)
            paths.append(str(out))
    return paths


def pdf_page_texts(pdf_path: Path) -> list[str]:
    with pymupdf.open(pdf_path) as doc:
        return [_clean(p.get_text("text", sort=True)) for p in doc]


def pptx_slide_texts(path: Path) -> list[tuple[str, str]]:
    """(title, text) per slide, including tables and speaker notes."""
    from pptx import Presentation

    prs = Presentation(str(path))
    out = []
    for slide in prs.slides:
        title = ""
        if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
            title = slide.shapes.title.text_frame.text.strip()
        parts: list[str] = []

        def walk(shapes):
            for sh in shapes:
                if sh.shape_type == 6:  # group
                    walk(sh.shapes)
                elif getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip():
                    parts.append(sh.text_frame.text.strip())
                elif getattr(sh, "has_table", False):
                    for row in sh.table.rows:
                        parts.append(" | ".join(c.text.strip() for c in row.cells))

        walk(slide.shapes)
        text = "\n".join(parts)
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip() if slide.notes_slide.notes_text_frame else ""
            if notes:
                text += "\n\nSpeaker notes:\n" + notes
        out.append((title or _first_line(text), _clean(text)))
    return out


BLANK_STDDEV = 4.0  # a rendered area this uniform (0-255 grey) is treated as "not drawn"


def repair_pptx_renders(pptx_path: Path, image_paths: list[str]) -> tuple[dict[int, str], list[str]]:
    """Fill in slide areas LibreOffice left blank.

    LibreOffice cannot draw some embedded media (e.g. animated GIFs) and leaves an empty box.
    For every picture and video on a slide, if its area in the rendered image is blank, the
    picture (first frame of a GIF) or the video's poster image is pasted there.
    Returns ({slide number: text note}, warnings).
    """
    import io

    from PIL import Image, ImageStat
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = Presentation(str(pptx_path))
    notes: dict[int, str] = {}
    warnings: list[str] = []

    def shapes_of(container):
        for sh in container:
            if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
                yield from shapes_of(sh.shapes)
            else:
                yield sh

    for number, (slide, img_path) in enumerate(zip(prs.slides, image_paths), start=1):
        img = Image.open(img_path).convert("RGB")
        scale = img.width / prs.slide_width
        changed, repaired, videos = False, 0, 0
        for sh in shapes_of(slide.shapes):
            blob = None
            if sh.shape_type == MSO_SHAPE_TYPE.MEDIA:
                videos += 1
                try:
                    blob = sh.poster_frame.blob if sh.poster_frame is not None else None
                except Exception:
                    blob = None
            elif sh.shape_type == MSO_SHAPE_TYPE.PICTURE:
                try:
                    blob = sh.image.blob
                except Exception:
                    blob = None
            if blob is None or sh.width is None or sh.left is None:
                continue
            box = (max(0, int(sh.left * scale)), max(0, int(sh.top * scale)),
                   min(img.width, int((sh.left + sh.width) * scale)),
                   min(img.height, int((sh.top + sh.height) * scale)))
            if box[2] - box[0] < 20 or box[3] - box[1] < 20:
                continue
            if ImageStat.Stat(img.crop(box).convert("L")).stddev[0] >= BLANK_STDDEV:
                continue  # drawn fine
            try:
                pic = Image.open(io.BytesIO(blob))
                frames = getattr(pic, "n_frames", 1)
                pic.seek(frames // 2 if frames > 1 else 0)  # a middle frame of an animated GIF
                pic = pic.convert("RGBA")
                if sh.shape_type == MSO_SHAPE_TYPE.PICTURE:  # apply PowerPoint's crop
                    cw, ch = pic.size
                    pic = pic.crop((int(cw * sh.crop_left), int(ch * sh.crop_top),
                                    int(cw * (1 - sh.crop_right)), int(ch * (1 - sh.crop_bottom))))
                pic = pic.resize((box[2] - box[0], box[3] - box[1]))
            except Exception:
                continue
            # Only a real failure if the picture itself has visible content (a mostly-white
            # chart or logo legitimately renders as a near-uniform area).
            if ImageStat.Stat(pic.convert("L")).stddev[0] < BLANK_STDDEV * 3:
                continue
            img.paste(pic, box[:2], pic)
            changed, repaired = True, repaired + 1
        if changed:
            img.save(img_path)
            warnings.append(f"Slide {number}: {repaired} image(s) LibreOffice could not draw (e.g. animated "
                            "GIF or video) were filled in with their first frame / poster image")
        if videos:
            notes[number] = (f"[This slide contains {videos} embedded video(s); only the video's poster image "
                             "is shown and the video itself is not searchable.]")
    return notes, warnings


def split_by_headings(text: str, heading_re: str = r"^#{1,3}\s+(.+)$") -> list[tuple[str, str]]:
    """Split text into (heading, body) sections. Text before the first heading is its own section."""
    sections: list[tuple[str, str]] = []
    title, buf = "", []
    for line in text.splitlines():
        m = re.match(heading_re, line.strip())
        if m:
            if "".join(buf).strip():
                sections.append((title, "\n".join(buf)))
            title, buf = m.group(1).strip(), [line]
        else:
            buf.append(line)
    if "".join(buf).strip():
        sections.append((title, "\n".join(buf)))
    return [(t, _clean(b)) for t, b in sections] or [("", _clean(text))]


def _plain_text_sections(text: str, target: int = 2500) -> list[tuple[str, str]]:
    """Group paragraphs of a plain-text file into ~target-character sections."""
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    sections, buf = [], ""
    for p in paras:
        if buf and len(buf) + len(p) > target:
            sections.append((_first_line(buf), buf))
            buf = ""
        buf = (buf + "\n\n" + p).strip()
    if buf:
        sections.append((_first_line(buf), buf))
    return [(t, _clean(b)) for t, b in sections] or [("", "")]


def html_sections(html: str) -> list[tuple[str, str]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "noscript"]):
        tag.decompose()
    for h in soup.find_all(["h1", "h2", "h3"]):
        h.replace_with(f"\n# {h.get_text(' ', strip=True)}\n")
    text = soup.get_text("\n")
    return split_by_headings(text)


def docx_sections(path: Path) -> list[tuple[str, str]]:
    import docx

    d = docx.Document(str(path))
    lines = []
    for para in d.paragraphs:
        style = (para.style.name or "").lower() if para.style is not None else ""
        if style.startswith("heading") or style == "title":
            lines.append(f"# {para.text}")
        else:
            lines.append(para.text)
    for table in d.tables:
        for row in table.rows:
            lines.append(" | ".join(c.text.strip() for c in row.cells))
    return split_by_headings("\n".join(lines))


def novel_words(ocr_text: str, text: str) -> int:
    """How many distinct words in the OCR text are missing from the text layer."""
    have = set(re.findall(r"[a-z0-9]{3,}", text.lower()))
    return len(set(re.findall(r"[a-z0-9]{3,}", ocr_text.lower())) - have)


def _add_ocr(pages: list[Page], services: Services, warnings: list[str]) -> None:
    """OCR every page image. Pages with (almost) no text layer take the OCR text as their
    text; otherwise OCR text that adds new words (diagram labels, table screenshots,
    charts) is kept in `visual_text`, so it can be searched and quoted."""
    imaged = [p for p in pages if p.image_path]

    def run(p: Page) -> str | Exception:
        try:
            return clean_ocr(services.ocr(p.image_path))
        except Exception as e:
            return e

    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(run, imaged))
    for p, ocr_text in zip(imaged, results):
        if isinstance(ocr_text, Exception):
            warnings.append(f"OCR failed on {p.label}: {ocr_text}")
            continue
        body = p.text.split("Speaker notes:")[0]
        if len(body.strip()) < MIN_TEXT_CHARS and len(ocr_text) > len(body.strip()):
            p.text = (p.text + "\n\n" + ocr_text).strip() if p.text else ocr_text
            p.ocr = True
            if p.kind != "image" and not p.title:
                p.title = _first_line(ocr_text)
        elif novel_words(ocr_text, p.text) >= MIN_NOVEL_WORDS:
            p.visual_text = ocr_text


def ingest_file(path: str | Path, data_dir: Path, services: Services | None = None,
                display_name: str | None = None) -> tuple[DocInfo, list[Page]]:
    """Parse one file into pages. `services` is used for OCR; pass None to skip OCR."""
    src = Path(path)
    name = display_name or src.name
    ext = src.suffix.lower()
    if ext not in ACCEPTED_FORMATS:
        raise UnsupportedFormat(
            f"'{name}' is not a supported format. Accepted: {', '.join(accepted_extensions())}")

    doc_id = file_doc_id(src)
    img_dir = data_dir / "pages" / doc_id
    conv_dir = data_dir / "converted" / doc_id
    warnings: list[str] = []
    pages: list[Page] = []

    def make(number: int, kind: Kind, title: str, text: str, image: str | None) -> Page:
        return Page(doc_id=doc_id, doc_name=name, number=number, kind=kind,
                    title=title, text=text, image_path=image)

    def from_pdf(pdf: Path, kind: Kind, texts: list[tuple[str, str]] | None = None) -> None:
        images = render_pdf_pages(pdf, img_dir)
        pdf_texts = pdf_page_texts(pdf)
        if texts is not None and len(texts) != len(images):
            warnings.append(f"{len(texts)} slides but {len(images)} rendered pages; "
                            "slide images may be misaligned, using rendered-page text instead")
            texts = None
        for i, img in enumerate(images):
            if texts is not None:
                title, text = texts[i]
            else:
                text = pdf_texts[i]
                title = _first_line(text)
            pages.append(make(i + 1, kind, title, text, img))

    if ext == ".pdf":
        from_pdf(src, "page")
    elif ext == ".pptx":
        texts = pptx_slide_texts(src)
        try:
            from_pdf(convert_to_pdf(src, conv_dir), "slide", texts)
            if pages and all(p.image_path for p in pages):
                media_notes, repair_warnings = repair_pptx_renders(src, [p.image_path for p in pages])
                warnings.extend(repair_warnings)
                for p in pages:
                    if p.number in media_notes:
                        p.text = (p.text + "\n\n" + media_notes[p.number]).strip()
        except Exception as e:  # LibreOffice missing or failed: keep text, no images
            warnings.append(f"Slide images unavailable ({e}); text only")
            pages = [make(i + 1, "slide", t, x, None) for i, (t, x) in enumerate(texts)]
    elif ext in (".ppt", ".doc", ".docx"):
        kind: Kind = "slide" if ext == ".ppt" else "page"
        try:
            from_pdf(convert_to_pdf(src, conv_dir), kind)
        except Exception as e:
            if ext != ".docx":
                raise RuntimeError(f"Could not convert {name}: {e}") from None
            warnings.append(f"Page images unavailable ({e}); split by heading instead")
            pages = [make(i + 1, "section", t, x, None) for i, (t, x) in enumerate(docx_sections(src))]
    elif ext in (".md", ".txt", ".html", ".htm"):
        raw = src.read_text(encoding="utf-8", errors="replace")
        if ext == ".md":
            secs = split_by_headings(raw)
        elif ext == ".txt":
            secs = _plain_text_sections(raw)
        else:
            secs = html_sections(raw)
        pages = [make(i + 1, "section", t, x, None) for i, (t, x) in enumerate(secs) if x.strip()]
    else:  # image file
        img_dir.mkdir(parents=True, exist_ok=True)
        dest = img_dir / f"p0001{ext}"
        shutil.copyfile(src, dest)
        pages = [make(1, "image", name, "", str(dest))]

    if services is not None:
        _add_ocr(pages, services, warnings)

    info = DocInfo(doc_id=doc_id, name=name, ext=ext,
                   kind=pages[0].kind if pages else "page", num_pages=len(pages),
                   has_images=any(p.image_path for p in pages), warnings=warnings)
    return info, pages
