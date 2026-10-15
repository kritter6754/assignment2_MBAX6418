"""Settings loaded from environment variables (or a local, git-ignored .env file).

The API key is read only on the server side. It is never passed to the Gradio
interface, and `redact()` scrubs it from any text that might be logged or shown.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Formats the app accepts, shown in the interface. Keys are file extensions.
ACCEPTED_FORMATS: dict[str, str] = {
    ".pdf": "PDF (text + page images; scanned pages are OCR'd)",
    ".pptx": "PowerPoint (slide text + speaker notes + slide images)",
    ".ppt": "PowerPoint 97-2003 (converted, then as .pptx)",
    ".docx": "Word (text by heading + page images)",
    ".doc": "Word 97-2003 (converted, then as PDF)",
    ".md": "Markdown (text by heading, no images)",
    ".txt": "Plain text (no images)",
    ".html": "HTML page, e.g. a Canvas page export (text by heading, no images)",
    ".htm": "HTML page (text by heading, no images)",
    ".png": "Image (OCR'd text + the image itself)",
    ".jpg": "Image (OCR'd text + the image itself)",
    ".jpeg": "Image (OCR'd text + the image itself)",
}


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)  # never printed by repr()
    llm_url: str
    llm_model: str
    text_embed_url: str
    text_embed_model: str
    text_query_prefix: str
    text_passage_prefix: str
    visual_embed_url: str
    visual_embed_model: str
    rerank_url: str
    rerank_model: str
    ocr_url: str
    ocr_model: str
    data_dir: Path
    timeout_s: float = 180.0

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and self.api_key != "replace-me"


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    e = os.environ.get
    data_dir = Path(e("DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    return Settings(
        api_key=e("CLASS_API_KEY", ""),
        llm_url=e("LLM_BASE_URL", ""),
        llm_model=e("LLM_MODEL", "cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit"),
        text_embed_url=e("TEXT_EMBED_BASE_URL", ""),
        text_embed_model=e("TEXT_EMBED_MODEL", "nvidia/Nemotron-3-Embed-1B-BF16"),
        # Nemotron embedders expect these prefixes; without them retrieval hit@3 drops 0.93 -> 0.55
        # on our benchmark (see README findings).
        text_query_prefix=e("TEXT_EMBED_QUERY_PREFIX", "query: "),
        text_passage_prefix=e("TEXT_EMBED_PASSAGE_PREFIX", "passage: "),
        visual_embed_url=e("VISUAL_EMBED_BASE_URL", ""),
        visual_embed_model=e("VISUAL_EMBED_MODEL", "Qwen/Qwen3-VL-Embedding-2B"),
        rerank_url=e("RERANK_BASE_URL", ""),
        rerank_model=e("RERANK_MODEL", "Qwen/Qwen3-VL-Reranker-2B"),
        ocr_url=e("OCR_BASE_URL", ""),
        ocr_model=e("OCR_MODEL", "dots.mocr"),
        data_dir=data_dir,
    )


@dataclass(frozen=True)
class ShareSettings:
    """How the interface is served. SHARE=true creates a temporary public *.gradio.live link
    (Gradio keeps it up to a week, best effort, and only while this computer runs the app); a password is then required."""

    share: bool
    username: str
    password: str = field(repr=False)
    read_only: bool  # hide upload/remove so visitors can't change the library
    host: str = "127.0.0.1"  # 0.0.0.0 on a server (e.g. Railway), so it is reachable from outside
    port: int = 7860

    @property
    def public(self) -> bool:
        return self.share or self.host not in ("127.0.0.1", "localhost")


def _flag(value: str | None, default: bool) -> bool:
    return default if value is None or value == "" else value.strip().lower() in ("1", "true", "yes", "on")


def load_share_settings(env_file: str | os.PathLike | None = None) -> ShareSettings:
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    share = _flag(os.environ.get("SHARE"), False)
    host = os.environ.get("HOST", "127.0.0.1")
    public = share or host not in ("127.0.0.1", "localhost")
    return ShareSettings(
        share=share,
        username=os.environ.get("APP_USERNAME", "mbax"),
        password=os.environ.get("APP_PASSWORD", ""),
        read_only=_flag(os.environ.get("READ_ONLY"), public),  # read-only by default when public
        host=host,
        port=int(os.environ.get("PORT", "7860")),  # Railway sets PORT
    )


_SECRETS: set[str] = set()


def register_secret(value: str) -> None:
    """Remember a value that must never appear in logs, errors or the UI."""
    if value and len(value) >= 3:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for s in _SECRETS:
        text = text.replace(s, "[REDACTED]")
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.msg))
        if record.args:
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        return True


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs full request lines; keep it quiet.
    for noisy in ("httpx", "bm25s", "chromadb"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
