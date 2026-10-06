"""Add every supported file in a folder to the library.

    uv run python scripts/ingest_folder.py /path/to/Canvas/downloads
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from course_assistant import get_library, setup_logging  # noqa: E402
from course_assistant.config import ACCEPTED_FORMATS  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    setup_logging()
    folder = Path(sys.argv[1])
    lib = get_library()
    files = sorted(p for p in folder.rglob("*") if p.suffix.lower() in ACCEPTED_FORMATS)
    skipped = sorted(p.name for p in folder.rglob("*") if p.is_file() and p.suffix.lower() not in ACCEPTED_FORMATS
                     and not p.name.startswith("."))
    for f in files:
        info = lib.add_file(f, progress=print)
        for w in info.warnings:
            print(f"  warning: {w}")
    if skipped:
        print("Skipped unsupported files:", ", ".join(skipped))
    print(f"Library: {len(lib.docs)} documents, {len(lib.pages)} pages, {len(lib.chunks)} chunks")


if __name__ == "__main__":
    main()
