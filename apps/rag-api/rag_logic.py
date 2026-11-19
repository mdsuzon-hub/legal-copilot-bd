#!/usr/bin/env python3
"""
==========================================================================
  rag_logic.py — pure-python helpers (no models / no network)
  intent routing · act & section detection · citation checks · query log
==========================================================================
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

import kb_json

# ── Language ────────────────────────────────────────────────────────────────

def detect_language(text: str) -> str:
    return "bn" if kb_json.bangla_ratio(text) > 0.3 else "en"


# ── Intent routing ──────────────────────────────────────────────────────────

INTENT_TOPK = {"metadata": 2, "section": 3, "list": 6, "compare": 10, "general": 6}

_COMPARE = re.compile(
    r"\b(compare|comparison|contrast|differ(?:s|ence|ences|ent)?|versus|vs\.?|similarit\w+|"
    r"walk(?:\s+me)?\s+through|step[- ]by[- ]step|in detail)\b|তুলনা|পার্থক্য|ধাপে ধাপে|বিস্তারিত", re.I)
_LIST = re.compile(
    r"\b(?:which|what)\s+(?:all\s+)?(?:sections?|provisions?|clauses?)\b|\blist\b|\benumerate\b|"
    r"\ball\s+(?:the\s+)?(?:sections|provisions|cases|powers|duties)\b|কোন কোন ধারা|তালিকা", re.I)
_METADATA = re.compile(
    r"\bhow\s+many\s+(?:sections|clauses|provisions)\b|\bnumber\s+of\s+sections\b|\bpublished?\b|"
    r"\b(?:when|what\s+(?:date|year))\b.*\b(?:enacted|passed|promulgated|issued|made)\b|"
    r"\bshort\s+title\b|\b(?:act|regulation)\s+number\b|\bwhat\s+is\s+the\s+(?:title|name)\b|\bpreamble\b|"
    r"কয়টি ধারা|কতটি ধারা|কত ধারা|কবে প্রকাশ|প্রকাশিত|শিরোনাম", re.I)
_SECTION = re.compile(r"(?:\bsec(?:tion)?s?\.?|\bs\.)\s*([0-9০-৯]+[A-Za-z]?)\b|ধারা\s*([0-9০-৯]+)|([0-9০-৯]+)\s*ধারা", re.I)


def detect_sections(question: str) -> List[str]:
    out = []
    for m in _SECTION.finditer(question):
        s = next(g for g in m.groups() if g)
        s = kb_json.normalize_digits(s).upper()
        if s not in out:
            out.append(s)
    return out


def classify_intent(question: str) -> str:
    if _COMPARE.search(question):
        return "compare"
    if _LIST.search(question):
        return "list"
    if _METADATA.search(question):
        return "metadata"
    if detect_sections(question):
        return "section"
    return "general"


def route_top_k(intent: str, override: Optional[int] = None) -> int:
    if override:
        return max(1, min(20, int(override)))
    return INTENT_TOPK.get(intent, INTENT_TOPK["general"])


# ── Act / section detection ────────────────────────────────────────────────

_ROMAN = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
          (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
_ACT_REF = re.compile(
    r"\b(?:regulation|reg|act|ordinance|order)s?\.?\s*(?:no\.?\s*)?([ivxlcdm]+|\d{1,3})\s*(?:of|,|\s)\s*(\d{4})\b", re.I)
_ACT_REF_BN = re.compile(r"([০-৯0-9]{4})\s*সনের\s*([০-৯0-9]+)\s*(?:নং|নম্বর)")
_GENERIC = {"the", "regulation", "regulations", "act", "ordinance", "order", "bengal", "bangladesh",
            "east", "pakistan", "of", "and"}


def int_to_roman(n: int) -> str:
    out = ""
    for v, s in _ROMAN:
        while n >= v:
            out += s
            n -= v
    return out


def act_core(title: str) -> str:
    t = re.sub(r"[^\w\s\u0980-\u09ff]", " ", title.lower())
    t = re.sub(r"\b\d{4}\b", " ", t)
    return " ".join(w for w in t.split() if w not in _GENERIC)


def detect_acts(question: str, acts: Dict[str, dict]) -> List[str]:
    """Return uids (e.g. 'V-1799') of acts the question names, by number+year or by title."""
    found: List[str] = []

    def add(base: str) -> None:
        for uid in acts:
            if uid == base or uid.startswith(base + "."):
                if uid not in found:
                    found.append(uid)

    q = kb_json.normalize_digits(question)
    for m in _ACT_REF.finditer(q):
        no, year = m.group(1).upper(), m.group(2)
        add(f"{no}-{year}")
        if no.isdigit():
            add(f"{int_to_roman(int(no))}-{year}")
    for m in _ACT_REF_BN.finditer(question):
        add(f"{kb_json.normalize_digits(m.group(2))}-{kb_json.normalize_digits(m.group(1))}")
    qn = " ".join(re.sub(r"[^\w\s\u0980-\u09ff]", " ", q.lower()).split())
    for uid, a in acts.items():
        core = act_core(a.get("title", ""))
        if len(core) >= 10 and core in qn and uid not in found:
            found.append(uid)
    return found


def compact_sections(nums: Iterable[str]) -> str:
    ints = sorted({int(n) for n in nums if str(n).isdigit()})
    others = sorted({str(n) for n in nums if not str(n).isdigit()})
    parts, i = [], 0
    while i < len(ints):
        j = i
        while j + 1 < len(ints) and ints[j + 1] == ints[j] + 1:
            j += 1
        parts.append(str(ints[i]) if i == j else f"{ints[i]}-{ints[j]}")
        i = j + 1
    return ", ".join(parts + others)


# ── Citations ───────────────────────────────────────────────────────────────

ID_RE = re.compile(r"\[((?:[A-Za-z0-9]+-\d{4}(?:\.\d+)?-s\d+(?:-\d+)?)|(?:PDF-[0-9a-f]{6}-p\d+-\d+))\]")
ALIAS_GROUP_RE = re.compile(r"\[S(\d+(?:\s*,\s*S?\d+)*)\]")
ABSENCE_RX = re.compile(
    r"(don't have enough information|do not have enough information|no such (?:section|provision)|"
    r"there is no|does not (?:contain|have|exist)|doesn't (?:contain|have|exist)|not (?:found|present|contain)|"
    r"has no (?:section|provision)|নেই|পাওয়া যায়নি|পাওয়া যায় না|পর্যাপ্ত তথ্য)", re.I)


def canonicalize_citations(answer: str, alias_to_sid: Dict[int, str]) -> str:
    """[S2] / [S1, S3] -> [<stable id>] (aliases are display-only)."""
    def repl(m: re.Match) -> str:
        nums = [int(x) for x in re.findall(r"\d+", m.group(1))]
        sids = [alias_to_sid[n] for n in nums if n in alias_to_sid]
        return "".join(f"[{s}]" for s in sids) if sids else m.group(0)
    return ALIAS_GROUP_RE.sub(repl, answer)


def split_sentences(text: str) -> List[str]:
    text = re.sub(r"\n\s*((?:\[[^\[\]\n]+\]\s*)+)(?=\n|$)", r" \1", text)      # citation alone on a line
    return [s.strip() for s in re.split(r"(?<=[.!?।])\s+|\n+", text) if s.strip()]


def _factual(s: str) -> bool:
    s2 = re.sub(r"^[\s\-\*\u2022#>\d.)]+", "", s)
    if len(s2) < 25 or s2.endswith("?") or s2.endswith(":") or ABSENCE_RX.search(s2):
        return False
    return bool(re.search(r"[A-Za-z\u0980-\u09ff]{3}", s2))


def _match_retrieved(cid: str, retrieved: Set[str]) -> bool:
    return cid in retrieved or any(r.startswith(cid + "-") for r in retrieved)


def validate_citations(answer: str, retrieved: Set[str]) -> dict:
    cited = [m.group(1) for m in ID_RE.finditer(answer)]
    valid = sorted({c for c in cited if _match_retrieved(c, retrieved)})
    invalid = sorted({c for c in cited if not _match_retrieved(c, retrieved)})
    uncited = [s for s in split_sentences(answer) if _factual(s) and not ID_RE.search(s)]
    return dict(valid=valid, invalid=invalid, uncited=uncited)


# ── Query log (logs/query.jsonl) ───────────────────────────────────────────

class QueryLogger:
    def __init__(self, path: Optional[str]):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()

    def log(self, rec: dict) -> None:
        if not self.path:
            return
        rec = dict(ts=datetime.now().isoformat(timespec="seconds"), **rec)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
