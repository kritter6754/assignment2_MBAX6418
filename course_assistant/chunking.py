"""Split page text into chunks. Chunks never cross a page/slide/section boundary,
so every chunk keeps its exact document, location and character offset."""
from __future__ import annotations

from langchain_text_splitters import RecursiveCharacterTextSplitter

from .models import Chunk, Page

CHUNK_SIZE = 900
CHUNK_OVERLAP = 150
MIN_CHUNK_CHARS = 25  # e.g. a heading split off on its own; its title is already in index_text()


def make_splitter(size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=size, chunk_overlap=overlap, add_start_index=True,
        separators=["\n\n", "\n", ". ", " ", ""],
    )


def chunk_pages(pages: list[Page], size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[Chunk]:
    splitter = make_splitter(size, overlap)
    chunks: list[Chunk] = []
    for p in pages:
        for field, prefix in (("text", ""), ("visual_text", "v")):
            source = getattr(p, field)
            if not source.strip():
                continue
            docs = splitter.create_documents([source])
            if len(docs) > 1:
                docs = [d for d in docs if len(d.page_content.strip()) >= MIN_CHUNK_CHARS] or docs
            for j, d in enumerate(docs):
                chunks.append(Chunk(
                    chunk_id=f"{p.doc_id}:{p.number}:{prefix}{j}", doc_id=p.doc_id, doc_name=p.doc_name,
                    number=p.number, kind=p.kind, title=p.title, text=d.page_content,
                    start=d.metadata.get("start_index", 0), field=field,
                ))
    return chunks


def index_text(c: Chunk) -> str:
    """Text used for search: adds the document name and slide/page title as context,
    so a chunk like '• 40% faster' is still findable by its slide title."""
    header = f"{c.doc_name} | {c.label}"
    if c.title and c.kind != "section":
        header += f" | {c.title}"
    if c.field == "visual_text":
        header += " | text visible in the page image"
    return f"{header}\n{c.text}"
