"""The course library: stored documents plus three separate search indexes.

- keyword index (bm25s) over text chunks, rebuilt in memory from saved chunks
- text vector index (Chroma collection `text_chunks`) of text embeddings
- visual vector index (Chroma collection `page_images`) of page/slide image embeddings
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
from pathlib import Path
from typing import Callable

import bm25s
import chromadb

from .chunking import chunk_pages, index_text
from .ingest import clean_ocr, file_doc_id, ingest_file
from .models import Chunk, DocInfo, Page
from .services import Services

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


def _tokenize(texts: list[str]):
    return bm25s.tokenize(texts, stopwords="en", show_progress=False)


class Library:
    def __init__(self, data_dir: Path, services: Services):
        self.dir = Path(data_dir)
        self.services = services
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._manifest_path = self.dir / "library.json"
        self.docs: dict[str, DocInfo] = {}
        self.pages: dict[str, Page] = {}  # page_id -> Page
        self.chunks: dict[str, Chunk] = {}  # chunk_id -> Chunk
        client = chromadb.PersistentClient(path=str(self.dir / "chroma"))
        self._text = client.get_or_create_collection("text_chunks", metadata={"hnsw:space": "cosine"})
        self._images = client.get_or_create_collection("page_images", metadata={"hnsw:space": "cosine"})
        self._bm25: bm25s.BM25 | None = None
        self._bm25_ids: list[str] = []
        self._load()

    # -- persistence --------------------------------------------------------
    def _doc_dir(self, doc_id: str) -> Path:
        return self.dir / "docs" / doc_id

    def _load(self) -> None:
        if self._manifest_path.exists():
            raw = json.loads(self._manifest_path.read_text())
            self.docs = {k: DocInfo(**v) for k, v in raw.items()}
        for doc_id in self.docs:
            d = self._doc_dir(doc_id)
            for p in json.loads((d / "pages.json").read_text()):
                page = Page(**p)
                self.pages[page.page_id] = page
            for c in json.loads((d / "chunks.json").read_text()):
                chunk = Chunk(**c)
                self.chunks[chunk.chunk_id] = chunk
        self._rebuild_bm25()

    def _save_manifest(self) -> None:
        self._manifest_path.write_text(json.dumps({k: v.model_dump() for k, v in self.docs.items()}, indent=1))

    def _rebuild_bm25(self) -> None:
        ids = list(self.chunks)
        if not ids:
            self._bm25, self._bm25_ids = None, []
            return
        retriever = bm25s.BM25()
        retriever.index(_tokenize([index_text(self.chunks[i]) for i in ids]), show_progress=False)
        self._bm25, self._bm25_ids = retriever, ids

    # -- documents ----------------------------------------------------------
    def add_file(self, path: str | Path, name: str | None = None, progress: Progress = log.info) -> DocInfo:
        path = Path(path)
        doc_id = file_doc_id(path)
        if doc_id in self.docs:
            progress(f"{name or path.name}: already in the library")
            return self.docs[doc_id]

        progress(f"{name or path.name}: extracting text and page images")
        info, pages = ingest_file(path, self.dir, self.services, display_name=name)
        chunks = chunk_pages(pages)

        progress(f"{info.name}: embedding {len(chunks)} text chunks")
        text_vecs = self.services.embed_text([index_text(c) for c in chunks]) if chunks else []
        imaged = [p for p in pages if p.image_path]
        progress(f"{info.name}: embedding {len(imaged)} page images")
        img_vecs = self.services.embed_images([p.image_path for p in imaged]) if imaged else []

        with self._lock:
            d = self._doc_dir(info.doc_id)
            d.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, d / ("original" + info.ext))
            (d / "pages.json").write_text(json.dumps([p.model_dump() for p in pages], indent=1))
            (d / "chunks.json").write_text(json.dumps([c.model_dump() for c in chunks], indent=1))
            if chunks:
                self._text.upsert(
                    ids=[c.chunk_id for c in chunks], embeddings=text_vecs,
                    metadatas=[{"doc_id": c.doc_id, "number": c.number} for c in chunks])
            if imaged:
                self._images.upsert(
                    ids=[p.page_id for p in imaged], embeddings=img_vecs,
                    metadatas=[{"doc_id": p.doc_id, "number": p.number} for p in imaged])
            self.docs[info.doc_id] = info
            self.pages.update({p.page_id: p for p in pages})
            self.chunks.update({c.chunk_id: c for c in chunks})
            self._save_manifest()
            self._rebuild_bm25()
        progress(f"{info.name}: done ({info.num_pages} {info.kind}s, {len(chunks)} chunks)")
        return info

    def remove(self, doc_id: str) -> None:
        with self._lock:
            if doc_id not in self.docs:
                return
            self._text.delete(where={"doc_id": doc_id})
            self._images.delete(where={"doc_id": doc_id})
            self.pages = {k: v for k, v in self.pages.items() if v.doc_id != doc_id}
            self.chunks = {k: v for k, v in self.chunks.items() if v.doc_id != doc_id}
            del self.docs[doc_id]
            for sub in (self._doc_dir(doc_id), self.dir / "pages" / doc_id, self.dir / "converted" / doc_id):
                shutil.rmtree(sub, ignore_errors=True)
            self._save_manifest()
            self._rebuild_bm25()

    def ocr_text(self, page: Page) -> str:
        """OCR of the page image (cached on disk). Used to verify quotes of text that is
        visible on a slide (e.g. inside a graphic) but absent from its text layer."""
        if not page.image_path:
            return ""
        if page.visual_text or page.ocr:
            return page.visual_text or page.text
        cache = self.dir / "ocr_cache" / f"{page.doc_id}_{page.number}.txt"
        if cache.exists():
            return cache.read_text()
        try:
            text = clean_ocr(self.services.ocr(page.image_path))
        except Exception as e:  # OCR is a fallback; never fail the answer because of it
            log.warning("OCR fallback failed for %s: %s", page.page_id, e)
            return ""
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text)
        return text

    def quote_on_page(self, quote: str, page: Page, chunk_text: str | None = None) -> bool:
        """Does `quote` appear in the chunk, the page text, or (for imaged pages) the page's OCR text?"""
        from .grounding import quote_found

        if chunk_text and quote_found(quote, chunk_text):
            return True
        if quote_found(quote, page.text):
            return True
        return bool(page.image_path) and quote_found(quote, self.ocr_text(page))

    def doc_pages(self, doc_id: str) -> list[Page]:
        return sorted((p for p in self.pages.values() if p.doc_id == doc_id), key=lambda p: p.number)

    def doc_chunks(self, doc_ids: list[str] | None = None) -> list[Chunk]:
        allowed = set(doc_ids) if doc_ids else None
        return [c for c in self.chunks.values() if allowed is None or c.doc_id in allowed]

    # -- search (each returns (id, score), best first) ----------------------
    @staticmethod
    def _where(doc_ids: list[str] | None) -> dict | None:
        if not doc_ids:
            return None
        return {"doc_id": doc_ids[0]} if len(doc_ids) == 1 else {"doc_id": {"$in": list(doc_ids)}}

    def search_keyword(self, query: str, k: int = 20, doc_ids: list[str] | None = None) -> list[tuple[str, float]]:
        with self._lock:
            bm25, ids = self._bm25, self._bm25_ids
        if bm25 is None:
            return []
        tokens = _tokenize([query])
        if not tokens.ids or not tokens.ids[0]:
            return []  # query was only stopwords
        allowed = set(doc_ids) if doc_ids else None
        n = len(ids) if allowed else min(k, len(ids))
        idx, scores = bm25.retrieve(tokens, k=n, show_progress=False)
        out = []
        for i, s in zip(idx[0], scores[0]):
            cid = ids[int(i)]
            if s > 0 and (allowed is None or self.chunks[cid].doc_id in allowed):
                out.append((cid, float(s)))
        return out[:k]

    def search_text(self, query: str, k: int = 20, doc_ids: list[str] | None = None) -> list[tuple[str, float]]:
        if self._text.count() == 0:
            return []
        vec = self.services.embed_text([query], kind="query")[0]
        res = self._text.query(query_embeddings=[vec], n_results=min(k, self._text.count()),
                               where=self._where(doc_ids))
        return [(i, 1 - d) for i, d in zip(res["ids"][0], res["distances"][0])]

    def search_images(self, query: str, k: int = 10, doc_ids: list[str] | None = None) -> list[tuple[str, float]]:
        if self._images.count() == 0:
            return []
        vec = self.services.embed_visual_query([query])[0]
        res = self._images.query(query_embeddings=[vec], n_results=min(k, self._images.count()),
                                 where=self._where(doc_ids))
        return [(i, 1 - d) for i, d in zip(res["ids"][0], res["distances"][0])]
