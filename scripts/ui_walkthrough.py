"""Drive the running app in a headless browser: ask a question, take a quiz, browse
the library. Saves screenshots to docs/screenshots/ and fails loudly if a step breaks.

    uv run python -m course_assistant.app          # in one terminal
    uv run python scripts/ui_walkthrough.py        # in another
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

URL = "http://127.0.0.1:7860"
OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
QUESTION = "Why does RAG use both keyword search and vector search?"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.goto(URL)

        # -- Ask ---------------------------------------------------------------
        page.get_by_label("Your question").fill(QUESTION)
        page.get_by_role("button", name="Ask", exact=True).click()
        expect(page.get_by_text("Sources", exact=True)).to_be_visible(timeout=120_000)
        img = page.locator("img[alt^='[S']").first
        expect(img).to_be_visible(timeout=30_000)  # the cited slide image is shown
        assert img.evaluate("e => e.naturalWidth") > 0, "source image did not load"
        page.wait_for_timeout(1000)
        page.screenshot(path=OUT / "ask.png", full_page=True)
        print("ask: ok")

        page.get_by_label("Your question").fill("What is the capital of Australia?")
        page.get_by_role("button", name="Ask", exact=True).click()
        expect(page.get_by_text("Not found in the selected materials")).to_be_visible(timeout=120_000)
        page.screenshot(path=OUT / "ask_not_found.png", full_page=True)
        print("ask (not in materials): ok")

        # -- Quiz --------------------------------------------------------------
        page.get_by_role("tab", name="Quiz").click()
        page.get_by_label("MBAX 6418 - Week 5 - Context Engineering and RAG v1.pptx").filter(visible=True).first.check()
        page.get_by_label("Topic filter (optional)").fill("hybrid search and reranking")
        slider = page.get_by_label("number input for Number of questions")
        slider.fill("3")
        slider.press("Enter")
        page.get_by_role("button", name="Generate quiz").click()
        expect(page.get_by_text("Q1.")).to_be_visible(timeout=180_000)
        body = page.content()
        assert "Correct" not in body and "Solution:" not in body, "solution visible before answering"
        page.screenshot(path=OUT / "quiz_new.png", full_page=True)

        page.get_by_role("radio").filter(visible=True).first.check()
        page.get_by_role("button", name="Check answer").first.click()
        expect(page.get_by_text("Source:").first).to_be_visible(timeout=60_000)
        page.get_by_role("button", name="Show solution").nth(1).click()
        expect(page.get_by_text("Solution:").first).to_be_visible(timeout=60_000)
        expect(page.get_by_text("2 of 3 questions finished")).to_be_visible()
        page.wait_for_timeout(1000)
        page.screenshot(path=OUT / "quiz_feedback.png", full_page=True)
        print("quiz: ok")

        # -- Library -----------------------------------------------------------
        page.get_by_role("tab", name="Library").click()
        expect(page.get_by_text("Accepted formats")).to_be_visible()
        page.get_by_label("Document", exact=True).click()
        page.get_by_role("option", name="MBAX 6418 - Week 2 - LLM Fundamentals v2.pptx").click()
        nb = page.get_by_label("Page / slide")
        nb.fill("11")
        nb.press("Enter")
        expect(page.get_by_label("Extracted text")).to_have_value(re.compile("KV Caching"), timeout=30_000)
        page.wait_for_timeout(1000)
        page.screenshot(path=OUT / "library.png", full_page=True)
        print("library: ok")

        # Nothing secret should ever reach the browser.
        html = page.content()
        for marker in ("CLASS_API_KEY", "Bearer ", "Authorization"):
            assert marker not in html, f"{marker!r} found in page HTML"
        print("no secrets in page: ok")
        browser.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("UI walkthrough FAILED:", e)
        sys.exit(1)
