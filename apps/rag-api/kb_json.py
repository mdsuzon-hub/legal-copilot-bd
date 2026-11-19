#!/usr/bin/env python3
"""
==========================================================================
  kb_json.py — JSON / JSONL knowledge-base loader for rag_pdf.py
==========================================================================
  Built for the bdlaws dataset (one record = one SECTION of an Act, with
  Act-level fields repeated on every record; Bangla + English mixed).

  What it does
    * Reads .json (array), .jsonl / .ndjson, and concatenated pretty-printed
      JSON objects (your sample.jsonl format) with one streaming parser.
    * Cleans legal markup:  2[Bangladesh] -> Bangladesh,  1[***] -> (empty),
      "1♠An Act" -> "An Act",  NBSP / zero-width chars.
    * Chunks per SECTION (never mixes sections), and prepends a short header
      (Act, number, date, REPEALED status, section) to EVERY chunk, so both
      the embedding and BM25 see the Act name + section number.
    * Emits ONE "act overview" chunk per Act (short_title + preamble +
      section count) instead of repeating the preamble on 585 sections.
    * Attaches only the amendment notes that the section actually references
      (footnote markers like 2[...]).
    * Deterministic chunk ids  -> indexing is resumable / idempotent.

  Standalone dry-run (no models needed):
      python kb_json.py bdlaws.jsonl --limit 100
==========================================================================
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

KB_EXTS = {".json", ".jsonl", ".ndjson"}

# ── Bangla helpers ──────────────────────────────────────────────────────────

_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


def normalize_digits(s: str) -> str:
    """Bangla digits -> ASCII, so '১৯৯৪' matches '1994' in search."""
    return s.translate(_BN_DIGITS)


def bangla_ratio(s: str) -> float:
    letters = [ch for ch in s if ch.isalpha() or "\u0980" <= ch <= "\u09ff"]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if "\u0980" <= ch <= "\u09ff") / len(letters)


# ── Reading (streaming, format-agnostic) ───────────────────────────────────

_SKIP = " \t\r\n,\ufeff"


def read_kb_file(path: Path) -> Tuple[str, str]:
    """Return (content_hash, text)."""
    raw = Path(path).read_bytes()
    return hashlib.sha1(raw).hexdigest()[:12], raw.decode("utf-8-sig")


def kb_doc_id(path: Path, limit: Optional[int] = None) -> str:
    """Cheap doc id (hash of file bytes + limit). Same value build_kb_chunks uses."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:12] + (f"L{limit}" if limit else "")


def iter_records(text: str, limit: Optional[int] = None,
                 stats: Optional[dict] = None) -> Iterator[dict]:
    """Yield dict records from: JSON array | JSONL | concatenated JSON objects."""
    dec = json.JSONDecoder()
    n, i = len(text), 0
    while i < n and text[i] in _SKIP:
        i += 1
    in_array = i < n and text[i] == "["
    if in_array:
        i += 1
    emitted = 0
    while True:
        while i < n and text[i] in _SKIP:
            i += 1
        if i >= n:
            break
        if text[i] == "]":
            if in_array:
                break
            i += 1
            continue
        try:
            obj, i = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            if stats is not None:
                stats["bad_records"] = stats.get("bad_records", 0) + 1
            nl = text.find("\n", i)
            if nl == -1:
                break
            i = nl + 1
            continue
        if isinstance(obj, dict):
            yield obj
            emitted += 1
            if limit and emitted >= limit:
                break


# ── Cleaning ────────────────────────────────────────────────────────────────

_FOOT_REF = re.compile(r"[0-9০-৯]+\[([^\[\]]*)\]")      # 2[Bangladesh] -> Bangladesh
_REF_NUM = re.compile(r"([0-9০-৯]+)\[")                  # footnote markers
_PLACEHOLDER = re.compile(r"\[\*+\]")                    # [***]
_MARK = re.compile(r"[0-9০-৯]*♠")                        # 1♠An Act
_NOTE = re.compile(r"^\s*\[([0-9০-৯]+)\]\s*(.*)$", re.S)
_SEC_NO = re.compile(r"^\s*([0-9০-৯]+[A-Za-z]{0,3})\s*[.\u0964\u0965\u09f7:\-–)]")


