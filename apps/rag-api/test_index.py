import json
import os
import re
from pathlib import Path

import pytest

import kb_json

ID_OK = re.compile(r"^(V-1799|XIX-1810)-s\d+(-\d+)?$")


def build(path, **kw):
    return kb_json.build_kb_chunks(Path(path), 1000, 120, kb_json.simple_split, **kw)


def test_stable_ids_match_pattern(corpus):
    _, chunks, _ = build(corpus)
    assert chunks
    for c in chunks:
        assert ID_OK.match(c["meta"]["sid"]), c["meta"]["sid"]
        assert c["text"].startswith("SOURCE_ID: " + c["meta"]["sid"])


def test_no_phantom_chunks(corpus, records):
    _, chunks, _ = build(corpus)
    real = {("V-1799" if r["act_no"] == "V" else "XIX-1810", r["section_id"]) for r in records}
    for c in chunks:
        m = c["meta"]
        if m["kind"] == "section":
            assert (m["act_uid"], m["section_id"]) in real
            assert re.fullmatch(r"\d*", m["sno"]), f"invented suffix: {m['sno']}"
    assert not any("16A" == c["meta"]["sno"] for c in chunks)      # 16A only appears inside text


def test_integrity_check_rejects_phantom(corpus):
    _, chunks, _ = build(corpus)
    bad = json.loads(json.dumps(chunks[-1]))
    bad["meta"].update(section_id=99, sid="XIX-1810-s99", act_uid="XIX-1810", kind="section")
    problems = kb_json.verify_chunks(chunks + [bad], {(c["meta"]["act_uid"], c["meta"]["section_id"]) for c in chunks})
    assert any("phantom" in p for p in problems)


def test_no_duplicate_hashes_or_ids(corpus):
    _, chunks, _ = build(corpus)
    keys = [(c["meta"]["act_uid"], c["meta"]["section_id"], c["meta"]["chash"]) for c in chunks]
    assert len(keys) == len(set(keys))
    assert len({c["id"] for c in chunks}) == len(chunks)


def test_exact_duplicate_records_are_deduped(tmp_path, records):
    base = tmp_path / "a.json"
    base.write_text(json.dumps(records), encoding="utf-8")
    dup = tmp_path / "b.json"
    dup.write_text(json.dumps(records + [records[3], records[0]]), encoding="utf-8")
    _, c1, _ = build(base)
    _, c2, st = build(dup)
    assert len(c1) == len(c2) and st["deduped"] >= 2


def test_conflicting_duplicates_fail_strict(tmp_path, records):
    clash = dict(records[2], section_text="2. Completely different text that conflicts with the original.")
    p = tmp_path / "c.json"
    p.write_text(json.dumps(records + [clash]), encoding="utf-8")
    with pytest.raises(kb_json.KbIntegrityError):
        build(p)
    _, chunks, st = build(p, strict=False)
    assert st["conflicts"] >= 1 and chunks


def test_num_of_sections_matches_index(corpus, records):
    _, chunks, st = build(corpus)
    per = {}
    for c in chunks:
        if c["meta"]["kind"] == "section":
            per.setdefault(c["meta"]["act_uid"], set()).add(c["meta"]["section_id"])
    assert len(per["V-1799"]) == 7 and len(per["XIX-1810"]) == 16
    assert not st["warnings"]


def test_overview_chunk_once_per_act_with_total(corpus):
    _, chunks, _ = build(corpus)
    ov = [c for c in chunks if c["meta"]["kind"] == "act"]
    assert sorted(c["meta"]["act_uid"] for c in ov) == ["V-1799", "XIX-1810"]
    assert all(c["meta"]["section_id"] == 0 and "SECTIONS_TOTAL:" in c["text"] for c in ov)


@pytest.mark.parametrize("fmt,layout", [("auto", "array"), ("json", "array"), ("auto", "jsonl"),
                                         ("jsonl", "jsonl"), ("auto", "concat"), ("concatenated", "concat")])
def test_loader_formats(tmp_path, records, fmt, layout):
    p = tmp_path / "f.txt"
    if layout == "array":
        p.write_text(json.dumps(records), encoding="utf-8")
    elif layout == "jsonl":
        p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    else:
        p.write_text("\n".join(json.dumps(r, indent=2) for r in records), encoding="utf-8")
    st = {}
    got = list(kb_json.iter_records(p.read_text(encoding="utf-8"), stats=st, fmt=fmt))
    assert [r["id"] for r in got] == [r["id"] for r in records]
    assert st["format"] == {"array": "json", "jsonl": "jsonl", "concat": "concatenated"}[layout]


def test_loader_limit(corpus):
    assert len(list(kb_json.iter_records(corpus.read_text(encoding="utf-8"), limit=5))) == 5


def test_cleaning():
    assert kb_json.clean_legal("the 2[Bangladesh] 1[***] x") == "the Bangladesh x"
    assert kb_json.section_number("16. Penalty") == "16"
    assert kb_json.section_number("১। সংজ্ঞা") == "1"


@pytest.mark.skipif(not os.environ.get("RAG_TEST_DATA"), reason="set RAG_TEST_DATA=path/to/bdlaws.jsonl")
def test_real_corpus_invariants():
    _, chunks, st = build(os.environ["RAG_TEST_DATA"], limit=int(os.environ.get("RAG_TEST_LIMIT", "0")) or None)
    keys = [(c["meta"]["act_uid"], c["meta"]["section_id"], c["meta"]["chash"]) for c in chunks]
    assert len(keys) == len(set(keys))
    assert all(kb_json.STABLE_ID_RE.match(c["meta"]["sid"]) for c in chunks)
