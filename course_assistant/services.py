"""Clients for the class model services (all OpenAI-compatible vLLM servers).

| Service            | Endpoint                 | Used for                                  |
|--------------------|--------------------------|-------------------------------------------|
| LLM (vision)       | POST /chat/completions   | answers, quiz generation, support checks  |
| Text embeddings    | POST /embeddings         | dense text search over chunks             |
| Visual embeddings  | POST /embeddings         | page-image search (text query -> image)   |
| Reranker           | POST /rerank             | rescoring mixed text + image candidates   |
| OCR / doc parsing  | POST /chat/completions   | text from scanned pages and images        |
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from PIL import Image

from .config import Settings, redact, register_secret

log = logging.getLogger(__name__)


class ServiceError(RuntimeError):
    """A class service failed. The message is safe to show (secrets redacted)."""


def image_data_url(path: str | Path, max_side: int = 1024, quality: int = 85) -> str:
    """Load an image, shrink it so its longest side is `max_side`, return a JPEG data URL."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


@dataclass
class RerankDoc:
    """A rerank candidate: text, an image, or both."""

    text: str | None = None
    image_path: str | None = None


class Services(Protocol):
    def chat(self, messages: list[dict], schema: dict | None = None, max_tokens: int = 2000) -> str: ...
    def embed_text(self, texts: list[str], kind: str = "passage") -> list[list[float]]: ...
    def embed_visual_query(self, texts: list[str]) -> list[list[float]]: ...
    def embed_images(self, paths: list[str]) -> list[list[float]]: ...
    def rerank(self, query: str, docs: list[RerankDoc]) -> list[float]: ...
    def ocr(self, image_path: str) -> str: ...


# Chat-template control strings such as <|im_start|> are tokenized as special tokens, so the
# model cannot write them as text (they vanish from its output). Course material *about*
# templates contains them, so they are swapped for look-alikes going in and restored coming out.
_SPECIAL_IN = (("<|", "\u2039|"), ("|>", "|\u203a"))


def protect_special(text: str) -> str:
    for a, b in _SPECIAL_IN:
        text = text.replace(a, b)
    return text


def restore_special(text: str) -> str:
    for a, b in _SPECIAL_IN:
        text = text.replace(b, a)
    return text


def extract_json(text: str) -> Any:
    """Parse JSON from model output, tolerating <think> blocks and code fences."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            raise
        return json.loads(m.group(0))


class ClassServices:
    """Real HTTP client for the class endpoints."""

    def __init__(self, settings: Settings, max_workers: int = 6,
                 transport: httpx.BaseTransport | None = None):
        self.s = settings
        register_secret(settings.api_key)
        self._http = httpx.Client(
            timeout=settings.timeout_s, transport=transport,
            headers={"Authorization": f"Bearer {settings.api_key}"},
        )
        self._pool_size = max_workers

    # -- plumbing -----------------------------------------------------------
    def _post(self, base: str, path: str, payload: dict) -> dict:
        url = base.rstrip("/") + path
        for attempt in range(3):  # retry dropped connections and 5xx with backoff
            try:
                r = self._http.post(url, json=payload)
            except httpx.HTTPError as e:
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise ServiceError(redact(f"Could not reach {url}: {type(e).__name__}")) from None
            if r.status_code >= 500 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise ServiceError(redact(f"{url} returned HTTP {r.status_code}: {r.text[:300]}"))
            return r.json()
        raise AssertionError("unreachable")

    def _map(self, fn, items):
        with ThreadPoolExecutor(self._pool_size) as ex:
            return list(ex.map(fn, items))

    # -- LLM ----------------------------------------------------------------
    def chat(self, messages: list[dict], schema: dict | None = None, max_tokens: int = 2000) -> str:
        payload: dict[str, Any] = {
            "model": self.s.llm_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0,
            "seed": 42,
            # The model "thinks" before answering by default; off keeps JSON calls fast.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": schema},
            }
        data = self._post(self.s.llm_url, "/chat/completions", payload)
        return restore_special(data["choices"][0]["message"]["content"] or "")

    # -- embeddings ---------------------------------------------------------
    def _embed_batch(self, base: str, model: str, texts: list[str], batch: int = 32) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), batch):
            data = self._post(base, "/embeddings", {"model": model, "input": texts[i : i + batch]})
            out.extend(d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"]))
        return out

    def embed_text(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        """kind="query" for search queries, "passage" for indexed chunks."""
        prefix = self.s.text_query_prefix if kind == "query" else self.s.text_passage_prefix
        return self._embed_batch(self.s.text_embed_url, self.s.text_embed_model, [prefix + t for t in texts])

    def embed_visual_query(self, texts: list[str]) -> list[list[float]]:
        return self._embed_batch(self.s.visual_embed_url, self.s.visual_embed_model, texts)

    def embed_images(self, paths: list[str]) -> list[list[float]]:
        def one(p: str) -> list[float]:
            msg = [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": image_data_url(p, max_side=768)}}]}]
            data = self._post(self.s.visual_embed_url, "/embeddings",
                              {"model": self.s.visual_embed_model, "messages": msg})
            return data["data"][0]["embedding"]

        return self._map(one, paths)

    # -- reranker -----------------------------------------------------------
    def rerank(self, query: str, docs: list[RerankDoc]) -> list[float]:
        """Relevance score per doc (same order). Each (query, doc) pair is scored
        independently, so scores from separate requests are comparable."""
        if not docs:
            return []

        def call(idx: list[int]) -> list[tuple[int, float]]:
            if docs[idx[0]].image_path:
                # Multimodal form: {"content": [parts]}. Consecutive text parts here would be
                # merged into ONE document, so this form is only used for a single image.
                documents: Any = {"content": [{"type": "image_url", "image_url": {
                    "url": image_data_url(docs[idx[0]].image_path, max_side=768)}}]}
            else:
                documents = [(docs[i].text or "")[:4000] for i in idx]
            data = self._post(self.s.rerank_url, "/rerank",
                              {"model": self.s.rerank_model, "query": query, "documents": documents,
                               "top_n": len(idx)})
            return [(idx[r["index"]], float(r["relevance_score"])) for r in data["results"]]

        # The server accepts at most 2 images per request: all text in one request,
        # each image in its own request.
        text_idx = [i for i, d in enumerate(docs) if not d.image_path]
        batches = ([text_idx] if text_idx else []) + [[i] for i, d in enumerate(docs) if d.image_path]
        scores = [0.0] * len(docs)
        for part in self._map(call, batches):
            for i, s in part:
                scores[i] = s
        return scores

    # -- OCR ----------------------------------------------------------------
    def ocr(self, image_path: str) -> str:
        msg = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": image_data_url(image_path, max_side=1600)}},
            {"type": "text", "text": "Extract the text content from this image."}]}]
        data = self._post(self.s.ocr_url, "/chat/completions",
                          {"model": self.s.ocr_model, "messages": msg, "max_tokens": 3000, "temperature": 0})
        return (data["choices"][0]["message"]["content"] or "").strip()
