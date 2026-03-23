#!/usr/bin/env python3
"""
Convert H.264 video files to HEVC (H.265) in-place using ffmpeg.
Shows a live progress bar for each file being converted.

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
import signal
import time
import logging
from datetime import datetime


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".m4v", ".ts", ".mts", ".m2ts"}

# ANSI colour helpers (auto-disabled when stdout is not a TTY)
_USE_COLOR = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

GREEN  = lambda t: _c("32", t)
YELLOW = lambda t: _c("33", t)
RED    = lambda t: _c("31", t)
CYAN   = lambda t: _c("36", t)
BOLD   = lambda t: _c("1",  t)
DIM    = lambda t: _c("2",  t)


# ── logger ───────────────────────────────────────────────────────────────────

# Module-level logger; configured in main() if --log-file is supplied.
log: logging.Logger = logging.getLogger("convert_to_hevc")
log.addHandler(logging.NullHandler())   # silent by default


def setup_logger(log_path: str) -> None:
    """Attach a file handler that writes plain-text (no ANSI) log entries."""
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)


# ── helpers ──────────────────────────────────────────────────────────────────

def get_video_info(filepath: str) -> dict:
    """Return {'codec': str, 'duration': float} for the first video stream."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,duration:format=duration",
        "-of", "json",
        filepath,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        codec = None
        duration = 0.0
        streams = data.get("streams", [])
        if streams:
            codec = streams[0].get("codec_name")
            if "duration" in streams[0]:
                duration = float(streams[0]["duration"])
        if duration == 0.0:
            fmt_dur = data.get("format", {}).get("duration")
            if fmt_dur:
                duration = float(fmt_dur)
        return {"codec": codec, "duration": duration}
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, ValueError):
        return {"codec": None, "duration": 0.0}


def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def _format_eta(seconds: float) -> str:
    if seconds < 0 or seconds > 359999:
        return "--:--"
    h, rem = divmod(int(seconds), 3600)
    m, s   = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _format_size(path: str) -> str:
    try:
        b = os.path.getsize(path)
        for unit in ("B", "KB", "MB", "GB"):
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} TB"
    except OSError:
        return "?"


def draw_progress(filename: str, pct: float, elapsed: float, eta: float,
                  speed: str, file_index: int, file_total: int) -> None:
    """Overwrite the current line with a progress bar (3-line block)."""
    width = _term_width()

    label = f"  [{file_index}/{file_total}] {BOLD(filename)}"
    print(f"\r{label:<{width}}", end="")
    print()

    bar_width = max(20, width - 30)
    filled = int(bar_width * pct / 100)
    bar = "█" * filled + "░" * (bar_width - filled)
    pct_str = f"{pct:5.1f}%"
    print(f"  {CYAN(bar)} {BOLD(pct_str)}", end="")
    print()

    elapsed_str = _format_eta(elapsed)
    eta_str     = _format_eta(eta)
    stats = f"  Elapsed {elapsed_str}  ETA {eta_str}  Speed {speed}x"
    print(f"{DIM(stats):<{width}}", end="", flush=True)

    # Move cursor back up 2 lines so next update overwrites these 3 lines
    print("\033[2A", end="", flush=True)


def clear_progress() -> None:
    """Erase the 3-line progress block."""
    for _ in range(3):
        print(f"\r\033[K")
    print("\033[3A", end="", flush=True)


# ── conversion ────────────────────────────────────────────────────────────────

def convert_to_hevc(
    filepath: str,
    duration: float,
    crf: int,
    preset: str,
    file_index: int,
    file_total: int,
) -> bool:
    """
    Transcode to HEVC with a live progress bar.
    Writes to a temp file, then atomically replaces the original.
    Returns True on success, False on failure.
    """
    filename  = os.path.basename(filepath)
    directory = os.path.dirname(os.path.abspath(filepath))
    _, ext    = os.path.splitext(filepath)

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tmp" + ext, dir=directory)
    os.close(tmp_fd)

    cmd = [
        "ffmpeg", "-y",
        "-i", filepath,
        "-c:v", "libx265",
        "-crf", str(crf),
        "-preset", preset,
        "-c:a", "copy",
        "-c:s", "copy",
        "-map", "0",
        "-tag:v", "hvc1",
        "-progress", "pipe:2",  # write progress key=value pairs to stderr
        "-nostats",
        tmp_path,
    ]

    # Reserve 3 blank lines for the progress block
    print("\n\n", end="", flush=True)

    start_time = time.monotonic()
    last_pct   = 0.0
    stderr_buf = []
    proc       = None

    def _handle_sigint(sig, frame):
        if proc:
            proc.terminate()
        clear_progress()
        print(RED("  Interrupted."))
        sys.exit(1)

    old_handler = signal.signal(signal.SIGINT, _handle_sigint)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        current: dict = {}

        for raw_line in proc.stderr:
            line = raw_line.rstrip()
            stderr_buf.append(line)

            if "=" in line:
                key, _, val = line.partition("=")
                current[key.strip()] = val.strip()

            if current.get("progress") in ("continue", "end"):
                # Prefer microseconds; fall back to the formatted time string
                out_time_us = current.get("out_time_us")
                speed_str   = current.get("speed", "?").replace("x", "")

                pct = last_pct
                if duration > 0 and out_time_us:
                    try:
                        elapsed_enc = float(out_time_us) / 1_000_000
                        pct = min(100.0, elapsed_enc / duration * 100)
                        last_pct = pct
                    except ValueError:
                        pass

                elapsed = time.monotonic() - start_time
                try:
                    spd = float(speed_str)
                    remaining_enc = duration * (1 - pct / 100)
                    eta = remaining_enc / spd if spd > 0 else 0.0
                    speed_fmt = f"{spd:.2f}"
                except ValueError:
                    eta = 0.0
                    speed_fmt = "?"

                if sys.stdout.isatty():
                    draw_progress(filename, pct, elapsed, eta, speed_fmt,
                                  file_index, file_total)
                current = {}

        proc.wait()
        return_code = proc.returncode

    finally:
        signal.signal(signal.SIGINT, old_handler)

    clear_progress()

    if return_code != 0:
        print(RED(f"  [ERROR] ffmpeg exited with code {return_code}"))
        # Filter noisy progress lines; keep only human-readable diagnostic lines
        meaningful = [l for l in stderr_buf
                      if l and not l.startswith((
                          "frame=", "fps=", "out_time", "speed=", "progress",
                          "bitrate", "total_size", "dup_frames", "drop_frames", "stream_"
                      ))]
        tail = meaningful[-10:]
        for l in tail:
            print(DIM(f"    {l}"))
        # Write full ffmpeg output to the log file for post-mortem analysis
        log.error("Conversion FAILED: %s  (ffmpeg exit code %d)", filepath, return_code)
        for l in meaningful:
            log.error("  ffmpeg: %s", l)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False

    size_before = _format_size(filepath)
    os.replace(tmp_path, filepath)
    size_after    = _format_size(filepath)
    elapsed_total = time.monotonic() - start_time
    print(GREEN(f"  ✔  Done in {_format_eta(elapsed_total)}  "
                f"{size_before} → {size_after}"))
    log.info("OK  %s  (%s → %s)  elapsed %s",
             filepath, size_before, size_after, _format_eta(elapsed_total))
    return True


