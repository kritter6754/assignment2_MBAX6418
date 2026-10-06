"""Shared data records. Every record carries enough source detail to cite it."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal["page", "slide", "section", "image"]


def location_label(kind: Kind, number: int, title: str = "") -> str:
    base = {"page": "Page", "slide": "Slide", "section": "Section", "image": "Image"}[kind]
    label = f"{base} {number}"
    if kind == "section" and title:
        label += f" ({title[:60]})"
    return label


class DocInfo(BaseModel):
    doc_id: str
    name: str  # original file name
    ext: str
    kind: Kind
    num_pages: int
    has_images: bool
    warnings: list[str] = Field(default_factory=list)


class Page(BaseModel):
    """One page / slide / section of a document, with its original image if there is one."""

    doc_id: str
    doc_name: str
    number: int  # 1-based page, slide or section number
    kind: Kind
    title: str = ""
    text: str
    image_path: str | None = None
    ocr: bool = False  # text came from OCR rather than the file itself
    # Text OCR'd from the page image that the text layer lacks (e.g. inside a diagram,
    # table screenshot or chart). Empty when OCR adds nothing new.
    visual_text: str = ""

    @property
    def page_id(self) -> str:
        return f"{self.doc_id}:{self.number}"

    @property
    def label(self) -> str:
        return location_label(self.kind, self.number, self.title)


class Chunk(BaseModel):
    """A passage of text from exactly one page, slide or section."""

    chunk_id: str
    doc_id: str
    doc_name: str
    number: int
    kind: Kind
    title: str = ""
    text: str
    start: int  # character offset of this chunk within its source field
    field: Literal["text", "visual_text"] = "text"  # which Page field the chunk comes from

    @property
    def page_id(self) -> str:
        return f"{self.doc_id}:{self.number}"

    @property
    def label(self) -> str:
        return location_label(self.kind, self.number, self.title)
