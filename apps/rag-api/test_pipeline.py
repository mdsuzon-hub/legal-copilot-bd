"""Pipeline logic with a fake store + fake LLM (no models, no network)."""
import json
import sys
import types
from collections import Counter

import pytest

for mod, attrs in {"groq": dict(Groq=object, RateLimitError=Exception, APIConnectionError=Exception,
                                InternalServerError=Exception),
                   "chromadb": {}, "pdfplumber": {}, "sentence_transformers": dict(SentenceTransformer=object)}.items():
    try:
        __import__(mod)
    except ImportError:
        m = types.ModuleType(mod)
        m.__dict__.update(attrs)
        sys.modules[mod] = m

import rag as R  # noqa: E402


def chunk(sid, act, sec, text="text", score=1.0):
    return dict(id="d:" + sid, sid=sid, text=f"SOURCE_ID: {sid}\n---\n{text}", source=act, doc_id="d",
                act_uid=sid.split("-s")[0], section_id=sec, sno=str(sec + 1), chash=sid, kind="section",
                published="", repealed=False, page=0, label=sid, dense=0, bm25=0, fused=score, score=score)


class FakeStore:
    def __init__(self):
        self.calls = []
        self._acts = {"V-1799": dict(uid="V-1799", title="Wills Regulation 1799", sections={"2", "3", "4", "5", "6", "7", "8"}, nsec=7),
                      "XIX-1810": dict(uid="XIX-1810", title="Charitable Regulation 1810", sections={str(i) for i in range(2, 18)}, nsec=16)}

    def acts(self):
        return self._acts

    def search(self, queries, k, scope, rerank, excl=False, kinds=None, act_uids=None, trace=None):
        self.calls.append(dict(k=k, kinds=kinds, act_uids=act_uids))
        if act_uids == ["XIX-1810"]:
            out = [chunk("XIX-1810-s1", "XIX", 1, score=0.2)]
        else:
            out = [chunk("V-1799-s4", "V", 4, score=0.9), chunk("V-1799-s3", "V", 3, score=0.5)]
        if trace is not None:
            trace["pre"] = [(c["sid"], c["fused"]) for c in out]
        return out[:k]


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def rewrite(self, q, h, usage=None): return q
    def expand(self, q, usage=None): return ""

    def answer(self, messages, usage=None):
        self.prompts.append(messages[-1]["content"])
        usage["prompt"] += 100
        usage["completion"] += 20
        return self.reply


def make(reply, tmp_path):
    cfg = R.default_cfg(log_path=str(tmp_path / "q.jsonl"))
    store, llm = FakeStore(), FakeLLM(reply)
    return R.RagPipeline(store, llm, cfg), store, llm


def test_trap_query_gets_mismatch_and_section_notes(tmp_path):
    p, store, llm = make("Regulation XIX has no such section; see V-1799-s4 [S1].", tmp_path)
    res = p.run("What does Section 20 of Regulation XIX of 1810 say about administrators?")
    kinds = " ".join(res["notes"])
    assert res["acts_named"] == ["XIX-1810"]
    assert "ACT_MISMATCH" in kinds and "NO section 20" in kinds
    assert {c["sid"] for c in res["chunks"]} >= {"V-1799-s4", "XIX-1810-s1"}   # coverage of the named act
    assert "[V-1799-s4]" in res["answer"] and "[S1]" not in res["answer"]       # alias -> stable id
    assert "RETRIEVAL NOTES" in llm.prompts[0] and "ALLOWED_IDS" in llm.prompts[0]


def test_intent_routes_top_k_and_kinds(tmp_path):
    p, store, _ = make("ok [V-1799-s4].", tmp_path)
    r = p.run("When was Regulation V of 1799 published?")
    assert r["intent"] == "metadata" and r["top_k"] == 2
    assert store.calls[0]["kinds"] == ["act"] and store.calls[0]["act_uids"] == ["V-1799"]
    assert p.run("Compare the executor and the administrator")["top_k"] == 10
    assert p.run("Who appoints an administrator?", top_k=4)["top_k"] == 4


def test_query_log_written(tmp_path):
    p, *_ = make("The Judge appoints an administrator [V-1799-s4].", tmp_path)
    p.run("Who appoints an administrator?")
    rec = json.loads((tmp_path / "q.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    for key in ("language", "rewrite", "intent", "top_k", "retrieved_pre_rerank", "retrieved_post_rerank",
                "final_citations", "tokens", "latency_ms"):
        assert key in rec
    assert rec["final_citations"] == ["V-1799-s4"] and rec["tokens"]["total"] == 120


def test_bengali_question_sets_language(tmp_path):
    p, _, llm = make("উত্তর [V-1799-s4]।", tmp_path)
    res = p.run("প্রশাসক কে নিয়োগ করেন?")
    assert res["language"] == "bn" and "Bengali" in llm.prompts[0]


def test_context_has_structured_header():
    ctx = R.build_context([chunk("V-1799-s4", "V", 4)])
    assert ctx.startswith("<<S1>>\nSOURCE_ID: V-1799-s4")
