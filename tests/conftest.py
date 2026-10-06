"""Shared test fixtures. FakeServices stands in for the class endpoints, so the
offline suite needs no network and no API key (it runs in CI)."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pytest

from course_assistant.library import Library
from course_assistant.services import RerankDoc

DIM = 64


def _bow_vector(text: str) -> list[float]:
    """Deterministic bag-of-words embedding: similar words -> similar vectors."""
    v = np.zeros(DIM)
    for w in re.findall(r"[a-z]+", text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1
    n = np.linalg.norm(v)
    return (v / n if n else v + 1 / np.sqrt(DIM)).tolist()


class FakeServices:
    """Implements the Services protocol. `chat_replies` is a queue of strings or
    callables(messages) -> str returned by successive chat() calls."""

    def __init__(self):
        self.chat_replies: list = []
        self.chat_calls: list[list[dict]] = []
        self.ocr_text = "OCR TEXT FROM IMAGE about photosynthesis in leaves"

    def chat(self, messages, schema=None, max_tokens=2000):
        self.chat_calls.append(messages)
        if not self.chat_replies:
            raise AssertionError("unexpected chat() call")
        r = self.chat_replies.pop(0)
        return r(messages) if callable(r) else r

    def embed_text(self, texts, kind="passage"):
        return [_bow_vector(t) for t in texts]

    def embed_visual_query(self, texts):
        return [_bow_vector(t) for t in texts]

    def embed_images(self, paths):
        return [_bow_vector(Path(p).name) for p in paths]

    def rerank(self, query, docs: list[RerankDoc]):
        q = set(re.findall(r"[a-z]+", query.lower()))
        out = []
        for d in docs:
            words = set(re.findall(r"[a-z]+", (d.text or "").lower()))
            out.append(len(q & words) / (len(q) or 1) if d.text else 0.05)
        return out

    def ocr(self, image_path):
        return self.ocr_text


def answer_json(found=True, answer="x", sources=()):
    return json.dumps({"found": found, "answer": answer,
                       "sources": [{"id": i, "quote": q} for i, q in sources]})


COURSE_MD = """# Photosynthesis
Photosynthesis converts light energy into chemical energy stored in glucose.
It takes place in the chloroplasts of plant cells.

# Cellular respiration
Cellular respiration breaks down glucose to release energy as ATP.
It happens in the mitochondria.
"""

OTHER_MD = """# Plate tectonics
The lithosphere is broken into tectonic plates that move slowly over the mantle.
"""


@pytest.fixture
def fake() -> FakeServices:
    return FakeServices()


@pytest.fixture
def lib(tmp_path, fake) -> Library:
    src = tmp_path / "src"
    src.mkdir()
    (src / "biology.md").write_text(COURSE_MD)
    (src / "geology.md").write_text(OTHER_MD)
    library = Library(tmp_path / "data", fake)
    library.add_file(src / "biology.md", progress=lambda m: None)
    library.add_file(src / "geology.md", progress=lambda m: None)
    return library


def doc_id_by_name(lib: Library, name: str) -> str:
    return next(d.doc_id for d in lib.docs.values() if d.name == name)
