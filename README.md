
Here's the script.

It requires **ffmpeg** to be installed — that's the primary dependency.
If `ffmpeg` conversion fails and `HandBrakeCLI` / `handbrake-cli` is installed, the script will automatically retry with HandBrakeCLI to preserve resolution and playback quality.

## How it works:

1. Scans a directory for common video extensions (`.mp4`, `.mkv`, `.mov`, `.avi`, `.m4v`, `.ts`, `.mts`, `.m2ts`)
2. Uses `ffprobe` to detect each file's video codec
3. Converts only H.264 (`h264`/`avc`) files to HEVC using `libx265`
4. Writes the output to a temp file first, then atomically replaces the original — so the filename stays identical and you never end up with a half-written file on failure
5. Audio and subtitle streams are copied as-is (no re-encoding)

## Basic usage:

### Convert files in the current directory
`python convert_to_hevc.py`

### Convert files in a specific directory
`python convert_to_hevc.py /path/to/videos`

### Preview what would be converted without doing anything
`python convert_to_hevc.py /path/to/videos --dry-run`


Optional flags:
|Flag | Default | Description |
| ----- | --------- | ------------- |
| `--crf` | `28` |Quality (18–28 is typical; lower = better quality, larger file)|
| `--preset` | `medium` |Speed vs compression tradeoff (fast, slow, veryslow, etc.)|
| `--dry-run` | `off` |List candidates without converting|
| `--batch` | no default | limits processing to the batch size, scans as normal|
| `--recurse` | no arguments | recurses through subdirectories in alphabetical order|
| `--nvenc` | no arguments | leverages NVidia encoder if available|
Tip: Run with `--dry-run` first to verify which files will be touched before committing to the conversion.


### Here's what's new in the updated script:

Live progress bar — uses ffmpeg's `-progress pipe:2` flag to receive structured `key=value` updates from ffmpeg in real time. Each update redraws a 3-line block in place:

```
  [1/3] my_video.mp4
  ████████████████░░░░░░░░░░░░░░  52.3%
  Elapsed 00:42  ETA 00:38  Speed 1.43x
```

Other improvements:

- Pre-scan phase — probes all files first and prints a summary (`3 file(s) to convert, 2 skipped`) before any conversion starts
- File size comparison — shows before/after size on completion (e.g. `1.2 GB → 820.4 MB`)
- Elapsed time — reports how long each file took when done
- Color output — green/yellow/red/dim highlights that auto-disable when piping to a file or non-TTY
- Clean Ctrl-C handling — kills ffmpeg gracefully and clears the progress block instead of leaving a half-drawn bar
- Better error output — filters out the noisy ffmpeg progress lines and shows only the relevant error messages on failure


Adding logging function:

Usage: `python convert_to_hevc.py /path/to/videos --log-file conversion.log`

**What gets logged:**

| Event | Level |
|---|---|
| Session start (directory, CRF, preset) | `INFO` |
| Skipped files (and why) | `INFO` |
| Successful conversions (sizes, elapsed time) | `INFO` |
| ffmpeg failures (exit code + full diagnostic output) | `ERROR` |
| Run summary (converted / errors / skipped counts) | `INFO` |

**Example log output:**
```
2026-03-22 14:01:00  INFO      ============================================================
2026-03-22 14:01:00  INFO      Session started  •  directory: /videos  •  CRF 28  •  preset medium
2026-03-22 14:01:01  INFO      SKIP  holiday.mp4  (hevc)
2026-03-22 14:02:45  INFO      OK  /videos/clip.mp4  (1.2 GB → 820.4 MB)  elapsed 01:44
2026-03-22 14:03:10  ERROR     Conversion FAILED: /videos/broken.mp4  (ffmpeg exit code 1)
2026-03-22 14:03:10  ERROR       ffmpeg: Invalid data found when processing input
2026-03-22 14:03:10  INFO      Run complete — converted: 1  errors: 1  skipped: 1
```

## added optional nvenc support:
usage: `python convert_to_hevc.py /path/to/videos --nvenc`

## automatic HandBrakeCLI fallback:
If `ffmpeg` fails during conversion and `HandBrakeCLI` or `handbrake-cli` is available, the script retries using HandBrakeCLI with `x265` and preserves resolution, aspect ratio, audio, and subtitles when possible. The fallback conversion displays the same live progress bar as ffmpeg, so you get real-time feedback on encoding progress, elapsed time, and ETA.

