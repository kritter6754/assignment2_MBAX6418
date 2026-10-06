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
