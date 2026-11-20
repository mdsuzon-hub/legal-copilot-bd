#!/usr/bin/env python3
"""
==========================================================================
  PDF + JSON RAG v4 — Multilingual (Bangla/English) Terminal Chat
  Hybrid search (Vector + BM25) · RRF fusion · Cross-encoder rerank
  Query rewrite + cross-language expansion · Streaming answers
  Incremental, resumable ChromaDB indexing
==========================================================================
  INSTALL
      pip install groq chromadb pdfplumber sentence-transformers
      (optional fallback extractor)  pip install pypdf
      Needs kb_json.py, kb_pdf.py and rag_logic.py in the same folder.

  USAGE
      python rag.py                                  # interactive
      python rag.py a.pdf b.pdf                      # index several PDFs
      python rag.py --dir ./papers                   # whole folder (pdf/json/jsonl)
      python rag.py --json bdlaws.jsonl              # FULL JSON knowledge base
      python rag.py --json sample.jsonl               # 100-record sample
      python rag.py --json bdlaws.jsonl --json-limit 100 # first 100 only
      python rag.py --reset a.pdf                    # wipe DB then index
      python rag.py --ask "what is X?"               # one-shot answer

  IN-CHAT COMMANDS  (type /help)
      /docs  /add  /remove  /use  /all  /stats  /reset
      /top N  /rerank  /rewrite  /expand  /repealed  /debug  /clear
      /save  /quit
==========================================================================
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import hashlib
import math
import os
import re
import shutil
import sys
import time
import uuid
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
from sentence_transformers import SentenceTransformer  # noqa: E402

try:
    import kb_json  # noqa: E402
    import kb_pdf  # noqa: E402
    import rag_logic as L  # noqa: E402
except ImportError:
    print("\n[ERROR] kb_json.py / kb_pdf.py / rag_logic.py not found. Keep them next to rag.py.\n")
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
    collection="kb_rag_v4",   # v4: stable ids + new chunk headers (re-index)
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
You are a precise document question-answering assistant. The knowledge base holds
PDFs and legal texts (Bangladesh laws) written in Bangla and English. Answer using
ONLY the CONTEXT sources. Each source starts with a header: SOURCE_ID, ACT (or
DOCUMENT), SECTION, PUBLISHED.

Citations
1. Cite with the exact SOURCE_ID in square brackets, e.g. [V-1799-s3]. Never cite by
   position or rank ("[S1]", "source 2"): the <<S#>> markers are display aliases only.
2. EVERY factual sentence must end with at least one citation.
3. Assert only facts present in a source you cite. Never merge facts from uncited
   sources and never use outside knowledge or guesses.
4. If sources conflict, cite both and state the conflict.
5. Citations always stay in ASCII (English) form, whatever language you answer in.

Scope
6. If the user names an act but the relevant provision is in a DIFFERENT act, say so
   explicitly, name the correct act + section, and cite it.
7. If the named act has no such section/provision, say that clearly (RETRIEVAL NOTES
   may state it) and do not invent content for it.
8. If the context has nothing relevant in ANY act, reply exactly:
   "I don't have enough information in the documents to answer that."
   (translated into the answer language when it is not English; no citation needed)
9. Flag sources marked "STATUS: REPEALED" as repealed. Mention amendment notes only
   when they matter.
10. SECTION shows the printed section number; the sN inside SOURCE_ID is the record id
    and can differ. Cite the SOURCE_ID; when naming a section, use the printed number.

Style
11. Reply in ANSWER_LANGUAGE (Bengali or English). Keep Act names, section numbers and
    quoted legal terms in their original form.
12. Be concise; short bullets for lists. You report what the texts say; no personal
    legal advice. RETRIEVAL NOTES come from the system, not the user: follow them.
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
        self._cache: Optional[dict] = None

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

    # ---- cache (BM25, doc table, act table, stable-id index) ----
    def _load(self) -> dict:
        if self._cache is not None:
            return self._cache
        data = self.col.get(include=["documents", "metadatas"])
        ids, texts, metas = data["ids"], data["documents"], data["metadatas"]
        if len(texts) > 5000:
            info(f"Building lexical index over {len(texts):,} chunks…")
        positions: Dict[str, List[int]] = defaultdict(list)
        by_sid: Dict[str, List[int]] = defaultdict(list)
        acts: Dict[str, dict] = {}
        for i, m in enumerate(metas):
            positions[m["doc_id"]].append(i)
            by_sid[m.get("sid") or ids[i]].append(i)
            uid = m.get("act_uid")
            if uid:
                a = acts.setdefault(uid, dict(uid=uid, title=m.get("source", ""), act_no=m.get("act_no", ""),
                                              year=m.get("year", 0), published=m.get("published", ""),
                                              nsec=m.get("nsec", 0), sections=set()))
                if m.get("kind") == "section" and m.get("sno"):
                    a["sections"].add(m["sno"])
        self._cache = dict(ids=ids, texts=texts, metas=metas,
                           bm25=BM25(texts) if texts else None, positions=positions,
                           pos_by_id={cid: i for i, cid in enumerate(ids)}, by_sid=by_sid, acts=acts)
        return self._cache

    def _invalidate(self) -> None:
        self._cache = None

    def count(self) -> int:
        return self.col.count()

    def acts(self) -> Dict[str, dict]:
        return self._load()["acts"]

    def get_chunks(self, sid: str) -> List[dict]:
        """Chunks for a stable id (a base id like V-1799-s3 also returns its -1, -2… parts)."""
        c = self._load()
        keys = [sid] if sid in c["by_sid"] else [k for k in c["by_sid"] if k.startswith(sid + "-")]
        out = []
        for k in sorted(keys):
            for i in c["by_sid"][k]:
                m = c["metas"][i]
                out.append(dict(sid=k, text=c["texts"][i], source=m.get("source", ""), label=m.get("loc", ""),
                                kind=m.get("kind", ""), published=m.get("published", ""),
                                repealed=bool(m.get("repealed", False)), act_uid=m.get("act_uid", "")))
        return out

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
        doc_id = kb_pdf.file_hash(path)
        existing = self.docs()
        if any(d["doc_id"] == doc_id for d in existing):
            return "skipped"
        replaced = [d for d in existing if d["path"] == str(path)]

        print(f"\n  {bold(path.name)}  {dim(human_size(path.stat().st_size))}")
        pages = kb_pdf.extract_pages(
            path,
            progress_cb=lambda done, total: progress(done, total, "  extracting"),
            warn_cb=warn,
            error_cb=err,
        )
        if not pages:
            warn("No extractable text (scanned PDF?). Tip: run `ocrmypdf in.pdf out.pdf` first.")
            return "failed"

        total_pages = max(p for p, _ in pages)
        ids, texts, metas = [], [], []
        now = datetime.now().isoformat(timespec="seconds")
        seen_hash = set()
        for page_no, text in pages:
            for j, ch in enumerate(kb_pdf.chunk_text(text, self.cfg.chunk_size, self.cfg.overlap)):
                h = hashlib.sha1(ch.encode("utf-8")).hexdigest()[:12]
                if h in seen_hash:                       # identical chunk repeated (headers/footers)
                    continue
                seen_hash.add(h)
                sid = f"PDF-{doc_id[:6]}-p{page_no}-{j}"
                ids.append(f"{doc_id}:{sid}")
                texts.append(ch)
                metas.append(dict(doc_id=doc_id, doc_name=path.name, source=path.name,
                                  path=str(path), page=page_no, total_pages=total_pages,
                                  loc=f"{path.name}, p.{page_no}", kind="page", sid=sid, act_uid="",
                                  act_no="", year=0, published="", volume="", section_id=0, sno="",
                                  chash=h, nsec=0, repealed=False,
                                  lang="bn" if kb_json.bangla_ratio(ch) > 0.3 else "en", indexed_at=now))
        if not texts:
            warn("Text too short to chunk.")
            return "failed"

        embs: List[List[float]] = []
        for i in range(0, len(texts), BATCH_INSERT):
            embs += self.embed_passages(texts[i:i + BATCH_INSERT])
            progress(min(i + BATCH_INSERT, len(texts)), len(texts), "  embedding")

        for d in replaced:
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
            have.update(self.col.get(ids=ids[i:i + 500], include=[])["ids"])
        return have

    def add_json(self, path: Path) -> str:
        """Section-aware, resumable, integrity-checked. Returns 'added' | 'skipped' | 'failed'."""
        limit = getattr(self.cfg, "json_limit", None)
        doc_id = kb_json.kb_doc_id(path, limit)
        for d in self.docs():
            if d["doc_id"] == doc_id and d["expected"] and d["chunks"] >= d["expected"]:
                return "skipped"

        print(f"\n  {bold(path.name)}  {dim(human_size(path.stat().st_size))}"
              + (f"  {dim(f'(first {limit} records)')}" if limit else ""))
        info("Parsing, chunking and verifying…")
        try:
            _, chunks, st = kb_json.build_kb_chunks(
                path, self.cfg.json_chunk_size, self.cfg.overlap, kb_pdf.chunk_text, limit=limit,
                source_name=path.name, fmt=getattr(self.cfg, "json_format", "auto"),
                strict=not getattr(self.cfg, "no_strict_index", False),
                progress_cb=lambda n: print(f"\r  {dim(f'processed {n:,} records')}", end="", flush=True))
        except kb_json.KbIntegrityError as e:
            print()
            err("Integrity check FAILED — nothing was indexed:")
            for p in e.problems[:10]:
                err(f"  {p}")
            if len(e.problems) > 10:
                err(f"  … and {len(e.problems) - 10} more")
            return "failed"
        print()
        if not chunks:
            warn("No usable records found in the file.")
            return "failed"
        info(f"format={st.get('format')} · {st['records']:,} records → {st['acts']:,} acts · "
             f"{st['section_chunks']:,} section + {st['act_chunks']:,} overview chunks · "
             f"deduped {st['deduped']:,} · empty {st['skipped_empty']:,} · "
             f"bn {st['bn_chunks']:,} / en {st['en_chunks']:,} · repealed {st['repealed_chunks']:,}")
        for w in st["warnings"][:8]:
            warn(w)
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
    def _where(scope, exclude_repealed, kinds, act_uids) -> Optional[dict]:
        conds = []
        if scope:
            conds.append({"doc_id": scope[0]} if len(scope) == 1 else {"doc_id": {"$in": scope}})
        if exclude_repealed:
            conds.append({"repealed": False})
        if kinds:
            conds.append({"kind": {"$in": list(kinds)}})
        if act_uids:
            conds.append({"act_uid": {"$in": list(act_uids)}})
        if not conds:
            return None
        return conds[0] if len(conds) == 1 else {"$and": conds}

    @staticmethod
    def _allowed(c, scope, exclude_repealed, kinds, act_uids) -> Optional[set]:
        if not (scope or exclude_repealed or kinds or act_uids):
            return None
        if scope:
            allowed: set = set()
            for d in scope:
                allowed.update(c["positions"].get(d, []))
        else:
            allowed = set(range(len(c["ids"])))
        metas = c["metas"]
        if exclude_repealed:
            allowed = {i for i in allowed if not metas[i].get("repealed", False)}
        if kinds:
            allowed = {i for i in allowed if metas[i].get("kind", "page") in kinds}
        if act_uids:
            allowed = {i for i in allowed if metas[i].get("act_uid") in act_uids}
        return allowed

    def search(self, query: Union[str, List[str]], top_k: int, scope: Optional[List[str]],
               use_rerank: bool, exclude_repealed: bool = False, kinds: Optional[List[str]] = None,
               act_uids: Optional[List[str]] = None, trace: Optional[dict] = None) -> List[dict]:
        """query may be a list [primary, translated…]; all are fused with RRF.
        trace (optional dict) receives 'pre' (id, fused score) and 'post' (id, final score)."""
        queries = [query] if isinstance(query, str) else [q for q in query if q and q.strip()]
        if not queries:
            return []
        primary = queries[0]
        c = self._load()
        if not c["ids"]:
            return []
        allowed = self._allowed(c, scope, exclude_repealed, kinds, act_uids)
        if allowed is not None and not allowed:
            return []
        n_cand = min(self.cfg.candidates, len(allowed) if allowed is not None else len(c["ids"]))
        where = self._where(scope, exclude_repealed, kinds, act_uids)

        fused: Dict[str, float] = defaultdict(float)
        dense_s: Dict[str, float] = {}
        lex_s: Dict[str, float] = {}
        for q in queries:
            kw = dict(query_embeddings=[self.embed_query(q)], n_results=n_cand, include=["distances"])
            if where:
                kw["where"] = where
            res = self.col.query(**kw)
            for r, (cid, dist) in enumerate(zip(res["ids"][0], res["distances"][0]), 1):
                fused[cid] += 1.0 / (RRF_K + r)
                dense_s[cid] = max(dense_s.get(cid, 0.0), 1 - dist)
            if c["bm25"]:
                for r, (i, s) in enumerate(c["bm25"].search(q, allowed, n_cand), 1):
                    cid = c["ids"][i]
                    fused[cid] += 1.0 / (RRF_K + r)
                    lex_s[cid] = max(lex_s.get(cid, 0.0), s)

        ranked = sorted(fused, key=lambda x: -fused[x])
        pool = ranked[: max(top_k * 4, 16)]
        sid_of = lambda cid: c["metas"][c["pos_by_id"][cid]].get("sid") or cid  # noqa: E731
        if trace is not None:
            trace["pre"] = [(sid_of(cid), round(fused[cid], 5)) for cid in pool]

        cands = []
        for cid in pool:
            i = c["pos_by_id"][cid]
            m = c["metas"][i]
            cands.append(dict(
                id=cid, sid=m.get("sid") or cid, text=c["texts"][i], source=m["source"],
                doc_id=m["doc_id"], act_uid=m.get("act_uid", ""), section_id=m.get("section_id", 0),
                sno=m.get("sno", ""), chash=m.get("chash", ""), kind=m.get("kind", "page"),
                published=m.get("published", ""), repealed=bool(m.get("repealed", False)),
                page=m.get("page", 0), label=m.get("loc") or f"{m['source']}, p.{m.get('page', '?')}",
                dense=round(dense_s.get(cid, 0.0), 3), bm25=round(lex_s.get(cid, 0.0), 2),
                fused=fused[cid], score=fused[cid]))

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

        # post-rerank dedupe: (act, section, content hash) + near-duplicate text
        final: List[dict] = []
        seen: set = set()
        for x in cands:
            key = (x["act_uid"], x["section_id"], x["chash"] or x["id"]) if x["act_uid"] \
                else (x["doc_id"], x["chash"] or x["id"])
            if key in seen:
                continue
            if any(x["label"] == y["label"] and _jaccard(x["text"], y["text"]) > 0.7 for y in final):
                continue
            seen.add(key)
            final.append(x)
            if len(final) >= top_k:
                break
        if trace is not None:
            trace["post"] = [(x["sid"], round(x["score"], 4)) for x in final]
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

    @staticmethod
    def _note_usage(usage: Optional[Counter], resp) -> None:
        u = getattr(resp, "usage", None)
        if usage is not None and u is not None:
            usage["prompt"] += getattr(u, "prompt_tokens", 0) or 0
            usage["completion"] += getattr(u, "completion_tokens", 0) or 0

    def _short(self, system: str, user: str, usage: Optional[Counter] = None) -> str:
        r = self._create(model=self.model, temperature=0, max_tokens=400,
                         messages=[{"role": "system", "content": system},
                                   {"role": "user", "content": user}], **self._extra("low"))
        self._note_usage(usage, r)
        return (r.choices[0].message.content or "").strip().strip('"')

    def rewrite(self, question: str, history: List[dict], usage: Optional[Counter] = None) -> str:
        if not history:
            return question
        convo = "\n".join(f"{t['role']}: {t['content'][:400]}" for t in history[-4:])
        try:
            out = self._short(REWRITE_PROMPT, f"HISTORY:\n{convo}\n\nLATEST: {question}", usage)
            return out if 3 <= len(out) <= 300 else question
        except Exception:  # noqa: BLE001
            return question

    def expand(self, query: str, usage: Optional[Counter] = None) -> str:
        try:
            out = self._short(EXPAND_PROMPT, query, usage)
            return out if 3 <= len(out) <= 300 else ""
        except Exception:  # noqa: BLE001
            return ""

    def answer(self, messages: List[dict], usage: Optional[Counter] = None) -> str:
        r = self._create(model=self.model, messages=messages, temperature=0.1,
                         max_tokens=2048, **self._extra("medium"))
        self._note_usage(usage, r)
        return (r.choices[0].message.content or "").strip()

    def answer_stream(self, messages: List[dict], usage: Optional[Counter] = None) -> str:
        stream = self._create(model=self.model, messages=messages, temperature=0.1,
                              max_tokens=2048, stream=True, **self._extra("medium"))
        buf: List[str] = []
        at_line_start = True
        got_usage = False
        for ev in stream:
            u = getattr(ev, "usage", None) or getattr(getattr(ev, "x_groq", None), "usage", None)
            if u is not None and usage is not None:
                usage["prompt"] += getattr(u, "prompt_tokens", 0) or 0
                usage["completion"] += getattr(u, "completion_tokens", 0) or 0
                got_usage = True
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
        text = "".join(buf).strip()
        if usage is not None and not got_usage:             # provider sent no usage: estimate
            usage["prompt"] += sum(len(m["content"]) for m in messages) // 4
            usage["completion"] += len(text) // 4
            usage["estimated"] = 1
        return text


def build_context(chunks: List[dict]) -> str:
    """Structured source blocks. The LLM cites SOURCE_ID; <<S#>> is a display alias only."""
    blocks = []
    for i, c in enumerate(chunks, 1):
        text = c["text"]
        if not text.startswith("SOURCE_ID:"):               # PDF chunks: add the same header shape
            text = (f"SOURCE_ID: {c['sid']}\nDOCUMENT: {c['source']}\nLOCATION: {c['label']}\n---\n{text}")
        blocks.append(f"<<S{i}>>\n{text}")
    return "\n\n".join(blocks)


