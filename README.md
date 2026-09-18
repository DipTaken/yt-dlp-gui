# YT-DLP GUI

A clean, dark-themed desktop GUI for [yt-dlp](https://github.com/yt-dlp/yt-dlp) — built with Python's standard `tkinter`, no extra GUI dependencies required.

Wraps the full power of the `yt-dlp` library behind a friendly interface: paste URLs, pick a quality preset, and download. Includes a built-in **Converter** tab for transcoding local audio/video files via FFmpeg.

## Features

### Downloader
- Paste a link and press **Enter** to download — no second click needed (Shift+Enter queues without starting)
- Titles and durations are looked up in the background, so a waiting queue reads as titles rather than URLs
- Per-item progress bars with speed, ETA, and status badges
- Playlists download as one item that reports `3/27`, and survive a private or deleted entry instead of failing outright
- Format presets: Best Video+Audio, MP4, 4K, 1080p, 720p, 480p, 360p, Audio Only, Custom
- Audio extraction in **MP3, WAV, FLAC, AAC, M4A, Opus, Vorbis, ALAC**
- **Practice controls** — slow a solo to 50–90% without dropping its pitch, transpose up to an octave either way, or isolate one side of a stereo mix. The speed and key are written into the filename
- Audio normalization (FFmpeg `loudnorm`) and sample-rate conversion
- **Per-item clip ranges** — queue the same video twice for two different sections; each file is named after its range
- **Chapters tab** in Fetch Info: queue individual chapters as clips instead of pulling the whole upload
- **After-download conversion** — send finished files straight through a Converter preset, e.g. download to a ProRes proxy in one step
- Subtitle download / embedding (with auto-generated subs option)
- Thumbnail and metadata embedding
- SponsorBlock integration with category selection
- Cookie import from browser (Chrome, Firefox, Edge, Brave, etc.) or file
- Network options: rate limit, proxy, retries, concurrent fragments
- Filters: max filesize, date range, single-video (ignore playlist)
- Failed downloads explain themselves in plain language (cookies needed, rate-limited, disk full…) with the raw error kept underneath
- **Open** and **Show in folder** on finished cards; right-click any card for the full set of actions
- Retry a single item, or **Retry Failed** for everything that went wrong at once
- Optional clipboard watching, and an unfinished queue that comes back after a restart

### Converter
- Add multiple local audio/video files via file picker
- Convert to any audio format (MP3, WAV, FLAC, AAC, M4A, OGG, Opus, ALAC)
- Convert to any video format (MP4 H.264 / H.265, MKV, WebM, MOV, AVI)
- Editing codecs — **ProRes** and **DNxHR** with real profile choices, constant-frame-rate conversion, and downscale-only resizing
- Practice speed, transpose and channel isolation for audio targets, written into the output filename
- EBU R128 loudness normalization (streaming, podcast and broadcast targets)
- Quick presets for music, delivery, editing and practice
- Per-file progress bars (parsed from FFmpeg `time=` output)
- Quality controls: audio bitrate (kbps) or video CRF
- Cancel individual conversions at any time

### Clip editor
- Frame-accurate in/out points against a real preview, with a waveform and keyframe markers
- **Play the selection**, optionally on a loop and at 25–100% speed, to check a cut by ear
- **Save frame** writes the current frame to PNG at the source's full resolution
- Audio-only files get a tall waveform instead of an empty video pane

### General
- Catppuccin Mocha dark color palette throughout
- Settings persist to `gui_settings.json` automatically on close
- Real-time color-coded log strip at the bottom
- App-level settings dialog for FFmpeg path configuration, with a download link when it is missing

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

1. Paste a URL into the top bar and press **Enter** — it downloads straight away
2. To set things up first, press **Shift+Enter** (or click **Add**) to queue without starting
3. Configure format/output settings in the right panel, then **Download Selected** or **Download All**
4. Double-click a queued item to pick in/out points; right-click any card for its full menu
5. Switch to the **Converter** tab to transcode local files

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
