
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