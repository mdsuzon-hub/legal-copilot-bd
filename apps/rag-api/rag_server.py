#!/usr/bin/env python3
"""
==========================================================================
  RAG Server — Flask API on top of rag_pdf.py  (default port 5001)
==========================================================================
  Put this file in the SAME folder as rag_pdf.py.

  INSTALL
      pip install flask groq chromadb pdfplumber sentence-transformers

  RUN
      set GROQ_API_KEY=your_key        (Windows cmd)
      $env:GROQ_API_KEY="your_key"     (PowerShell)
      export GROQ_API_KEY=your_key     (Linux/macOS)
      python rag_server.py

  OPTIONAL ENV
      RAG_PORT=5001  RAG_HOST=0.0.0.0  RAG_DB=./rag_chroma_db
      RAG_COLLECTION=pdf_rag_v2  RAG_MODEL=openai/gpt-oss-120b
      RAG_MODEL_NAME="GPT-OSS 120B"   RAG_RELATED=1 (follow-up suggestions)
      RAG_PDF_DIR=./pdfs  (auto-index this folder on startup)

  ENDPOINTS
      GET    /health            -> {"status":"ok", docs, chunks}
      POST   /ask               -> {"question": "...", "history":[{role,content}],
                                    "top_k": 5, "docs": ["name-part" | doc_id]}
      GET    /docs              -> indexed documents
      POST   /upload            -> multipart field "file" (one or many PDFs)
      DELETE /docs/<doc_id>     -> remove a document
      GET    /stats
==========================================================================
"""

from __future__ import annotations

import argparse
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

try:  # optional .env support
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import rag_pdf as R  # noqa: E402  (must live next to this file)

# ── Config ──────────────────────────────────────────────────────────────────

PORT = int(os.environ.get("RAG_PORT", "5001"))
HOST = os.environ.get("RAG_HOST", "0.0.0.0")      # IPv4 on all interfaces
UPLOAD_DIR = Path(os.environ.get("RAG_UPLOAD_DIR", "./uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
WANT_RELATED = os.environ.get("RAG_RELATED", "1") != "0"

API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
if not API_KEY:
    print("\n[ERROR] GROQ_API_KEY is not set in this terminal.")
    print("  Windows cmd:  set GROQ_API_KEY=your_key")
    print("  PowerShell :  $env:GROQ_API_KEY=\"your_key\"\n")
    sys.exit(1)

cfg = argparse.Namespace(**R.DEFAULTS)
cfg.db_dir = os.environ.get("RAG_DB", cfg.db_dir)
cfg.collection = os.environ.get("RAG_COLLECTION", cfg.collection)
cfg.chat_model = os.environ.get("RAG_MODEL", cfg.chat_model)
cfg.no_rerank = False
cfg.no_rewrite = False
cfg.debug = False

MODEL_NAME = os.environ.get("RAG_MODEL_NAME") or {
    "openai/gpt-oss-120b": "GPT-OSS 120B"
}.get(cfg.chat_model, cfg.chat_model)

# ── Boot (load models once) ────────────────────────────────────────────────

print("\n  Starting RAG server …")
store = R.Store(cfg)
llm = R.LLM(API_KEY, cfg.chat_model)
LOCK = threading.RLock()   # chroma + BM25 cache are not thread-safe for writes

try:                        # warm the reranker so the first request isn't slow
    store._get_reranker()
except Exception as e:      # noqa: BLE001
    print(f"  [warn] reranker warm-up failed: {e}")

pdf_dir = os.environ.get("RAG_PDF_DIR")
if pdf_dir:
    with LOCK:
        store.add_many(R.resolve_pdf_paths([pdf_dir]))

# ── App ─────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024   # 200 MB uploads
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
    return jsonify({"answer": "", "mode": "error", "error": msg, "sources": [],
                    "notices": [msg], "related": [], "model_name": MODEL_NAME, **extra}), code


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


def _related(question: str, answer: str) -> list:
    if not WANT_RELATED:
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
        return jsonify({"status": "ok", "docs": len(store.docs()), "chunks": store.count(),
                        "model": MODEL_NAME, "port": PORT})


@app.get("/stats")
def stats():
    with LOCK:
        return jsonify({"docs": len(store.docs()), "chunks": store.count(),
                        "db_size": R.human_size(store.disk_usage()),
                        "embed_model": cfg.embed_model, "rerank_model": cfg.rerank_model,
                        "llm": cfg.chat_model, "collection": cfg.collection})


@app.get("/docs")
def list_docs():
    with LOCK:
        return jsonify({"docs": store.docs()})


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
        if not name.lower().endswith(".pdf"):
            continue
        dest = UPLOAD_DIR / name
        f.save(dest)
        saved.append(dest.resolve())
    if not saved:
        return _err("Only PDF files are accepted", 400)
    with LOCK:
        store.add_many(saved)
        return jsonify({"uploaded": [p.name for p in saved], "docs": store.docs(),
                        "chunks": store.count()})


@app.post("/ask")
def ask():
    try:
        data = request.get_json(silent=True) or {}
        question = (data.get("question") or data.get("query") or data.get("message") or "").strip()
        if not question:
            return _err("Empty question", 400)

        history = _clean_history(data.get("history"))
        top_k = max(1, min(15, int(data.get("top_k") or cfg.top_k)))
        notices = []

        with LOCK:
            n_chunks = store.count()
            scope = _resolve_scope(data.get("docs") or data.get("scope"))
        if n_chunks == 0:
            return jsonify({"answer": "No documents are indexed yet. Upload a PDF first.",
                            "mode": "rag", "sources": [],
                            "notices": ["Index is empty"], "related": [],
                            "model_name": MODEL_NAME})

        search_q = llm.rewrite(question, history) if history else question
        t0 = time.time()
        with LOCK:
            chunks = store.search(search_q, top_k, scope or None, True)
        t_ret = time.time() - t0

        if not chunks:
            return jsonify({"answer": "I don't have enough information in the documents to answer that.",
                            "mode": "rag", "sources": [],
                            "notices": ["No relevant chunks found"], "related": [],
                            "model_name": MODEL_NAME})

        messages = [{"role": "system", "content": R.SYSTEM_PROMPT}] + history
        messages.append({"role": "user",
                         "content": f"CONTEXT:\n{R.build_context(chunks)}\n\nQUESTION:\n{question}"})
        resp = llm._create(model=llm.model, messages=messages, temperature=0.1,
                           max_tokens=2048, **llm._extra("medium"))
        answer = (resp.choices[0].message.content or "").strip()
        if not answer:
            notices.append("Model returned an empty answer")

        cited = {int(n) for n in re.findall(r"\[S(\d+)\]", answer)}
        sources = [{
            "id": i, "tag": f"S{i}", "source": c["source"], "page": c.get("page"),
            "score": round(c["score"], 4), "cited": (i in cited) if cited else True,
            "snippet": c["text"][:300],
        } for i, c in enumerate(chunks, 1)]

        return jsonify({
            "answer": answer, "mode": "rag", "sources": sources, "notices": notices,
            "related": _related(question, answer), "model_name": MODEL_NAME,
            "search_query": search_q, "retrieval_ms": int(t_ret * 1000),
        })
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return _err(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    print(f"\n  ✅ RAG server ready → http://127.0.0.1:{PORT}  "
          f"({len(store.docs())} docs, {store.count()} chunks)\n")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
