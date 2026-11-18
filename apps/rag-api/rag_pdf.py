#!/usr/bin/env python3
"""
==========================================================================
  PDF RAG v2 — Multi-PDF Terminal Chat
  Hybrid search (Vector + BM25) · RRF fusion · Cross-encoder rerank
  Query rewriting · Streaming answers · Incremental ChromaDB management
==========================================================================
  INSTALL
      pip install groq chromadb pdfplumber sentence-transformers
      (optional fallback extractor)  pip install pypdf

  USAGE
      python rag_pdf.py                         # interactive
      python rag_pdf.py a.pdf b.pdf             # index several PDFs
      python rag_pdf.py --dir ./papers          # index a whole folder
      python rag_pdf.py --dir ./papers x.pdf    # mix folders and files
      python rag_pdf.py --reset a.pdf           # wipe DB then index
      python rag_pdf.py --ask "what is X?"      # one-shot answer, no chat

  IN-CHAT COMMANDS  (type /help)
      /docs  /add  /remove  /use  /all  /stats  /reset
      /top N  /rerank  /rewrite  /debug  /clear  /save  /quit
==========================================================================
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import math
import os
import re
import shutil
import sys
import textwrap
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Dependency check ────────────────────────────────────────────────────────

def _check_deps() -> None:
    missing = []
    for pkg, imp in [("groq", "groq"), ("chromadb", "chromadb"),
                     ("pdfplumber", "pdfplumber"),
                     ("sentence-transformers", "sentence_transformers")]:
        try:
            __import__(imp)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"\n[ERROR] Missing packages: {', '.join(missing)}")
        print(f"  Run:  pip install {' '.join(missing)}\n")
        sys.exit(1)


_check_deps()

import chromadb  # noqa: E402
import groq  # noqa: E402
import pdfplumber  # noqa: E402
from sentence_transformers import SentenceTransformer  # noqa: E402

try:
    import readline  # noqa: F401  (arrow-key history on Linux/macOS)
except ImportError:
    pass

# ── Config ──────────────────────────────────────────────────────────────────

DEFAULTS = dict(
    embed_model="all-MiniLM-L6-v2",            # alt: BAAI/bge-small-en-v1.5
    rerank_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
    chat_model="openai/gpt-oss-120b",
    chunk_size=700,        # chars per chunk (sentence-aware)
    overlap=140,           # chars carried into next chunk
    top_k=5,               # final chunks sent to the LLM
    candidates=30,         # candidates per retriever before fusion
    db_dir="./rag_chroma_db",
    collection="pdf_rag_v2",
    history_turns=6,
)
RRF_K = 60
BATCH_EMBED = 32
BATCH_INSERT = 200

# ── Colors / UI helpers ────────────────────────────────────────────────────

if os.name == "nt":
    os.system("")  # enable ANSI escapes on Windows 10+

_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s


bold = lambda s: _c("1", s)
dim = lambda s: _c("2", s)
red = lambda s: _c("31", s)
green = lambda s: _c("32", s)
yellow = lambda s: _c("33", s)
cyan = lambda s: _c("36", s)
magenta = lambda s: _c("35", s)


def term_width() -> int:
    return max(60, min(shutil.get_terminal_size((100, 20)).columns, 110))


def info(msg: str) -> None:
    print(f"  {cyan('•')} {msg}")


def ok(msg: str) -> None:
    print(f"  {green('✓')} {msg}")


def warn(msg: str) -> None:
    print(f"  {yellow('!')} {msg}")


def err(msg: str) -> None:
    print(f"  {red('✗')} {msg}")


def progress(done: int, total: int, label: str = "") -> None:
    total = max(total, 1)
    n = int(done / total * 28)
    bar = "█" * n + "░" * (28 - n)
    print(f"\r  {label:<22} {cyan(bar)} {done}/{total}", end="", flush=True)
    if done >= total:
        print()


def confirm(prompt: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        a = input(f"  {yellow('?')} {prompt} {suffix}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not a:
        return default
    return a in ("y", "yes")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


BANNER = r"""
  ___  ___  ___   ___    _    ___     ___ ___
 | _ \|   \| __| | _ \  /_\  / __|   |_  )_  )
 |  _/| |) | _|  |   / / _ \| (_ |    / / / /
 |_|  |___/|_|   |_|_\/_/ \_\\___|   /___/___|

   Multi-PDF · Hybrid Search · Rerank · Groq
"""

# ── Prompts ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a precise document question-answering assistant.
Answer the user's question using ONLY the numbered CONTEXT sources provided.

Rules:
1. Use only the context. Never use outside knowledge or guess.
2. If the context does not contain the answer, reply exactly:
   "I don't have enough information in the documents to answer that."
3. Be concise and factual. Use short bullet points for lists or comparisons.
4. Cite every claim inline with its source tag, e.g. [S1] or [S2][S4].
5. If sources from different documents disagree, say so and cite both.
6. Answer in the same language as the question.
"""

REWRITE_PROMPT = """\
Rewrite the user's latest message as a standalone search query, using the chat \
history to resolve pronouns and references. Keep it short and keep the original \
language. Output ONLY the rewritten query, nothing else. If the message is \
already standalone, output it unchanged."""

# ── Text cleaning & chunking ───────────────────────────────────────────────

_SENT_SPLIT = re.compile(r"(?<=[.!?।])\s+|\n+")


def clean_text(text: str) -> str:
    text = text.replace("\r", "")
    text = re.sub(r"-\n(?=[a-z])", "", text)            # de-hyphenate line breaks
    # join soft-wrapped lines (line not ending in sentence punctuation)
    text = re.sub(r"(?<![.!?:;।\n])\n(?!\s*(?:[-•*●▪]|\d+[.)]))", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, size: int, overlap: int) -> List[str]:
    """Sentence-aware packing with sentence-level overlap."""
    units: List[str] = []
    for u in _SENT_SPLIT.split(text):
        u = u.strip()
        if not u:
            continue
        while len(u) > size:                      # hard-split monster sentences
            units.append(u[:size])
            u = u[size - overlap:]
        units.append(u)

    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for u in units:
        if cur and cur_len + len(u) + 1 > size:
            chunks.append(" ".join(cur))
            keep: List[str] = []
            keep_len = 0
            for prev in reversed(cur):            # carry tail as overlap
                if keep_len + len(prev) > overlap:
                    break
                keep.insert(0, prev)
                keep_len += len(prev) + 1
            cur, cur_len = keep, keep_len
        cur.append(u)
        cur_len += len(u) + 1
    if cur:
        chunks.append(" ".join(cur))
    return [c for c in chunks if len(c) > 40]


# ── PDF extraction ──────────────────────────────────────────────────────────

def file_hash(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:16]


def extract_pages(path: Path) -> List[Tuple[int, str]]:
    """Return [(page_no, text)]. Falls back to pypdf if pdfplumber fails."""
    pages: List[Tuple[int, str]] = []
    try:
        with pdfplumber.open(str(path)) as pdf:
            total = len(pdf.pages)
            for i, page in enumerate(pdf.pages, 1):
                t = clean_text(page.extract_text() or "")
                if t:
                    pages.append((i, t))
                progress(i, total, "  extracting")
        return pages
    except Exception as e:  # noqa: BLE001
        warn(f"pdfplumber failed ({e}); trying pypdf…")
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        total = len(reader.pages)
        for i, page in enumerate(reader.pages, 1):
            t = clean_text(page.extract_text() or "")
            if t:
                pages.append((i, t))
            progress(i, total, "  extracting")
    except Exception as e:  # noqa: BLE001
        err(f"Could not read {path.name}: {e}")
    return pages


def resolve_pdf_paths(raw_items: List[str]) -> List[Path]:
    """Expand files, folders (recursive) and glob patterns into unique PDFs."""
    found: List[Path] = []
    for item in raw_items:
        item = os.path.expanduser(item.strip().strip('"').strip("'"))
        if not item:
            continue
        p = Path(item)
        if p.is_dir():
            found += sorted(p.rglob("*.pdf")) + sorted(p.rglob("*.PDF"))
        elif any(ch in item for ch in "*?["):
            found += [Path(x) for x in sorted(glob.glob(item, recursive=True))
                      if x.lower().endswith(".pdf")]
        elif p.is_file():
            found.append(p)
        else:
            warn(f"Not found: {item}")
    seen, out = set(), []
    for p in found:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def parse_path_input(s: str) -> List[str]:
    """Parse a typed line of paths: quoted, or separated by , or ;"""
    if '"' in s or "'" in s:
        return [a or b for a, b in re.findall(r'"([^"]+)"|\'([^\']+)\'', s)] or [s]
    return [x for x in re.split(r"[;,]", s) if x.strip()]


# ── BM25 (dependency-free) ─────────────────────────────────────────────────

_TOKEN = re.compile(r"\w+", re.UNICODE)
_STOP = set("""a an and are as at be but by for from has have he her his i if in into is it its
of on or our she that the their them they this to was we were what when where which who will with
you your do does did not no so than then there these those can could should would about over
under also how why""".split())


def tokenize(text: str) -> List[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1]


class BM25:
    def __init__(self, docs: List[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.postings: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
        self.doc_len: List[int] = []
        for idx, d in enumerate(docs):
            toks = tokenize(d)
            self.doc_len.append(len(toks))
            for term, tf in Counter(toks).items():
                self.postings[term].append((idx, tf))
        self.n = len(docs)
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 1.0

    def search(self, query: str, allowed: Optional[set], limit: int) -> List[Tuple[int, float]]:
        scores: Dict[int, float] = defaultdict(float)
        for term in set(tokenize(query)):
            plist = self.postings.get(term)
            if not plist:
                continue
            df = len(plist)
            idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
            for idx, tf in plist:
                if allowed is not None and idx not in allowed:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * self.doc_len[idx] / self.avgdl)
                scores[idx] += idf * tf * (self.k1 + 1) / denom
        return sorted(scores.items(), key=lambda x: -x[1])[:limit]


# ── Vector store / index manager ───────────────────────────────────────────

class Store:
    """ChromaDB wrapper: incremental multi-document indexing + hybrid search."""

    def __init__(self, cfg: argparse.Namespace):
        self.cfg = cfg
        print()
        info(f"Loading embedding model: {bold(cfg.embed_model)} (first run downloads it)")
        self.embedder = SentenceTransformer(cfg.embed_model)
        self.client = chromadb.PersistentClient(path=cfg.db_dir)
        self.col = self._open_collection()
        self.reranker = None
        self.rerank_failed = False
        self._cache: Optional[dict] = None   # ids/texts/metas/bm25/doc→positions

    # ---- collection lifecycle ----
    def _open_collection(self):
        col = self.client.get_or_create_collection(
            name=self.cfg.collection,
            metadata={"hnsw:space": "cosine", "embed_model": self.cfg.embed_model},
        )
        stored = (col.metadata or {}).get("embed_model")
        if stored and stored != self.cfg.embed_model and col.count() > 0:
            err(f"DB was built with '{stored}', but you selected '{self.cfg.embed_model}'.")
            err("Mixing models corrupts search. Use --reset or the same --embed-model.")
            sys.exit(1)
        return col

    def wipe(self) -> None:
        try:
            self.client.delete_collection(self.cfg.collection)
        except Exception:  # noqa: BLE001
            pass
        self.col = self._open_collection()
        self._cache = None

    # ---- cache (BM25 + doc table) ----
    def _load(self) -> dict:
        if self._cache is not None:
            return self._cache
        data = self.col.get(include=["documents", "metadatas"])
        ids, texts, metas = data["ids"], data["documents"], data["metadatas"]
        positions: Dict[str, List[int]] = defaultdict(list)
        for i, m in enumerate(metas):
            positions[m["doc_id"]].append(i)
        self._cache = dict(ids=ids, texts=texts, metas=metas,
                           bm25=BM25(texts) if texts else None, positions=positions)
        return self._cache

    def _invalidate(self) -> None:
        self._cache = None

    def count(self) -> int:
        return self.col.count()

    def docs(self) -> List[dict]:
        c = self._load()
        out = []
        for doc_id, pos in c["positions"].items():
            m = c["metas"][pos[0]]
            out.append(dict(doc_id=doc_id, name=m["source"], path=m.get("path", ""),
                            pages=m.get("total_pages", 0), chunks=len(pos),
                            indexed_at=m.get("indexed_at", "")))
        out.sort(key=lambda d: d["name"].lower())
        return out

    # ---- indexing ----
    def add_pdf(self, path: Path) -> str:
        """Returns 'added' | 'skipped' | 'updated' | 'failed'."""
        doc_id = file_hash(path)
        existing = self.docs()
        if any(d["doc_id"] == doc_id for d in existing):
            return "skipped"
        replaced = [d for d in existing if d["path"] == str(path)]

        print(f"\n  {bold(path.name)}  {dim(human_size(path.stat().st_size))}")
        pages = extract_pages(path)
        if not pages:
            warn("No extractable text (scanned PDF?). Tip: run `ocrmypdf in.pdf out.pdf` first.")
            return "failed"

        total_pages = max(p for p, _ in pages)
        ids, texts, metas = [], [], []
        now = datetime.now().isoformat(timespec="seconds")
        for page_no, text in pages:
            for j, ch in enumerate(chunk_text(text, self.cfg.chunk_size, self.cfg.overlap)):
                ids.append(f"{doc_id}_{page_no}_{j}")
                texts.append(ch)
                metas.append(dict(doc_id=doc_id, source=path.name, path=str(path),
                                  page=page_no, total_pages=total_pages, indexed_at=now))
        if not texts:
            warn("Text too short to chunk.")
            return "failed"

        embs: List[List[float]] = []
        for i in range(0, len(texts), BATCH_EMBED):
            vecs = self.embedder.encode(texts[i:i + BATCH_EMBED], normalize_embeddings=True,
                                        show_progress_bar=False)
            embs += [v.tolist() for v in vecs]
            progress(min(i + BATCH_EMBED, len(texts)), len(texts), "  embedding")

        for d in replaced:                              # same path, new content
            self.col.delete(where={"doc_id": d["doc_id"]})
        for i in range(0, len(texts), BATCH_INSERT):
            self.col.upsert(ids=ids[i:i + BATCH_INSERT], embeddings=embs[i:i + BATCH_INSERT],
                            documents=texts[i:i + BATCH_INSERT], metadatas=metas[i:i + BATCH_INSERT])
        self._invalidate()
        ok(f"{len(texts)} chunks from {len(pages)} pages")
        return "updated" if replaced else "added"

    def add_many(self, paths: List[Path]) -> None:
        stats = Counter()
        t0 = time.time()
        for p in paths:
            try:
                stats[self.add_pdf(p)] += 1
            except Exception as e:  # noqa: BLE001
                err(f"{p.name}: {e}")
                stats["failed"] += 1
        print()
        ok(f"Done in {time.time() - t0:.1f}s — added {stats['added']}, updated {stats['updated']}, "
           f"skipped (already indexed) {stats['skipped']}, failed {stats['failed']}")

    def remove(self, doc_ids: List[str]) -> int:
        for d in doc_ids:
            self.col.delete(where={"doc_id": d})
        self._invalidate()
        return len(doc_ids)

    # ---- search ----
    def _get_reranker(self):
        if self.reranker is None and not self.rerank_failed:
            try:
                from sentence_transformers import CrossEncoder
                info(f"Loading reranker: {self.cfg.rerank_model} (first run downloads it)")
                self.reranker = CrossEncoder(self.cfg.rerank_model)
            except Exception as e:  # noqa: BLE001
                self.rerank_failed = True
                warn(f"Reranker unavailable ({e}); continuing without it.")
        return self.reranker

    def search(self, query: str, top_k: int, scope: Optional[List[str]],
               use_rerank: bool) -> List[dict]:
        c = self._load()
        if not c["ids"]:
            return []
        allowed: Optional[set] = None
        if scope:
            allowed = set()
            for d in scope:
                allowed.update(c["positions"].get(d, []))
            if not allowed:
                return []
        n_cand = min(self.cfg.candidates, len(allowed) if allowed else len(c["ids"]))

        # 1) dense retrieval
        q = self.embedder.encode([query], normalize_embeddings=True)[0].tolist()
        kwargs = dict(query_embeddings=[q], n_results=n_cand, include=["distances"])
        if scope:
            kwargs["where"] = {"doc_id": scope[0]} if len(scope) == 1 else {"doc_id": {"$in": scope}}
        res = self.col.query(**kwargs)
        dense = {cid: 1 - dist for cid, dist in zip(res["ids"][0], res["distances"][0])}
        dense_rank = {cid: r for r, cid in enumerate(res["ids"][0], 1)}

        # 2) lexical retrieval (BM25)
        pos_by_id = {cid: i for i, cid in enumerate(c["ids"])}
        lex = c["bm25"].search(query, allowed, n_cand) if c["bm25"] else []
        lex_rank = {c["ids"][i]: r for r, (i, _) in enumerate(lex, 1)}
        lex_score = {c["ids"][i]: s for i, s in lex}

        # 3) Reciprocal Rank Fusion
        fused: Dict[str, float] = defaultdict(float)
        for cid, r in dense_rank.items():
            fused[cid] += 1.0 / (RRF_K + r)
        for cid, r in lex_rank.items():
            fused[cid] += 1.0 / (RRF_K + r)
        ranked = sorted(fused, key=lambda x: -fused[x])
        pool = ranked[: max(top_k * 4, 12)]

        cands = []
        for cid in pool:
            i = pos_by_id[cid]
            cands.append(dict(id=cid, text=c["texts"][i], source=c["metas"][i]["source"],
                              page=c["metas"][i]["page"], doc_id=c["metas"][i]["doc_id"],
                              dense=round(dense.get(cid, 0.0), 3),
                              bm25=round(lex_score.get(cid, 0.0), 2),
                              fused=fused[cid], score=fused[cid]))

        # 4) cross-encoder rerank
        if use_rerank and len(cands) > 1:
            rr = self._get_reranker()
            if rr is not None:
                scores = rr.predict([(query, x["text"]) for x in cands], show_progress_bar=False)
                for x, s in zip(cands, scores):
                    x["score"] = float(s)
                cands.sort(key=lambda x: -x["score"])
            else:
                cands.sort(key=lambda x: -x["fused"])
        else:
            cands.sort(key=lambda x: -x["fused"])

        # 5) drop near-duplicate chunks (overlap) and cap
        final: List[dict] = []
        for x in cands:
            if any(x["source"] == y["source"] and x["page"] == y["page"]
                   and _jaccard(x["text"], y["text"]) > 0.7 for y in final):
                continue
            final.append(x)
            if len(final) >= top_k:
                break
        return final

    def disk_usage(self) -> int:
        total = 0
        for root, _, files in os.walk(self.cfg.db_dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(tokenize(a)), set(tokenize(b))
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


# ── LLM layer ───────────────────────────────────────────────────────────────

class LLM:
    def __init__(self, api_key: str, model: str):
        self.client = groq.Groq(api_key=api_key)
        self.model = model

    def _extra(self, effort: str) -> dict:
        return {"extra_body": {"reasoning_effort": effort}} if "gpt-oss" in self.model else {}

    def _create(self, **kw):
        delay = 2.0
        for attempt in range(4):
            try:
                return self.client.chat.completions.create(**kw)
            except (groq.RateLimitError, groq.APIConnectionError, groq.InternalServerError) as e:
                if attempt == 3:
                    raise
                warn(f"{type(e).__name__}; retrying in {delay:.0f}s…")
                time.sleep(delay)
                delay *= 2

    def rewrite(self, question: str, history: List[dict]) -> str:
        if not history:
            return question
        convo = "\n".join(f"{t['role']}: {t['content'][:400]}" for t in history[-4:])
        try:
            r = self._create(
                model=self.model, temperature=0, max_tokens=400,
                messages=[{"role": "system", "content": REWRITE_PROMPT},
                          {"role": "user", "content": f"HISTORY:\n{convo}\n\nLATEST: {question}"}],
                **self._extra("low"))
            out = (r.choices[0].message.content or "").strip().strip('"')
            return out if 3 <= len(out) <= 300 else question
        except Exception:  # noqa: BLE001
            return question

    def answer_stream(self, messages: List[dict]) -> str:
        stream = self._create(model=self.model, messages=messages, temperature=0.1,
                              max_tokens=2048, stream=True, **self._extra("medium"))
        buf: List[str] = []
        at_line_start = True
        for ev in stream:
            if not ev.choices:
                continue
            tok = ev.choices[0].delta.content
            if not tok:
                continue
            buf.append(tok)
            out = tok.replace("\n", "\n  ")
            if at_line_start:
                out = "  " + out
                at_line_start = False
            if tok.endswith("\n"):
                at_line_start = False
            print(out, end="", flush=True)
        print()
        return "".join(buf).strip()


def build_context(chunks: List[dict]) -> str:
    return "\n\n".join(f"[S{i}] (file: {c['source']}, page {c['page']})\n{c['text']}"
                       for i, c in enumerate(chunks, 1))


# ── Chat session ────────────────────────────────────────────────────────────

class Session:
    def __init__(self, store: Store, llm: LLM, cfg: argparse.Namespace):
        self.store, self.llm, self.cfg = store, llm, cfg
        self.history: List[dict] = []
        self.log: List[Tuple[str, str, List[dict]]] = []
        self.scope: List[str] = []       # empty = all docs
        self.top_k = cfg.top_k
        self.rerank = not cfg.no_rerank
        self.rewrite = not cfg.no_rewrite
        self.debug = cfg.debug

    # ---- ask ----
    def ask(self, question: str) -> None:
        if self.store.count() == 0:
            warn("Index is empty. Use /add <pdf|folder> first.")
            return
        search_q = question
        if self.rewrite and self.history:
            search_q = self.llm.rewrite(question, self.history)
            if search_q != question:
                print(dim(f"  ↳ searching for: {search_q}"))

        t0 = time.time()
        chunks = self.store.search(search_q, self.top_k, self.scope or None, self.rerank)
        t_ret = time.time() - t0
        if not chunks:
            warn("No relevant chunks found.")
            return
        if self.debug:
            self._print_debug(chunks, t_ret)

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages += self.history[-self.cfg.history_turns:]
        messages.append({"role": "user",
                         "content": f"CONTEXT:\n{build_context(chunks)}\n\nQUESTION:\n{question}"})

        print("\n" + cyan("─" * term_width()))
        try:
            answer = self.llm.answer_stream(messages)
        except KeyboardInterrupt:
            print(yellow("\n  [interrupted]"))
            return
        except Exception as e:  # noqa: BLE001
            print()
            err(f"LLM error: {e}")
            return
        self._print_sources(answer, chunks)
        print(dim(f"  retrieval {t_ret:.2f}s · {len(chunks)} chunks · "
                  f"{'rerank on' if self.rerank and self.store.reranker else 'rerank off'}"))
        print(cyan("─" * term_width()) + "\n")

        self.history += [{"role": "user", "content": question},
                         {"role": "assistant", "content": answer}]
        self.log.append((question, answer, chunks))

    def _print_sources(self, answer: str, chunks: List[dict]) -> None:
        cited = {int(n) for n in re.findall(r"\[S(\d+)\]", answer)}
        shown = [(i, c) for i, c in enumerate(chunks, 1) if not cited or i in cited]
        print("\n  " + bold("Sources"))
        for i, c in shown:
            print(f"   {magenta(f'[S{i}]')} {c['source']} · p.{c['page']}")

    def _print_debug(self, chunks: List[dict], t: float) -> None:
        print(dim(f"\n  [debug] retrieval {t:.2f}s"))
        for i, c in enumerate(chunks, 1):
            prev = c["text"][:90].replace("\n", " ")
            print(dim(f"   {i}. {c['source']} p.{c['page']}  dense={c['dense']}  "
                      f"bm25={c['bm25']}  rank_score={c['score']:.3f}  » {prev}…"))

    # ---- command handling ----
    def handle_command(self, line: str) -> bool:
        """Returns False to quit."""
        parts = line.lstrip("/").split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        docs = self.store.docs()

        if cmd in ("quit", "exit", "q"):
            return False
        elif cmd in ("help", "h", "?"):
            self.cmd_help()
        elif cmd in ("docs", "ls", "list"):
            self.cmd_docs(docs)
        elif cmd == "add":
            self.cmd_add(arg)
        elif cmd in ("remove", "rm", "del"):
            self.cmd_remove(arg, docs)
        elif cmd == "use":
            self.cmd_use(arg, docs)
        elif cmd == "all":
            self.scope = []
            ok("Searching all documents.")
        elif cmd == "stats":
            self.cmd_stats(docs)
        elif cmd == "reset":
            if confirm("Wipe the ENTIRE index?"):
                self.store.wipe()
                self.scope = []
                ok("Index wiped.")
        elif cmd == "top":
            try:
                self.top_k = max(1, min(20, int(arg)))
                ok(f"top-k = {self.top_k}")
            except ValueError:
                warn("Usage: /top 8")
        elif cmd == "rerank":
            self.rerank = not self.rerank
            ok(f"Rerank: {'ON' if self.rerank else 'OFF'}")
        elif cmd == "rewrite":
            self.rewrite = not self.rewrite
            ok(f"Query rewriting: {'ON' if self.rewrite else 'OFF'}")
        elif cmd in ("debug", "chunks"):
            self.debug = not self.debug
            ok(f"Debug: {'ON' if self.debug else 'OFF'}")
        elif cmd == "clear":
            self.history = []
            ok("Conversation history cleared.")
        elif cmd == "save":
            self.cmd_save(arg)
        else:
            warn(f"Unknown command '/{cmd}'. Type /help")
        return True

    def cmd_help(self) -> None:
        rows = [
            ("/docs", "list indexed documents"),
            ("/add <path…>", "index PDFs / folders / globs (comma or quote-separated)"),
            ("/remove <#|name…>", "delete documents from the index"),
            ("/use <#…>", "restrict search to these documents  (/all to undo)"),
            ("/all", "search all documents"),
            ("/stats", "DB + settings overview"),
            ("/reset", "wipe the whole index"),
            ("/top N", "chunks sent to the LLM"),
            ("/rerank", "toggle cross-encoder reranking"),
            ("/rewrite", "toggle follow-up query rewriting"),
            ("/debug", "toggle retrieval debug output"),
            ("/clear", "clear chat memory"),
            ("/save [file.md]", "export this conversation"),
            ("/quit", "exit"),
        ]
        print()
        for k, v in rows:
            print(f"   {cyan(f'{k:<20}')} {v}")
        print()

    def cmd_docs(self, docs: List[dict]) -> None:
        if not docs:
            warn("No documents indexed. Use /add <pdf|folder>.")
            return
        w = term_width()
        name_w = max(20, w - 38)
        print(f"\n   {bold('#'):<4} {bold('Document'):<{name_w}} {bold('Pages'):>5} {bold('Chunks'):>7}")
        print("   " + dim("─" * (w - 6)))
        for i, d in enumerate(docs, 1):
            active = (not self.scope) or d["doc_id"] in self.scope
            mark = green("●") if active else dim("○")
            name = d["name"] if len(d["name"]) <= name_w else d["name"][: name_w - 1] + "…"
            print(f"  {mark}{i:<3} {name:<{name_w}} {d['pages']:>5} {d['chunks']:>7}")
        scope_txt = "all documents" if not self.scope else f"{len(self.scope)} selected"
        print(dim(f"\n   ● active  ○ excluded — searching {scope_txt}\n"))

    def cmd_add(self, arg: str) -> None:
        if not arg:
            try:
                arg = input("  PDF path(s) / folder (comma-separated): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
        paths = resolve_pdf_paths(parse_path_input(arg))
        if not paths:
            warn("No PDFs found.")
            return
        info(f"{len(paths)} PDF(s) found")
        self.store.add_many(paths)

    def _pick(self, arg: str, docs: List[dict]) -> List[dict]:
        picked: List[dict] = []
        for tok in re.split(r"[,\s]+", arg):
            if not tok:
                continue
            if "-" in tok and all(p.isdigit() for p in tok.split("-", 1)):
                a, b = map(int, tok.split("-", 1))
                picked += [docs[i - 1] for i in range(a, b + 1) if 1 <= i <= len(docs)]
            elif tok.isdigit():
                i = int(tok)
                if 1 <= i <= len(docs):
                    picked.append(docs[i - 1])
            else:
                picked += [d for d in docs if tok.lower() in d["name"].lower()]
        seen, out = set(), []
        for d in picked:
            if d["doc_id"] not in seen:
                seen.add(d["doc_id"])
                out.append(d)
        return out

    def cmd_remove(self, arg: str, docs: List[dict]) -> None:
        if not arg:
            self.cmd_docs(docs)
            warn("Usage: /remove 2   or   /remove 1,3   or   /remove 2-4   or   /remove name-part")
            return
        picked = self._pick(arg, docs)
        if not picked:
            warn("No matching documents.")
            return
        for d in picked:
            print(f"   - {d['name']} ({d['chunks']} chunks)")
        if confirm(f"Remove {len(picked)} document(s)?"):
            ids = [d["doc_id"] for d in picked]
            self.store.remove(ids)
            self.scope = [s for s in self.scope if s not in ids]
            ok(f"Removed {len(ids)} document(s).")

    def cmd_use(self, arg: str, docs: List[dict]) -> None:
        if not arg:
            self.cmd_docs(docs)
            warn("Usage: /use 1,3   or   /use 2-4   or   /use name-part")
            return
        picked = self._pick(arg, docs)
        if not picked:
            warn("No matching documents.")
            return
        self.scope = [d["doc_id"] for d in picked]
        ok("Scope: " + ", ".join(d["name"] for d in picked))

    def cmd_stats(self, docs: List[dict]) -> None:
        rows = [
            ("Documents", len(docs)),
            ("Chunks", self.store.count()),
            ("DB path", os.path.abspath(self.cfg.db_dir)),
            ("DB size", human_size(self.store.disk_usage())),
            ("Collection", self.cfg.collection),
            ("Embed model", self.cfg.embed_model),
            ("Reranker", f"{self.cfg.rerank_model} ({'on' if self.rerank else 'off'})"),
            ("LLM", self.cfg.chat_model),
            ("Chunk size/overlap", f"{self.cfg.chunk_size}/{self.cfg.overlap}"),
            ("Top-k", self.top_k),
            ("Query rewrite", "on" if self.rewrite else "off"),
            ("Scope", "all" if not self.scope else f"{len(self.scope)} doc(s)"),
        ]
        print()
        for k, v in rows:
            print(f"   {cyan(f'{k:<20}')} {v}")
        print()

    def cmd_save(self, arg: str) -> None:
        if not self.log:
            warn("Nothing to save yet.")
            return
        fn = arg or f"rag_chat_{datetime.now():%Y%m%d_%H%M%S}.md"
        lines = [f"# RAG chat — {datetime.now():%Y-%m-%d %H:%M}\n"]
        for q, a, ch in self.log:
            lines += [f"## Q: {q}\n", a + "\n", "**Sources:**"]
            lines += [f"- [S{i}] {c['source']} p.{c['page']}" for i, c in enumerate(ch, 1)]
            lines.append("")
        Path(fn).write_text("\n".join(lines), encoding="utf-8")
        ok(f"Saved to {fn}")

    # ---- prompt ----
    def prompt(self) -> str:
        n = len(self.scope) if self.scope else len(self.store.docs())
        tag = f"{n} doc{'s' if n != 1 else ''}" + (" ⌖" if self.scope else "")
        return f"{bold(green('You'))} {dim(f'[{tag}]')}: "


# ── Setup ───────────────────────────────────────────────────────────────────

def get_api_key() -> str:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        ok("GROQ_API_KEY found in environment.")
        return key
    info("Get a FREE key at: https://console.groq.com/keys")
    try:
        key = input("  Enter your Groq API key: ").strip()
    except (EOFError, KeyboardInterrupt):
        sys.exit(1)
    if not key:
        err("No API key provided.")
        sys.exit(1)
    return key


def parse_args() -> argparse.Namespace:
    d = DEFAULTS
    p = argparse.ArgumentParser(description="PDF RAG v2 — multi-PDF hybrid-search terminal chat")
    p.add_argument("paths", nargs="*", help="PDF files, folders or glob patterns to index")
    p.add_argument("--pdf", action="append", default=[], help="(compat) PDF path, repeatable")
    p.add_argument("--dir", action="append", default=[], help="Folder of PDFs (recursive), repeatable")
    p.add_argument("--reset", action="store_true", help="Wipe the index before indexing")
    p.add_argument("--ask", help="Ask one question and exit")
    p.add_argument("--debug", action="store_true", help="Show retrieved chunks and scores")
    p.add_argument("--top-k", type=int, default=d["top_k"])
    p.add_argument("--no-rerank", action="store_true", help="Disable cross-encoder reranking")
    p.add_argument("--no-rewrite", action="store_true", help="Disable follow-up query rewriting")
    p.add_argument("--db", dest="db_dir", default=d["db_dir"])
    p.add_argument("--collection", default=d["collection"])
    p.add_argument("--model", dest="chat_model", default=d["chat_model"])
    p.add_argument("--embed-model", default=d["embed_model"])
    p.add_argument("--rerank-model", default=d["rerank_model"])
    p.add_argument("--chunk-size", type=int, default=d["chunk_size"])
    p.add_argument("--overlap", type=int, default=d["overlap"])
    p.set_defaults(candidates=d["candidates"], history_turns=d["history_turns"])
    a = p.parse_args()
    if a.overlap >= a.chunk_size:
        p.error("--overlap must be smaller than --chunk-size")
    return a


def main() -> None:
    cfg = parse_args()
    print(cyan(BANNER))

    llm = LLM(get_api_key(), cfg.chat_model)
    store = Store(cfg)

    if cfg.reset:
        store.wipe()
        ok("Old index wiped.")

    # collect PDFs from CLI
    raw = list(cfg.paths) + list(cfg.pdf) + list(cfg.dir)
    paths = resolve_pdf_paths(raw) if raw else []
    if paths:
        info(f"{len(paths)} PDF(s) to process")
        store.add_many(paths)

    # interactive onboarding if DB empty
    if store.count() == 0 and not paths:
        warn("Index is empty.")
        try:
            line = input("  PDF path(s) / folder (comma-separated): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        paths = resolve_pdf_paths(parse_path_input(line))
        if not paths:
            err("No PDFs found. Exiting.")
            sys.exit(1)
        store.add_many(paths)
        if store.count() == 0:
            err("Nothing was indexed. Exiting.")
            sys.exit(1)

    session = Session(store, llm, cfg)

    if cfg.ask:
        session.ask(cfg.ask)
        return

    docs = store.docs()
    print("\n" + bold("═" * term_width()))
    print(f"  {green('✅ Ready')} — {len(docs)} document(s), {store.count()} chunks.  "
          f"Type a question, or {cyan('/help')}.")
    print(bold("═" * term_width()) + "\n")

    while True:
        try:
            line = input(session.prompt()).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n  Bye!\n")
            break
        if not line:
            continue
        if line.startswith("/") or line.lower() in ("quit", "exit", "q", "help", "docs", "clear", "stats"):
            if not session.handle_command(line):
                print("\n  Bye!\n")
                break
            continue
        session.ask(line)


if __name__ == "__main__":
    main()
