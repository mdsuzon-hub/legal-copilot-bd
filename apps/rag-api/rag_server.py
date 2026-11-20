#!/usr/bin/env python3
"""
==========================================================================
  RAG Server — Flask API on top of rag.py  (default port 5001)   v4
==========================================================================
  Same folder as: rag.py  kb_json.py  kb_pdf.py  rag_logic.py

  INSTALL   pip install flask groq chromadb pdfplumber sentence-transformers
  RUN       export GROQ_API_KEY=...   (PowerShell: $env:GROQ_API_KEY="...")
            python rag_server.py

  ENV (all optional)
      RAG_PORT=5001  RAG_HOST=0.0.0.0  RAG_DB=./rag_chroma_db
      RAG_COLLECTION=kb_rag_v4  RAG_MODEL=openai/gpt-oss-120b  RAG_MODEL_NAME="GPT-OSS 120B"
      RAG_RELATED=1            follow-up suggestions
      RAG_PDF_DIR=./kb         auto-index a folder (pdf / json / jsonl) on startup
      RAG_KB=a.jsonl,b.pdf     auto-index these files on startup
      RAG_JSON_LIMIT=100       index only first N JSON records
      RAG_JSON_FORMAT=auto     auto|json|jsonl|concatenated
      RAG_CITE_REPAIR=1        repair uncited / invalid citations (1 extra LLM call when needed)
      RAG_LOG=logs/query.jsonl ('' disables)

  ENDPOINTS
      GET  /health | /stats | /docs | /acts
      GET  /source/<stable_id>        full text behind a citation, e.g. /source/V-1799-s3
      POST /ask     {"question","history":[{role,content}],"top_k":int?,"docs":[...],
                     "exclude_repealed":bool?, "expand":bool?, "rewrite":bool?}
                    top_k omitted -> routed by intent (metadata 2 / section 3 / list 6 / compare 10)
      POST /upload  multipart "file" (pdf / json / jsonl / ndjson, one or many)
      DELETE /docs/<doc_id>

  /ask response: answer, mode, sources[{id (stable), tag (S#, display alias), rank, source,
  label, act, published, repealed, score, cited, snippet}], citations{valid,invalid,uncited},
  notices, related, intent, top_k, language, search_query, expanded_query, tokens, latency.
==========================================================================
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path

try:
    from flask import Flask, jsonify, request
except ImportError:
    print("\n[ERROR] Flask missing. Run:  pip install flask\n")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import kb_json  # noqa: E402
import rag_logic as L  # noqa: E402
import rag as R  # noqa: E402

# ── Config ──────────────────────────────────────────────────────────────────

PORT = int(os.environ.get("RAG_PORT", "5001"))
HOST = os.environ.get("RAG_HOST", "0.0.0.0")
UPLOAD_DIR = Path(os.environ.get("RAG_UPLOAD_DIR", "./uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
WANT_RELATED = os.environ.get("RAG_RELATED", "1") != "0"
ALLOWED_UPLOAD = {".pdf"} | kb_json.KB_EXTS

API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
if not API_KEY:
    print("\n[ERROR] GROQ_API_KEY is not set in this terminal.")
    print("  Windows cmd:  set GROQ_API_KEY=your_key")
    print("  PowerShell :  $env:GROQ_API_KEY=\"your_key\"\n")
    sys.exit(1)

cfg = R.default_cfg(
    db_dir=os.environ.get("RAG_DB", R.DEFAULTS["db_dir"]),
    collection=os.environ.get("RAG_COLLECTION", R.DEFAULTS["collection"]),
    chat_model=os.environ.get("RAG_MODEL", R.DEFAULTS["chat_model"]),
    json_limit=int(os.environ["RAG_JSON_LIMIT"]) if os.environ.get("RAG_JSON_LIMIT") else None,
    json_format=os.environ.get("RAG_JSON_FORMAT", "auto"),
    cite_repair=os.environ.get("RAG_CITE_REPAIR", "1") != "0",
    log_path=os.environ.get("RAG_LOG", "logs/query.jsonl"),
)
MODEL_NAME = os.environ.get("RAG_MODEL_NAME") or {
    "openai/gpt-oss-120b": "GPT-OSS 120B"}.get(cfg.chat_model, cfg.chat_model)

# ── Boot (load models once) ────────────────────────────────────────────────

print("\n  Starting RAG server …")
store = R.Store(cfg)
llm = R.LLM(API_KEY, cfg.chat_model)
LOCK = threading.RLock()          # guards Chroma + BM25 cache; LLM calls run outside it
pipeline = R.RagPipeline(store, llm, cfg, lock=LOCK)

try:
    store._get_reranker()         # warm up so the first request is fast
except Exception as e:  # noqa: BLE001
    print(f"  [warn] reranker warm-up failed: {e}")

boot_items = [os.environ["RAG_PDF_DIR"]] if os.environ.get("RAG_PDF_DIR") else []
boot_items += [x for x in os.environ.get("RAG_KB", "").split(",") if x.strip()]
if boot_items:
    with LOCK:
        store.add_many(R.resolve_kb_paths(boot_items))

# ── App ─────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
try:
    app.json.ensure_ascii = False                       # keep Bangla readable
except Exception:  # noqa: BLE001
    app.config["JSON_AS_ASCII"] = False


@app.before_request
def _preflight():
    if request.method == "OPTIONS":
        return ("", 204)


@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return resp


def _err(msg: str, code: int = 500, **extra):
    return jsonify({"answer": "", "mode": "error", "error": msg, "sources": [], "notices": [msg],
                    "related": [], "model_name": MODEL_NAME, **extra}), code


def _clean_history(raw) -> list:
    out = []
    if isinstance(raw, list):
        for t in raw[-cfg.history_turns:]:
            if isinstance(t, dict) and t.get("role") in ("user", "assistant") and t.get("content"):
                out.append({"role": t["role"], "content": str(t["content"])[:2000]})
    return out


def _resolve_scope(raw) -> list:
    """Accept doc_ids or filename fragments."""
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    docs = store.docs()
    ids = set()
    for item in raw:
        item = str(item).strip().lower()
        for d in docs:
            if item == d["doc_id"].lower() or item in d["name"].lower():
                ids.add(d["doc_id"])
    return list(ids)


def _flag(data: dict, key: str, default: bool) -> bool:
    v = data.get(key)
    return default if v is None else bool(v)


def _related(question: str, answer: str) -> list:
    if not WANT_RELATED or not answer:
        return []
    try:
        r = llm._create(
            model=llm.model, temperature=0.4, max_tokens=500,
            messages=[
                {"role": "system", "content":
                    "Suggest exactly 3 short follow-up questions a student could ask next, "
                    "answerable from the same documents. Same language as the question. "
                    "Output one question per line, no numbering, no extra text."},
                {"role": "user", "content": f"Question: {question}\n\nAnswer: {answer[:1200]}"},
            ], **llm._extra("low"))
        lines = [re.sub(r"^[\s\-\*\d\.\)]+", "", ln).strip()
                 for ln in (r.choices[0].message.content or "").splitlines()]
        return [ln for ln in lines if 5 < len(ln) < 160][:3]
    except Exception:  # noqa: BLE001
        return []


# ── Routes ──────────────────────────────────────────────────────────────────

@app.get("/")
@app.get("/health")
def health():
    with LOCK:
        return jsonify({"status": "ok", "docs": len(store.docs()), "acts": len(store.acts()),
                        "chunks": store.count(), "model": MODEL_NAME, "port": PORT})


@app.get("/stats")
def stats():
    with LOCK:
        return jsonify({"docs": len(store.docs()), "acts": len(store.acts()), "chunks": store.count(),
                        "db_size": R.human_size(store.disk_usage()), "embed_model": cfg.embed_model,
                        "rerank_model": cfg.rerank_model, "llm": cfg.chat_model,
                        "collection": cfg.collection, "intent_top_k": L.INTENT_TOPK,
                        "cite_repair": cfg.cite_repair, "query_log": cfg.log_path or None})


@app.get("/docs")
def list_docs():
    with LOCK:
        return jsonify({"docs": store.docs()})


@app.get("/acts")
def list_acts():
    with LOCK:
        return jsonify({"acts": [dict(uid=a["uid"], title=a["title"], act_no=a["act_no"], year=a["year"],
                                      published=a["published"], declared_sections=a["nsec"],
                                      sections=L.compact_sections(a["sections"]))
                                 for a in sorted(store.acts().values(), key=lambda x: x["uid"])]})


@app.get("/source/<sid>")
def source(sid: str):
    with LOCK:
        chunks = store.get_chunks(sid)
    if not chunks:
        return _err(f"Unknown source id: {sid}", 404)
    return jsonify({"id": sid, "chunks": chunks})


@app.delete("/docs/<doc_id>")
def delete_doc(doc_id: str):
    with LOCK:
        if not any(d["doc_id"] == doc_id for d in store.docs()):
            return _err("Document not found", 404)
        store.remove([doc_id])
        return jsonify({"removed": doc_id, "docs": store.docs()})


@app.post("/upload")
def upload():
    files = request.files.getlist("file") + request.files.getlist("files")
    if not files:
        return _err("No file uploaded (use multipart field 'file')", 400)
    saved = []
    for f in files:
        name = re.sub(r"[^\w\-. ]", "_", Path(f.filename or "upload.pdf").name, flags=re.UNICODE)
        if Path(name).suffix.lower() not in ALLOWED_UPLOAD:
            continue
        dest = UPLOAD_DIR / name
        f.save(dest)
        saved.append(dest.resolve())
    if not saved:
        return _err("Only PDF, JSON, JSONL or NDJSON files are accepted", 400)
    with LOCK:
        store.add_many(saved)
        return jsonify({"uploaded": [p.name for p in saved], "docs": store.docs(), "chunks": store.count()})


@app.post("/ask")
def ask():
    try:
        data = request.get_json(silent=True) or {}
        question = (data.get("question") or data.get("query") or data.get("message") or "").strip()
        if not question:
            return _err("Empty question", 400)
        history = _clean_history(data.get("history"))
        top_k = int(data["top_k"]) if data.get("top_k") else None      # None -> intent routing
        with LOCK:
            n_chunks = store.count()
            scope = _resolve_scope(data.get("docs") or data.get("scope"))
        if n_chunks == 0:
            return jsonify({"answer": "No documents are indexed yet. Upload a PDF or JSON file first.",
                            "mode": "rag", "sources": [], "notices": ["Index is empty"], "related": [],
                            "model_name": MODEL_NAME})

        res = pipeline.run(
            question, history, scope or None, top_k,
            rewrite=_flag(data, "rewrite", True), expand=_flag(data, "expand", True),
            rerank=True, exclude_repealed=_flag(data, "exclude_repealed", False))

        val = res["citations"]
        cited = set(val["valid"])
        sources = []
        for i, c in enumerate(res["chunks"], 1):
            is_cited = any(c["sid"] == v or c["sid"].startswith(v + "-") for v in cited)
            sources.append({
                "id": c["sid"], "tag": f"S{i}", "rank": i, "source": c["source"], "label": c["label"],
                "act": c["act_uid"] or None, "published": c["published"] or None, "repealed": c["repealed"],
                "page": c["page"], "score": round(c["score"], 4),
                "cited": is_cited if cited else True, "snippet": c["text"][:300]})

        notices = list(res["notes"])
        if not res["chunks"]:
            notices.append("No relevant chunks found")
        if val["invalid"]:
            notices.append("Answer cited ids that were not retrieved: " + ", ".join(val["invalid"]))
        if val["uncited"]:
            notices.append(f"{len(val['uncited'])} sentence(s) without a citation")
        if not res["answer"]:
            notices.append("Model returned an empty answer")

        u = dict(res["usage"])
        u["total"] = u.get("prompt", 0) + u.get("completion", 0)
        return jsonify({
            "answer": res["answer"], "mode": "rag", "sources": sources, "notices": notices,
            "citations": {"valid": val["valid"], "invalid": val["invalid"], "uncited": len(val["uncited"]),
                          "repaired": res["repaired"]},
            "related": _related(question, res["answer"]), "model_name": MODEL_NAME, "qid": res["qid"],
            "language": res["language"], "intent": res["intent"], "top_k": res["top_k"],
            "acts_named": res["acts_named"], "search_query": res["search_query"],
            "expanded_query": res["expanded"], "tokens": u,
            "retrieval_ms": int(res["t_retrieve"] * 1000), "total_ms": int(res["t_total"] * 1000),
        })
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return _err(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    print(f"\n  ✅ RAG server ready → http://127.0.0.1:{PORT}  "
          f"({len(store.docs())} docs, {len(store.acts())} acts, {store.count()} chunks)\n")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
