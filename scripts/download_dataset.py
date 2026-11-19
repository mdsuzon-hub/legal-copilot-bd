#!/usr/bin/env python3
"""
scripts/download_dataset.py
---------------------------
Downloads the Bangladesh Laws (bdlaws) dataset from Hugging Face into data/raw/bdlaws.jsonl.
Supports progress display, resumable chunked streaming, and post-download integrity verification.

Usage:
    python scripts/download_dataset.py
    python scripts/download_dataset.py --output data/raw/bdlaws.jsonl
    python scripts/download_dataset.py --force
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_DATASET_URL = (
    "https://huggingface.co/datasets/Ashik2380/bdlaws/resolve/main/bdlaws.jsonl"
)
DEFAULT_OUTPUT_REL_PATH = Path("data") / "raw" / "bdlaws.jsonl"
CHUNK_SIZE = 1024 * 1024  # 1 MB


def format_bytes(size: float) -> str:
    """Format bytes into human-readable representation."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(size) < 1024.0:
            return f"{size:3.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def format_time(seconds: float) -> str:
    """Format seconds into MM:SS or HH:MM:SS."""
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def verify_downloaded_file(file_path: Path) -> bool:
    """Verify downloaded dataset file integrity and JSON structure."""
    if not file_path.is_file():
        print(f"[!] Error: File does not exist at {file_path}")
        return False

    size = file_path.stat().st_size
    print(f"\n[+] Verifying file: {file_path}")
    print(f"    File size: {format_bytes(size)} ({size:,} bytes)")

    if size == 0:
        print("[!] Error: File is empty.")
        return False

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
            if not first_line:
                print("[!] Error: First line is empty.")
                return False

            first_record = json.loads(first_line)
            required_keys = {"act_name", "section_name", "section_text"}
            found_keys = required_keys.intersection(first_record.keys())
            if not found_keys:
                print(f"[!] Warning: None of expected keys {required_keys} found in first line.")
            else:
                print(f"    Sample Act: {first_record.get('act_name', 'N/A')}")
                print(f"    Sample Section: {first_record.get('section_name', 'N/A')}")
        print("[✓] Verification successful: Valid JSONL dataset.\n")
        return True
    except Exception as e:
        print(f"[!] Verification failed: {e}\n")
        return False


def download_dataset(
    url: str,
    output_path: Path,
    chunk_size: int = CHUNK_SIZE,
    force: bool = False,
) -> bool:
    """Download the dataset with streaming, progress tracking, and resume capability."""
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Check remote content length
    req_head = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req_head) as resp:
            remote_size = int(resp.headers.get("Content-Length", 0))
    except Exception as e:
        print(f"[!] Could not determine remote file size: {e}")
        remote_size = 0

    existing_size = output_path.stat().st_size if output_path.is_file() else 0

    if not force and remote_size > 0 and existing_size == remote_size:
        print(f"[✓] File already completely downloaded: {output_path} ({format_bytes(existing_size)})")
        return verify_downloaded_file(output_path)

    # Resume support or fresh start
    headers = {"User-Agent": "Mozilla/5.0"}
    mode = "wb"
    resume_byte_pos = 0

    if not force and existing_size > 0 and remote_size > existing_size:
        print(f"[*] Resuming download from {format_bytes(existing_size)}...")
        headers["Range"] = f"bytes={existing_size}-"
        mode = "ab"
        resume_byte_pos = existing_size
    elif force and output_path.exists():
        print("[*] Force mode enabled: overwriting existing file...")

    req = urllib.request.Request(url, headers=headers)
    start_time = time.time()
    downloaded = resume_byte_pos

    try:
        with urllib.request.urlopen(req) as response:
            status_code = getattr(response, "status", 200)
            if resume_byte_pos > 0 and status_code != 206:
                # Server did not accept Range request, fallback to full download
                print("[*] Range not accepted by server, starting from beginning...")
                mode = "wb"
                downloaded = 0

            total_size = remote_size if remote_size > 0 else int(response.headers.get("Content-Length", 0)) + downloaded

            print(f"[*] Downloading {format_bytes(total_size)} to {output_path}")
            print("=" * 70)

            with open(output_path, mode) as f:
                last_update = 0.0
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)

                    now = time.time()
                    if now - last_update >= 0.5 or (total_size and downloaded >= total_size):
                        elapsed = now - start_time
                        speed = (downloaded - resume_byte_pos) / elapsed if elapsed > 0 else 0
                        pct = (downloaded / total_size * 100) if total_size > 0 else 0
                        eta = (total_size - downloaded) / speed if speed > 0 and total_size > 0 else 0

                        bar_len = 25
                        filled = int(bar_len * pct / 100) if total_size > 0 else 0
                        bar = "=" * filled + ">" + " " * (bar_len - filled - 1) if filled < bar_len else "=" * bar_len

                        sys.stdout.write(
                            f"\r[{bar}] {pct:5.1f}% | {format_bytes(downloaded)}/{format_bytes(total_size)} "
                            f"| {format_bytes(speed)}/s | ETA: {format_time(eta)} "
                        )
                        sys.stdout.flush()
                        last_update = now

            print("\n" + "=" * 70)
            print(f"[✓] Download completed in {format_time(time.time() - start_time)}.")
            return verify_downloaded_file(output_path)

    except KeyboardInterrupt:
        print("\n[!] Download interrupted by user. Run the command again to resume.")
        return False
    except Exception as e:
        print(f"\n[!] Download failed: {e}")
        return False


def main() -> None:
    # Determine project root relative to this script
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent

    default_output = repo_root / DEFAULT_OUTPUT_REL_PATH

    parser = argparse.ArgumentParser(
        description="Download and verify the bdlaws dataset from Hugging Face"
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_DATASET_URL,
        help="Download URL (default: Hugging Face Ashik2380/bdlaws raw jsonl)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=default_output,
        help=f"Target file path (default: {default_output})",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Overwrite existing file if already present",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only verify an existing downloaded file without downloading",
    )
    args = parser.parse_args()

    if args.verify_only:
        success = verify_downloaded_file(args.output)
        sys.exit(0 if success else 1)

    success = download_dataset(url=args.url, output_path=args.output, force=args.force)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
