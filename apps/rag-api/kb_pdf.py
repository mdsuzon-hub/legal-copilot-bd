#!/usr/bin/env python3
"""PDF knowledge-base extraction and chunking helpers."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Callable, List, Optional, Tuple


_SENT_SPLIT = re.compile(r"(?<=[.!?।])\s+|\n+")


def clean_text(text: str) -> str:
    text = text.replace("\r", "")
    text = re.sub(r"-\n(?=[a-z])", "", text)
    text = re.sub(r"(?<![.!?:;।\n])\n(?!\s*(?:[-•*●▪]|\d+[.)]))", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, size: int, overlap: int) -> List[str]:
    """Split text into sentence-aware chunks with sentence-level overlap."""
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


def file_hash(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:16]


def extract_pages(
    path: Path,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    warn_cb: Optional[Callable[[str], None]] = None,
    error_cb: Optional[Callable[[str], None]] = None,
) -> List[Tuple[int, str]]:
    """Return ``(page_number, text)`` pairs, with a pypdf fallback."""
    pages: List[Tuple[int, str]] = []
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            total = len(pdf.pages)
            for number, page in enumerate(pdf.pages, 1):
                text = clean_text(page.extract_text() or "")
                if text:
                    pages.append((number, text))
                if progress_cb:
                    progress_cb(number, total)
        return pages
    except Exception as exc:
        if warn_cb:
            warn_cb(f"pdfplumber failed ({exc}); trying pypdf…")

    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        total = len(reader.pages)
        for number, page in enumerate(reader.pages, 1):
            text = clean_text(page.extract_text() or "")
            if text:
                pages.append((number, text))
            if progress_cb:
                progress_cb(number, total)
    except Exception as exc:
        if error_cb:
            error_cb(f"Could not read {path.name}: {exc}")
    return pages
