#!/usr/bin/env python3
"""
==========================================================================
  kb_json.py — JSON / JSONL knowledge-base loader for rag.py  (v4)
==========================================================================
  One record = one SECTION of an Act (bdlaws layout). Bangla + English.

  Guarantees
    * STABLE CHUNK IDS:  <ACTNO>-<YEAR>-s<section_id>[-<part>]
        e.g. V-1799-s3, XIX-1810-s4, V-1799-s0 (= act overview).
        `sN` is the record's own `section_id` (NOT the printed number —
        the printed number is shown in the SECTION header line).
        Two different acts sharing number+year get ".2", ".3" suffixes.
    * INTEGRITY (build fails, nothing is indexed):
        - every section chunk's (act, section_id) exists in the source
        - the printed section number must be traceable to the source
          section_name (never invented, e.g. no "16A")
        - no two chunks share a stable id with different content
    * DEDUPE by (act, section_id, content_hash).
    * LOADER: json.loads -> JSONL -> raw_decode loop (concatenated JSON),
      or force one with --format auto|json|jsonl|concatenated.

  Dry run (no models):
      python kb_json.py bdlaws.jsonl --limit 100 --format auto
==========================================================================
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple

KB_EXTS = {".json", ".jsonl", ".ndjson"}
FORMATS = ("auto", "json", "jsonl", "concatenated")
STABLE_ID_RE = re.compile(r"^[A-Za-z0-9]+-\d{4}(?:\.\d+)?-s\d+(?:-\d+)?$")


class KbIntegrityError(Exception):
    def __init__(self, problems: List[str]):
        self.problems = list(problems)
        super().__init__(f"{len(self.problems)} integrity problem(s): " + "; ".join(self.problems[:5]))


# ── Bangla helpers ──────────────────────────────────────────────────────────

_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


def normalize_digits(s: str) -> str:
    return s.translate(_BN_DIGITS)


def bangla_ratio(s: str) -> float:
    letters = [ch for ch in s if ch.isalpha() or "\u0980" <= ch <= "\u09ff"]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if "\u0980" <= ch <= "\u09ff") / len(letters)


# ── Reading ─────────────────────────────────────────────────────────────────

_SKIP = " \t\r\n,\ufeff"


def read_kb_file(path: Path) -> Tuple[str, str]:
    raw = Path(path).read_bytes()
    return hashlib.sha1(raw).hexdigest()[:12], raw.decode("utf-8-sig")


def kb_doc_id(path: Path, limit: Optional[int] = None) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:12] + (f"L{limit}" if limit else "")


def _records_from_value(obj) -> List[dict]:
    if isinstance(obj, list):
        return [o for o in obj if isinstance(o, dict)]
    if isinstance(obj, dict):
        for v in obj.values():                      # {"data": [...]} wrappers
            if isinstance(v, list) and v and isinstance(v[0], dict) and \
                    ("section_text" in v[0] or "act_name" in v[0]):
                return [o for o in v if isinstance(o, dict)]
        return [obj]
    return []


def _try_jsonl(text: str, stats: dict, lenient: bool) -> Optional[List[dict]]:
    out: List[dict] = []
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if not lenient:
                return None
            stats["bad_records"] = stats.get("bad_records", 0) + 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        elif not lenient:
            return None
    return out


def _iter_concat(text: str, stats: dict) -> Iterator[dict]:
    """Concatenated JSON values (pretty-printed objects back to back)."""
    dec = json.JSONDecoder()
    n, i = len(text), 0
    while True:
        while i < n and text[i] in _SKIP:
            i += 1
        if i >= n:
            break
        if text[i] in "[]":
            i += 1
            continue
        try:
            obj, i = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            stats["bad_records"] = stats.get("bad_records", 0) + 1
            nl = text.find("\n", i)
            if nl == -1:
                break
            i = nl + 1
            continue
        if isinstance(obj, dict):
            yield obj


def iter_records(text: str, limit: Optional[int] = None, stats: Optional[dict] = None,
                 fmt: str = "auto") -> Iterator[dict]:
    """json.loads -> JSONL -> raw_decode loop. `stats['format']` reports what was used."""
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {FORMATS}")
    stats = stats if stats is not None else {}
    src = None
    if fmt in ("auto", "json"):
        try:
            src = iter(_records_from_value(json.loads(text)))
            stats["format"] = "json"
        except json.JSONDecodeError:
            if fmt == "json":
                raise
    if src is None and fmt in ("auto", "jsonl"):
        recs = _try_jsonl(text, stats, lenient=(fmt == "jsonl"))
        if recs is not None:
            src, stats["format"] = iter(recs), "jsonl"
    if src is None:
        src, stats["format"] = _iter_concat(text, stats), "concatenated"
    yield from (islice(src, limit) if limit else src)


# ── Cleaning ────────────────────────────────────────────────────────────────

_FOOT_REF = re.compile(r"[0-9০-৯]+\[([^\[\]]*)\]")
_REF_NUM = re.compile(r"([0-9০-৯]+)\[")
_PLACEHOLDER = re.compile(r"\[\*+\]")
_MARK = re.compile(r"[0-9০-৯]*♠")
_NOTE = re.compile(r"^\s*\[([0-9০-৯]+)\]\s*(.*)$", re.S)
# printed number is read ONLY from section_name; letters only if attached in the source
_SEC_NO = re.compile(r"^\s*([0-9০-৯]+(?:[A-Za-z]{1,2})?)\s*[.\u0964\u0965\u09f7:\-–)]")


def _unwrap(m: re.Match) -> str:
    inner = m.group(1)
    return "" if not inner.strip(" *\t") else inner


def clean_legal(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r", "").replace("\u00a0", " ").replace("\u200b", "")
    text = _MARK.sub("", text)
    for _ in range(3):
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
    if name.isupper():
        name = re.sub(r"(?<=\w)'S\b", "'s", name.title())
    return name


def section_number(section_name: str) -> str:
    m = _SEC_NO.match(section_name or "")
    return normalize_digits(m.group(1)) if m else ""


def _meaningful(body: str) -> bool:
    return len(re.sub(r"[\W\d_]+", "", body)) >= 8


def _parse_notes(notes) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for n in notes or []:
        m = _NOTE.match(str(n))
        if m:
            out[normalize_digits(m.group(1))] = clean_legal(m.group(2))
    return out


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", normalize_digits(s or ""))


def _year(date: str, title: str) -> str:
    for s in (date, title):
        m = re.search(r"(?<!\d)(\d{4})(?!\d)", normalize_digits(s or ""))
        if m:
            return m.group(1)
    return "0000"


def _chash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


# ── Integrity ───────────────────────────────────────────────────────────────

def verify_chunks(chunks: List[dict], source_pairs: Set[Tuple[str, int]]) -> List[str]:
    """Return a list of problems (empty = OK)."""
    problems: List[str] = []
    seen: Dict[str, str] = {}
    for ch in chunks:
        m = ch["meta"]
        sid = m.get("sid", "")
        if not STABLE_ID_RE.match(sid):
            problems.append(f"bad stable id {sid!r}")
        if m["kind"] == "section" and (m["act_uid"], m["section_id"]) not in source_pairs:
            problems.append(f"phantom chunk {sid}: ({m['act_uid']}, section_id={m['section_id']}) not in source records")
        if m["kind"] == "act" and m["section_id"] != 0:
            problems.append(f"overview chunk {sid} must use s0")
        if sid in seen and seen[sid] != m["chash"]:
            problems.append(f"stable id {sid} used by two different contents")
        seen[sid] = m["chash"]
    return problems


# ── Chunk building ──────────────────────────────────────────────────────────

ChunkFn = Callable[[str, int, int], List[str]]


def _header(sid: str, a: dict, section_line: str, extra: Tuple[str, ...] = ()) -> str:
    lines = [f"SOURCE_ID: {sid}",
             f"ACT: {a['title']}" + (f" (No. {a['no']})" if a["no"] else ""),
             f"SECTION: {section_line}"]
    if a["date"]:
        lines.append(f"PUBLISHED: {a['date']}")
    if a["repealed"]:
        lines.append("STATUS: REPEALED" + (f" — {a['notice']}" if a["notice"] else ""))
    lines += list(extra)
    lines.append("---")
    return "\n".join(lines)


def build_kb_chunks(
    path: Path, chunk_size: int, overlap: int, chunk_fn: ChunkFn,
    limit: Optional[int] = None, source_name: Optional[str] = None,
    progress_cb: Optional[Callable[[int], None]] = None,
    fmt: str = "auto", strict: bool = True,
) -> Tuple[str, List[dict], dict]:
    """Returns (doc_id, chunks, stats). chunk = dict(id, text, meta). Raises KbIntegrityError (strict)."""
    path = Path(path)
    sha, text = read_kb_file(path)
    doc_id = sha + (f"L{limit}" if limit else "")
    source_name = source_name or path.name
    now = datetime.now().isoformat(timespec="seconds")
    st: Counter = Counter()
    st.update(records=0, acts=0, section_chunks=0, act_chunks=0, skipped_empty=0, bad_records=0,
              bn_chunks=0, en_chunks=0, repealed_chunks=0, deduped=0, conflicts=0, synth_section_ids=0)
    warnings: List[str] = []

    # ---- pass 1: acts, stable uids, the set of real (act, section_id) pairs ----
    acts: Dict[tuple, dict] = {}
    uids: Dict[str, tuple] = {}
    counters: Counter = Counter()
    source_pairs: Set[Tuple[str, int]] = set()
    rows: List[Tuple[dict, dict, int]] = []
    for r in iter_records(text, limit, st, fmt):
        st["records"] += 1
        title = clean_title(r.get("act_name", "")) or "Unknown Act"
        no = normalize_digits(str(r.get("act_no", "")).strip())
        date = clean_legal(str(r.get("published_date", "")))
        key = (title, no, date)
        a = acts.get(key)
        if a is None:
            base = f"{_slug(no) or 'X'}-{_year(date, title)}"
            uid, n = base, 1
            while uid in uids:
                n += 1
                uid = f"{base}.{n}"
            uids[uid] = key
            try:
                nsec = int(r.get("num_of_sections") or 0)
            except (TypeError, ValueError):
                nsec = 0
            a = dict(uid=uid, title=title, no=no, date=date, year=int(_year(date, title)),
                     repealed=bool(r.get("repealed", False)),
                     notice=clean_legal(str(r.get("repealed_notice", ""))),
                     notes=_parse_notes(r.get("notes")),
                     volume=clean_legal(str(r.get("volume_name", "")))[:120],
                     nsec=nsec, overview_done=False)
            acts[key] = a
            st["acts"] += 1
        try:
            secnum = int(r.get("section_id"))
        except (TypeError, ValueError):
            counters[a["uid"]] += 1
            secnum = counters[a["uid"]]
            st["synth_section_ids"] += 1
        source_pairs.add((a["uid"], secnum))
        rows.append((r, a, secnum))

    # ---- pass 2: chunks ----
    chunks: List[dict] = []
    used: Dict[str, str] = {}
    seen_keys: Set[tuple] = set()
    problems: List[str] = []

    def add(sid: str, uid: str, section_id: int, body: str, header: str, kind: str,
            label: str, a: dict, sno: str, sname: str) -> None:
        h = _chash(body)
        if sno and not normalize_digits(sname).startswith(sno):
            problems.append(f"{sid}: printed number {sno!r} not traceable to source section_name {sname!r}")
            return
        if (uid, section_id, h) in seen_keys:
            st["deduped"] += 1
            return
        if sid in used and used[sid] != h:
            st["conflicts"] += 1
            problems.append(f"{sid}: same id, different content (conflicting duplicate records)")
            return
        seen_keys.add((uid, section_id, h))
        used[sid] = h
        lang = "bn" if bangla_ratio(body) > 0.3 else "en"
        st["bn_chunks" if lang == "bn" else "en_chunks"] += 1
        if a["repealed"]:
            st["repealed_chunks"] += 1
        st["section_chunks" if kind == "section" else "act_chunks"] += 1
        chunks.append(dict(id=f"{doc_id}:{sid}", text=header + "\n" + body, meta=dict(
            doc_id=doc_id, doc_name=source_name, source=a["title"], path=str(path), page=0,
            total_pages=0, loc=label, kind=kind, sid=sid, act_uid=uid, act_no=a["no"],
            year=a["year"], published=a["date"], volume=a["volume"], section_id=section_id,
            sno=sno, chash=h, nsec=a["nsec"], repealed=a["repealed"], lang=lang, indexed_at=now)))

    for n_row, (r, a, secnum) in enumerate(rows, 1):
        uid = a["uid"]
        if not a["overview_done"]:
            a["overview_done"] = True
            body = "\n".join(p for p in (clean_legal(str(r.get("short_title", ""))),
                                         clean_legal(str(r.get("preamble", "")))) if p)
            if body:
                pieces = chunk_fn(body, chunk_size, overlap) or [body]
                for k, piece in enumerate(pieces):
                    sid = f"{uid}-s0" + (f"-{k + 1}" if len(pieces) > 1 else "")
                    sline = "Overview / preamble" + (f" (part {k + 1}/{len(pieces)})" if len(pieces) > 1 else "")
                    extra = (f"SECTIONS_TOTAL: {a['nsec']}",) if a["nsec"] else ()
                    add(sid, uid, 0, piece, _header(sid, a, sline, extra), "act",
                        f"{a['title'][:80]} — overview", a, "", "")

        raw_text, raw_name = str(r.get("section_text", "")), str(r.get("section_name", ""))
        body = clean_legal(raw_text)
        sname = re.sub(r"\s+", " ", clean_legal(raw_name)).strip()
        if not _meaningful(body):
            st["skipped_empty"] += 1
            continue
        refs = sorted({normalize_digits(x) for x in _REF_NUM.findall(raw_text + " " + raw_name)})
        notes = [f"[{n}] {a['notes'][n][:300]}" for n in refs if n in a["notes"]][:3]
        if notes:
            body += "\n\nAmendment notes:\n" + "\n".join(notes)
        sno = section_number(sname)
        label = f"{a['title'][:80]}, " + (f"Sec. {sno}" if sno else (sname[:60] or "section"))
        pieces = chunk_fn(body, chunk_size, overlap) or [body]
        for k, piece in enumerate(pieces):
            sid = f"{uid}-s{secnum}" + (f"-{k + 1}" if len(pieces) > 1 else "")
            sline = (sname or body[:60]) + (f" (part {k + 1}/{len(pieces)})" if len(pieces) > 1 else "")
            add(sid, uid, secnum, piece, _header(sid, a, sline), "section", label, a, sno, sname)
        if progress_cb and n_row % 2000 == 0:
            progress_cb(n_row)

    problems += verify_chunks(chunks, source_pairs)
    if problems:
        if strict:
            raise KbIntegrityError(problems)
        warnings += [f"skipped: {p}" for p in problems]

    if limit is None:                                   # declared vs indexed section counts
        got: Dict[str, Set[int]] = {}
        for ch in chunks:
            if ch["meta"]["kind"] == "section":
                got.setdefault(ch["meta"]["act_uid"], set()).add(ch["meta"]["section_id"])
        for a in acts.values():
            if a["nsec"] and len(got.get(a["uid"], ())) != a["nsec"]:
                warnings.append(f"{a['uid']}: num_of_sections={a['nsec']} but {len(got.get(a['uid'], ()))} indexed")

    for ch in chunks:
        ch["meta"]["expected"] = len(chunks)
        ch["meta"]["total_pages"] = st["acts"]
    out = dict(st)
    out["warnings"] = warnings
    return doc_id, chunks, out


# ── Standalone dry-run ──────────────────────────────────────────────────────

def simple_split(text: str, size: int, overlap: int) -> List[str]:
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
    ap = argparse.ArgumentParser(description="Dry-run the JSON KB chunker + integrity checks")
    ap.add_argument("file")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--format", choices=FORMATS, default="auto")
    ap.add_argument("--chunk-size", type=int, default=1000)
    ap.add_argument("--lenient", action="store_true", help="skip bad chunks instead of failing")
    ap.add_argument("--show", type=int, default=3)
    a = ap.parse_args()
    try:
        did, chs, stats = build_kb_chunks(Path(a.file), a.chunk_size, 120, simple_split, limit=a.limit,
                                          fmt=a.format, strict=not a.lenient)
    except KbIntegrityError as e:
        print("INTEGRITY CHECK FAILED:")
        for p in e.problems[:20]:
            print("  -", p)
        raise SystemExit(1)
    print(f"doc_id={did}")
    for k, v in stats.items():
        if k != "warnings":
            print(f"  {k:<18} {v}")
    print(f"  total_chunks       {len(chs)}")
    for w in stats["warnings"][:20]:
        print("  WARN", w)
    for ch in chs[: a.show]:
        print("\n" + "─" * 70 + f"\n{ch['meta']['sid']}  [{ch['meta']['loc']}]\n{ch['text'][:500]}")
