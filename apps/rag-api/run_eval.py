#!/usr/bin/env python3
"""
==========================================================================
  eval/run_eval.py — per-tier scoreboard for the legal RAG
==========================================================================
  python eval/run_eval.py --validate bdlaws.jsonl     # no models: check the questions
                                                      # against the corpus ground truth
  python eval/run_eval.py                             # full run (needs index + GROQ_API_KEY)
  python eval/run_eval.py --retrieval-only --no-expand  # retrieval metrics only, no answers
  options: --questions eval/questions.jsonl --tier adversarial --limit 5
           --collection kb_rag_v4 --out eval/report.json

  Item pass = every applicable check passes:
    retrieval   expected_section_ids all in the retrieved chunks
    cites       answer cites >=1 expected id, and every cited id was retrieved
    contains    all expected_answer_contains present (case/digit-insensitive)
    must_not    none of must_not_contain present
    absence     must_indicate_absence -> answer states the thing is absent
    language    expected_language matches the answer script
  `cited_all` (no uncited factual sentence) is reported but not part of pass.
  Items with needs_review=true rest on assumptions: run --validate first.
==========================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import kb_json  # noqa: E402
import rag_logic as L  # noqa: E402


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", kb_json.normalize_digits(s or "")).casefold().strip()


def load_questions(path: Path, tier=None, limit=None):
    items = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if tier:
        items = [i for i in items if i["tier"] == tier]
    return items[:limit] if limit else items


def id_match(sid: str, expected: str) -> bool:
    return sid == expected or sid.startswith(expected + "-")


# ── ground-truth validation (no models) ────────────────────────────────────

def validate(questions, data_path: Path) -> int:
    _, chunks, _ = kb_json.build_kb_chunks(data_path, 1000, 120, kb_json.simple_split)
    by_id = {c["meta"]["sid"]: c["text"] for c in chunks}
    bad = 0
    for it in questions:
        problems = []
        texts = []
        for e in it["expected_section_ids"]:
            hit = [t for s, t in by_id.items() if id_match(s, e)]
            if not hit:
                problems.append(f"expected id {e} not in corpus")
            texts += hit
        if texts and not it["must_indicate_absence"]:
            blob = norm(" ".join(texts))
            for s in it["expected_answer_contains"]:
                if norm(s) not in blob and it["tier"] in ("metadata", "factual", "two-hop", "reasoning"):
                    problems.append(f"'{s}' not found in the expected chunks (may still be a valid paraphrase)")
        if problems:
            bad += 1
        print(f"{'WARN' if problems else ' ok '} {it['id']:<4}{' [needs_review]' if it.get('needs_review') else ''}"
              + "".join(f"\n       - {p}" for p in problems))
    print(f"\n{len(questions) - bad}/{len(questions)} questions consistent with the corpus")
    return 1 if bad else 0


# ── scoring ─────────────────────────────────────────────────────────────────

def score_item(it: dict, res: dict, answers: bool) -> dict:
    retrieved = [c["sid"] for c in res["chunks"]]
    exp = it["expected_section_ids"]
    chk = {}
    if exp:
        chk["retrieval"] = all(any(id_match(r, e) for r in retrieved) for e in exp)
    if answers:
        ans, val = res["answer"], res["citations"]
        chk["valid_cites"] = not val["invalid"]
        if exp and not it["must_indicate_absence"]:
            chk["cites"] = any(id_match(c, e) for c in val["valid"] for e in exp)
        if it["expected_answer_contains"]:
            chk["contains"] = all(norm(s) in norm(ans) for s in it["expected_answer_contains"])
        if it["must_not_contain"]:
            chk["must_not"] = not any(norm(s) in norm(ans) for s in it["must_not_contain"])
        if it["must_indicate_absence"]:
            chk["absence"] = bool(L.ABSENCE_RX.search(ans))
        if it.get("expected_language"):
            chk["language"] = L.detect_language(ans) == it["expected_language"]
    out = dict(checks=chk, passed=all(chk.values()) if chk else False, retrieved=retrieved)
    if answers:
        out["cited_all"] = not res["citations"]["uncited"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=str(ROOT / "eval" / "questions.jsonl"))
    ap.add_argument("--validate", metavar="DATA", help="check questions against a corpus file (no models)")
    ap.add_argument("--tier")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--retrieval-only", action="store_true")
    ap.add_argument("--no-expand", action="store_true")
    ap.add_argument("--collection")
    ap.add_argument("--db")
    ap.add_argument("--out", default=str(ROOT / "eval" / "report.json"))
    a = ap.parse_args()

    items = load_questions(Path(a.questions), a.tier, a.limit)
    if a.validate:
        return validate(items, Path(a.validate))

    import rag_pdf as R  # heavy imports only for a real run
    over = dict(log_path="logs/eval_query.jsonl")
    if a.collection:
        over["collection"] = a.collection
    if a.db:
        over["db_dir"] = a.db
    cfg = R.default_cfg(**over)
    key = os.environ.get("GROQ_API_KEY", "")
    if not key and not a.retrieval_only:
        print("GROQ_API_KEY missing (use --retrieval-only --no-expand to skip the LLM)")
        return 2
    store, llm = R.Store(cfg), R.LLM(key or "unused", cfg.chat_model)
    pipe = R.RagPipeline(store, llm, cfg)

    rows, t_all = [], time.time()
    for it in items:
        t0 = time.time()
        if a.retrieval_only:
            res = pipe.retrieve(it["question"], expand=not a.no_expand)
            res.update(answer="", citations=dict(valid=[], invalid=[], uncited=[]))
        else:
            res = pipe.run(it["question"], expand=not a.no_expand)
        sc = score_item(it, res, answers=not a.retrieval_only)
        sc.update(id=it["id"], tier=it["tier"], intent=res["intent"], top_k=res["top_k"],
                  ms=int((time.time() - t0) * 1000),
                  tokens=sum(res["usage"].get(k, 0) for k in ("prompt", "completion")),
                  answer=res["answer"][:400], needs_review=it.get("needs_review", False))
        rows.append(sc)
        print(f"{'PASS' if sc['passed'] else 'FAIL'} {it['id']:<4} {it['tier']:<12} "
              + " ".join(f"{k}={'✓' if v else '✗'}" for k, v in sc["checks"].items()))

    tiers = defaultdict(list)
    for r in rows:
        tiers[r["tier"]].append(r)
    print("\n" + "=" * 86)
    print(f"{'tier':<13}{'n':>3}{'pass':>7}{'retrieval':>11}{'cites':>7}{'cited_all':>11}{'avg ms':>9}{'avg tok':>9}")
    print("-" * 86)
    pct = lambda xs: f"{100 * sum(xs) / len(xs):.0f}%" if xs else "-"  # noqa: E731
    for t, rs in tiers.items():
        print(f"{t:<13}{len(rs):>3}{pct([r['passed'] for r in rs]):>7}"
              f"{pct([r['checks']['retrieval'] for r in rs if 'retrieval' in r['checks']]):>11}"
              f"{pct([r['checks']['cites'] for r in rs if 'cites' in r['checks']]):>7}"
              f"{pct([r['cited_all'] for r in rs if 'cited_all' in r]):>11}"
              f"{sum(r['ms'] for r in rs) // len(rs):>9}{sum(r['tokens'] for r in rs) // len(rs):>9}")
    print("-" * 86)
    print(f"{'TOTAL':<13}{len(rows):>3}{pct([r['passed'] for r in rows]):>7}   ({time.time() - t_all:.0f}s)")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"report -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