# ── scanner ───────────────────────────────────────────────────────────────────

def scan_and_convert(directory: str, crf: int, preset: str, dry_run: bool) -> None:
    if not os.path.isdir(directory):
        print(RED(f"[ERROR] Not a directory: {directory}"))
        log.error("Not a directory: %s", directory)
        sys.exit(1)

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print(RED("[ERROR] ffmpeg / ffprobe not found. Please install ffmpeg."))
        log.error("ffmpeg / ffprobe not found")
        sys.exit(1)

    entries    = sorted(os.listdir(directory))
    candidates = [
        os.path.join(directory, f)
        for f in entries
        if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS
    ]

    if not candidates:
        print("No video files found.")
        return

    # Pre-scan all files so we can report a tally up front
    to_convert = []
    to_skip    = []

    print("Scanning files…")
    for filepath in candidates:
        info  = get_video_info(filepath)
        fname = os.path.basename(filepath)
        if info["codec"] is None:
            to_skip.append((fname, "unreadable"))
        elif info["codec"].lower() not in ("h264", "avc"):
            to_skip.append((fname, info["codec"]))
        else:
            to_convert.append((filepath, info["duration"]))

    print(f"  {GREEN(str(len(to_convert)))} file(s) to convert, "
          f"{YELLOW(str(len(to_skip)))} skipped\n")

    for fname, reason in to_skip:
        print(DIM(f"  [skip] {fname}  ({reason})"))
        log.info("SKIP  %s  (%s)", fname, reason)
    if to_skip:
        print()

    if not to_convert:
        return

    if dry_run:
        for fp, _ in to_convert:
            print(YELLOW(f"  [dry-run] {os.path.basename(fp)}"))
        return

    converted = errors = 0
    total     = len(to_convert)

    for idx, (filepath, duration) in enumerate(to_convert, start=1):
        fname = os.path.basename(filepath)
        print(BOLD(f"Converting ({idx}/{total}): {fname}"))
        ok = convert_to_hevc(filepath, duration, crf=crf, preset=preset,
                             file_index=idx, file_total=total)
        if ok:
            converted += 1
        else:
            errors += 1
        print()

    summary = (f"Finished — "
               f"{GREEN(str(converted))} converted, "
               f"{(RED(str(errors)) if errors else DIM('0'))} errors")
    print(BOLD(summary))
    log.info("Run complete — converted: %d  errors: %d  skipped: %d",
             converted, errors, len(to_skip))


# ── entry point ───────────────────────────────────────────────────────────────

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
        help="CRF quality (lower = better quality / larger file, default: 28)",
    )
    parser.add_argument(
        "--preset",
        default="medium",
        choices=["ultrafast", "superfast", "veryfast", "faster", "fast",
                 "medium", "slow", "slower", "veryslow"],
        help="Encoding speed/compression preset (default: medium)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files that would be converted without converting them",
    )
    parser.add_argument(
        "--log-file",
        metavar="PATH",
        default=None,
        help="Append errors (and a run summary) to a log file",
    )
    args = parser.parse_args()

    if args.log_file:
        setup_logger(args.log_file)
        log.info("=" * 60)
        log.info("Session started  •  directory: %s  •  CRF %d  •  preset %s%s",
                 os.path.abspath(args.directory), args.crf, args.preset,
                 "  •  DRY RUN" if args.dry_run else "")

    print(BOLD(f"convert_to_hevc  •  {os.path.abspath(args.directory)}"))
    print(DIM(f"CRF {args.crf}  •  preset {args.preset}"
              + ("  •  DRY RUN" if args.dry_run else "")
              + (f"  •  log → {args.log_file}" if args.log_file else "")))
    print()

    scan_and_convert(args.directory, crf=args.crf, preset=args.preset,
                     dry_run=args.dry_run)


if __name__ == "__main__":
    main()