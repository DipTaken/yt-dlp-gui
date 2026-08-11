"""YT-DLP GUI — a tkinter front-end for the vendored yt-dlp.

Layout:
    config        settings schema, presets, format/codec tables
    theme         palette + ttk styles
    models        DownloadItem / ConvItem / GUILogger
    ffmpeg_utils  locating and driving ffmpeg
    download      yt-dlp option building (no tkinter)
    convert       ffmpeg argument building (no tkinter)
    widgets       shared widget builders
    app           the App window itself
"""
