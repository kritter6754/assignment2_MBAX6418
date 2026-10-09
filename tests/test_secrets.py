"""Keys must never appear in errors, logs, reprs, the UI or the repository."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import httpx
import pytest

from course_assistant.config import PROJECT_ROOT, RedactingFilter, Settings, redact
from course_assistant.services import ClassServices, ServiceError

FAKE_KEY = "sk-test-SUPERSECRET-123"


def settings(tmp_path) -> Settings:
    return Settings(api_key=FAKE_KEY, llm_url="http://llm.test/v1", llm_model="m",
                    text_embed_url="http://e.test/v1", text_embed_model="m",
                    text_query_prefix="query: ", text_passage_prefix="passage: ",
                    visual_embed_url="http://v.test/v1", visual_embed_model="m",
                    rerank_url="http://r.test/v1", rerank_model="m",
                    ocr_url="http://o.test/v1", ocr_model="m", data_dir=tmp_path)


def test_key_not_in_settings_repr(tmp_path):
    assert FAKE_KEY not in repr(settings(tmp_path))


def test_server_errors_echoing_the_key_are_redacted(tmp_path):
    # A misbehaving server that echoes the Authorization header back in its error body.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad token {request.headers['authorization']}")

    svc = ClassServices(settings(tmp_path), transport=httpx.MockTransport(handler))
    with pytest.raises(ServiceError) as exc:
        svc.chat([{"role": "user", "content": "hi"}])
    assert FAKE_KEY not in str(exc.value) and "[REDACTED]" in str(exc.value)


def test_key_sent_only_as_header(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(request.url), request.content.decode()
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]})

    ClassServices(settings(tmp_path), transport=httpx.MockTransport(handler)).embed_text(["hello"])
    assert FAKE_KEY not in seen["url"] and FAKE_KEY not in seen["body"]


def test_logs_are_redacted(tmp_path, caplog):
    ClassServices(settings(tmp_path))  # registers the key as a secret
    assert redact(f"key={FAKE_KEY}") == "key=[REDACTED]"
    rec = logging.LogRecord("t", logging.INFO, "", 0, "using %s", (FAKE_KEY,), None)
    RedactingFilter().filter(rec)
    assert FAKE_KEY not in rec.getMessage()
    num = logging.LogRecord("t", logging.INFO, "", 0, "%d docs", (5,), None)
    RedactingFilter().filter(num)
    assert num.getMessage() == "5 docs"  # non-string args keep their type


def test_env_file_is_gitignored_and_example_has_dummies():
    ignore = (PROJECT_ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in ignore and "data/" in ignore
    example = (PROJECT_ROOT / ".env.example").read_text()
    assert "CLASS_API_KEY=replace-me" in example
    assert "dobolyi" not in example


def test_real_key_not_in_tracked_files():
    """If a local .env exists, its key value must not appear in any git-tracked file."""
    env = PROJECT_ROOT / ".env"
    if not env.exists():
        pytest.skip("no local .env")
    key = next((l.split("=", 1)[1].strip() for l in env.read_text().splitlines()
                if l.startswith("CLASS_API_KEY=")), "")
    if not key or key == "replace-me":
        pytest.skip("no real key configured")
    files = subprocess.run(["git", "ls-files"], cwd=PROJECT_ROOT, capture_output=True, text=True).stdout.split()
    for f in files:
        p = Path(PROJECT_ROOT / f)
        if p.suffix in {".png", ".jpg", ".pdf"} or not p.is_file():
            continue
        text = p.read_text(errors="ignore")
        # Course names like "MBAX 6418" contain digits; check the key as an assignment/header value.
        for pattern in (f"KEY={key}", f"KEY = {key}", f"Bearer {key}", f'"{key}"', f"'{key}'"):
            assert pattern not in text, f"API key found in tracked file {f}"
