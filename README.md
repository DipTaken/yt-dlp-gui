# YT-DLP GUI

A clean, dark-themed desktop GUI for [yt-dlp](https://github.com/yt-dlp/yt-dlp) — built with Python's standard `tkinter`, no extra GUI dependencies required.

Wraps the full power of the `yt-dlp` library behind a friendly interface: paste URLs, pick a quality preset, and download. Includes a built-in **Converter** tab for transcoding local audio/video files via FFmpeg.

## Features

### Downloader
- Paste one or more URLs and queue them for download
- Per-item progress bars with speed, ETA, and status badges
- Format presets: Best Video+Audio, MP4, 4K, 1080p, 720p, 480p, 360p, Audio Only, Custom
- Audio extraction in **MP3, WAV, FLAC, AAC, M4A, OGG, Opus, Vorbis, ALAC**
- Audio normalization (FFmpeg `loudnorm`) and sample-rate conversion
- Subtitle download / embedding (with auto-generated subs option)
- Thumbnail and metadata embedding
- SponsorBlock integration with category selection
- Cookie import from browser (Chrome, Firefox, Edge, Brave, etc.) or file
- Network options: rate limit, proxy, retries, concurrent fragments
- Filters: max filesize, date range, single-video (ignore playlist)
- Retry button on failed downloads, full error messages in cards and log
- "Fetch Info" popup showing all available formats in a sortable table

### Converter
- Add multiple local audio/video files via file picker
- Convert to any audio format (MP3, WAV, FLAC, AAC, M4A, OGG, Opus, ALAC)
- Convert to any video format (MP4 H.264 / H.265, MKV, WebM, MOV, AVI)
- Per-file progress bars (parsed from FFmpeg `time=` output)
- Quality controls: audio bitrate (kbps) or video CRF
- Cancel individual conversions at any time

### General
- Catppuccin Mocha dark color palette throughout
- Settings persist to `gui_settings.json` automatically on close
- Real-time color-coded log strip at the bottom
- App-level settings dialog for FFmpeg path configuration

## Installation

### Requirements
- **Python 3.9+**
- **FFmpeg** — required for format merging, audio extraction, embedding thumbnails/subtitles, and the Converter tab

The GUI auto-detects FFmpeg in:
- `PATH`
- Common Windows install dirs (`C:\ffmpeg\bin`, Program Files, etc.)
- Scoop, Chocolatey, Winget locations
- Same folder as `gui.py` (drop in `ffmpeg.exe` for a portable setup)

If not found, you can set the path manually via the gear icon in the top-right of the app.

### Setup
```bash
git clone https://github.com/DipTaken/yt-dlp-gui
cd yt-dlp-gui
pip install -e .   # installs yt-dlp from the bundled source
```

## Usage

```bash
python gui.py
```

For a console-free launch on Windows:
```bash
pythonw gui.py
```

1. Paste a URL into the top bar (Enter or click **Add**)
2. Configure format/output settings in the right panel
3. Click **Download Selected** or **Download All**
4. Switch to the **Converter** tab to transcode local files

## Configuration

User settings are saved to `gui_settings.json` in the project root, including:
- Output directory and filename template
- Format preset and audio options
- Subtitle, thumbnail, and metadata preferences
- Network and cookie settings
- FFmpeg path override
- Converter defaults

Delete this file to reset to defaults.

## Credits

- Built on top of [yt-dlp](https://github.com/yt-dlp/yt-dlp) — all download functionality is provided by the upstream library
- Conversion powered by [FFmpeg](https://ffmpeg.org)
- Color palette: [Catppuccin Mocha](https://github.com/catppuccin/catppuccin)

## License

This project follows yt-dlp's [Unlicense](LICENSE) — public domain.
