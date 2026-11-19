#!/usr/bin/env python3
"""
==========================================================================
  PDF + JSON RAG v3 — Multilingual (Bangla/English) Terminal Chat
  Hybrid search (Vector + BM25) · RRF fusion · Cross-encoder rerank
  Query rewrite + cross-language expansion · Streaming answers
  Incremental, resumable ChromaDB indexing
==========================================================================
  INSTALL
      pip install groq chromadb pdfplumber sentence-transformers
      (optional fallback extractor)  pip install pypdf
      Needs kb_json.py in the same folder.

  USAGE
      python rag_pdf.py                              # interactive
      python rag_pdf.py a.pdf b.pdf                  # index several PDFs
      python rag_pdf.py --dir ./papers               # whole folder (pdf/json/jsonl)
      python rag_pdf.py --json bdlaws.jsonl          # FULL JSON knowledge base
      python rag_pdf.py --json sample.jsonl          # 100-record sample
      python rag_pdf.py --json bdlaws.jsonl --json-limit 100   # first 100 only
      python rag_pdf.py --reset a.pdf                # wipe DB then index
      python rag_pdf.py --ask "what is X?"           # one-shot answer

  IN-CHAT COMMANDS  (type /help)
      /docs  /add  /remove  /use  /all  /stats  /reset
      /top N  /rerank  /rewrite  /expand  /repealed  /debug  /clear
      /save  /quit
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
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

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
    import kb_json  # noqa: E402
except ImportError:
    print("\n[ERROR] kb_json.py not found. Put it in the same folder as rag_pdf.py.\n")
    sys.exit(1)

try:
    import readline  # noqa: F401  (arrow-key history on Linux/macOS)
except ImportError:
    pass

# ── Config ──────────────────────────────────────────────────────────────────

DEFAULTS = dict(
    # multilingual (Bangla + English). English-only alt: all-MiniLM-L6-v2
    # stronger/heavier: intfloat/multilingual-e5-base  |  BAAI/bge-m3
    embed_model="intfloat/multilingual-e5-small",
    # multilingual reranker. stronger/heavier: BAAI/bge-reranker-v2-m3
    rerank_model="cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
    chat_model="openai/gpt-oss-120b",
    chunk_size=700,        # PDF chunk (chars, sentence-aware)
    json_chunk_size=1000,  # JSON section chunk (legal sections are longer)
    overlap=140,
    top_k=6,               # final chunks sent to the LLM
    candidates=40,         # candidates per retriever per query before fusion
    db_dir="./rag_chroma_db",
    collection="kb_rag_v3",
    history_turns=6,
)
RRF_K = 60
BATCH_EMBED = 64
BATCH_INSERT = 256

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
█     █████  ███   ███  █         ███   ███  ████  ███ █      ███  █████    █   █ ████    
█░    █░░░░░█ ░░░ █ ░░█ █░       █ ░░░ █ ░░█ █░░░█  █░░█░    █ ░░█  ░█░░░   █░  █░ ░░░█   
█░░   ████░░█░ ██░█████░█░░      █░ ░░░█░ ░█░████░░ █░░█░░   █░ ░█░  █░░░░  █░░ █░░███░░  
█░░   █░░░░ █░░ █░█░░░█░█░░      █░░   █░░ █░█░░░░ ░█░░█░░   █░░ █░░ █░░     █░█ ░░ ░░█ ░ 
█████ █████░ ███ ░█░░░█░█████     ███   ███ ░█░░░░░███░█████  ███ ░░ █░░      █ ░ ████░░  
 ░░░░░ ░░░░░  ░░░ ░░░  ░░░░░░░     ░░░   ░░░ ░░░    ░░░ ░░░░░  ░░░ ░  ░░       ░ ░ ░░░░ ░ 
  ░░░░░ ░░░░░  ░░░  ░   ░ ░░░░░     ░░░   ░░░  ░     ░░░ ░░░░░  ░░░    ░        ░   ░░░░  

   PDF + JSON · বাংলা / English · Hybrid Search · Rerank · Groq
"""

# ── Prompts ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a precise document question-answering assistant. The knowledge base
holds PDFs and legal texts (Bangladesh laws) written in Bangla and English.
Answer the user's question using ONLY the numbered CONTEXT sources provided.

