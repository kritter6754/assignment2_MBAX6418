"""Course assistant: hybrid RAG question answering and practice quizzes over course files."""
from __future__ import annotations

from functools import lru_cache

from .config import load_settings, setup_logging
from .library import Library
from .services import ClassServices


@lru_cache(maxsize=1)
def get_library() -> Library:
    settings = load_settings()
    if not settings.configured:
        raise RuntimeError("CLASS_API_KEY is not set. Copy .env.example to .env and fill it in.")
    return Library(settings.data_dir, ClassServices(settings))


__all__ = ["get_library", "load_settings", "setup_logging"]
