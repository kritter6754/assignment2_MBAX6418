"""Drive the running app in a headless browser and save the README screenshots.

Asks a question (answer with its slide image), asks several numbered questions at once,
generates a quiz with image questions, checks an answer (feedback with sources), opens
Prior Questions and the Library, and switches to dark mode. Fails loudly if a step breaks,
if a quiz answer is visible before it is checked, or if anything secret reaches the page.

    uv run course-assistant                        # in one terminal
    uv run python scripts/ui_walkthrough.py        # in another (or pass a URL)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7860"
OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
WEEK5 = "MBAX 6418 - Week 5 - Context Engineering and RAG v1.pptx"
WEEK2 = "MBAX 6418 - Week 2 - LLM Fundamentals v2.pptx"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000}, color_scheme="light")
        page.goto(URL)
        expect(page.get_by_text("MBAX 6418 - Course Assistant")).to_be_visible(timeout=60_000)

        # -- Ask: one question, answer with its supporting slide image -------------
        page.get_by_label("Your question").fill("Why does RAG use both keyword search and vector search?")
        page.get_by_role("button", name="Ask", exact=True).click()
        img = page.locator("img[alt^='[S']").first
        expect(img).to_be_visible(timeout=120_000)
        assert img.evaluate("e => e.naturalWidth") > 0, "source image did not load"
        page.wait_for_timeout(1000)
        page.screenshot(path=OUT / "ask.png", full_page=True)
        print("ask: ok")

        # -- Ask: several numbered questions, one answer box each -------------------
        page.get_by_label("Your question").fill(
            "1. What is the tradeoff of using a KV cache?\n2. What does reranking do in hybrid RAG?\n"
            "3. What is the capital of Australia?")
        page.get_by_role("button", name="Ask", exact=True).click()
        expect(page.locator(".qa-heading h3", has_text="Question 3")).to_be_visible(timeout=180_000)
        expect(page.locator(".answer-card").filter(visible=True)).to_have_count(3)
        expect(page.get_by_text("Not found in the selected materials")).to_be_visible()
        page.wait_for_timeout(1000)
        page.screenshot(path=OUT / "ask_multi.png", full_page=True)
        print("ask (3 numbered questions, one not in the materials): ok")

        # -- Quiz with image questions ----------------------------------------------
        page.get_by_role("tab", name="Quiz").click()
        page.get_by_label(WEEK5).filter(visible=True).first.check()
        slider = page.get_by_label("number input for Number of questions")
        slider.fill("5")
        slider.press("Enter")
        page.get_by_label(re.compile("Produce questions with viewable images")).check()
        page.get_by_role("button", name="Generate quiz").click()
        expect(page.get_by_text("Q1.")).to_be_visible(timeout=240_000)
        page.wait_for_timeout(1000)
        cards_text = " ".join(page.locator(".q-card").filter(visible=True).all_inner_texts())
        assert "Source:" not in cards_text and "Solution:" not in cards_text and "Correct —" not in cards_text, \
            "solution visible before answering"
        # Unpin the scoreboard for the screenshots so it doesn't cover the question cards.
        page.add_style_tag(content=".quiz-summary { position: static !important; }")
        image_card = page.locator(".q-card", has=page.locator(".img-badge")).first
        expect(image_card).to_be_visible()
        image_card.scroll_into_view_if_needed()
        image_card.screenshot(path=OUT / "quiz_image_question.png")
        print("quiz: generated, answers hidden, image question shown: ok")

        # check one answer -> feedback with explanation, source excerpt and slide image
        card = page.locator(".q-card").filter(visible=True).first
        card.locator("input[type=radio]").first.check()
        card.get_by_role("button", name="Check answer").click()
        expect(card.get_by_text("Source:")).to_be_visible(timeout=60_000)
        expect(page.locator(".score-pill", has_text="Questions finished")).to_contain_text("1 / ")
        page.wait_for_timeout(1500)
        page.locator(".quiz-summary").scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        scroll_y = page.evaluate("window.scrollY")  # bounding boxes are window-relative
        board = page.locator(".quiz-summary").bounding_box()
        box = card.bounding_box()
        top = board["y"] + scroll_y - 10
        page.screenshot(path=OUT / "quiz_feedback.png", full_page=True, clip={
            "x": box["x"] - 10, "y": top, "width": box["width"] + 20,
            "height": box["y"] + scroll_y + box["height"] + 10 - top})
        print("quiz: feedback with sources and live scoreboard: ok")

        # -- Prior Questions -----------------------------------------------------------
        page.get_by_role("tab", name="Prior Questions").click()
        expect(page.get_by_role("heading", name="Missed Questions")).to_be_visible()
        page.wait_for_timeout(1000)
        page.screenshot(path=OUT / "prior_questions.png")
        print("prior questions: ok")

        # -- Library browser ----------------------------------------------------------
        page.get_by_role("tab", name="Library").click()
        page.get_by_label("Document", exact=True).click()
        page.get_by_role("option", name=WEEK2).click()
        nb = page.get_by_label("Page / slide")
        nb.fill("11")
        nb.press("Enter")
        expect(page.get_by_label("Extracted text")).to_have_value(re.compile("KV Caching"), timeout=30_000)
        page.wait_for_timeout(1000)
        page.screenshot(path=OUT / "library.png", full_page=True)
        print("library: ok")

        # -- Dark mode ------------------------------------------------------------------
        page.get_by_role("tab", name="Ask").click()
        page.locator("#theme-toggle-btn").click()
        page.wait_for_timeout(800)
        assert page.evaluate("document.body.classList.contains('dark')"), "dark mode did not turn on"
        page.screenshot(path=OUT / "dark_mode.png")
        print("dark mode: ok")

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
