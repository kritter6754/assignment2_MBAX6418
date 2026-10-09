"""Assemble the folder that gets deployed to the server: code + the prepared library.

    uv run python scripts/build_deploy_bundle.py          # -> deploy/build/
    railway up deploy/build --path-as-root

Only what the app needs at runtime is copied: page images, extracted text, chunks and
the Chroma indexes. Original course files, .env, tests and the local virtualenv are
never included, and deploy/ is git-ignored, so slide content stays out of GitHub.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from course_assistant.config import load_settings  # noqa: E402

CODE = ["Dockerfile", "pyproject.toml", "uv.lock", ".python-version", "course_assistant"]


def main() -> None:
    src = load_settings().data_dir
    if not (src / "library.json").exists():
        sys.exit(f"No library at {src}. Run scripts/ingest_folder.py first.")
    out = ROOT / "deploy" / "build"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    for name in CODE:
        p = ROOT / name
        if p.is_dir():
            shutil.copytree(p, out / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(p, out / name)

    lib = out / "library"
    lib.mkdir()
    shutil.copy2(src / "library.json", lib / "library.json")
    for sub in ("pages", "chroma", "ocr_cache"):
        if (src / sub).exists():
            shutil.copytree(src / sub, lib / sub)
    for doc_dir in (src / "docs").iterdir():
        dest = lib / "docs" / doc_dir.name
        dest.mkdir(parents=True)
        shutil.copy2(doc_dir / "chunks.json", dest / "chunks.json")
        pages = json.loads((doc_dir / "pages.json").read_text())
        for p in pages:  # store image paths relative to the library folder
            if p.get("image_path"):
                parts = Path(p["image_path"]).parts
                p["image_path"] = str(Path(*parts[parts.index("pages"):]))
        (dest / "pages.json").write_text(json.dumps(pages, indent=1))

    (out / ".dockerignore").write_text("**/__pycache__\n")
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1e6
    leaked = [f for f in out.rglob("*") if f.name == ".env" or f.suffix in (".pptx", ".ppt", ".docx", ".pdf")]
    if leaked:
        sys.exit(f"Refusing to build: unexpected files in bundle: {leaked}")
    print(f"Built {out} ({size:.0f} MB, {len(json.loads((lib / 'library.json').read_text()))} documents)")


if __name__ == "__main__":
    main()