# ── Pipeline (shared by CLI, server and eval) ──────────────────────────────

NO_INFO = {"en": "I don't have enough information in the documents to answer that.",
           "bn": "এই প্রশ্নের উত্তর দেওয়ার মতো পর্যাপ্ত তথ্য নথিগুলোতে নেই।"}

REPAIR_PROMPT = """\
You fix the citations of a draft ANSWER using the CONTEXT. Rules:
- Every factual sentence must end with at least one citation using ONLY a SOURCE_ID from ALLOWED_IDS, in square brackets.
- Remove or soften any claim that is not supported by the source it cites; remove citations not in ALLOWED_IDS.
- Never cite by rank (S1, S2). Keep the answer's language and meaning. Output ONLY the corrected answer."""


class RagPipeline:
    """retrieve() -> generate() -> validate/repair -> log. Thread-safe if `lock` guards the Store."""

    def __init__(self, store: "Store", llm: "LLM", cfg: argparse.Namespace,
                 qlog: Optional[L.QueryLogger] = None, lock=None):
        self.store, self.llm, self.cfg = store, llm, cfg
        self.qlog = qlog or L.QueryLogger(getattr(cfg, "log_path", None))
        self.lock = lock or contextlib.nullcontext()

    # ---- retrieval ----
    def retrieve(self, question: str, history: Optional[List[dict]] = None,
                 scope: Optional[List[str]] = None, top_k: Optional[int] = None,
                 rewrite: bool = True, expand: bool = True, rerank: bool = True,
                 exclude_repealed: bool = False) -> dict:
        t0 = time.time()
        usage: Counter = Counter()
        search_q = self.llm.rewrite(question, history, usage) if (rewrite and history) else question
        queries = [search_q]
        alt = self.llm.expand(search_q, usage) if expand else ""
        if alt and alt.strip().lower() != search_q.strip().lower():
            queries.append(alt)

        with self.lock:
            acts = self.store.acts()
        named = L.detect_acts(f"{question} {search_q}", acts)
        intent = L.classify_intent(question)
        k = L.route_top_k(intent, top_k)
        kinds = ["act"] if intent == "metadata" else None
        act_filter = named if (intent == "metadata" and named) else None

        stage: dict = {}
        with self.lock:
            chunks = self.store.search(queries, k, scope, rerank, exclude_repealed,
                                       kinds=kinds, act_uids=act_filter, trace=stage)
            if not chunks and (kinds or act_filter):
                stage = {}
                chunks = self.store.search(queries, k, scope, rerank, exclude_repealed, trace=stage)
            if not chunks and named:
                stage = {}
                chunks = self.store.search(queries, k, scope, rerank, exclude_repealed,
                                           act_uids=named, trace=stage)

        notes: List[str] = []
        if named and chunks:
            present = {c["act_uid"] for c in chunks}
            top_act = chunks[0]["act_uid"]
            for u in named:                                  # every named act gets at least 2 chunks
                if u not in present:
                    with self.lock:
                        extra = self.store.search(queries, 2, scope, rerank, exclude_repealed, act_uids=[u])
                    have = {c["id"] for c in chunks}
                    chunks += [e for e in extra if e["id"] not in have]
            if top_act not in named:
                nm = ", ".join(f"{acts[u]['title']} [{u}]" for u in named)
                other = ", ".join(sorted({f"{c['source']} [{c['act_uid']}]" for c in chunks
                                          if c["act_uid"] and c["act_uid"] not in named}))
                notes.append(f"ACT_MISMATCH: the question names {nm}, but the best-matching provisions come "
                             f"from {other or 'another act'}. If the provision asked about is not in the named "
                             f"act, say so explicitly, name the correct act and section, and cite its SOURCE_ID.")
            for s in L.detect_sections(question):
                for u in named:
                    a = acts.get(u)
                    if a and a["sections"] and s != "1" and s not in a["sections"]:
                        notes.append(
                            f"ACT_FACT: the indexed text of {a['title']} [{u}] has printed sections "
                            f"{L.compact_sections(a['sections'])}"
                            + (f" (declared total {a['nsec']})" if a["nsec"] else "")
                            + f"; there is NO section {s} in it. Do not describe a section {s} for this act; "
                              f"say it does not exist and, if the provision is in another retrieved act, name that "
                              f"act and section and cite it.")

        return dict(qid=uuid.uuid4().hex[:12], question=question, language=L.detect_language(question),
                    search_query=search_q, expanded=queries[1] if len(queries) > 1 else "",
                    intent=intent, top_k=k, acts_named=named, notes=notes, chunks=chunks,
                    pre=stage.get("pre", []), post=[(c["sid"], round(c["score"], 4)) for c in chunks],
                    usage=usage, t_retrieve=time.time() - t0)

    # ---- generation ----
    def generate(self, question: str, history: Optional[List[dict]], tr: dict, stream: bool = False) -> dict:
        chunks = tr["chunks"]
        alias = {i: c["sid"] for i, c in enumerate(chunks, 1)}
        lang = "Bengali (বাংলা)" if tr["language"] == "bn" else "English"
        notes = ("RETRIEVAL NOTES (from the system, not the user):\n" + "\n".join(f"- {n}" for n in tr["notes"])
                 + "\n\n") if tr["notes"] else ""
        user = (f"CONTEXT:\n{build_context(chunks)}\n\n{notes}ANSWER_LANGUAGE: {lang}\n"
                f"ALLOWED_IDS: {', '.join(alias.values())}\n\nQUESTION:\n{question}")
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages += (history or [])[-self.cfg.history_turns:]
        messages.append({"role": "user", "content": user})

        t0 = time.time()
        usage = tr["usage"]
        raw = self.llm.answer_stream(messages, usage) if stream else self.llm.answer(messages, usage)
        answer = L.canonicalize_citations(raw, alias)
        retrieved = {c["sid"] for c in chunks}
        val = L.validate_citations(answer, retrieved)
        repaired = False
        if answer and (val["invalid"] or val["uncited"]) and not stream and getattr(self.cfg, "cite_repair", True):
            try:
                fixed = self.llm.answer(
                    [{"role": "system", "content": REPAIR_PROMPT},
                     {"role": "user", "content": f"CONTEXT:\n{build_context(chunks)}\n\n"
                                                 f"ALLOWED_IDS: {', '.join(alias.values())}\n\nANSWER:\n{answer}"}],
                    usage)
                fixed = L.canonicalize_citations(fixed, alias)
                v2 = L.validate_citations(fixed, retrieved)
                if fixed and len(v2["invalid"]) + len(v2["uncited"]) < len(val["invalid"]) + len(val["uncited"]):
                    answer, val, repaired = fixed, v2, True
            except Exception:  # noqa: BLE001
                pass
        return dict(answer=answer, citations=val, repaired=repaired, t_generate=time.time() - t0)

    # ---- end-to-end ----
    def run(self, question: str, history: Optional[List[dict]] = None, scope: Optional[List[str]] = None,
            top_k: Optional[int] = None, rewrite: bool = True, expand: bool = True, rerank: bool = True,
            exclude_repealed: bool = False, stream: bool = False, on_retrieved=None) -> dict:
        t0 = time.time()
        tr = self.retrieve(question, history, scope, top_k, rewrite, expand, rerank, exclude_repealed)
        if on_retrieved:
            on_retrieved(tr)
        if not tr["chunks"]:
            gen = dict(answer=NO_INFO[tr["language"]], citations=dict(valid=[], invalid=[], uncited=[]),
                       repaired=False, t_generate=0.0)
        else:
            gen = self.generate(question, history, tr, stream=stream)
        res = dict(tr, **gen, t_total=time.time() - t0)
        self._log(res)
        return res

    def _log(self, res: dict) -> None:
        u = dict(res["usage"])
        u["total"] = u.get("prompt", 0) + u.get("completion", 0)
        self.qlog.log(dict(
            qid=res["qid"], question=res["question"], language=res["language"],
            rewrite=res["search_query"] if res["search_query"] != res["question"] else "",
            expanded=res["expanded"], intent=res["intent"], top_k=res["top_k"],
            acts_named=res["acts_named"], notes=res["notes"],
            retrieved_pre_rerank=[{"id": i, "score": s} for i, s in res["pre"]],
            retrieved_post_rerank=[{"id": i, "score": s} for i, s in res["post"]],
            final_citations=res["citations"]["valid"], invalid_citations=res["citations"]["invalid"],
            uncited_sentences=len(res["citations"]["uncited"]), repaired=res["repaired"], tokens=u,
            latency_ms=dict(retrieve=int(res["t_retrieve"] * 1000), generate=int(res["t_generate"] * 1000),
                            total=int(res["t_total"] * 1000))))


