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


def check_nvenc_available() -> bool:
    """Return True if ffmpeg was built with hevc_nvenc support."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=True,
        )
        return "hevc_nvenc" in result.stdout
    except subprocess.CalledProcessError:
        return False


# ── helpers ──────────────────────────────────────────────────────────────────

def get_video_info(filepath: str) -> dict:
    """
    Return info about the file's streams:
      codec      - video codec name of the first video stream
      duration   - duration in seconds (float)
      bad_tmcd   - True if a faulty timecode (tmcd) stream is present
      has_neg_ts - True if the file has negative or near-zero start PTS
    """
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries",
        "stream=codec_name,codec_type,index,start_time:format=duration,start_time",
        "-of", "json",
        filepath,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        codec    = None
        duration = 0.0
        bad_tmcd   = False
        has_neg_ts = False

        streams = data.get("streams", [])
        for s in streams:
            ctype = s.get("codec_type", "")
            cname = s.get("codec_name", "")

            # First video stream → grab codec and duration
            if ctype == "video" and codec is None:
                codec = cname
                if "duration" in s:
                    try:
                        duration = float(s["duration"])
                    except ValueError:
                        pass

            # Timecode streams with irrational start times are faulty
            if cname == "tmcd":
                st = s.get("start_time", "")
                try:
                    # A tmcd start_time that isn't close to 0 signals corruption
                    if st and abs(float(st)) > 3600 * 24:
                        bad_tmcd = True
                except ValueError:
                    bad_tmcd = True   # unparseable → treat as faulty

        # Check container-level start time for negative PTS
        fmt = data.get("format", {})
        if duration == 0.0 and "duration" in fmt:
            try:
                duration = float(fmt["duration"])
            except ValueError:
                pass
        fmt_start = fmt.get("start_time", "0")
        try:
            if float(fmt_start) < -0.1:
                has_neg_ts = True
        except ValueError:
            pass

        return {
            "codec":      codec,
            "duration":   duration,
            "bad_tmcd":   bad_tmcd,
            "has_neg_ts": has_neg_ts,
        }
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, ValueError):
        return {"codec": None, "duration": 0.0, "bad_tmcd": False, "has_neg_ts": False}


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
    encoder: str,
    file_index: int,
    file_total: int,
    bad_tmcd: bool = False,
    has_neg_ts: bool = False,
) -> bool:
    """
    Transcode to HEVC with a live progress bar.
    Writes to a temp file, then atomically replaces the original.
    bad_tmcd   - drop faulty tmcd streams instead of copying them
    has_neg_ts - rebase negative timestamps to zero before encoding
    Returns True on success, False on failure.
    """
    filename  = os.path.basename(filepath)
    directory = os.path.dirname(os.path.abspath(filepath))
    _, ext    = os.path.splitext(filepath)

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tmp" + ext, dir=directory)
    os.close(tmp_fd)

    # Build encoder-specific arguments.
    # nvenc uses -qp (constant QP) instead of -crf, and has its own preset names.
    if encoder == "hevc_nvenc":
        enc_args = [
            "-c:v", "hevc_nvenc",
            "-qp", str(crf),       # nvenc quality knob closest to CRF
            "-preset", "p4",       # nvenc balanced preset (p1=fastest … p7=slowest)
        ]
    else:
        enc_args = [
            "-c:v", "libx265",
            "-crf", str(crf),
            "-preset", preset,
        ]

    # Stream mapping: default is -map 0 (copy everything).
    # If there's a bad timecode stream, map only the safe stream types explicitly
    # so the faulty tmcd track is silently dropped.
    if bad_tmcd:
        map_args = [
            "-map", "0:v",   # all video streams
            "-map", "0:a?",  # all audio streams (optional — ok if absent)
            "-map", "0:s?",  # all subtitle streams (optional)
            # tmcd and other data streams are intentionally omitted
        ]
    else:
        map_args = ["-map", "0"]

    # Timestamp fixup: rebase negative PTS to zero so muxers don't choke.
    ts_args = ["-avoid_negative_ts", "make_zero"] if has_neg_ts else []

    cmd = [
        "ffmpeg", "-y",
        "-i", filepath,
        *enc_args,
        "-c:a", "copy",
        "-c:s", "copy",
        *map_args,
        *ts_args,
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
    fixups = []
    if bad_tmcd:   fixups.append("dropped faulty tmcd")
    if has_neg_ts: fixups.append("rebased negative timestamps")
    fixup_note = f"  [{', '.join(fixups)}]" if fixups else ""
    print(GREEN(f"  ✔  Done in {_format_eta(elapsed_total)}  "
                f"{size_before} → {size_after}{fixup_note}"))
    log.info("OK  %s  (%s → %s)  elapsed %s  encoder %s%s",
             filepath, size_before, size_after, _format_eta(elapsed_total),
             encoder, fixup_note)
    return True


def collect_candidates(directory: str, recurse: bool) -> list[str]:
    """Return a sorted list of video file paths under directory."""
    matches = []
    if recurse:
        for root, dirs, files in os.walk(directory):
            dirs.sort()   # walk subdirectories in alphabetical order
            for fname in sorted(files):
                if os.path.splitext(fname)[1].lower() in VIDEO_EXTENSIONS:
                    matches.append(os.path.join(root, fname))
    else:
        for fname in sorted(os.listdir(directory)):
            fpath = os.path.join(directory, fname)
            if os.path.isfile(fpath) and os.path.splitext(fname)[1].lower() in VIDEO_EXTENSIONS:
                matches.append(fpath)
    return matches


# ── scanner ───────────────────────────────────────────────────────────────────

def scan_and_convert(directory: str, crf: int, preset: str, encoder: str,
                     dry_run: bool, batch_size: int | None, recurse: bool) -> None:
    if not os.path.isdir(directory):
        print(RED(f"[ERROR] Not a directory: {directory}"))
        log.error("Not a directory: %s", directory)
        sys.exit(1)

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print(RED("[ERROR] ffmpeg / ffprobe not found. Please install ffmpeg."))
        log.error("ffmpeg / ffprobe not found")
        sys.exit(1)

    candidates = collect_candidates(directory, recurse)

    if not candidates:
        print("No video files found.")
        return

    # Pre-scan all files so we can report a tally up front
    to_convert = []
    to_skip    = []

    print("Scanning files…")
    for filepath in candidates:
        info     = get_video_info(filepath)
        rel_path = os.path.relpath(filepath, directory)
        if info["codec"] is None:
            to_skip.append((rel_path, "unreadable"))
        elif info["codec"].lower() not in ("h264", "avc"):
            to_skip.append((rel_path, info["codec"]))
        else:
            if info["bad_tmcd"]:
                print(YELLOW(f"  [warn] {rel_path}  — faulty timecode stream detected, will be dropped"))
                log.warning("Faulty tmcd stream detected: %s", filepath)
            if info["has_neg_ts"]:
                print(YELLOW(f"  [warn] {rel_path}  — negative timestamps detected, will rebase to zero"))
                log.warning("Negative timestamps detected: %s", filepath)
            to_convert.append((filepath, info["duration"], info["bad_tmcd"], info["has_neg_ts"]))

    print(f"  {GREEN(str(len(to_convert)))} file(s) to convert, "
          f"{YELLOW(str(len(to_skip)))} skipped\n")

    for fname, reason in to_skip:
        print(DIM(f"  [skip] {fname}  ({reason})"))
        log.info("SKIP  %s  (%s)", fname, reason)
    if to_skip:
        print()

    if not to_convert:
        return

    # Apply batch limit
    total_eligible = len(to_convert)
    if batch_size is not None and batch_size < total_eligible:
        to_convert = to_convert[:batch_size]
        remaining  = total_eligible - batch_size
        print(YELLOW(f"  Batch limit: processing {batch_size} of {total_eligible} "
                     f"eligible file(s)  ({remaining} remaining for next run)"))
        log.info("Batch limit %d applied — %d of %d eligible files queued, %d deferred",
                 batch_size, batch_size, total_eligible, remaining)
        print()

    if dry_run:
        for fp, _, bad_tmcd, has_neg_ts in to_convert:
            flags = []
            if bad_tmcd:   flags.append("drop-tmcd")
            if has_neg_ts: flags.append("rebase-ts")
            flag_str = f"  [{', '.join(flags)}]" if flags else ""
            print(YELLOW(f"  [dry-run] {os.path.relpath(fp, directory)}{flag_str}"))
        if batch_size is not None and batch_size < total_eligible:
            print(DIM(f"  (+ {total_eligible - batch_size} more deferred by --batch)"))
        return

    converted = errors = 0
    total     = len(to_convert)

    for idx, (filepath, duration, bad_tmcd, has_neg_ts) in enumerate(to_convert, start=1):
        rel_path = os.path.relpath(filepath, directory)
        print(BOLD(f"Converting ({idx}/{total}): {rel_path}"))
        ok = convert_to_hevc(filepath, duration, crf=crf, preset=preset,
                             encoder=encoder, file_index=idx, file_total=total,
                             bad_tmcd=bad_tmcd, has_neg_ts=has_neg_ts)
        if ok:
            converted += 1
        else:
            errors += 1
        print()

    summary = (f"Finished — "
               f"{GREEN(str(converted))} converted, "
               f"{(RED(str(errors)) if errors else DIM('0'))} errors")
    if batch_size is not None and batch_size < total_eligible:
        summary += f"  ({total_eligible - batch_size} file(s) deferred — re-run to continue)"
    print(BOLD(summary))
    log.info("Run complete — converted: %d  errors: %d  skipped: %d  deferred: %d",
             converted, errors, len(to_skip),
             max(0, total_eligible - len(to_convert)))


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
        "--recurse", "-r",
        action="store_true",
        help="Recurse into subdirectories",
    )
    parser.add_argument(
        "--batch",
        metavar="N",
        type=int,
        default=None,
        help="Stop after converting N files (useful for scheduled/incremental runs)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files that would be converted without converting them",
    )
    parser.add_argument(
        "--nvenc",
        action="store_true",
        help="Use NVIDIA GPU encoder (hevc_nvenc) instead of libx265",
    )
    parser.add_argument(
        "--log-file",
        metavar="PATH",
        default=None,
        help="Append errors (and a run summary) to a log file",
    )
    args = parser.parse_args()

    # Resolve encoder
    if args.nvenc:
        if not check_nvenc_available():
            print(RED("[ERROR] hevc_nvenc is not available in your ffmpeg build."))
            print(DIM("        Install an ffmpeg build with NVIDIA GPU support, or omit --nvenc."))
            sys.exit(1)
        encoder = "hevc_nvenc"
    else:
        encoder = "libx265"

    if args.batch is not None and args.batch < 1:
        print(RED("[ERROR] --batch must be a positive integer."))
        sys.exit(1)

    if args.log_file:
        setup_logger(args.log_file)
        log.info("=" * 60)
        log.info("Session started  •  directory: %s  •  encoder: %s  •  CRF/QP %d  •  preset %s%s%s%s",
                 os.path.abspath(args.directory), encoder, args.crf, args.preset,
                 "  •  recurse" if args.recurse else "",
                 f"  •  batch {args.batch}" if args.batch else "",
                 "  •  DRY RUN" if args.dry_run else "")

    print(BOLD(f"convert_to_hevc  •  {os.path.abspath(args.directory)}"))
    encoder_label = YELLOW("hevc_nvenc (GPU)") if encoder == "hevc_nvenc" else "libx265 (CPU)"
    print(DIM(f"encoder {encoder_label}  •  CRF/QP {args.crf}  •  preset {args.preset}"
              + ("  •  recurse" if args.recurse else "")
              + (f"  •  batch {args.batch}" if args.batch else "")
              + ("  •  DRY RUN" if args.dry_run else "")
              + (f"  •  log → {args.log_file}" if args.log_file else "")))
    print()

    scan_and_convert(args.directory, crf=args.crf, preset=args.preset,
                     encoder=encoder, dry_run=args.dry_run, batch_size=args.batch,
                     recurse=args.recurse)


if __name__ == "__main__":
    main()