Rules:
1. Use only the context. Never use outside knowledge or guess.
2. If the context does not contain the answer, reply exactly:
   "I don't have enough information in the documents to answer that."
   (translate that one sentence into the question's language if it is not English)
3. Be concise and factual. Use short bullet points for lists or comparisons.
4. Cite every claim inline with its source tag, e.g. [S1] or [S2][S4].
5. If sources from different documents disagree, say so and cite both.
6. Answer in the same language as the question, even when the sources are in
   another language. Keep Act names, section numbers and quoted legal terms
   in their original form.
7. For legal sources, name the Act and the section number. If a source is
   marked "Status: REPEALED", say clearly that it is repealed. Mention
   amendment notes only when they matter to the question.
8. You report what the texts say; you do not give personal legal advice.
"""

REWRITE_PROMPT = """\
Rewrite the user's latest message as a standalone search query, using the chat \
history to resolve pronouns and references. Keep it short and keep the original \
language. Output ONLY the rewritten query, nothing else. If the message is \
already standalone, output it unchanged."""

EXPAND_PROMPT = """\
You help a search engine over Bangladeshi legal texts and other documents \
written in Bangla and English. Translate the user's search query into the OTHER \
language (Bangla -> English, English -> Bangla; if mixed, output English). If it \
names a Bangladeshi law, section or legal term, use the official title/term \
used in the statute (e.g. দণ্ডবিধি -> The Penal Code, 1860). Keep numbers. \
Output ONLY the translated query, max 40 words."""

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


# ── File helpers ────────────────────────────────────────────────────────────

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


def _expand_items(raw_items: List[str], exts: set) -> List[Path]:
    """Expand files, folders (recursive) and globs into unique files with given extensions."""
    found: List[Path] = []
    for item in raw_items:
        item = os.path.expanduser(item.strip().strip('"').strip("'"))
        if not item:
            continue
        p = Path(item)
        if p.is_dir():
            found += sorted(x for x in p.rglob("*") if x.is_file() and x.suffix.lower() in exts)
        elif any(ch in item for ch in "*?["):
            found += [Path(x) for x in sorted(glob.glob(item, recursive=True))
                      if Path(x).suffix.lower() in exts]
        elif p.is_file():
            if p.suffix.lower() in exts:
                found.append(p)
            else:
                warn(f"Unsupported file type: {item}")
        else:
            warn(f"Not found: {item}")
    seen, out = set(), []
    for p in found:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def resolve_pdf_paths(raw_items: List[str]) -> List[Path]:
    """PDF-only (kept for backward compatibility)."""
    return _expand_items(raw_items, {".pdf"})


def resolve_kb_paths(raw_items: List[str]) -> List[Path]:
    """PDFs + JSON/JSONL/NDJSON knowledge-base files."""
    return _expand_items(raw_items, {".pdf"} | kb_json.KB_EXTS)


def parse_path_input(s: str) -> List[str]:
    """Parse a typed line of paths: quoted, or separated by , or ;"""
    if '"' in s or "'" in s:
        return [a or b for a, b in re.findall(r'"([^"]+)"|\'([^\']+)\'', s)] or [s]
    return [x for x in re.split(r"[;,]", s) if x.strip()]


# ── BM25 (dependency-free, Bangla-aware) ───────────────────────────────────

# \w alone splits Bangla words at vowel signs (matras are not \w), so the
# Bengali block is added explicitly (minus currency/numerator symbols).
_TOKEN = re.compile(r"[\w\u0980-\u09F1\u09FC-\u09FF\u200c\u200d]+", re.UNICODE)
_STOP_EN = set("""a an and are as at be but by for from has have he her his i if in into is it its
of on or our she that the their them they this to was we were what when where which who will with
you your do does did not no so than then there these those can could should would about over
under also how why shall may said""".split())
_STOP_BN = set("""এবং ও বা এর এই যে যদি তাহা তাহার হইবে হইয়াছে করা করিয়া কোন কোনো একটি না সে তিনি
জন্য হইতে দ্বারা কর্তৃক মধ্যে পর্যন্ত থাকিলে হয় হবে করে করেন এ যাহা যিনি সকল উক্ত তাহাকে আছে নেই
কি কী কেন কিভাবে কীভাবে কত কোথায় কখন কে কাকে""".split())
_STOP = _STOP_EN | _STOP_BN
# conservative Bangla suffix stripping (longest first): আইনের/আইনে/আইনসমূহ -> আইন
_BN_SUFFIX = tuple(sorted(
    ["গুলোর", "গুলির", "সমূহের", "গুলো", "গুলি", "সমূহ", "দের", "েরা", "ের",
     "রা", "কে", "তে", "টি", "টা", "ে", "র"], key=len, reverse=True))


def _stem_bn(t: str) -> str:
    if len(t) >= 4 and "\u0980" <= t[0] <= "\u09ff":
        for suf in _BN_SUFFIX:
            if t.endswith(suf) and len(t) - len(suf) >= 2:
                return t[: -len(suf)]
    return t


def tokenize(text: str) -> List[str]:
    out: List[str] = []
    for t in _TOKEN.findall(kb_json.normalize_digits(text.lower())):
        if (len(t) > 1 or t.isdigit()) and t not in _STOP:
            out.append(_stem_bn(t))
    return out


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


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(tokenize(a)), set(tokenize(b))
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


# ── Embedding prefixes (model-specific) ────────────────────────────────────

def _prefixes(model: str) -> Tuple[str, str]:
    """(passage_prefix, query_prefix) required by the embedding model family."""
    m = model.lower()
    if "e5" in m:
        return "passage: ", "query: "
    if "bge" in m and "m3" not in m:
        return "", "Represent this sentence for searching relevant passages: "
    return "", ""


# ── Vector store / index manager ───────────────────────────────────────────

class Store:
    """ChromaDB wrapper: incremental multi-document indexing + hybrid search."""

    def __init__(self, cfg: argparse.Namespace):
        self.cfg = cfg
        print()
        info(f"Loading embedding model: {bold(cfg.embed_model)} (first run downloads it)")
        self.embedder = SentenceTransformer(cfg.embed_model)
        self.p_prefix, self.q_prefix = _prefixes(cfg.embed_model)
        self.client = chromadb.PersistentClient(path=cfg.db_dir)
        self.col = self._open_collection()
        self.reranker = None
        self.rerank_failed = False
        self._cache: Optional[dict] = None   # ids/texts/metas/bm25/positions

    # ---- embeddings ----
    def embed_passages(self, texts: List[str]) -> List[List[float]]:
        vecs = self.embedder.encode([self.p_prefix + t for t in texts], batch_size=BATCH_EMBED,
                                    normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vecs]

    def embed_query(self, q: str) -> List[float]:
        return self.embedder.encode([self.q_prefix + q], normalize_embeddings=True,
                                    show_progress_bar=False)[0].tolist()

    # ---- collection lifecycle ----
    def _open_collection(self):
        col = self.client.get_or_create_collection(
            name=self.cfg.collection,
            metadata={"hnsw:space": "cosine", "embed_model": self.cfg.embed_model},
        )
        stored = (col.metadata or {}).get("embed_model")
        if stored and stored != self.cfg.embed_model and col.count() > 0:
            err(f"DB was built with '{stored}', but you selected '{self.cfg.embed_model}'.")
            err("Mixing models corrupts search. Use --reset, --collection <new>, or the same --embed-model.")
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
        if len(texts) > 5000:
            info(f"Building lexical index over {len(texts):,} chunks…")
        positions: Dict[str, List[int]] = defaultdict(list)
        for i, m in enumerate(metas):
            positions[m["doc_id"]].append(i)
        self._cache = dict(ids=ids, texts=texts, metas=metas,
                           bm25=BM25(texts) if texts else None, positions=positions,
                           pos_by_id={cid: i for i, cid in enumerate(ids)})
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
            out.append(dict(doc_id=doc_id, name=m.get("doc_name") or m["source"],
                            path=m.get("path", ""), pages=m.get("total_pages", 0),
                            chunks=len(pos), expected=m.get("expected", 0),
                            indexed_at=m.get("indexed_at", "")))
        out.sort(key=lambda d: d["name"].lower())
        return out

    # ---- indexing: PDF ----
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
                metas.append(dict(doc_id=doc_id, doc_name=path.name, source=path.name,
                                  path=str(path), page=page_no, total_pages=total_pages,
                                  loc=f"{path.name}, p.{page_no}", kind="page",
                                  repealed=False, lang="bn" if kb_json.bangla_ratio(ch) > 0.3 else "en",
                                  indexed_at=now))
        if not texts:
            warn("Text too short to chunk.")
            return "failed"

        embs: List[List[float]] = []
        for i in range(0, len(texts), BATCH_INSERT):
            embs += self.embed_passages(texts[i:i + BATCH_INSERT])
            progress(min(i + BATCH_INSERT, len(texts)), len(texts), "  embedding")

        for d in replaced:                              # same path, new content
            self.col.delete(where={"doc_id": d["doc_id"]})
        for i in range(0, len(texts), BATCH_INSERT):
            self.col.upsert(ids=ids[i:i + BATCH_INSERT], embeddings=embs[i:i + BATCH_INSERT],
                            documents=texts[i:i + BATCH_INSERT], metadatas=metas[i:i + BATCH_INSERT])
        self._invalidate()
        ok(f"{len(texts)} chunks from {len(pages)} pages")
        return "updated" if replaced else "added"

    # ---- indexing: JSON / JSONL knowledge base ----
    def _existing_ids(self, ids: List[str]) -> set:
        have: set = set()
        for i in range(0, len(ids), 500):
            got = self.col.get(ids=ids[i:i + 500], include=[])
            have.update(got["ids"])
        return have

    def add_json(self, path: Path) -> str:
        """Section-aware, resumable JSON/JSONL indexing. Returns 'added' | 'skipped' | 'failed'."""
        limit = getattr(self.cfg, "json_limit", None)
        doc_id = kb_json.kb_doc_id(path, limit)
        for d in self.docs():
            if d["doc_id"] == doc_id and d["expected"] and d["chunks"] >= d["expected"]:
                return "skipped"

        print(f"\n  {bold(path.name)}  {dim(human_size(path.stat().st_size))}"
              + (f"  {dim(f'(first {limit} records)')}" if limit else ""))
        info("Parsing and chunking…")
        _, chunks, st = kb_json.build_kb_chunks(
            path, self.cfg.json_chunk_size, self.cfg.overlap, chunk_text, limit=limit,
            source_name=path.name,
            progress_cb=lambda n: print(f"\r  {dim(f'parsed {n:,} records')}", end="", flush=True))
        print()
        if not chunks:
            warn("No usable records found in the file.")
            return "failed"
        info(f"{st['records']:,} records → {st['acts']:,} acts · {st['section_chunks']:,} section chunks · "
             f"{st['act_chunks']:,} overview chunks · skipped empty {st['skipped_empty']:,} · "
             f"bn {st['bn_chunks']:,} / en {st['en_chunks']:,} · repealed {st['repealed_chunks']:,}")
        if st["bad_records"]:
            warn(f"{st['bad_records']} malformed record(s) skipped")

        have = self._existing_ids([c["id"] for c in chunks])
        todo = [c for c in chunks if c["id"] not in have]
        if have:
            info(f"Resuming: {len(have):,} chunks already indexed, {len(todo):,} to go")
        try:
            for i in range(0, len(todo), BATCH_INSERT):
                part = todo[i:i + BATCH_INSERT]
                embs = self.embed_passages([c["text"] for c in part])
                self.col.upsert(ids=[c["id"] for c in part], embeddings=embs,
                                documents=[c["text"] for c in part],
                                metadatas=[c["meta"] for c in part])
                progress(min(i + BATCH_INSERT, len(todo)), len(todo), "  embedding")
        except KeyboardInterrupt:
            self._invalidate()
            warn("Interrupted — progress is saved. Run the same command again to resume.")
            return "failed"
        self._invalidate()
        ok(f"{len(chunks):,} chunks indexed")
        return "added"

    def add_path(self, path: Path) -> str:
        if path.suffix.lower() in kb_json.KB_EXTS:
            return self.add_json(path)
        return self.add_pdf(path)

    def add_many(self, paths: List[Path]) -> None:
        stats = Counter()
        t0 = time.time()
        for p in paths:
            try:
                stats[self.add_path(p)] += 1
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
                self.reranker = CrossEncoder(self.cfg.rerank_model, max_length=512)
            except Exception as e:  # noqa: BLE001
                self.rerank_failed = True
                warn(f"Reranker unavailable ({e}); continuing without it.")
        return self.reranker

    @staticmethod
    def _where(scope: Optional[List[str]], exclude_repealed: bool) -> Optional[dict]:
        conds = []
        if scope:
            conds.append({"doc_id": scope[0]} if len(scope) == 1 else {"doc_id": {"$in": scope}})
        if exclude_repealed:
            conds.append({"repealed": False})
        if not conds:
            return None
        return conds[0] if len(conds) == 1 else {"$and": conds}

    def search(self, query: Union[str, List[str]], top_k: int, scope: Optional[List[str]],
               use_rerank: bool, exclude_repealed: bool = False) -> List[dict]:
        """query may be a list: [primary, translated/expanded…] — all are fused with RRF."""
        queries = [query] if isinstance(query, str) else [q for q in query if q and q.strip()]
        if not queries:
            return []
        primary = queries[0]
        c = self._load()
        if not c["ids"]:
            return []

        allowed: Optional[set] = None
        if scope or exclude_repealed:
            if scope:
                allowed = set()
                for d in scope:
                    allowed.update(c["positions"].get(d, []))
            else:
                allowed = set(range(len(c["ids"])))
            if exclude_repealed:
                allowed = {i for i in allowed if not c["metas"][i].get("repealed", False)}
            if not allowed:
                return []
        pool_n = len(allowed) if allowed is not None else len(c["ids"])
        n_cand = min(self.cfg.candidates, pool_n)
        where = self._where(scope, exclude_repealed)

        fused: Dict[str, float] = defaultdict(float)
        dense_s: Dict[str, float] = {}
        lex_s: Dict[str, float] = {}
        for q in queries:
            # dense retrieval
            kw = dict(query_embeddings=[self.embed_query(q)], n_results=n_cand, include=["distances"])
            if where:
                kw["where"] = where
            res = self.col.query(**kw)
            for r, (cid, dist) in enumerate(zip(res["ids"][0], res["distances"][0]), 1):
                fused[cid] += 1.0 / (RRF_K + r)
                dense_s[cid] = max(dense_s.get(cid, 0.0), 1 - dist)
            # lexical retrieval (BM25)
            if c["bm25"]:
                for r, (i, s) in enumerate(c["bm25"].search(q, allowed, n_cand), 1):
                    cid = c["ids"][i]
                    fused[cid] += 1.0 / (RRF_K + r)
                    lex_s[cid] = max(lex_s.get(cid, 0.0), s)

        ranked = sorted(fused, key=lambda x: -fused[x])
        pool = ranked[: max(top_k * 4, 16)]

        cands = []
        for cid in pool:
            i = c["pos_by_id"][cid]
            m = c["metas"][i]
            cands.append(dict(
                id=cid, text=c["texts"][i], source=m["source"], doc_id=m["doc_id"],
                label=m.get("loc") or f"{m['source']}, p.{m.get('page', '?')}",
                dense=round(dense_s.get(cid, 0.0), 3), bm25=round(lex_s.get(cid, 0.0), 2),
                fused=fused[cid], score=fused[cid]))

        # cross-encoder rerank (against the primary query)
        if use_rerank and len(cands) > 1:
            rr = self._get_reranker()
            if rr is not None:
                scores = rr.predict([(primary, x["text"]) for x in cands], show_progress_bar=False)
                for x, s in zip(cands, scores):
                    x["score"] = float(s)
                cands.sort(key=lambda x: -x["score"])
            else:
                cands.sort(key=lambda x: -x["fused"])
        else:
            cands.sort(key=lambda x: -x["fused"])

        # drop near-duplicate chunks (overlap) and cap
        final: List[dict] = []
        for x in cands:
            if any(x["doc_id"] == y["doc_id"] and x["label"] == y["label"]
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

    def _short(self, system: str, user: str) -> str:
        r = self._create(model=self.model, temperature=0, max_tokens=400,
                         messages=[{"role": "system", "content": system},
                                   {"role": "user", "content": user}],
                         **self._extra("low"))
        return (r.choices[0].message.content or "").strip().strip('"')

    def rewrite(self, question: str, history: List[dict]) -> str:
        if not history:
            return question
        convo = "\n".join(f"{t['role']}: {t['content'][:400]}" for t in history[-4:])
        try:
            out = self._short(REWRITE_PROMPT, f"HISTORY:\n{convo}\n\nLATEST: {question}")
            return out if 3 <= len(out) <= 300 else question
        except Exception:  # noqa: BLE001
            return question

    def expand(self, query: str) -> str:
        """Cross-language variant of the query (helps BM25 when docs are in the other language)."""
        try:
            out = self._short(EXPAND_PROMPT, query)
            return out if 3 <= len(out) <= 300 else ""
        except Exception:  # noqa: BLE001
            return ""

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
    return "\n\n".join(f"[S{i}] ({c['label']})\n{c['text']}" for i, c in enumerate(chunks, 1))


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
        self.expand = not cfg.no_expand
        self.exclude_repealed = cfg.exclude_repealed
        self.debug = cfg.debug

    # ---- ask ----
    def ask(self, question: str) -> None:
        if self.store.count() == 0:
            warn("Index is empty. Use /add <pdf|json|folder> first.")
            return
        search_q = question
        if self.rewrite and self.history:
            search_q = self.llm.rewrite(question, self.history)
            if search_q != question:
                print(dim(f"  ↳ searching for: {search_q}"))
        queries = [search_q]
        if self.expand:
            alt = self.llm.expand(search_q)
            if alt and alt.strip().lower() != search_q.strip().lower():
                queries.append(alt)
                print(dim(f"  ↳ also searching: {alt}"))

        t0 = time.time()
        chunks = self.store.search(queries, self.top_k, self.scope or None, self.rerank,
                                   self.exclude_repealed)
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
            print(f"   {magenta(f'[S{i}]')} {c['label']}")

    def _print_debug(self, chunks: List[dict], t: float) -> None:
        print(dim(f"\n  [debug] retrieval {t:.2f}s"))
        for i, c in enumerate(chunks, 1):
            prev = c["text"][:90].replace("\n", " ")
            print(dim(f"   {i}. {c['label']}  dense={c['dense']}  "
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
        elif cmd == "expand":
            self.expand = not self.expand
            ok(f"Cross-language query expansion: {'ON' if self.expand else 'OFF'}")
        elif cmd == "repealed":
            self.exclude_repealed = not self.exclude_repealed
            ok(f"Repealed laws: {'EXCLUDED' if self.exclude_repealed else 'included (marked REPEALED)'}")
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
            ("/add <path…>", "index PDFs / JSON / JSONL / folders / globs"),
            ("/remove <#|name…>", "delete documents from the index"),
            ("/use <#…>", "restrict search to these documents  (/all to undo)"),
            ("/all", "search all documents"),
            ("/stats", "DB + settings overview"),
            ("/reset", "wipe the whole index"),
            ("/top N", "chunks sent to the LLM"),
            ("/rerank", "toggle cross-encoder reranking"),
            ("/rewrite", "toggle follow-up query rewriting"),
            ("/expand", "toggle Bangla<->English query expansion"),
            ("/repealed", "toggle excluding repealed laws"),
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
            warn("No documents indexed. Use /add <pdf|json|folder>.")
            return
        w = term_width()
        name_w = max(20, w - 38)
        print(f"\n   {bold('#'):<4} {bold('Document'):<{name_w}} {bold('Pg/Acts'):>7} {bold('Chunks'):>7}")
        print("   " + dim("─" * (w - 6)))
        for i, d in enumerate(docs, 1):
            active = (not self.scope) or d["doc_id"] in self.scope
            mark = green("●") if active else dim("○")
            name = d["name"] if len(d["name"]) <= name_w else d["name"][: name_w - 1] + "…"
            partial = ""
            if d["expected"] and d["chunks"] < d["expected"]:
                partial = yellow(f"  incomplete {d['chunks']}/{d['expected']} — re-run /add to resume")
            print(f"  {mark}{i:<3} {name:<{name_w}} {d['pages']:>7} {d['chunks']:>7}{partial}")
        scope_txt = "all documents" if not self.scope else f"{len(self.scope)} selected"
        print(dim(f"\n   ● active  ○ excluded — searching {scope_txt}\n"))

    def cmd_add(self, arg: str) -> None:
        if not arg:
            try:
                arg = input("  PDF / JSON path(s) / folder (comma-separated): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
        paths = resolve_kb_paths(parse_path_input(arg))
        if not paths:
            warn("No PDF/JSON files found.")
            return
        info(f"{len(paths)} file(s) found")
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
            ("Chunks", f"{self.store.count():,}"),
            ("DB path", os.path.abspath(self.cfg.db_dir)),
            ("DB size", human_size(self.store.disk_usage())),
            ("Collection", self.cfg.collection),
            ("Embed model", self.cfg.embed_model),
            ("Reranker", f"{self.cfg.rerank_model} ({'on' if self.rerank else 'off'})"),
            ("LLM", self.cfg.chat_model),
            ("Chunk PDF / JSON", f"{self.cfg.chunk_size} / {self.cfg.json_chunk_size}  (overlap {self.cfg.overlap})"),
            ("Top-k", self.top_k),
            ("Query rewrite", "on" if self.rewrite else "off"),
            ("Query expansion", "on" if self.expand else "off"),
            ("Repealed laws", "excluded" if self.exclude_repealed else "included"),
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
            lines += [f"- [S{i}] {c['label']}" for i, c in enumerate(ch, 1)]
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
    p = argparse.ArgumentParser(description="PDF + JSON RAG v3 — multilingual hybrid-search terminal chat")
    p.add_argument("paths", nargs="*", help="PDF / JSON / JSONL files, folders or glob patterns")
    p.add_argument("--pdf", action="append", default=[], help="(compat) PDF path, repeatable")
    p.add_argument("--json", action="append", default=[], help="JSON/JSONL knowledge-base file, repeatable")
    p.add_argument("--dir", action="append", default=[], help="Folder (recursive), repeatable")
    p.add_argument("--json-limit", type=int, default=None,
                   help="Index only the first N JSON records (e.g. 100 for the analysis sample)")
    p.add_argument("--reset", action="store_true", help="Wipe the index before indexing")
    p.add_argument("--ask", help="Ask one question and exit")
    p.add_argument("--debug", action="store_true", help="Show retrieved chunks and scores")
    p.add_argument("--top-k", type=int, default=d["top_k"])
    p.add_argument("--no-rerank", action="store_true", help="Disable cross-encoder reranking")
    p.add_argument("--no-rewrite", action="store_true", help="Disable follow-up query rewriting")
    p.add_argument("--no-expand", action="store_true", help="Disable Bangla<->English query expansion")
    p.add_argument("--exclude-repealed", action="store_true", help="Hide repealed laws from search")
    p.add_argument("--db", dest="db_dir", default=d["db_dir"])
    p.add_argument("--collection", default=d["collection"])
    p.add_argument("--model", dest="chat_model", default=d["chat_model"])
    p.add_argument("--embed-model", default=d["embed_model"])
    p.add_argument("--rerank-model", default=d["rerank_model"])
    p.add_argument("--chunk-size", type=int, default=d["chunk_size"])
    p.add_argument("--json-chunk-size", type=int, default=d["json_chunk_size"])
    p.add_argument("--overlap", type=int, default=d["overlap"])
    p.set_defaults(candidates=d["candidates"], history_turns=d["history_turns"])
    a = p.parse_args()
    if a.overlap >= min(a.chunk_size, a.json_chunk_size):
        p.error("--overlap must be smaller than --chunk-size and --json-chunk-size")
    return a


def main() -> None:
    cfg = parse_args()
    print(cyan(BANNER))

    llm = LLM(get_api_key(), cfg.chat_model)
    store = Store(cfg)

    if cfg.reset:
        store.wipe()
        ok("Old index wiped.")

    # collect files from CLI
    raw = list(cfg.paths) + list(cfg.pdf) + list(cfg.json) + list(cfg.dir)
    paths = resolve_kb_paths(raw) if raw else []
    if paths:
        info(f"{len(paths)} file(s) to process")
        store.add_many(paths)

    # interactive onboarding if DB empty
    if store.count() == 0 and not paths:
        warn("Index is empty.")
        try:
            line = input("  PDF / JSON path(s) / folder (comma-separated): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        paths = resolve_kb_paths(parse_path_input(line))
        if not paths:
            err("No PDF/JSON files found. Exiting.")
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
    print(f"  {green('✅ Ready')} — {len(docs)} document(s), {store.count():,} chunks.  "
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