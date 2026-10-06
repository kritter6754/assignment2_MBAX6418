from __future__ import annotations

from conftest import answer_json, doc_id_by_name

from course_assistant.answer import answer_question
from course_assistant.grounding import quote_found
from course_assistant.retrieval import retrieve, rrf

SUPPORTED = '{"supported": true, "unsupported_claims": []}'
UNSUPPORTED = '{"supported": false, "unsupported_claims": ["made-up number"]}'


# -- quote checks -----------------------------------------------------------------
def test_quote_found_tolerates_formatting_not_paraphrase():
    src = "• Photosynthesis converts light energy\ninto chemical energy stored in glucose."
    assert quote_found("Photosynthesis converts light energy into chemical energy", src)
    assert quote_found("“photosynthesis converts LIGHT energy”", src)
    assert not quote_found("Plants turn sunlight into sugar for food", src)
    assert not quote_found("glucose", src)  # too short to count as evidence


def test_quote_found_ellipsis_requires_every_fragment():
    src = "The first phase is prefill. It is measured by TTFT. The second phase is decode."
    assert quote_found("The first phase is prefill... The second phase is decode", src)
    assert not quote_found("The first phase is prefill... The third phase is training", src)


# -- retrieval --------------------------------------------------------------------
def test_rrf_rewards_agreement():
    s = rrf([["a", "b", "c"], ["b", "a", "d"]])
    assert max(s, key=s.get) in ("a", "b") and s["d"] < s["a"]


def test_keyword_search_and_doc_filter(lib):
    hits = lib.search_keyword("mitochondria ATP", k=5)
    assert hits and "mitochondria" in lib.chunks[hits[0][0]].text
    geo = doc_id_by_name(lib, "geology.md")
    assert all(lib.chunks[i].doc_id == geo for i, _ in lib.search_keyword("energy plates", 5, [geo]))
    assert lib.search_keyword("the and of", 5) == []  # stopwords only


def test_hybrid_retrieve_labels_and_filters(lib):
    ev = retrieve(lib, "Where does cellular respiration happen?")
    assert [e.eid for e in ev] == [f"S{i}" for i in range(1, len(ev) + 1)]
    assert "mitochondria" in ev[0].text
    geo = doc_id_by_name(lib, "geology.md")
    assert all(e.page.doc_id == geo for e in retrieve(lib, "energy", [geo]))
    for mode in ("hybrid_no_rerank", "text_vector", "keyword"):
        assert retrieve(lib, "mitochondria", mode=mode)


# -- answering --------------------------------------------------------------------
def _evidence_id(lib, phrase):
    ev = retrieve(lib, "Where does cellular respiration happen?")
    return next(e.eid for e in ev if phrase in e.text)


def test_valid_answer_keeps_verified_source(lib, fake):
    sid = _evidence_id(lib, "mitochondria")
    fake.chat_replies = [answer_json(True, f"In the mitochondria [{sid}].",
                                     [(sid, "It happens in the mitochondria.")]), SUPPORTED]
    r = answer_question(lib, "Where does cellular respiration happen?")
    assert r.status == "answered"
    assert len(r.sources) == 1 and r.sources[0].doc_name == "biology.md"
    assert r.sources[0].location.startswith("Section 2")
    assert r.sources[0].excerpt == "It happens in the mitochondria."


def test_fabricated_quote_is_rejected(lib, fake):
    sid = _evidence_id(lib, "mitochondria")
    fake.chat_replies = [answer_json(True, f"In the nucleus [{sid}].",
                                     [(sid, "Respiration happens in the nucleus of the cell.")])]
    r = answer_question(lib, "Where does cellular respiration happen?")
    assert r.status == "not_found" and r.sources == []
    assert "nucleus" not in r.answer  # the unverified draft is not shown
    assert any("not found there" in n for n in r.notes)


def test_unknown_source_id_is_dropped(lib, fake):
    sid = _evidence_id(lib, "mitochondria")
    fake.chat_replies = [answer_json(True, f"Mitochondria [{sid}] [S99].",
                                     [(sid, "It happens in the mitochondria."), ("S99", "anything at all here")]),
                         SUPPORTED]
    r = answer_question(lib, "Where does cellular respiration happen?")
    assert [s.eid for s in r.sources] == [sid]
    assert "S99" not in r.answer
    assert any("S99" in n for n in r.notes)


