import pytest

import rag_logic as L

ACTS = {"V-1799": dict(title="The Wills And Intestacy Regulation, 1799"),
        "XIX-1810": dict(title="The Charitable Endowments Regulation, 1810")}


@pytest.mark.parametrize("q,intent,k", [
    ("When was Regulation V of 1799 published?", "metadata", 2),
    ("How many sections does Regulation XIX of 1810 have?", "metadata", 2),
    ("What is Section 4 about?", "section", 3),
    ("ধারা ৪ কী বলে?", "section", 3),
    ("Which sections deal with administrators?", "list", 6),
    ("Compare section 2 and section 3", "compare", 10),
    ("Walk me through what happens when heirs dispute", "compare", 10),
    ("Who appoints an administrator?", "general", 6),
])
def test_intent_routing(q, intent, k):
    assert L.classify_intent(q) == intent
    assert L.route_top_k(intent) == k


def test_top_k_override():
    assert L.route_top_k("metadata", 7) == 7


def test_act_detection():
    assert L.detect_acts("What does Regulation XIX of 1810 say about X?", ACTS) == ["XIX-1810"]
    assert L.detect_acts("Reg. V, 1799 vs Regulation XIX of 1810", ACTS) == ["V-1799", "XIX-1810"]
    assert L.detect_acts("the wills and intestacy regulation", ACTS) == ["V-1799"]
    assert L.detect_acts("Regulation 19 of 1810", ACTS) == ["XIX-1810"]
    assert L.detect_acts("nothing here", ACTS) == []


def test_section_detection():
    assert L.detect_sections("section 16A of Reg XIX and Sec. 4") == ["16A", "4"]
    assert L.detect_sections("১৬ ধারা") == ["16"]


def test_compact_sections():
    assert L.compact_sections(["2", "3", "4", "8", "9", "12"]) == "2-4, 8-9, 12"


def test_canonicalize_and_validate():
    ans = "Heirs may agree on a manager [S1]. The Judge takes security [S2, S1]."
    out = L.canonicalize_citations(ans, {1: "V-1799-s3", 2: "V-1799-s4"})
    assert "[S1]" not in out and "[V-1799-s4][V-1799-s3]" in out
    v = L.validate_citations(out, {"V-1799-s3", "V-1799-s4"})
    assert v["valid"] == ["V-1799-s3", "V-1799-s4"] and not v["invalid"] and not v["uncited"]


def test_validate_flags_invalid_and_uncited():
    v = L.validate_citations("The Judge appoints an administrator for the estate. Another claim [XIX-1810-s9].",
                             {"V-1799-s4"})
    assert v["invalid"] == ["XIX-1810-s9"]
    assert len(v["uncited"]) == 1


def test_base_id_matches_part_ids():
    v = L.validate_citations("A long section is split across chunks and cited by base id [V-1799-s3].",
                             {"V-1799-s3-1", "V-1799-s3-2"})
    assert v["valid"] == ["V-1799-s3"] and not v["invalid"]


def test_absence_sentence_not_flagged():
    v = L.validate_citations("There is no section 16A in Regulation XIX of 1810.", set())
    assert not v["uncited"]