def default_cfg(**over) -> argparse.Namespace:
    """Config used by server / eval / tests (same defaults as the CLI)."""
    base = dict(DEFAULTS)
    base.update(no_rerank=False, no_rewrite=False, no_expand=False, exclude_repealed=False, debug=False,
                json_limit=None, json_format="auto", no_strict_index=False, cite_repair=True,
                log_path="logs/query.jsonl", top_k_override=None)
    base.update(over)
    return argparse.Namespace(**base)


# ── Chat session ────────────────────────────────────────────────────────────

class Session:
    def __init__(self, store: Store, llm: LLM, cfg: argparse.Namespace):
        self.store, self.llm, self.cfg = store, llm, cfg
        self.pipe = RagPipeline(store, llm, cfg)
        self.history: List[dict] = []
        self.log: List[Tuple[str, str, List[dict]]] = []
        self.scope: List[str] = []
        self.top_k = cfg.top_k_override          # None = route by intent
        self.rerank = not cfg.no_rerank
        self.rewrite = not cfg.no_rewrite
        self.expand = not cfg.no_expand
        self.exclude_repealed = cfg.exclude_repealed
        self.debug = cfg.debug

    def _on_retrieved(self, tr: dict) -> None:
        if tr["search_query"] != tr["question"]:
            print(dim(f"  ↳ searching for: {tr['search_query']}"))
        if tr["expanded"]:
            print(dim(f"  ↳ also searching: {tr['expanded']}"))
        print(dim(f"  ↳ intent={tr['intent']} top_k={tr['top_k']}"
                  + (f" acts={','.join(tr['acts_named'])}" if tr["acts_named"] else "")))
        for n in tr["notes"]:
            print(yellow(f"  ! {n[:160]}"))
        if self.debug:
            self._print_debug(tr["chunks"], tr["t_retrieve"])
        if tr["chunks"]:
            print("\n" + cyan("─" * term_width()))

    def ask(self, question: str) -> None:
        if self.store.count() == 0:
            warn("Index is empty. Use /add <pdf|json|folder> first.")
            return
        try:
            res = self.pipe.run(question, self.history, self.scope or None, self.top_k, self.rewrite,
                                self.expand, self.rerank, self.exclude_repealed, stream=True,
                                on_retrieved=self._on_retrieved)
        except KeyboardInterrupt:
            print(yellow("\n  [interrupted]"))
            return
        except Exception as e:  # noqa: BLE001
            print()
            err(f"Error: {type(e).__name__}: {e}")
            return
        if not res["chunks"]:
            warn(res["answer"])
            return
        self._print_sources(res)
        print(dim(f"  retrieval {res['t_retrieve']:.2f}s · {len(res['chunks'])} chunks · "
                  f"{'rerank on' if self.rerank and self.store.reranker else 'rerank off'}"))
        print(cyan("─" * term_width()) + "\n")
        self.history += [{"role": "user", "content": question},
                         {"role": "assistant", "content": res["answer"]}]
        self.log.append((question, res["answer"], res["chunks"]))

    def _print_sources(self, res: dict) -> None:
        val = res["citations"]
        valid = set(val["valid"])
        shown = [(i, c) for i, c in enumerate(res["chunks"], 1)
                 if not valid or any(c["sid"] == v or c["sid"].startswith(v + "-") for v in valid)]
        print("\n  " + bold("Sources"))
        for i, c in shown:
            print(f"   {magenta('[' + c['sid'] + ']')} {dim(f'(S{i})')} {c['label']}"
                  + (red("  REPEALED") if c.get("repealed") else ""))
        if val["invalid"]:
            warn(f"Answer cites ids that were not retrieved: {', '.join(val['invalid'])}")
        if val["uncited"]:
            warn(f"{len(val['uncited'])} factual sentence(s) without a citation")

    def _print_debug(self, chunks: List[dict], t: float) -> None:
        print(dim(f"\n  [debug] retrieval {t:.2f}s"))
        for i, c in enumerate(chunks, 1):
            prev = c["text"][:90].replace("\n", " ")
            print(dim(f"   {i}. {c['sid']}  {c['label']}  dense={c['dense']}  "
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
        elif cmd == "acts":
            self.cmd_acts()
        elif cmd == "reset":
            if confirm("Wipe the ENTIRE index?"):
                self.store.wipe()
                self.scope = []
                ok("Index wiped.")
        elif cmd == "top":
            if arg.lower() in ("auto", ""):
                self.top_k = None
                ok("top-k = auto (routed by intent)")
            else:
                try:
                    self.top_k = max(1, min(20, int(arg)))
                    ok(f"top-k = {self.top_k} (fixed)")
                except ValueError:
                    warn("Usage: /top 8   or   /top auto")
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
            ("/acts", "list indexed acts and their stable-id prefix"),
            ("/add <path…>", "index PDFs / JSON / JSONL / folders / globs"),
            ("/remove <#|name…>", "delete documents from the index"),
            ("/use <#…>", "restrict search to these documents  (/all to undo)"),
            ("/all", "search all documents"),
            ("/stats", "DB + settings overview"),
            ("/reset", "wipe the whole index"),
            ("/top N|auto", "fixed top-k, or route by intent"),
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

    def cmd_acts(self) -> None:
        acts = self.store.acts()
        if not acts:
            warn("No acts indexed (acts come from JSON knowledge bases).")
            return
        print()
        for uid, a in sorted(acts.items()):
            print(f"   {magenta(f'{uid:<14}')} {a['title'][:60]:<60} "
                  f"secs {L.compact_sections(a['sections']) or '-'}")
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
            ("Acts", len(self.store.acts())),
            ("Chunks", f"{self.store.count():,}"),
            ("DB path", os.path.abspath(self.cfg.db_dir)),
            ("DB size", human_size(self.store.disk_usage())),
            ("Collection", self.cfg.collection),
            ("Embed model", self.cfg.embed_model),
            ("Reranker", f"{self.cfg.rerank_model} ({'on' if self.rerank else 'off'})"),
            ("LLM", self.cfg.chat_model),
            ("Chunk PDF / JSON", f"{self.cfg.chunk_size} / {self.cfg.json_chunk_size}  (overlap {self.cfg.overlap})"),
            ("Top-k", self.top_k or f"auto {L.INTENT_TOPK}"),
            ("Query rewrite", "on" if self.rewrite else "off"),
            ("Query expansion", "on" if self.expand else "off"),
            ("Repealed laws", "excluded" if self.exclude_repealed else "included"),
            ("Query log", self.cfg.log_path or "off"),
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
            lines += [f"- [{c['sid']}] {c['label']}" for c in ch]
            lines.append("")
        Path(fn).write_text("\n".join(lines), encoding="utf-8")
        ok(f"Saved to {fn}")

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
    p = argparse.ArgumentParser(description="PDF + JSON RAG v4 — multilingual hybrid-search terminal chat")
    p.add_argument("paths", nargs="*", help="PDF / JSON / JSONL files, folders or glob patterns")
    p.add_argument("--pdf", action="append", default=[], help="(compat) PDF path, repeatable")
    p.add_argument("--json", action="append", default=[], help="JSON/JSONL knowledge-base file, repeatable")
    p.add_argument("--dir", action="append", default=[], help="Folder (recursive), repeatable")
    p.add_argument("--json-limit", type=int, default=None, help="Index only the first N JSON records")
    p.add_argument("--format", dest="json_format", choices=kb_json.FORMATS, default="auto",
                   help="JSON layout: auto | json | jsonl | concatenated")
    p.add_argument("--no-strict-index", action="store_true",
                   help="Skip chunks that fail integrity checks instead of failing the build")
    p.add_argument("--reset", action="store_true", help="Wipe the index before indexing")
    p.add_argument("--ask", help="Ask one question and exit")
    p.add_argument("--debug", action="store_true", help="Show retrieved chunks and scores")
    p.add_argument("--top-k", dest="top_k_override", type=int, default=None,
                   help="Fixed top-k (default: routed by question intent)")
    p.add_argument("--no-rerank", action="store_true")
    p.add_argument("--no-rewrite", action="store_true")
    p.add_argument("--no-expand", action="store_true", help="Disable Bangla<->English query expansion")
    p.add_argument("--no-cite-repair", dest="cite_repair", action="store_false",
                   help="(server/eval) disable the citation repair pass")
    p.add_argument("--exclude-repealed", action="store_true", help="Hide repealed laws from search")
    p.add_argument("--log", dest="log_path", default="logs/query.jsonl", help="Query log path ('' to disable)")
    p.add_argument("--db", dest="db_dir", default=d["db_dir"])
    p.add_argument("--collection", default=d["collection"])
    p.add_argument("--model", dest="chat_model", default=d["chat_model"])
    p.add_argument("--embed-model", default=d["embed_model"])
    p.add_argument("--rerank-model", default=d["rerank_model"])
    p.add_argument("--chunk-size", type=int, default=d["chunk_size"])
    p.add_argument("--json-chunk-size", type=int, default=d["json_chunk_size"])
    p.add_argument("--overlap", type=int, default=d["overlap"])
    p.set_defaults(candidates=d["candidates"], history_turns=d["history_turns"], top_k=d["top_k"])
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

    raw = list(cfg.paths) + list(cfg.pdf) + list(cfg.json) + list(cfg.dir)
    paths = resolve_kb_paths(raw) if raw else []
    if paths:
        info(f"{len(paths)} file(s) to process")
        store.add_many(paths)

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
    print(f"  {green('✅ Ready')} — {len(docs)} document(s), {len(store.acts())} act(s), "
          f"{store.count():,} chunks.  Type a question, or {cyan('/help')}.")
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