def test_not_found_is_reported(lib, fake):
    fake.chat_replies = [answer_json(False, "The materials do not mention Australia.", [])]
    r = answer_question(lib, "What is the capital of Australia?")
    assert r.status == "not_found" and r.sources == []
    assert "couldn't find" in r.answer


def test_malformed_json_is_retried_once(lib, fake):
    sid = _evidence_id(lib, "mitochondria")
    fake.chat_replies = ["not json at all",
                         answer_json(True, f"Mitochondria [{sid}].", [(sid, "It happens in the mitochondria.")]),
                         SUPPORTED]
    assert answer_question(lib, "Where does cellular respiration happen?").status == "answered"


def test_unsupported_answer_is_flagged(lib, fake):
    sid = _evidence_id(lib, "mitochondria")
    bad = answer_json(True, f"In the mitochondria, 38 ATP each time [{sid}].",
                      [(sid, "It happens in the mitochondria.")])
    fake.chat_replies = [bad, UNSUPPORTED, bad, UNSUPPORTED]
    r = answer_question(lib, "Where does cellular respiration happen?")
    assert r.status == "partially_supported"
    assert any("made-up number" in n for n in r.notes)


def test_empty_question_and_empty_library(lib, fake, tmp_path):
    assert answer_question(lib, "   ").status == "error"
    from course_assistant.library import Library
    assert answer_question(Library(tmp_path / "empty", fake), "hi").status == "error"


def test_images_are_sent_to_the_llm_for_visual_evidence(tmp_path, fake):
    from PIL import Image

    from course_assistant.library import Library

    img = tmp_path / "chart.png"
    Image.new("RGB", (80, 60), "white").save(img)
    library = Library(tmp_path / "data", fake)
    library.add_file(img, progress=lambda m: None)
    ev = retrieve(library, "photosynthesis leaves chart")
    assert any(e.kind == "image" for e in ev)
    sid = next(e.eid for e in ev if e.kind == "image")
    fake.chat_replies = [answer_json(True, f"About photosynthesis [{sid}].",
                                     [(sid, "OCR TEXT FROM IMAGE about photosynthesis")]), SUPPORTED]
    r = answer_question(library, "photosynthesis leaves chart")
    sent = fake.chat_calls[0][1]["content"]
    assert any(part.get("type") == "image_url" for part in sent)
    assert r.status == "answered" and r.sources[0].image_path


def _image_library(tmp_path, fake):
    from PIL import Image

    from course_assistant.library import Library

    img = tmp_path / "diagram.png"
    Image.new("RGB", (80, 60), "white").save(img)
    library = Library(tmp_path / "data", fake)
    library.add_file(img, progress=lambda m: None)
    return library


def test_visual_description_kept_only_with_support_check(tmp_path, fake):
    library = _image_library(tmp_path, fake)
    q = "photosynthesis leaves diagram"
    sid = next(e.eid for e in retrieve(library, q) if e.kind == "image")
    described = answer_json(True, f"The diagram links leaves to sunlight [{sid}].",
                            [(sid, "Leaves -> Sunlight -> Sugar")])  # a description, not exact text
    fake.chat_replies = [described, SUPPORTED]
    r = answer_question(library, q, check_support=False)  # forced on for unverified visual quotes
    assert r.status == "answered" and r.sources[0].quote_verified is False
    support_call = fake.chat_calls[1][1]["content"]
    assert any(p.get("type") == "image_url" for p in support_call)  # the checker sees the image

    fake.chat_replies = [described, UNSUPPORTED, described, UNSUPPORTED]
    assert answer_question(library, q).status == "partially_supported"


def test_special_tokens_survive_round_trip():
    from course_assistant.services import protect_special, restore_special

    s = "ChatML uses <|im_start|>system and <|im_end|>"
    assert "<|" not in protect_special(s)
    assert restore_special(protect_special(s)) == s
