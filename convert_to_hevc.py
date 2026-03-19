#!/usr/bin/env python3
"""
Convert H.264 video files to HEVC (H.265) in-place using ffmpeg.
Usage: python convert_to_hevc.py [directory]
       Defaults to current directory if no argument is given.
"""

import os
import sys
import subprocess
import json
import shutil
import tempfile
import argparse


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".m4v", ".ts", ".mts", ".m2ts"}


def get_codec(filepath: str) -> str | None:
    """Return the video codec name for the first video stream, or None on error."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name",
        "-of", "json",
        filepath,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if streams:
            return streams[0].get("codec_name")
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError):
        pass
    return None


def convert_to_hevc(filepath: str, crf: int = 28, preset: str = "medium") -> bool:
    """
    Transcode a file to HEVC in a temp file, then replace the original.
    Returns True on success, False on failure.
    """
    directory = os.path.dirname(os.path.abspath(filepath))
    base, ext = os.path.splitext(filepath)

    # Use a temp file in the same directory so the final rename is atomic
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tmp" + ext, dir=directory)
    os.close(tmp_fd)

    cmd = [
        "ffmpeg", "-y",
        "-i", filepath,
        "-c:v", "hevc_nvenc",    # was libx265, changed to hvec_nvenc 
        "-crf", str(crf),
        "-preset", preset,
        "-c:a", "copy",       # keep audio as-is
        "-c:s", "copy",       # keep subtitles as-is
        "-map", "0",          # copy all streams
        "-tag:v", "hvc1",     # broad player compatibility
        tmp_path,
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
        os.replace(tmp_path, filepath)   # atomic rename
        return True
    except subprocess.CalledProcessError as exc:
        print(f"    [ERROR] ffmpeg failed:\n{exc.stderr.decode(errors='replace')}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False


def scan_and_convert(directory: str, crf: int, preset: str, dry_run: bool) -> None:
    if not os.path.isdir(directory):
        print(f"[ERROR] Not a directory: {directory}")
        sys.exit(1)

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("[ERROR] ffmpeg / ffprobe not found. Please install ffmpeg.")
        sys.exit(1)

    candidates = [
        os.path.join(directory, f)
        for f in sorted(os.listdir(directory))
        if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS
    ]

    if not candidates:
        print("No video files found.")
        return

    converted = skipped = errors = 0

    for filepath in candidates:
        filename = os.path.basename(filepath)
        codec = get_codec(filepath)

        if codec is None:
            print(f"  [SKIP]    {filename}  (could not detect codec)")
            skipped += 1
            continue

        if codec.lower() not in ("h264", "avc"):
            print(f"  [SKIP]    {filename}  (codec: {codec})")
            skipped += 1
            continue

        print(f"  [CONVERT] {filename}  (codec: {codec})", end="", flush=True)

        if dry_run:
            print("  → dry-run, skipping.")
            skipped += 1
            continue

        print("  → converting...", end="", flush=True)
        ok = convert_to_hevc(filepath, crf=crf, preset=preset)
        if ok:
            print(" done.")
            converted += 1
        else:
            print(" FAILED.")
            errors += 1

    print(f"\nDone. Converted: {converted}  Skipped: {skipped}  Errors: {errors}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert H.264 video files to HEVC (H.265) in-place."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory to scan (default: current directory)",
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=28,
        help="CRF quality value for libx265 (lower = better quality, default: 28)",
    )
    parser.add_argument(
        "--preset",
        default="medium",
        choices=["ultrafast","superfast","veryfast","faster","fast",
                 "medium","slow","slower","veryslow"],
        help="ffmpeg encoding preset (default: medium)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files that would be converted without actually converting",
    )
    args = parser.parse_args()

    print(f"Scanning: {os.path.abspath(args.directory)}")
    print(f"Settings: CRF={args.crf}  preset={args.preset}"
          + ("  [DRY RUN]" if args.dry_run else ""))
    print()

    scan_and_convert(args.directory, crf=args.crf, preset=args.preset, dry_run=args.dry_run)


if __name__ == "__main__":
    main()