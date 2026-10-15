"""Smoke tests against the real class services. Skipped by default; run with:

    uv run pytest -m live
"""
from __future__ import annotations

import pytest
from PIL import Image

from course_assistant.config import load_settings
from course_assistant.services import ClassServices, RerankDoc

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def svc():
    s = load_settings()
    if not s.configured:
        pytest.skip("CLASS_API_KEY not set")
    return ClassServices(s)


def test_llm_json(svc):
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    assert '"ok"' in svc.chat([{"role": "user", "content": "Return ok=true as JSON."}], schema=schema)


def test_embeddings_and_rerank(svc, tmp_path):
    assert len(svc.embed_text(["hello"], kind="query")[0]) > 100
    img = tmp_path / "x.png"
    Image.new("RGB", (64, 64), "white").save(img)
    assert len(svc.embed_images([str(img)])[0]) > 100
    scores = svc.rerank("retrieval augmented generation",
                        [RerankDoc(text="RAG adds retrieved documents to the prompt."),
                         RerankDoc(text="The cat sat on the mat."), RerankDoc(image_path=str(img))])
    assert len(scores) == 3 and scores[0] > scores[1]


def test_ocr(svc, tmp_path):
    from PIL import ImageDraw

    img = tmp_path / "t.png"
    im = Image.new("RGB", (400, 100), "white")
    ImageDraw.Draw(im).text((10, 40), "HELLO COURSE", fill="black")
    im.save(img)
    assert "HELLO" in svc.ocr(str(img)).upper()


def test_scanned_pdf_is_read_with_ocr(svc, tmp_path):
    """Issue #4: a PDF with no text layer (a scan) is OCR'd page by page."""
    from PIL import ImageDraw, ImageFilter

    from course_assistant.ingest import ingest_file

    page = Image.new("L", (1700, 2200), 255)
    draw = ImageDraw.Draw(page)
    lines = ["Course Syllabus", "Late work loses ten percent per day.", "The final exam is on December 12."]
    for i, line in enumerate(lines):
        draw.text((150, 300 + i * 120), line, fill=0, font_size=56)
    page = page.rotate(0.8, fillcolor=245).filter(ImageFilter.GaussianBlur(0.6))  # scanner tilt and blur
    pdf = tmp_path / "scan.pdf"
    page.convert("RGB").save(pdf, resolution=200)

    info, pages = ingest_file(pdf, tmp_path / "data", services=svc)
    assert pages[0].ocr
    text = pages[0].text.lower()
    assert "late work" in text and "december" in text
