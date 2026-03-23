
Here's the script.

It requires **ffmpeg** to be installed — that's the only dependency.

### How it works:

1. Scans a directory for common video extensions (`.mp4`, `.mkv`, `.mov`, `.avi`, `.m4v`, `.ts`, `.mts`, `.m2ts`)
2. Uses `ffprobe` to detect each file's video codec
3. Converts only H.264 (`h264`/`avc`) files to HEVC using `libx265`
4. Writes the output to a temp file first, then atomically replaces the original — so the filename stays identical and you never end up with a half-written file on failure
5. Audio and subtitle streams are copied as-is (no re-encoding)

### Basic usage:
```
bash
# Convert files in the current directory
python convert_to_hevc.py

# Convert files in a specific directory
python convert_to_hevc.py /path/to/videos

# Preview what would be converted without doing anything
python convert_to_hevc.py /path/to/videos --dry-run
```

Optional flags:
|Flag | Default | Description |
| ----- | --------- | ------------- |
| `--crf` | `28` |Quality (18–28 is typical; lower = better quality, larger file)|
| `--preset` | `medium` |Speed vs compression tradeoff (fast, slow, veryslow, etc.)|
| `--dry-run` | `off` |List candidates without converting|

Tip: Run with --dry-run first to verify which files will be touched before committing to the conversion.


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
python convert_to_hevc.py /path/to/videos --log-file conversion.log
```

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

added optional nvenc:
python convert_to_hevc.py /path/to/videos --nvenc
```

A few things worth knowing about the implementation:

**Availability check** — before any conversion starts, the script probes `ffmpeg -encoders` to confirm `hevc_nvenc` is actually present. If it's not (e.g. you're on a CPU-only ffmpeg build), it exits immediately with a clear error rather than failing mid-batch.

**Quality parameter** — nvenc doesn't support `-crf` the way libx265 does, so the script maps it to `-qp` instead, which is the closest equivalent. The same `--crf` value you'd use for libx265 works as a reasonable starting point for nvenc too, though nvenc tends to be less efficient so you may want a slightly lower number (higher quality) like `--crf 24`.

**Preset** — nvenc uses its own preset scale (`p1` fastest through `p7` slowest), which is different from libx265's named presets. The script pins nvenc to `p4` (balanced) regardless of `--preset`, since the two scales aren't directly comparable. The `--preset` flag still applies normally when using libx265.

**Header display** — the encoder is shown in the startup line so it's always clear which path is being used:
```
encoder hevc_nvenc (GPU)  •  CRF/QP 28  •  preset medium