def _unwrap(m: re.Match) -> str:
    inner = m.group(1)
    return "" if not inner.strip(" *\t") else inner


def clean_legal(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r", "").replace("\u00a0", " ").replace("\u200b", "")
    text = _MARK.sub("", text)
    for _ in range(3):                                   # nested footnote refs
        new = _FOOT_REF.sub(_unwrap, text)
        if new == text:
            break
        text = new
    text = _PLACEHOLDER.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_title(name: str) -> str:
    name = re.sub(r"\s+", " ", clean_legal(name or "")).strip()
    if name.isupper():                                   # 'THE PENAL CODE' -> 'The Penal Code'
        name = re.sub(r"(?<=\w)'S\b", "'s", name.title())
    return name


def section_number(name: str, text: str) -> str:
    for s in (name or "", text or ""):
        m = _SEC_NO.match(s)
        if m:
            return normalize_digits(m.group(1))
    return ""


def _meaningful(body: str) -> bool:
    return len(re.sub(r"[\W\d_]+", "", body)) >= 8


def _parse_notes(notes) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for n in notes or []:
        m = _NOTE.match(str(n))
        if m:
            out[normalize_digits(m.group(1))] = clean_legal(m.group(2))
    return out


# ── Chunk building ──────────────────────────────────────────────────────────

ChunkFn = Callable[[str, int, int], List[str]]


def _act_key(r: dict) -> str:
    return "|".join(str(r.get(k, "")).strip()
                    for k in ("volume_name", "act_name", "act_no", "published_date"))


def _act_header(a: dict) -> List[str]:
    bits = [x for x in (f"Act No. {a['no']}" if a["no"] else "", a["date"]) if x]
    lines = [f"Act: {a['title']}" + (f" ({'; '.join(bits)})" if bits else "")]
    if a["repealed"]:
        lines.append("Status: REPEALED" + (f" — {a['notice']}" if a["notice"] else ""))
    return lines


def build_kb_chunks(
    path: Path,
    chunk_size: int,
    overlap: int,
    chunk_fn: ChunkFn,
    limit: Optional[int] = None,
    source_name: Optional[str] = None,
    progress_cb: Optional[Callable[[int], None]] = None,
) -> Tuple[str, List[dict], dict]:
    """
    Returns (doc_id, chunks, stats).
    chunk = dict(id=str, text=str, meta=dict)   # meta values are str/int/bool only
    """
    path = Path(path)
    sha, text = read_kb_file(path)
    doc_id = sha + (f"L{limit}" if limit else "")
    source_name = source_name or path.name
    now = datetime.now().isoformat(timespec="seconds")

    st = Counter(records=0, acts=0, section_chunks=0, act_chunks=0, skipped_empty=0,
                 bad_records=0, bn_chunks=0, en_chunks=0, repealed_chunks=0)
    chunks: List[dict] = []
    acts: Dict[str, dict] = {}

    def add(cid: str, body: str, header: List[str], meta: dict) -> None:
        full = "\n".join(header + [body])
        lang = "bn" if bangla_ratio(body) > 0.3 else "en"
        st["bn_chunks" if lang == "bn" else "en_chunks"] += 1
        if meta["repealed"]:
            st["repealed_chunks"] += 1
        chunks.append(dict(id=cid, text=full, meta=dict(meta, lang=lang)))

    for idx, r in enumerate(iter_records(text, limit, st)):
        st["records"] += 1
        rid = r.get("id", idx)
        key = _act_key(r)
        akey = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]

        a = acts.get(key)
        if a is None:
            a = dict(
                akey=akey,
                title=clean_title(r.get("act_name", "")) or "Unknown Act",
                no=normalize_digits(str(r.get("act_no", "")).strip()),
                date=clean_legal(str(r.get("published_date", ""))),
                repealed=bool(r.get("repealed", False)),
                notice=clean_legal(str(r.get("repealed_notice", ""))),
                notes=_parse_notes(r.get("notes")),
                volume=clean_legal(str(r.get("volume_name", "")))[:120],
            )
            acts[key] = a
            st["acts"] += 1

            # ---- one overview chunk per Act (short title + preamble) ----
            parts = [clean_legal(str(r.get("short_title", ""))),
                     clean_legal(str(r.get("preamble", "")))]
            body = "\n".join(p for p in parts if p)
            nsec = r.get("num_of_sections")
            if body:
                if nsec:
                    body += f"\nTotal sections: {nsec}"
                pieces = chunk_fn(body, chunk_size, overlap) or [body]
                for k, piece in enumerate(pieces):
                    hdr = _act_header(a) + [
                        "Overview / preamble" + (f" (part {k + 1}/{len(pieces)})" if len(pieces) > 1 else "")]
                    add(f"{doc_id}_A{akey}_{k}", piece, hdr, dict(
                        doc_id=doc_id, doc_name=source_name, source=a["title"], path=str(path),
                        page=0, total_pages=0, loc=f"{a['title'][:80]} — overview",
                        kind="act", act_no=a["no"], published=a["date"], volume=a["volume"],
                        section_no="", repealed=a["repealed"], indexed_at=now))
                    st["act_chunks"] += 1

        # ---- section chunks ----
        raw_name = str(r.get("section_name", ""))
        raw_text = str(r.get("section_text", ""))
        body = clean_legal(raw_text)
        sname = re.sub(r"\s+", " ", clean_legal(raw_name)).strip()
        if not _meaningful(body):
            st["skipped_empty"] += 1
            continue

        refs = sorted({normalize_digits(x) for x in _REF_NUM.findall(raw_text + " " + raw_name)})
        notes = [f"[{n}] {a['notes'][n][:300]}" for n in refs if n in a["notes"]][:3]
        if notes:
            body += "\n\nAmendment notes:\n" + "\n".join(notes)

        sno = section_number(sname, body)
        label = f"{a['title'][:80]}, " + (f"Sec. {sno}" if sno else (sname[:60] or "section"))
        pieces = chunk_fn(body, chunk_size, overlap) or [body]
        for k, piece in enumerate(pieces):
            hdr = _act_header(a) + [
                f"Section: {sname}" + (f" (part {k + 1}/{len(pieces)})" if len(pieces) > 1 else "")]
            add(f"{doc_id}_{rid}_{k}", piece, hdr, dict(
                doc_id=doc_id, doc_name=source_name, source=a["title"], path=str(path),
                page=0, total_pages=0, loc=label, kind="section", act_no=a["no"],
                published=a["date"], volume=a["volume"], section_no=sno,
                repealed=a["repealed"], indexed_at=now))
            st["section_chunks"] += 1
        if progress_cb and st["records"] % 2000 == 0:
            progress_cb(st["records"])

    for ch in chunks:                       # lets the store detect half-finished indexing
        ch["meta"]["expected"] = len(chunks)
        ch["meta"]["total_pages"] = st["acts"]
    return doc_id, chunks, dict(st)


# ── Standalone dry-run ──────────────────────────────────────────────────────

def _simple_split(text: str, size: int, overlap: int) -> List[str]:
    units = [u.strip() for u in re.split(r"(?<=[.!?\u0964])\s+|\n+", text) if u.strip()]
    out, cur = [], ""
    for u in units:
        if cur and len(cur) + len(u) + 1 > size:
            out.append(cur)
            cur = u
        else:
            cur = (cur + " " + u).strip()
    if cur:
        out.append(cur)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Dry-run the JSON KB chunker")
    ap.add_argument("file")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--chunk-size", type=int, default=1000)
    ap.add_argument("--show", type=int, default=3, help="print N sample chunks")
    a = ap.parse_args()
    did, chs, stats = build_kb_chunks(Path(a.file), a.chunk_size, 120, _simple_split, limit=a.limit)
    print(f"doc_id={did}")
    for k, v in stats.items():
        print(f"  {k:<16} {v}")
    print(f"  total_chunks     {len(chs)}")
    for ch in chs[: a.show]:
        print("\n" + "─" * 70 + f"\n{ch['id']}  [{ch['meta']['loc']}]\n{ch['text'][:600]}")
