#!/usr/bin/env python3
"""
==========================================================================
  kb_pdf.py — PDF knowledge-base extraction and chunking
==========================================================================
  This module owns PDF-specific work for rag_pdf.py:
    * text extraction with a pypdf fallback
    * PDF text cleanup and sentence-aware chunking
    * content hashing for resumable indexing

  Standalone dry-run:
      python kb_pdf.py document.pdf --chunk-size 700 --show 3
==========================================================================
"""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path
from typing import Callable, List, Optional, Tuple


ProgressFn = Callable[[int, int, str], None]
MessageFn = Callable[[str], None]


def file_hash(path: Path) -> str:
    """Return a short deterministic hash of a PDF's contents."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:16]


def clean_text(text: str) -> str:
    """Normalize extracted PDF text without changing its meaning."""
    text = text.replace("\r", "")
    text = re.sub(r"-\n(?=[a-z])", "", text)
    text = re.sub(r"(?<![.!?:;।\n])\n(?!\s*(?:[-•*●▪]|\d+[.)]))", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_SENT_SPLIT = re.compile(r"(?<=[.!?।])\s+|\n+")


def chunk_text(text: str, size: int, overlap: int) -> List[str]:
    """Pack extracted text into sentence-aware, overlapping chunks."""
    units: List[str] = []
    for unit in _SENT_SPLIT.split(text):
        unit = unit.strip()
        if not unit:
            continue
        while len(unit) > size:
            units.append(unit[:size])
            unit = unit[size - overlap:]
        units.append(unit)

    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for unit in units:
        if current and current_len + len(unit) + 1 > size:
            chunks.append(" ".join(current))
            keep: List[str] = []
            keep_len = 0
            for previous in reversed(current):
                if keep_len + len(previous) > overlap:
                    break
                keep.insert(0, previous)
                keep_len += len(previous) + 1
            current, current_len = keep, keep_len
        current.append(unit)
        current_len += len(unit) + 1
    if current:
        chunks.append(" ".join(current))
    return [chunk for chunk in chunks if len(chunk) > 40]


def _default_progress(done: int, total: int, label: str) -> None:
    total = max(total, 1)
    width = 28
    filled = int(done / total * width)
    bar = "█" * filled + "░" * (width - filled)
    print(f"\r  {label:<22} {bar} {done}/{total}", end="", flush=True)
    if done >= total:
        print()


def _default_message(message: str) -> None:
    print(message)


def extract_pages(
    path: Path,
    progress_cb: Optional[ProgressFn] = None,
    warn_cb: Optional[MessageFn] = None,
    error_cb: Optional[MessageFn] = None,
) -> List[Tuple[int, str]]:
    """Return ``(page_number, text)`` pairs from a PDF."""
    progress_cb = progress_cb or _default_progress
    warn_cb = warn_cb or _default_message
    error_cb = error_cb or _default_message
    pages: List[Tuple[int, str]] = []

    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            total = len(pdf.pages)
            for page_no, page in enumerate(pdf.pages, 1):
                text = clean_text(page.extract_text() or "")
                if text:
                    pages.append((page_no, text))
                progress_cb(page_no, total, "  extracting")
        return pages
    except Exception as exc:  # noqa: BLE001
        warn_cb(f"pdfplumber failed ({exc}); trying pypdf…")

    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        total = len(reader.pages)
        for page_no, page in enumerate(reader.pages, 1):
            text = clean_text(page.extract_text() or "")
            if text:
                pages.append((page_no, text))
            progress_cb(page_no, total, "  extracting")
    except Exception as exc:  # noqa: BLE001
        error_cb(f"Could not read {path.name}: {exc}")
    return pages


def _simple_report(path: Path, chunk_size: int, overlap: int, show: int) -> None:
    pages = extract_pages(path)
    chunks = [
        (page_no, chunk)
        for page_no, text in pages
        for chunk in chunk_text(text, chunk_size, overlap)
    ]
    print(f"pages={len(pages)}")
    print(f"chunks={len(chunks)}")
    for page_no, chunk in chunks[:show]:
        print(f"\n--- page {page_no} ---\n{chunk[:600]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry-run the PDF knowledge-base chunker")
    parser.add_argument("file", type=Path)
    parser.add_argument("--chunk-size", type=int, default=700)
    parser.add_argument("--overlap", type=int, default=140)
    parser.add_argument("--show", type=int, default=3)
    args = parser.parse_args()
    if args.overlap >= args.chunk_size:
        parser.error("--overlap must be smaller than --chunk-size")
    _simple_report(args.file, args.chunk_size, args.overlap, args.show)