A few things worth knowing about the implementation:

**Availability check** — before any conversion starts, the script probes `ffmpeg -encoders` to confirm `hevc_nvenc` is actually present. If it's not (e.g. you're on a CPU-only ffmpeg build), it exits immediately with a clear error rather than failing mid-batch.

**Quality parameter** — nvenc doesn't support `-crf` the way libx265 does, so the script maps it to `-qp` instead, which is the closest equivalent. The same `--crf` value you'd use for libx265 works as a reasonable starting point for nvenc too, though nvenc tends to be less efficient so you may want a slightly lower number (higher quality) like `--crf 24`.

**Preset** — nvenc uses its own preset scale (`p1` fastest through `p7` slowest), which is different from libx265's named presets. The script pins nvenc to `p4` (balanced) regardless of `--preset`, since the two scales aren't directly comparable. The `--preset` flag still applies normally when using libx265.

**Header display** — the encoder is shown in the startup line so it's always clear which path is being used:
```
encoder hevc_nvenc (GPU)  •  CRF/QP 28  •  preset medium
```


Added batch function:
usage: `python convert_to_hevc.py /path/to/videos --batch 5`

**How it works:**

The script scans the full directory as normal, then slices the eligible H.264 files down to the first N before starting any conversions. Files are always processed in alphabetical order, so each run picks up the next N unconverted files naturally — since converted files are re-encoded to HEVC in-place, they'll be skipped on subsequent runs.

The output makes it clear what was deferred:
```
  Batch limit: processing 5 of 23 eligible file(s)  (18 remaining for next run)
  ...
Finished — 5 converted, 0 errors  (18 file(s) deferred — re-run to continue)
```

This pairs well with a cron job or Task Scheduler entry if you want to spread encoding across low-activity windows — just run with `--batch 5` nightly until the directory is fully converted. `--dry-run --batch N` also works to preview exactly which files would be picked up.

added:
Use `--recurse` (or the shorthand `-r`):
Usage: `python convert_to_hevc.py /path/to/videos --recurse`

It works with all other flags too — for example, a recursive dry-run with a batch limit:

`python convert_to_hevc.py /media/library --recurse --batch 10 --dry-run`


A couple of implementation details worth noting:

**Ordering** — subdirectories are walked alphabetically and files within each directory are also sorted alphabetically, so the order is consistent and predictable across runs. This means `--batch` behaves reliably for incremental processing: each run picks up the next N files in the same deterministic order.

**Display** — skip and convert lines now show the relative path from the root directory rather than just the bare filename, so you can tell at a glance which subfolder each file belongs to:
```
  [skip] Movies/oldfilm.mp4  (hevc)
  [skip] TV/Show/ep01.mkv   (av1)
Converting (1/4): TV/Show/ep02.mkv
```

The script now handles two common classes of timecode fault:
1. Faulty `tmcd` streams — H.264 files (especially those from cameras or capture cards) sometimes carry a `tmcd` (timecode) track with a wildly out-of-range `start_time`, which causes ffmpeg to error or produce output with broken timestamps. The pre-scan now inspects every stream, and if it finds a `tmcd` with a start time beyond ±24 hours it marks the file as affected. During conversion, instead of `-map 0` (copy all streams), it uses explicit per-type mapping (`-map 0:v -map 0:a? -map 0:s?`) to carry over video, audio, and subtitles while silently dropping the faulty timecode track.
2. Negative timestamps — some files have a negative container `start_time`, which can cause certain muxers (especially MP4) to reject the output. When detected, `-avoid_negative_ts make_zero` is added to the ffmpeg command to rebase all timestamps to zero before encoding.

Both fixups are detected during the pre-scan phase and flagged with a yellow warning so you know they're being applied before conversion starts:
```
  [warn] footage/clip.mp4  — faulty timecode stream detected, will be dropped
  [warn] footage/raw.mp4   — negative timestamps detected, will rebase to zero
```
And they're noted on the completion line and in the log:
```
  ✔  Done in 01:23  1.2 GB → 820.4 MB  [dropped faulty tmcd]
```