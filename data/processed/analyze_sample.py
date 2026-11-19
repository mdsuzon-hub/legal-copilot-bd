import json
import csv
import os
import argparse
from collections import Counter, defaultdict
from statistics import mean, median, mode
from typing import Any, Dict, List, Tuple


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_empty(value: Any) -> bool:
    """Treat '', [], {}, and whitespace-only strings as empty."""
    if isinstance(value, str):
        return value.strip() == ""
    return value in ([], {})


def safe_repr(value: Any, max_len: int = 40) -> str:
    """Short, single-line representation of a value for sampling."""
    try:
        text = repr(value)
    except Exception:
        text = f"<unrepr-{type(value).__name__}>"
    text = text.replace("\n", "\\n")
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def flatten_keys(obj: Any, prefix: str = "") -> List[str]:
    """Recursively collect dotted key paths from nested dicts/lists."""
    paths: List[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            paths.append(path)
            paths.extend(flatten_keys(v, path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            path = f"{prefix}[{i}]"
            paths.extend(flatten_keys(item, path))
    return paths


def depth_of(obj: Any) -> int:
    """Max nesting depth of a structure (scalars = 1)."""
    if isinstance(obj, dict) and obj:
        return 1 + max(depth_of(v) for v in obj.values())
    if isinstance(obj, list) and obj:
        return 1 + max(depth_of(v) for v in obj)
    return 1


# ---------------------------------------------------------------------------
# Loaders  (JSON  |  JSONL  |  Concatenated JSON)
# ---------------------------------------------------------------------------

def _normalize(records: List[Any]) -> List[dict]:
    """Ensure every record is a dict so downstream code is safe."""
    out: List[dict] = []
    for r in records:
        if isinstance(r, dict):
            out.append(r)
        else:
            out.append({"_value": r})
    return out


def _try_plain_json(content: str):
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None

    if isinstance(data, dict):
        return _normalize([data]), "json-object"
    if isinstance(data, list):
        return _normalize(data), "json-array"
    return _normalize([data]), "json-scalar"


def _try_jsonl(content: str):
    """Strict JSONL: every non-blank line must parse on its own."""
    lines = [ln for ln in content.splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        records = [json.loads(ln) for ln in lines]
    except json.JSONDecodeError:
        return None
    return _normalize(records), "jsonl"


def _try_concatenated_json(content: str):
    """
    Parse N JSON objects pasted back-to-back
    (pretty-printed, possibly comma- or newline-separated, no array wrapper).
    This is the shape of the sample file in this project.
    """
    decoder = json.JSONDecoder()
    records: List[Any] = []
    idx = 0
    n = len(content)

    while idx < n:
        # Skip whitespace and stray separators between records
        while idx < n and content[idx] in " \t\r\n,":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(content, idx)
        except json.JSONDecodeError as e:
            # Nothing parsed yet → not this format; otherwise surface the error
            if not records:
                return None
            raise ValueError(
                f"Failed parsing concatenated JSON at char {idx}: {e}"
            ) from e
        records.append(obj)
        idx = end

    if not records:
        return None
    return _normalize(records), "concatenated-json"


def _try_jsonl_stream(path: str):
    """Line-by-line streaming parser for large JSONL files."""
    records: List[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                ln = line.strip()
                if not ln:
                    continue
                records.append(json.loads(ln))
    except Exception:
        return None
    if not records:
        return None
    return _normalize(records), "jsonl"


def load_records(path: str, forced_format: str = "auto") -> Tuple[List[dict], str]:
    """
    Load records from a file. Supported shapes:
      - json-object       : single {...}
      - json-array        : [{...}, {...}]
      - json-scalar       : bare scalar (rare)
      - jsonl             : one object per line
      - concatenated-json : {...}\n{...}\n{...}   (no commas / no array)
    """
    if forced_format == "jsonl" or (forced_format == "auto" and path.endswith(".jsonl")):
        # Try fast streaming JSONL first for efficiency
        streamed = _try_jsonl_stream(path)
        if streamed is not None:
            return streamed

    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()

    if not content:
        return [], "empty"

    strategies = {
        "json": _try_plain_json,
        "jsonl": _try_jsonl,
        "concatenated": _try_concatenated_json,
    }

    if forced_format != "auto":
        fn = strategies.get(forced_format)
        if fn is None:
            raise ValueError(
                f"Unknown format '{forced_format}'. "
                f"Choose from: auto, {', '.join(strategies)}"
            )
        result = fn(content)
        if result is None:
            raise ValueError(
                f"File could not be parsed as '{forced_format}'."
            )
        return result

    # Auto: try plain JSON first, then JSONL, then concatenated
    for fn in (_try_plain_json, _try_jsonl, _try_concatenated_json):
        result = fn(content)
        if result is not None:
            return result

    raise ValueError(
        f"Could not parse {path} as JSON, JSONL, or concatenated JSON. "
        f"Try --format json|jsonl|concatenated."
    )


# ---------------------------------------------------------------------------
# Profiling
# ---------------------------------------------------------------------------

def profile_field(records: List[dict], field: str) -> Dict[str, Any]:
    """Compute an exhaustive profile for a single top-level field."""
    stats: Dict[str, Any] = {
        "present": 0,
        "missing": 0,
        "null": 0,
        "empty": 0,
        "types": Counter(),
        "unique_values": set(),
        "unhashable": 0,
        "lengths": [],
        "samples": [],
        "value_freq": Counter(),
        "nested_keys": Counter(),
        "max_depth": 0,
    }

    for rec in records:
        if field not in rec:
            stats["missing"] += 1
            continue

        stats["present"] += 1
        value = rec[field]

        stats["types"][type(value).__name__] += 1

        if value is None:
            stats["null"] += 1
            continue
        if is_empty(value):
            stats["empty"] += 1

        # Uniqueness
        try:
            stats["unique_values"].add(value)
            if isinstance(value, (str, int, float, bool)):
                stats["value_freq"][value] += 1
        except TypeError:
            stats["unhashable"] += 1

        # Length / size
        if isinstance(value, (str, list, dict)):
            stats["lengths"].append(len(value))

        # Depth
        d = depth_of(value)
        if d > stats["max_depth"]:
            stats["max_depth"] = d

        # Nested keys (one level)
        if isinstance(value, dict):
            for k in value:
                stats["nested_keys"][k] += 1

        # Samples (first few distinct)
        if len(stats["samples"]) < 3:
            stats["samples"].append(safe_repr(value))

    return stats


def compute_quality_score(profile: Dict[str, Any], total: int) -> float:
    """0–100 completeness score (non-missing, non-null, non-empty)."""
    if total == 0:
        return 0.0
    good = profile["present"] - profile["null"] - profile["empty"]
    return max(0.0, good / total * 100)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_header(title: str, width: int = 110) -> None:
    print("\n" + "=" * width)
    print(title)
    print("=" * width)


def report(
    records: List[dict],
    fmt: str,
    file_path: str,
    export_csv: bool = False,
    out_dir: str = None,
) -> None:
    total = len(records)
    all_fields = set()
    for rec in records:
        if isinstance(rec, dict):
            all_fields.update(rec.keys())

    # ---------------- Overview ----------------
    print_header("DATASET OVERVIEW")
    print(f"File            : {file_path}")
    print(f"Format detected : {fmt}")
    print(f"Total records   : {total}")
    print(f"Unique fields   : {len(all_fields)}")
    if total:
        non_dict = sum(1 for r in records if not isinstance(r, dict))
        print(f"Non-object recs : {non_dict}")

        # Duplicate detection
        try:
            hashes = [json.dumps(r, sort_keys=True) for r in records]
            dupes = total - len(set(hashes))
            print(f"Exact duplicates: {dupes}")
        except Exception:
            pass

        # Field-set variation
        field_sets = Counter(
            tuple(sorted(r.keys())) for r in records if isinstance(r, dict)
        )
        if len(field_sets) > 1:
            print(f"Distinct field sets: {len(field_sets)}")
            for fs, cnt in field_sets.most_common(3):
                print(f"  {cnt:>5} recs → {len(fs)} fields: {', '.join(fs[:8])}"
                      + (" ..." if len(fs) > 8 else ""))

    # ---------------- Field frequency ----------------
    print_header("FIELD FREQUENCY ANALYSIS")
    fmt_row = (
        f"{'Field':<30}{'Present':>9}{'Missing':>9}{'Null':>7}"
        f"{'Empty':>7}{'Present%':>10}{'Quality':>9}{'Unique':>8}{'Types':>22}"
    )
    print(fmt_row)
    print("-" * 110)

    profiles = {}
    for field in sorted(all_fields):
        p = profile_field(records, field)
        profiles[field] = p
        pct = (p["present"] / total * 100) if total else 0
        q = compute_quality_score(p, total)
        unique_str = f"{len(p['unique_values'])}"
        if p["unhashable"]:
            unique_str += "*"
        types_str = ", ".join(f"{t}({c})" for t, c in p["types"].most_common())
        print(
            f"{field:<30}{p['present']:>9}{p['missing']:>9}{p['null']:>7}"
            f"{p['empty']:>7}{pct:>9.2f}%{q:>8.1f}%{unique_str:>8}"
            f"{types_str:>22}"
        )

    # ---------------- Detailed per-field ----------------
    print_header("DETAILED FIELD PROFILES (length / depth / samples)")

    for field, p in profiles.items():
        print(f"\n• {field}")
        print(f"    types        : "
              + ", ".join(f"{t}={c}" for t, c in p["types"].items()))
        if p["lengths"]:
            print(f"    length stats : "
                  f"min={min(p['lengths'])}, max={max(p['lengths'])}, "
                  f"mean={mean(p['lengths']):.2f}, "
                  f"median={median(p['lengths'])}")
        if p["max_depth"] > 1:
            print(f"    max depth    : {p['max_depth']}")
        if p["nested_keys"]:
            top_nested = ", ".join(
                f"{k}({c})" for k, c in p["nested_keys"].most_common(5)
            )
            print(f"    nested keys  : {top_nested}")

        # Top repeated values (only for scalar types)
        if p["value_freq"]:
            top = p["value_freq"].most_common(5)
            if len(top) > 1 or (len(top) == 1 and top[0][1] > 1):
                print(f"    top values   : "
                      + ", ".join(f"{safe_repr(v, 25)}×{c}" for v, c in top))

        if p["samples"]:
            print(f"    samples      : "
                  + " | ".join(p["samples"]))

    # ---------------- Data quality summary ----------------
    print_header("DATA QUALITY SUMMARY")
    if total:
        good_cells = 0
        total_cells = 0
        for f, p in profiles.items():
            total_cells += total
            good_cells += p["present"] - p["null"] - p["empty"]
        overall = good_cells / total_cells * 100 if total_cells else 0
        print(f"Overall completeness (populated cells / total cells): {overall:.2f}%")

        low_quality = [
            (f, compute_quality_score(p, total))
            for f, p in profiles.items()
            if compute_quality_score(p, total) < 60
        ]
        if low_quality:
            print("\nFields with quality < 60%:")
            for f, q in sorted(low_quality, key=lambda x: x[1]):
                print(f"  - {f:<40} {q:5.1f}%")
        else:
            print("All fields have quality ≥ 60%.")

        inconsistent = [
            (f, p["types"]) for f, p in profiles.items() if len(p["types"]) > 1
        ]
        if inconsistent:
            print("\nFields with mixed/multiple types:")
            for f, t in inconsistent:
                print(f"  - {f:<40} {dict(t)}")

    # ---------------- Legal Domain & Language Analysis ----------------
    if total and any("act_name" in r for r in records if isinstance(r, dict)):
        print_header("LEGAL DOMAIN & LANGUAGE ANALYSIS")
        acts = set()
        repealed_acts = set()
        active_acts = set()
        act_section_cnt = Counter()
        bangla_cnt = 0
        english_cnt = 0
        mixed_cnt = 0
        empty_txt_cnt = 0
        volumes = set()

        for r in records:
            if not isinstance(r, dict):
                continue
            act = r.get("act_name")
            if act:
                acts.add(act)
                act_section_cnt[act] += 1
                if r.get("repealed", False):
                    repealed_acts.add(act)
                else:
                    active_acts.add(act)
            vol = r.get("volume_name")
            if vol:
                volumes.add(vol)

            txt = r.get("section_text", "")
            if not txt or not isinstance(txt, str):
                empty_txt_cnt += 1
                continue
            letters = [ch for ch in txt if ch.isalpha() or "\u0980" <= ch <= "\u09ff"]
            if not letters:
                empty_txt_cnt += 1
                continue
            bn = sum(1 for ch in letters if "\u0980" <= ch <= "\u09ff")
            ratio = bn / len(letters)
            if ratio > 0.6:
                bangla_cnt += 1
            elif ratio < 0.2:
                english_cnt += 1
            else:
                mixed_cnt += 1

        rep_sec = sum(1 for r in records if isinstance(r, dict) and r.get("repealed", False))
        act_sec = total - rep_sec

        print(f"Total Sections / Records : {total}")
        print(f"Total Unique Acts        : {len(acts)}")
        print(f"Active Acts              : {len(active_acts)} ({(len(active_acts)/len(acts)*100 if acts else 0):.2f}%)")
        print(f"Repealed Acts            : {len(repealed_acts)} ({(len(repealed_acts)/len(acts)*100 if acts else 0):.2f}%)")
        print(f"Active Sections          : {act_sec} ({(act_sec/total*100):.2f}%)")
        print(f"Repealed Sections        : {rep_sec} ({(rep_sec/total*100):.2f}%)")
        print(f"Total Volumes Represented: {len(volumes)}")
        print("\nLanguage Distribution (section_text):")
        print(f"  - Bangla               : {bangla_cnt:>6} ({bangla_cnt/total*100:5.2f}%)")
        print(f"  - English              : {english_cnt:>6} ({english_cnt/total*100:5.2f}%)")
        print(f"  - Mixed / Bilingual    : {mixed_cnt:>6} ({mixed_cnt/total*100:5.2f}%)")
        print(f"  - Empty / No text      : {empty_txt_cnt:>6} ({empty_txt_cnt/total*100:5.2f}%)")

        if act_section_cnt:
            print("\nTop 5 Acts by Section Count:")
            for act, cnt in act_section_cnt.most_common(5):
                print(f"  - {act[:55]:<55} : {cnt} sections")

    # ---------------- CSV export ----------------
    if export_csv:
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            base_name = os.path.splitext(os.path.basename(file_path))[0] + "_profile.csv"
            out = os.path.join(out_dir, base_name)
        else:
            out = os.path.splitext(file_path)[0] + "_profile.csv"
        with open(out, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "field", "present", "missing", "null", "empty",
                "present_pct", "quality_pct", "unique", "types",
            ])
            for field, p in profiles.items():
                pct = (p["present"] / total * 100) if total else 0
                writer.writerow([
                    field, p["present"], p["missing"], p["null"], p["empty"],
                    f"{pct:.2f}", f"{compute_quality_score(p, total):.1f}",
                    len(p["unique_values"]),
                    ";".join(f"{t}={c}" for t, c in p["types"].items()),
                ])
        print(f"\nCSV profile exported to: {out}")

    print("\n" + "=" * 110)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Advanced JSON / JSONL / concatenated-JSON dataset profiler"
    )
    parser.add_argument(
        "file", nargs="?", default="sample.json",
        help="Path to the data file (default: sample.json)"
    )
    parser.add_argument(
        "--csv", action="store_true",
        help="Export per-field profile to CSV"
    )
    parser.add_argument(
        "--out-dir", default=None,
        help="Optional directory to save exported CSV (default: same as input file)"
    )
    parser.add_argument(
        "--format",
        choices=["auto", "json", "jsonl", "concatenated"],
        default="auto",
        help="Force a specific parser instead of auto-detection "
             "(default: auto)"
    )
    args = parser.parse_args()

    try:
        records, fmt = load_records(args.file, forced_format=args.format)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        print(f"[!] Load error: {e}")
        return

    if not records:
        print(f"[!] No records found in {args.file}")
        return

    report(records, fmt, args.file, export_csv=args.csv, out_dir=args.out_dir)


if __name__ == "__main__":
    main()