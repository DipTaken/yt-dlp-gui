#!/usr/bin/env python3
"""
YT-DLP GUI — A clean, feature-rich graphical interface for yt-dlp.

Run:  python gui.py
"""
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import json
import uuid
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

# ── Make sure yt_dlp is importable from this project directory ───────────────
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from yt_dlp import YoutubeDL
from yt_dlp.utils import format_bytes

# ─────────────────────────────────────────────────────────────────────────────
# Catppuccin Mocha palette
# ─────────────────────────────────────────────────────────────────────────────
BASE   = '#1e1e2e'
MANTLE = '#181825'
CRUST  = '#11111b'
SURF0  = '#313244'
SURF1  = '#45475a'
SURF2  = '#585b70'
OVL0   = '#6c7086'
TEXT   = '#cdd6f4'
SUBT0  = '#a6adc8'
MAUVE  = '#cba6f7'
BLUE   = '#89b4fa'
GREEN  = '#a6e3a1'
RED    = '#f38ba8'
YELLOW = '#f9e2af'
PEACH  = '#fab387'

# ─────────────────────────────────────────────────────────────────────────────
# FFmpeg detection
# ─────────────────────────────────────────────────────────────────────────────
_FFMPEG_SEARCH_PATHS = [
    # Common Windows install locations
    r'C:\ffmpeg\bin\ffmpeg.exe',
    r'C:\Program Files\ffmpeg\bin\ffmpeg.exe',
    r'C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe',
    os.path.join(os.environ.get('LOCALAPPDATA', ''), 'ffmpeg', 'bin', 'ffmpeg.exe'),
    os.path.join(os.environ.get('APPDATA', ''), 'ffmpeg', 'bin', 'ffmpeg.exe'),
    # Scoop
    os.path.join(os.environ.get('USERPROFILE', ''), 'scoop', 'apps', 'ffmpeg', 'current', 'bin', 'ffmpeg.exe'),
    # Chocolatey
    r'C:\ProgramData\chocolatey\bin\ffmpeg.exe',
    # Winget default
    os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'WinGet', 'Packages',
                 'Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe', 'ffmpeg-*', 'bin', 'ffmpeg.exe'),
    # yt-dlp bundled (same dir as this script)
    os.path.join(ROOT, 'ffmpeg.exe'),
    os.path.join(ROOT, 'ffmpeg', 'ffmpeg.exe'),
    os.path.join(ROOT, 'bin', 'ffmpeg.exe'),
]


def find_ffmpeg() -> str:
    """Return the path to ffmpeg, or '' if not found."""
    # 1. Check PATH first (most reliable)
    on_path = shutil.which('ffmpeg')
    if on_path:
        return on_path
    # 2. Check known install locations
    for p in _FFMPEG_SEARCH_PATHS:
        if '*' in p:
            # Glob expansion for wildcard paths
            import glob
            matches = glob.glob(p)
            if matches:
                return matches[0]
        elif os.path.isfile(p):
            return p
    return ''


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
SETTINGS_FILE = os.path.join(ROOT, 'gui_settings.json')

FORMAT_PRESETS = {
    'Best (Video + Audio)': 'bestvideo+bestaudio/best',
    'Best MP4':             'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
    '4K (2160p)':           'bestvideo[height<=2160]+bestaudio/best[height<=2160]',
    '1080p':                'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]',
    '720p':                 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]',
    '480p':                 'bestvideo[height<=480]+bestaudio/best[height<=480]',
    '360p':                 'bestvideo[height<=360]+bestaudio/best[height<=360]',
    'Audio Only (Best)':    'bestaudio/best',
    'Custom…':              '',
}

SB_CATEGORIES = ['all', 'sponsor', 'intro', 'outro', 'selfpromo',
                  'interaction', 'music_offtopic', 'preview', 'filler']

# ─────────────────────────────────────────────────────────────────────────────
# Converter constants
# ─────────────────────────────────────────────────────────────────────────────
CONV_AUDIO_FORMATS = ['MP3', 'WAV', 'FLAC', 'AAC', 'M4A', 'OGG', 'Opus', 'ALAC']
CONV_VIDEO_FORMATS = ['MP4 (H.264)', 'MP4 (H.265/HEVC)', 'MKV', 'WebM', 'MOV', 'AVI']
CONV_LOSSLESS      = {'WAV', 'FLAC', 'ALAC'}

# (base_ffmpeg_args, kind, output_ext)
CONV_FORMAT_INFO: dict[str, tuple[list, str, str]] = {
    'MP3':              (['-c:a', 'libmp3lame'],                              'audio', 'mp3'),
    'WAV':              (['-c:a', 'pcm_s16le'],                              'audio', 'wav'),
    'FLAC':             (['-c:a', 'flac'],                                   'audio', 'flac'),
    'AAC':              (['-c:a', 'aac'],                                    'audio', 'm4a'),
    'M4A':              (['-c:a', 'aac', '-movflags', 'faststart'],          'audio', 'm4a'),
    'OGG':              (['-c:a', 'libvorbis'],                              'audio', 'ogg'),
    'Opus':             (['-c:a', 'libopus'],                                'audio', 'opus'),
    'ALAC':             (['-c:a', 'alac', '-movflags', 'faststart'],         'audio', 'm4a'),
    'MP4 (H.264)':      (['-c:v', 'libx264', '-c:a', 'aac',
                          '-movflags', '+faststart'],                         'video', 'mp4'),
    'MP4 (H.265/HEVC)': (['-c:v', 'libx265', '-c:a', 'aac',
                          '-movflags', '+faststart'],                         'video', 'mp4'),
    'MKV':              (['-c:v', 'libx264', '-c:a', 'aac'],                'video', 'mkv'),
    'WebM':             (['-c:v', 'libvpx-vp9', '-b:v', '0', '-c:a',
                          'libopus'],                                         'video', 'webm'),
    'MOV':              (['-c:v', 'libx264', '-c:a', 'aac'],                'video', 'mov'),
    'AVI':              (['-c:v', 'libx264', '-c:a', 'mp3'],                'video', 'avi'),
}

DEFAULT_SETTINGS = {
    'output_dir':           str(Path.home() / 'Downloads'),
    'output_template':      '%(title)s [%(id)s].%(ext)s',
    'format_preset':        'Best (Video + Audio)',
    'custom_format':        '',
    'audio_extract':        False,
    'audio_format':         'mp3',
    'audio_quality':        '192',
    'audio_normalize':      False,
    'audio_sample_rate':    '',
    'embed_thumbnail':      True,
    'write_thumbnail':      False,
    'embed_metadata':       True,
    'write_infojson':       False,
    'write_subs':           False,
    'auto_subs':            False,
    'embed_subs':           True,
    'sub_langs':            'en',
    'sponsorblock_enabled': False,
    'sponsorblock_cats':    'all',
    'rate_limit':           '',
    'proxy':                '',
    'retries':              '3',
    'concurrent_fragments': '4',
    'no_playlist':          False,
    'max_filesize':         '',
    'date_after':           '',
    'date_before':          '',
    'cookie_browser':       '',
    'cookie_file':          '',
    'ffmpeg_path':          '',   # '' means "auto-detect"
    # Converter
    'conv_output_dir':      str(Path.home() / 'Downloads'),
    'conv_format_type':     'Audio',
    'conv_output_format':   'MP3',
    'conv_audio_quality':   '192',
    'conv_video_crf':       '23',
    'conv_overwrite':       True,
}


# ─────────────────────────────────────────────────────────────────────────────
# Logger — routes yt-dlp output into the GUI message queue
# ─────────────────────────────────────────────────────────────────────────────
class _GUILogger:
    def __init__(self, msg_q: queue.Queue, item_id: str):
        self._q  = msg_q
        self._id = item_id

    def debug(self, msg):
        if msg.startswith('[debug]'):
            return
        self._q.put(('log', msg.rstrip('\n') + '\n', ''))

    def info(self, msg):
        self._q.put(('log', msg.rstrip('\n') + '\n', ''))

    def warning(self, msg):
        self._q.put(('log', '[WARN] ' + msg.rstrip('\n') + '\n', 'yellow'))

    def error(self, msg):
        self._q.put(('log', '[ERR]  ' + msg.rstrip('\n') + '\n', 'red'))
        self._q.put(('set_error', self._id, msg.rstrip('\n')))


# ─────────────────────────────────────────────────────────────────────────────
# DownloadItem
# ─────────────────────────────────────────────────────────────────────────────
class DownloadItem:
    PENDING     = 'pending'
    FETCHING    = 'fetching'
    DOWNLOADING = 'downloading'
    CONVERTING  = 'converting'
    DONE        = 'done'
    ERROR       = 'error'
    CANCELLED   = 'cancelled'

    def __init__(self, url: str):
        self.id       = uuid.uuid4().hex[:8]
        self.url      = url
        self.title    = url
        self.status   = self.PENDING
        self.progress = 0.0
        self.speed    = ''
        self.eta      = ''
        self.size_str = ''
        self.error    = ''
        self._cancel  = threading.Event()

    def cancel(self):
        self._cancel.set()

    def reset(self):
        """Reset for a retry."""
        self._cancel = threading.Event()
        self.status   = self.PENDING
        self.error    = ''
        self.progress = 0.0
        self.speed    = ''
        self.eta      = ''
        self.size_str = ''

    @property
    def is_cancelled(self) -> bool:
        return self._cancel.is_set()


# ─────────────────────────────────────────────────────────────────────────────
# ConvItem
# ─────────────────────────────────────────────────────────────────────────────
class ConvItem:
    PENDING   = 'pending'
    RUNNING   = 'running'
    DONE      = 'done'
    ERROR     = 'error'
    CANCELLED = 'cancelled'

    def __init__(self, path: str):
        self.id       = uuid.uuid4().hex[:8]
        self.path     = path
        self.filename = os.path.basename(path)
        self.duration = 0.0      # probed seconds, 0 = unknown
        self.status   = self.PENDING
        self.progress = 0.0
        self.error    = ''
        self._proc: 'subprocess.Popen | None' = None
        self._cancel  = threading.Event()

    def cancel(self):
        self._cancel.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('YT-DLP GUI')
        self.geometry('1200x760')
        self.minsize(900, 620)
        self.configure(bg=BASE)
        self._apply_dark_titlebar(self)

        self.settings = self._load_settings()
        self.items: dict[str, DownloadItem] = {}
        self.msg_q: queue.Queue = queue.Queue()
        self._threads: dict[str, threading.Thread] = {}

        # Converter state
        self.conv_items: dict[str, ConvItem] = {}
        self._conv_widgets: dict[str, dict] = {}
        self._conv_threads: dict[str, threading.Thread] = {}
        self._conv_canvas: 'tk.Canvas | None' = None
        self._conv_empty_lbl: 'tk.Label | None' = None

        # FFmpeg state (resolved at startup; may be overridden by settings)
        self._ffmpeg_path = self._resolve_ffmpeg()

        # Global mousewheel routing
        self._wheel_target: 'tk.Canvas | None' = None
        self.bind_all('<MouseWheel>', self._on_mousewheel)

        self._build_styles()
        self._build_ui()
        self._poll()

    # ── helpers ───────────────────────────────────────────────────────────────
    def _apply_dark_titlebar(self, win: tk.BaseWidget):
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int))
        except Exception:
            pass

    def _resolve_ffmpeg(self) -> str:
        """Return the effective ffmpeg path from settings or auto-detection."""
        saved = self.settings.get('ffmpeg_path', '').strip()
        if saved and os.path.isfile(saved):
            return saved
        return find_ffmpeg()

    # ── settings persistence ──────────────────────────────────────────────────
    def _load_settings(self) -> dict:
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, encoding='utf-8') as f:
                    return {**DEFAULT_SETTINGS, **json.load(f)}
            except Exception:
                pass
        return dict(DEFAULT_SETTINGS)

    def _save_settings(self):
        try:
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=2)
        except Exception:
            pass

    # ── styles ────────────────────────────────────────────────────────────────
    def _build_styles(self):
        st = ttk.Style(self)
        st.theme_use('clam')
        st.configure('.', background=BASE, foreground=TEXT,
                     font=('Segoe UI', 10), borderwidth=0, relief='flat')
        st.configure('TFrame', background=BASE)
        st.configure('TLabel', background=BASE, foreground=TEXT, font=('Segoe UI', 10))

        # Combobox
        st.configure('TCombobox', fieldbackground=SURF0, foreground=TEXT,
                     background=SURF0, selectbackground=SURF1,
                     arrowcolor=SUBT0, borderwidth=1, padding=(8, 5))
        st.map('TCombobox',
               fieldbackground=[('focus', SURF1)],
               selectbackground=[('!focus', SURF0)],
               bordercolor=[('focus', MAUVE), ('!focus', SURF1)])

        # Checkbutton
        st.configure('TCheckbutton', background=MANTLE, foreground=TEXT,
                     focuscolor='', indicatorcolor=SURF1)
        st.map('TCheckbutton',
               background=[('active', MANTLE)],
               foreground=[('active', TEXT)],
               indicatorcolor=[('selected', MAUVE), ('!selected', SURF1)])

        # Progress bars
        st.configure('Horizontal.TProgressbar', background=MAUVE,
                     troughcolor=SURF1, borderwidth=0, thickness=5)
        st.configure('Green.Horizontal.TProgressbar', background=GREEN,
                     troughcolor=SURF1, borderwidth=0, thickness=5)

        # Notebook (tabbed panels)
        st.configure('TNotebook', background=MANTLE, borderwidth=0,
                     tabmargins=[0, 0, 0, 0])
        st.configure('TNotebook.Tab', background=SURF0, foreground=SUBT0,
                     font=('Segoe UI', 10, 'bold'), padding=[16, 8], borderwidth=0)
        st.map('TNotebook.Tab',
               background=[('selected', BASE), ('active', SURF1)],
               foreground=[('selected', MAUVE), ('active', TEXT)])

        # Scrollbar
        st.configure('TScrollbar', background=SURF0, troughcolor=MANTLE,
                     arrowcolor=SUBT0, borderwidth=0, width=10)
        st.map('TScrollbar', background=[('active', SURF1)])

        # Treeview
        st.configure('Treeview', background=SURF0, fieldbackground=SURF0,
                     foreground=TEXT, rowheight=24, font=('Segoe UI', 9),
                     borderwidth=0)
        st.configure('Treeview.Heading', background=MANTLE, foreground=MAUVE,
                     font=('Segoe UI', 9, 'bold'), borderwidth=0)
        st.map('Treeview',
               background=[('selected', MAUVE)],
               foreground=[('selected', CRUST)])

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self._build_header()
        tk.Frame(self, bg=SURF1, height=1).pack(fill='x')

        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True)

        # ── Tab 1: Downloader ─────────────────────────────────────────────────
        dl_tab = tk.Frame(nb, bg=BASE)
        nb.add(dl_tab, text='  Downloader  ')
        content = tk.Frame(dl_tab, bg=BASE)
        content.pack(fill='both', expand=True)
        self._build_queue_panel(content)
        tk.Frame(content, bg=SURF1, width=1).pack(side='left', fill='y')
        self._build_settings_panel(content)

        # ── Tab 2: Converter ──────────────────────────────────────────────────
        conv_tab = tk.Frame(nb, bg=BASE)
        nb.add(conv_tab, text='  Converter  ')
        self._build_converter_tab(conv_tab)

        tk.Frame(self, bg=SURF1, height=1).pack(fill='x')
        self._build_bottom()

    # ─── Header ──────────────────────────────────────────────────────────────
    def _build_header(self):
        hdr = tk.Frame(self, bg=MANTLE)
        hdr.pack(fill='x')

        # Logo
        tk.Label(hdr, text='YT-DLP GUI', bg=MANTLE, fg=MAUVE,
                 font=('Segoe UI', 14, 'bold')).pack(side='left', padx=(16, 6), pady=12)
        tk.Label(hdr, text='v2026.03', bg=MANTLE, fg=OVL0,
                 font=('Segoe UI', 9)).pack(side='left', pady=12)

        # Settings gear button (right side, packed before URL so it's rightmost)
        tk.Button(hdr, text=' ⚙ ', command=self._open_settings_dialog,
                  bg=SURF0, fg=SUBT0, font=('Segoe UI', 11),
                  relief='flat', bd=0, padx=10, pady=6,
                  cursor='hand2', activebackground=SURF1,
                  activeforeground=TEXT).pack(side='right', padx=(4, 12))

        # FFmpeg badge
        self._ffmpeg_badge = tk.Label(hdr, bg=MANTLE, font=('Segoe UI', 8, 'bold'),
                                      cursor='hand2')
        self._ffmpeg_badge.pack(side='right', padx=(4, 2))
        self._ffmpeg_badge.bind('<Button-1>', lambda _e: self._open_settings_dialog())
        self._refresh_ffmpeg_badge()

        # URL entry
        url_wrap = tk.Frame(hdr, bg=MANTLE)
        url_wrap.pack(side='left', fill='x', expand=True, padx=16, pady=10)

        self._url_placeholder = 'Paste one or more URLs (Enter or comma-separated)…'
        self.url_var = tk.StringVar()
        self.url_entry = tk.Entry(
            url_wrap, textvariable=self.url_var,
            bg=SURF0, fg=SUBT0, insertbackground=TEXT,
            font=('Segoe UI', 10), relief='flat', bd=0,
            highlightthickness=1, highlightbackground=SURF1,
            highlightcolor=MAUVE)
        self.url_entry.insert(0, self._url_placeholder)
        self.url_entry.pack(side='left', fill='x', expand=True, ipady=6, padx=(0, 4))
        self.url_entry.bind('<FocusIn>',  self._url_focus_in)
        self.url_entry.bind('<FocusOut>', self._url_focus_out)
        self.url_entry.bind('<Return>',   lambda _e: self._add_urls())

        _bkw = {'bg': SURF0, 'fg': TEXT, 'font': ('Segoe UI', 9),
                 'relief': 'flat', 'bd': 0, 'padx': 10, 'pady': 5,
                 'cursor': 'hand2', 'activebackground': SURF1, 'activeforeground': TEXT}

        tk.Button(url_wrap, text='Add', command=self._add_urls,
                  **{**_bkw, 'bg': MAUVE, 'fg': CRUST,
                     'activebackground': '#b89be6',
                     'font': ('Segoe UI', 9, 'bold')}
                  ).pack(side='left', padx=(0, 2))
        tk.Button(url_wrap, text='Paste', command=self._paste_url,
                  **_bkw).pack(side='left', padx=2)
        tk.Button(url_wrap, text='Fetch Info', command=self._fetch_info_btn,
                  **_bkw).pack(side='left', padx=2)

    def _refresh_ffmpeg_badge(self):
        if self._ffmpeg_path:
            self._ffmpeg_badge.configure(
                text=' FFmpeg ✓ ', bg='#1a3a1a', fg=GREEN)
        else:
            self._ffmpeg_badge.configure(
                text=' FFmpeg ✗ ', bg='#3a1a1a', fg=RED)

    def _url_focus_in(self, _e):
        if self.url_var.get() == self._url_placeholder:
            self.url_entry.delete(0, 'end')
            self.url_entry.configure(fg=TEXT)

    def _url_focus_out(self, _e):
        if not self.url_var.get().strip():
            self.url_entry.insert(0, self._url_placeholder)
            self.url_entry.configure(fg=SUBT0)

    def _paste_url(self):
        try:
            text = self.clipboard_get().strip()
            self.url_entry.delete(0, 'end')
            self.url_entry.configure(fg=TEXT)
            self.url_entry.insert(0, text)
        except Exception:
            pass

    # ─── Settings dialog ─────────────────────────────────────────────────────
    def _open_settings_dialog(self):
        dlg = tk.Toplevel(self)
        dlg.title('App Settings')
        dlg.geometry('520x260')
        dlg.configure(bg=BASE)
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)
        self._apply_dark_titlebar(dlg)

        # ── FFmpeg section ────────────────────────────────────────────────────
        self._dlg_sec(dlg, 'FFMPEG')

        ffmpeg_frame = tk.Frame(dlg, bg=BASE)
        ffmpeg_frame.pack(fill='x', padx=20, pady=(4, 0))

        tk.Label(ffmpeg_frame,
                 text='Path to ffmpeg executable  (leave blank to auto-detect)',
                 bg=BASE, fg=SUBT0, font=('Segoe UI', 9)).pack(anchor='w')

        row = tk.Frame(ffmpeg_frame, bg=BASE)
        row.pack(fill='x', pady=(3, 0))

        ffmpeg_var = tk.StringVar(value=self.settings.get('ffmpeg_path', ''))
        entry = tk.Entry(row, textvariable=ffmpeg_var,
                         bg=SURF0, fg=TEXT, insertbackground=TEXT,
                         font=('Segoe UI', 10), relief='flat', bd=0,
                         highlightthickness=1, highlightbackground=SURF1,
                         highlightcolor=MAUVE)
        entry.pack(side='left', fill='x', expand=True, ipady=5)

        def browse_ffmpeg():
            p = filedialog.askopenfilename(
                filetypes=[('FFmpeg executable', 'ffmpeg ffmpeg.exe'), ('All', '*.*')],
                title='Locate ffmpeg')
            if p:
                ffmpeg_var.set(p)

        tk.Button(row, text='Browse', command=browse_ffmpeg,
                  bg=SURF0, fg=TEXT, font=('Segoe UI', 9), relief='flat', bd=0,
                  padx=10, pady=5, cursor='hand2',
                  activebackground=SURF1).pack(side='left', padx=(6, 0))

        # Status line
        detected = find_ffmpeg()
        status_text = f'Auto-detected: {detected}' if detected else 'Not found on PATH or common locations'
        status_col  = GREEN if detected else YELLOW
        tk.Label(ffmpeg_frame, text=status_text, bg=BASE, fg=status_col,
                 font=('Segoe UI', 8)).pack(anchor='w', pady=(4, 0))

        # ── Buttons ───────────────────────────────────────────────────────────
        tk.Frame(dlg, bg=SURF1, height=1).pack(fill='x', pady=(20, 0))
        btn_row = tk.Frame(dlg, bg=BASE)
        btn_row.pack(fill='x', padx=20, pady=12)

        _bkw = {'font': ('Segoe UI', 9), 'relief': 'flat', 'bd': 0,
                 'padx': 14, 'pady': 6, 'cursor': 'hand2'}

        def save():
            self.settings['ffmpeg_path'] = ffmpeg_var.get().strip()
            self._ffmpeg_path = self._resolve_ffmpeg()
            self._refresh_ffmpeg_badge()
            self._save_settings()
            dlg.destroy()

        tk.Button(btn_row, text='Save', command=save,
                  bg=MAUVE, fg=CRUST, activebackground='#b89be6',
                  font=('Segoe UI', 9, 'bold'), relief='flat', bd=0,
                  padx=14, pady=6, cursor='hand2').pack(side='left')
        tk.Button(btn_row, text='Cancel', command=dlg.destroy,
                  bg=SURF0, fg=TEXT, activebackground=SURF1,
                  **_bkw).pack(side='left', padx=(8, 0))

        # Help text
        tk.Label(btn_row,
                 text='Merging formats, thumbnail embedding, and subtitle embedding all require FFmpeg.',
                 bg=BASE, fg=OVL0, font=('Segoe UI', 8),
                 wraplength=340, justify='left').pack(side='left', padx=(16, 0))

    def _dlg_sec(self, parent, text):
        f = tk.Frame(parent, bg=BASE)
        f.pack(fill='x', padx=20, pady=(16, 4))
        tk.Label(f, text=text, bg=BASE, fg=MAUVE,
                 font=('Segoe UI', 9, 'bold')).pack(side='left')
        tk.Frame(f, bg=SURF1, height=1).pack(side='left', fill='x',
                                             expand=True, padx=(8, 0), pady=3)

    # ─── Queue panel ─────────────────────────────────────────────────────────
    def _build_queue_panel(self, parent):
        frame = tk.Frame(parent, bg=MANTLE, width=480)
        frame.pack(side='left', fill='both', expand=True)
        frame.pack_propagate(False)

        # Header
        hdr = tk.Frame(frame, bg=MANTLE)
        hdr.pack(fill='x', padx=12, pady=(10, 6))
        tk.Label(hdr, text='DOWNLOAD QUEUE', bg=MANTLE, fg=SUBT0,
                 font=('Segoe UI', 9, 'bold')).pack(side='left')
        _bkw = {'bg': SURF0, 'fg': SUBT0, 'font': ('Segoe UI', 8),
                'relief': 'flat', 'bd': 0, 'padx': 8, 'pady': 3,
                'cursor': 'hand2', 'activebackground': SURF1, 'activeforeground': TEXT}
        tk.Button(hdr, text='Clear Done',   command=self._clear_done,   **_bkw).pack(side='right', padx=(2, 0))
        tk.Button(hdr, text='Select All',   command=self._select_all,   **_bkw).pack(side='right', padx=2)
        tk.Button(hdr, text='Deselect All', command=self._deselect_all, **_bkw).pack(side='right', padx=2)

        # Canvas + scrollbar
        outer = tk.Frame(frame, bg=MANTLE)
        outer.pack(fill='both', expand=True, padx=8, pady=(0, 8))

        self._q_vsb = ttk.Scrollbar(outer, orient='vertical')
        self._q_vsb.pack(side='right', fill='y')

        self._q_canvas = tk.Canvas(outer, bg=MANTLE, highlightthickness=0, bd=0,
                                   yscrollcommand=self._q_vsb.set)
        self._q_canvas.pack(side='left', fill='both', expand=True)
        self._q_vsb.configure(command=self._q_canvas.yview)

        self._q_frame = tk.Frame(self._q_canvas, bg=MANTLE)
        self._q_win = self._q_canvas.create_window((0, 0), window=self._q_frame, anchor='nw')

        self._q_frame.bind('<Configure>', self._q_sync_scroll)
        self._q_canvas.bind('<Configure>',
                            lambda e: self._q_canvas.itemconfig(self._q_win, width=e.width))

        # Scroll when mouse enters the queue area; release when it leaves
        self._q_canvas.bind('<Enter>', lambda _e: self._set_wheel_target(self._q_canvas))
        self._q_canvas.bind('<Leave>', lambda _e: self._set_wheel_target(None))

        self._q_widgets: dict[str, dict] = {}

        self._empty_lbl = tk.Label(
            self._q_frame,
            text='No downloads yet.\nPaste a URL above and click Add.',
            bg=MANTLE, fg=OVL0, font=('Segoe UI', 11), justify='center')
        self._empty_lbl.pack(pady=80)

    def _q_sync_scroll(self, _e=None):
        self._q_canvas.configure(scrollregion=self._q_canvas.bbox('all'))

    # ─── Settings panel ──────────────────────────────────────────────────────
    def _build_settings_panel(self, parent):
        frame = tk.Frame(parent, bg=MANTLE, width=400)
        frame.pack(side='left', fill='both')
        frame.pack_propagate(False)

        self._s_vsb = ttk.Scrollbar(frame, orient='vertical')
        self._s_vsb.pack(side='right', fill='y')

        self._s_canvas = tk.Canvas(frame, bg=MANTLE, highlightthickness=0, bd=0,
                                   yscrollcommand=self._s_vsb.set)
        self._s_canvas.pack(side='left', fill='both', expand=True)
        self._s_vsb.configure(command=self._s_canvas.yview)

        inner = tk.Frame(self._s_canvas, bg=MANTLE)
        s_win = self._s_canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>',
                   lambda _e: self._s_canvas.configure(
                       scrollregion=self._s_canvas.bbox('all')))
        self._s_canvas.bind('<Configure>',
                            lambda e: self._s_canvas.itemconfig(s_win, width=e.width))

        self._s_canvas.bind('<Enter>', lambda _e: self._set_wheel_target(self._s_canvas))
        self._s_canvas.bind('<Leave>', lambda _e: self._set_wheel_target(None))

        # ── bind Enter/Leave on all descendant widgets created in this panel ──
        # We do it by overriding the Frame's pack/grid to auto-bind — simpler:
        # just store the canvas ref so _bind_children can use it
        self._s_inner = inner
        self._populate_settings(inner)

    def _set_wheel_target(self, canvas):
        self._wheel_target = canvas

    def _on_mousewheel(self, event):
        if self._wheel_target:
            self._wheel_target.yview_scroll(-1 * (event.delta // 120), 'units')

    def _bind_scroll_on(self, widget: tk.Widget, canvas: tk.Canvas):
        """Recursively bind Enter/Leave so any child widget activates the right canvas."""
        def on_enter(_e): self._set_wheel_target(canvas)
        def on_leave(_e): self._set_wheel_target(None)
        widget.bind('<Enter>', on_enter, add='+')
        widget.bind('<Leave>', on_leave, add='+')
        for child in widget.winfo_children():
            self._bind_scroll_on(child, canvas)

    def _populate_settings(self, inner):
        P = {'padx': 16, 'pady': 3}

        # ── FORMAT ───────────────────────────────────────────────────────────
        self._sec(inner, 'FORMAT')
        self.fmt_preset_var = tk.StringVar(value=self.settings['format_preset'])
        self._labeled_combo(inner, 'Format Preset',
                            self.fmt_preset_var, list(FORMAT_PRESETS.keys()),
                            on_select=self._on_format_change)

        self._custom_fmt_frame = tk.Frame(inner, bg=MANTLE)
        tk.Label(self._custom_fmt_frame, text='Custom Format String',
                 bg=MANTLE, fg=SUBT0, font=('Segoe UI', 9)).pack(anchor='w')
        self.custom_fmt_var = tk.StringVar(value=self.settings['custom_format'])
        self._entry(self._custom_fmt_frame, self.custom_fmt_var).pack(fill='x', pady=(2, 0), ipady=4)

        self.audio_extract_var = tk.BooleanVar(value=self.settings['audio_extract'])
        ttk.Checkbutton(inner, text='Extract Audio Only',
                        variable=self.audio_extract_var,
                        command=self._on_audio_toggle).pack(anchor='w', **P)

        self._audio_opts_frame = tk.Frame(inner, bg=MANTLE)
        arow = tk.Frame(self._audio_opts_frame, bg=MANTLE)
        arow.pack(fill='x')
        self.audio_fmt_var = tk.StringVar(value=self.settings['audio_format'])
        self.audio_q_var   = tk.StringVar(value=self.settings['audio_quality'])
        self._mini_combo(arow, 'Audio Format', self.audio_fmt_var,
                         ['mp3', 'aac', 'm4a', 'flac', 'wav', 'ogg', 'opus', 'vorbis', 'alac'])
        tk.Frame(arow, bg=MANTLE, width=12).pack(side='left')
        self._mini_combo(arow, 'Quality (kbps)', self.audio_q_var,
                         ['best', '320', '256', '192', '128', '96', '64', '32'])

        arow2 = tk.Frame(self._audio_opts_frame, bg=MANTLE)
        arow2.pack(fill='x', pady=(6, 0))
        self.audio_sr_var = tk.StringVar(value=self.settings['audio_sample_rate'])
        self._mini_combo(arow2, 'Sample Rate', self.audio_sr_var,
                         ['', '22050', '44100', '48000', '96000'])
        tk.Label(arow2, text='(blank = keep original)', bg=MANTLE, fg=OVL0,
                 font=('Segoe UI', 8)).pack(side='left', padx=(8, 0), pady=(16, 0))

        self.audio_norm_var = tk.BooleanVar(value=self.settings['audio_normalize'])
        ttk.Checkbutton(self._audio_opts_frame, text='Normalize Audio Volume (FFmpeg loudnorm)',
                        variable=self.audio_norm_var).pack(anchor='w', pady=(4, 0))

        if self.settings['format_preset'] == 'Custom…':
            self._custom_fmt_frame.pack(fill='x', **P)
        if self.settings['audio_extract']:
            self._audio_opts_frame.pack(fill='x', **P)

        # ── OUTPUT ───────────────────────────────────────────────────────────
        self._sec(inner, 'OUTPUT')
        self.outdir_var = tk.StringVar(value=self.settings['output_dir'])
        self._browse_row(inner, 'Save To', self.outdir_var, self._browse_dir)

        self.tmpl_var = tk.StringVar(value=self.settings['output_template'])
        f = tk.Frame(inner, bg=MANTLE)
        f.pack(fill='x', **P)
        tk.Label(f, text='Filename Template', bg=MANTLE, fg=SUBT0,
                 font=('Segoe UI', 9)).pack(anchor='w')
        self._entry(f, self.tmpl_var).pack(fill='x', pady=(2, 0), ipady=4)
        tk.Label(f, text='%(title)s  %(id)s  %(uploader)s  %(height)sp  %(ext)s',
                 bg=MANTLE, fg=SURF2, font=('Segoe UI', 8)).pack(anchor='w')

        # ── POST-PROCESSING ───────────────────────────────────────────────────
        self._sec(inner, 'POST-PROCESSING')
        self.embed_thumb_var = tk.BooleanVar(value=self.settings['embed_thumbnail'])
        self.write_thumb_var = tk.BooleanVar(value=self.settings['write_thumbnail'])
        self.embed_meta_var  = tk.BooleanVar(value=self.settings['embed_metadata'])
        self.write_json_var  = tk.BooleanVar(value=self.settings['write_infojson'])
        self._chk(inner, 'Embed Thumbnail (requires FFmpeg)',      self.embed_thumb_var)
        self._chk(inner, 'Save Thumbnail File',                    self.write_thumb_var)
        self._chk(inner, 'Embed Metadata / ID3 (requires FFmpeg)', self.embed_meta_var)
        self._chk(inner, 'Write Info JSON',                        self.write_json_var)

        # ── SUBTITLES ─────────────────────────────────────────────────────────
        self._sec(inner, 'SUBTITLES')
        self.write_subs_var = tk.BooleanVar(value=self.settings['write_subs'])
        self.auto_subs_var  = tk.BooleanVar(value=self.settings['auto_subs'])
        self.embed_subs_var = tk.BooleanVar(value=self.settings['embed_subs'])
        self.sub_langs_var  = tk.StringVar(value=self.settings['sub_langs'])
        self._chk(inner, 'Download Subtitles',                         self.write_subs_var)
        self._chk(inner, 'Include Auto-generated',                     self.auto_subs_var)
        self._chk(inner, 'Embed Subtitles into Video (requires FFmpeg)', self.embed_subs_var)
        f2 = tk.Frame(inner, bg=MANTLE)
        f2.pack(fill='x', **P)
        tk.Label(f2, text='Languages (comma-separated, e.g. en,es)',
                 bg=MANTLE, fg=SUBT0, font=('Segoe UI', 9)).pack(anchor='w')
        self._entry(f2, self.sub_langs_var).pack(fill='x', pady=(2, 0), ipady=4)

        # ── SPONSORBLOCK ──────────────────────────────────────────────────────
        self._sec(inner, 'SPONSORBLOCK')
        self.sb_var = tk.BooleanVar(value=self.settings['sponsorblock_enabled'])
        ttk.Checkbutton(inner, text='Enable SponsorBlock',
                        variable=self.sb_var,
                        command=self._on_sb_toggle).pack(anchor='w', **P)
        self._sb_frame = tk.Frame(inner, bg=MANTLE)
        self.sb_cats_var = tk.StringVar(value=self.settings['sponsorblock_cats'])
        self._labeled_combo(self._sb_frame, 'Categories to Mark',
                            self.sb_cats_var, SB_CATEGORIES)
        if self.settings['sponsorblock_enabled']:
            self._sb_frame.pack(fill='x', **P)

        # ── NETWORK ───────────────────────────────────────────────────────────
        self._sec(inner, 'NETWORK')
        self.rate_limit_var = tk.StringVar(value=self.settings['rate_limit'])
        self.proxy_var      = tk.StringVar(value=self.settings['proxy'])
        self.retries_var    = tk.StringVar(value=self.settings['retries'])
        self.concurrent_var = tk.StringVar(value=self.settings['concurrent_fragments'])
        self._labeled_entry(inner, 'Rate Limit (e.g. 2M, 500K — blank = unlimited)',
                            self.rate_limit_var, width=14)
        self._labeled_entry(inner, 'Proxy (http://… or socks5://…)', self.proxy_var)
        rn = tk.Frame(inner, bg=MANTLE)
        rn.pack(fill='x', **P)
        self._mini_entry(rn, 'Retries',               self.retries_var,    width=6)
        tk.Frame(rn, bg=MANTLE, width=16).pack(side='left')
        self._mini_entry(rn, 'Concurrent Fragments',  self.concurrent_var, width=6)

        # ── COOKIES & AUTH ────────────────────────────────────────────────────
        self._sec(inner, 'COOKIES & AUTH')
        self.cookie_browser_var = tk.StringVar(value=self.settings['cookie_browser'])
        self._labeled_combo(inner, 'Import Cookies from Browser',
                            self.cookie_browser_var,
                            ['', 'chrome', 'firefox', 'edge', 'brave',
                             'safari', 'chromium', 'opera', 'vivaldi'])
        self.cookie_file_var = tk.StringVar(value=self.settings['cookie_file'])
        self._browse_row(inner, 'Cookie File (Netscape format)',
                         self.cookie_file_var, self._browse_cookies)

        # ── FILTERS ───────────────────────────────────────────────────────────
        self._sec(inner, 'FILTERS')
        self.no_playlist_var = tk.BooleanVar(value=self.settings['no_playlist'])
        self.maxfs_var       = tk.StringVar(value=self.settings['max_filesize'])
        self.date_after_var  = tk.StringVar(value=self.settings['date_after'])
        self.date_before_var = tk.StringVar(value=self.settings['date_before'])
        self._chk(inner, 'Single Video (ignore playlist)', self.no_playlist_var)
        self._labeled_entry(inner, 'Max Filesize (e.g. 500M, 2G)', self.maxfs_var, width=14)
        dr = tk.Frame(inner, bg=MANTLE)
        dr.pack(fill='x', **P)
        self._mini_entry(dr, 'Date After  (YYYYMMDD)', self.date_after_var,  width=12)
        tk.Frame(dr, bg=MANTLE, width=12).pack(side='left')
        self._mini_entry(dr, 'Date Before (YYYYMMDD)', self.date_before_var, width=12)

        tk.Frame(inner, bg=MANTLE, height=24).pack()

        # Bind scroll activation to every widget in the settings panel
        self.after(100, lambda: self._bind_scroll_on(inner, self._s_canvas))

    # ─── Bottom bar ───────────────────────────────────────────────────────────
    def _build_bottom(self):
        bar = tk.Frame(self, bg=BASE)
        bar.pack(fill='x', padx=12, pady=6)

        _bkw = {'font': ('Segoe UI', 9), 'relief': 'flat', 'bd': 0,
                 'padx': 14, 'pady': 6, 'cursor': 'hand2',
                 'activeforeground': TEXT}
        tk.Button(bar, text='⬇  Download Selected', command=self._download_selected,
                  bg=MAUVE, fg=CRUST, activebackground='#b89be6',
                  font=('Segoe UI', 9, 'bold'), relief='flat', bd=0,
                  padx=14, pady=6, cursor='hand2').pack(side='left')
        tk.Button(bar, text='⬇  Download All',   command=self._download_all,
                  bg=SURF0, activebackground=SURF1, **_bkw).pack(side='left', padx=(4, 0))
        tk.Button(bar, text='⏹  Cancel All',      command=self._cancel_all,
                  bg=SURF0, activebackground=SURF1, **_bkw).pack(side='left', padx=(4, 0))
        tk.Button(bar, text='📁  Open Folder',    command=self._open_folder,
                  bg=SURF0, activebackground=SURF1, **_bkw).pack(side='left', padx=(4, 0))
        tk.Button(bar, text='🗑  Remove Selected', command=self._remove_selected,
                  bg=SURF0, activebackground=SURF1, **_bkw).pack(side='left', padx=(4, 0))

        self.status_var = tk.StringVar(value='Ready')
        tk.Label(bar, textvariable=self.status_var, bg=BASE, fg=SUBT0,
                 font=('Segoe UI', 9)).pack(side='right')

        # Log strip
        tk.Frame(self, bg=SURF1, height=1).pack(fill='x')
        log_hdr = tk.Frame(self, bg=MANTLE)
        log_hdr.pack(fill='x')
        tk.Label(log_hdr, text='LOG', bg=MANTLE, fg=MAUVE,
                 font=('Segoe UI', 9, 'bold')).pack(side='left', padx=(12, 0), pady=4)
        tk.Button(log_hdr, text='Clear', command=self._clear_log,
                  bg=MANTLE, fg=SUBT0, font=('Segoe UI', 8), relief='flat',
                  bd=0, padx=8, pady=2, cursor='hand2',
                  activebackground=SURF0).pack(side='right', padx=8, pady=2)

        self.log_txt = tk.Text(self, height=5, bg=MANTLE, fg=SUBT0,
                               font=('Consolas', 9), relief='flat', bd=0,
                               state='disabled', wrap='word',
                               insertbackground=TEXT,
                               selectbackground=SURF1, selectforeground=TEXT)
        self.log_txt.pack(fill='x', padx=8, pady=(0, 6))
        self.log_txt.tag_config('green',  foreground=GREEN)
        self.log_txt.tag_config('red',    foreground=RED)
        self.log_txt.tag_config('yellow', foreground=YELLOW)
        self.log_txt.tag_config('blue',   foreground=BLUE)

    # ─── Settings widget helpers ──────────────────────────────────────────────
    def _sec(self, parent, text):
        f = tk.Frame(parent, bg=MANTLE)
        f.pack(fill='x', padx=16, pady=(14, 4))
        tk.Label(f, text=text, bg=MANTLE, fg=MAUVE,
                 font=('Segoe UI', 9, 'bold')).pack(side='left')
        tk.Frame(f, bg=SURF1, height=1).pack(side='left', fill='x',
                                             expand=True, padx=(8, 0), pady=3)

    def _entry(self, parent, var):
        return tk.Entry(parent, textvariable=var,
                        bg=SURF0, fg=TEXT, insertbackground=TEXT,
                        font=('Segoe UI', 10), relief='flat', bd=0,
                        highlightthickness=1, highlightbackground=SURF1,
                        highlightcolor=MAUVE)

    def _chk(self, parent, text, var):
        ttk.Checkbutton(parent, text=text, variable=var).pack(
            anchor='w', padx=16, pady=2)

    def _labeled_combo(self, parent, label, var, values, on_select=None):
        f = tk.Frame(parent, bg=MANTLE)
        f.pack(fill='x', padx=16, pady=3)
        tk.Label(f, text=label, bg=MANTLE, fg=SUBT0,
                 font=('Segoe UI', 9)).pack(anchor='w')
        cb = ttk.Combobox(f, textvariable=var, values=values,
                          state='readonly', font=('Segoe UI', 10))
        cb.pack(fill='x', pady=(2, 0))
        if on_select:
            cb.bind('<<ComboboxSelected>>', on_select)

    def _labeled_entry(self, parent, label, var, width=None):
        f = tk.Frame(parent, bg=MANTLE)
        f.pack(fill='x', padx=16, pady=3)
        tk.Label(f, text=label, bg=MANTLE, fg=SUBT0,
                 font=('Segoe UI', 9)).pack(anchor='w')
        e = self._entry(f, var)
        if width:
            e.configure(width=width)
            e.pack(anchor='w', pady=(2, 0), ipady=4)
        else:
            e.pack(fill='x', pady=(2, 0), ipady=4)

    def _mini_combo(self, parent, label, var, values):
        f = tk.Frame(parent, bg=MANTLE)
        f.pack(side='left', fill='x', expand=True)
        tk.Label(f, text=label, bg=MANTLE, fg=SUBT0,
                 font=('Segoe UI', 9)).pack(anchor='w')
        ttk.Combobox(f, textvariable=var, values=values,
                     state='readonly', width=10).pack(fill='x', pady=(2, 0))

    def _mini_entry(self, parent, label, var, width=8):
        f = tk.Frame(parent, bg=MANTLE)
        f.pack(side='left')
        tk.Label(f, text=label, bg=MANTLE, fg=SUBT0,
                 font=('Segoe UI', 9)).pack(anchor='w')
        e = self._entry(f, var)
        e.configure(width=width)
        e.pack(anchor='w', ipady=4)

    def _browse_row(self, parent, label, var, cmd):
        f = tk.Frame(parent, bg=MANTLE)
        f.pack(fill='x', padx=16, pady=3)
        tk.Label(f, text=label, bg=MANTLE, fg=SUBT0,
                 font=('Segoe UI', 9)).pack(anchor='w')
        row = tk.Frame(f, bg=MANTLE)
        row.pack(fill='x', pady=(2, 0))
        self._entry(row, var).pack(side='left', fill='x', expand=True, ipady=4)
        tk.Button(row, text='…', command=cmd,
                  bg=SURF1, fg=TEXT, font=('Segoe UI', 10), relief='flat',
                  bd=0, padx=8, pady=4, cursor='hand2',
                  activebackground=SURF2).pack(side='left', padx=(4, 0))

    # ─── Toggle handlers ──────────────────────────────────────────────────────
    def _on_format_change(self, _e=None):
        if self.fmt_preset_var.get() == 'Custom…':
            self._custom_fmt_frame.pack(fill='x', padx=16, pady=3)
        else:
            self._custom_fmt_frame.pack_forget()

    def _on_audio_toggle(self):
        if self.audio_extract_var.get():
            self._audio_opts_frame.pack(fill='x', padx=16, pady=3)
        else:
            self._audio_opts_frame.pack_forget()

    def _on_sb_toggle(self):
        if self.sb_var.get():
            self._sb_frame.pack(fill='x', padx=16, pady=3)
        else:
            self._sb_frame.pack_forget()

    def _browse_dir(self):
        path = filedialog.askdirectory(
            initialdir=self.outdir_var.get(), title='Select Output Directory')
        if path:
            self.outdir_var.set(path)

    def _browse_cookies(self):
        path = filedialog.askopenfilename(
            filetypes=[('Cookie files', '*.txt'), ('All files', '*.*')],
            title='Select Cookie File')
        if path:
            self.cookie_file_var.set(path)

    # ─── URL input ────────────────────────────────────────────────────────────
    def _add_urls(self):
        raw = self.url_var.get().strip()
        if not raw or raw == self._url_placeholder:
            return
        urls = [u.strip() for u in raw.replace(',', '\n').split('\n')
                if u.strip() and u.strip() != self._url_placeholder]
        for url in urls:
            item = DownloadItem(url)
            self.items[item.id] = item
            self._add_card(item)
        self.url_entry.delete(0, 'end')
        self.url_entry.insert(0, self._url_placeholder)
        self.url_entry.configure(fg=SUBT0)
        self._log(f'Added {len(urls)} URL(s) to queue.\n', 'blue')

    def _fetch_info_btn(self):
        raw = self.url_var.get().strip()
        if not raw or raw == self._url_placeholder:
            return
        self.status_var.set('Fetching info…')
        threading.Thread(target=self._fetch_info_thread, args=(raw,),
                         daemon=True).start()

    def _fetch_info_thread(self, url: str):
        opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info:
                self.msg_q.put(('show_info', info))
            else:
                self.msg_q.put(('status', 'Could not fetch info.'))
        except Exception as exc:
            self.msg_q.put(('status', f'Fetch error: {exc}'))

    # ─── Queue card management ────────────────────────────────────────────────
    _CARD_BG   = SURF0
    _DONE_BG   = '#1c2f1c'
    _ERROR_BG  = '#2f1c1c'
    _CANCEL_BG = '#26262e'

    def _add_card(self, item: DownloadItem):
        if self._empty_lbl.winfo_ismapped():
            self._empty_lbl.pack_forget()

        card = tk.Frame(self._q_frame, bg=self._CARD_BG, padx=10, pady=8)
        card.pack(fill='x', padx=4, pady=3)

        # Row 1: checkbox + title + status badge
        top = tk.Frame(card, bg=self._CARD_BG)
        top.pack(fill='x')

        check_var = tk.BooleanVar(value=True)
        tk.Checkbutton(top, variable=check_var, bg=self._CARD_BG,
                       activebackground=self._CARD_BG, fg=TEXT,
                       selectcolor=SURF1, relief='flat', bd=0).pack(side='left')

        title_lbl = tk.Label(top, text=self._trunc(item.url, 44),
                             bg=self._CARD_BG, fg=TEXT, font=('Segoe UI', 10),
                             anchor='w', justify='left')
        title_lbl.pack(side='left', fill='x', expand=True, padx=(4, 0))

        status_lbl = tk.Label(top, text='PENDING', bg=self._CARD_BG, fg=YELLOW,
                              font=('Segoe UI', 8, 'bold'))
        status_lbl.pack(side='right')

        # Row 2: progress bar
        prog = ttk.Progressbar(card, style='Horizontal.TProgressbar',
                               mode='determinate', maximum=100, value=0)
        prog.pack(fill='x', pady=(6, 4))

        # Row 3: info text + action button
        bot = tk.Frame(card, bg=self._CARD_BG)
        bot.pack(fill='x')

        info_lbl = tk.Label(bot, text='', bg=self._CARD_BG, fg=SUBT0,
                            font=('Segoe UI', 9), anchor='w', justify='left',
                            wraplength=0)
        info_lbl.pack(side='left', fill='x', expand=True)

        # Single action button — label changes between Cancel / Retry / Done
        action_btn = tk.Button(bot, text='Cancel',
                               bg=self._CARD_BG, fg=SUBT0,
                               font=('Segoe UI', 8), relief='flat', bd=0,
                               padx=6, pady=2, cursor='hand2',
                               activebackground=SURF1, activeforeground=TEXT,
                               command=lambda: self.items.get(item.id) and self.items[item.id].cancel())
        action_btn.pack(side='right')

        wdg = {
            'card': card, 'check_var': check_var,
            'title_lbl': title_lbl, 'status_lbl': status_lbl,
            'prog': prog, 'info_lbl': info_lbl,
            'action_btn': action_btn,
            'card_bg': self._CARD_BG,  # current bg, updated on tint
        }
        self._q_widgets[item.id] = wdg

        # Bind scroll events so hovering over a card still scrolls the queue
        self._bind_scroll_on(card, self._q_canvas)

    def _remove_card(self, item_id: str):
        wdg = self._q_widgets.pop(item_id, None)
        if wdg:
            wdg['card'].destroy()
        if not self._q_widgets:
            self._empty_lbl.pack(pady=80)
        self._q_sync_scroll()

    def _update_card(self, item: DownloadItem):
        wdg = self._q_widgets.get(item.id)
        if not wdg:
            return

        wdg['title_lbl'].configure(text=self._trunc(item.title, 44))
        wdg['prog']['value'] = item.progress * 100

        STATUS_COLORS = {
            DownloadItem.PENDING:     (YELLOW, 'PENDING'),
            DownloadItem.FETCHING:    (BLUE,   'FETCHING'),
            DownloadItem.DOWNLOADING: (MAUVE,  'DOWNLOADING'),
            DownloadItem.CONVERTING:  (PEACH,  'CONVERTING'),
            DownloadItem.DONE:        (GREEN,  'DONE'),
            DownloadItem.ERROR:       (RED,    'ERROR'),
            DownloadItem.CANCELLED:   (SUBT0,  'CANCELLED'),
        }
        col, label = STATUS_COLORS.get(item.status, (SUBT0, item.status.upper()))
        wdg['status_lbl'].configure(text=label, fg=col)

        # Info / error text
        if item.error:
            wdg['info_lbl'].configure(text=item.error, fg=RED, wraplength=360)
        else:
            parts = [p for p in [item.size_str, item.speed,
                                  f'ETA {item.eta}' if item.eta else ''] if p]
            wdg['info_lbl'].configure(text='   '.join(parts), fg=SUBT0, wraplength=0)

        # Tint + action button
        if item.status == DownloadItem.DONE:
            self._tint(wdg, self._DONE_BG)
            wdg['prog'].configure(style='Green.Horizontal.TProgressbar')
            wdg['action_btn'].configure(
                text='✓ Done', state='disabled',
                bg=self._DONE_BG, fg=GREEN,
                activebackground=self._DONE_BG)

        elif item.status == DownloadItem.ERROR:
            self._tint(wdg, self._ERROR_BG)
            wdg['action_btn'].configure(
                text='↺ Retry', state='normal',
                bg='#4a2020', fg=PEACH,
                activebackground='#5a2828',
                command=lambda iid=item.id: self._retry_item(iid))

        elif item.status == DownloadItem.CANCELLED:
            self._tint(wdg, self._CANCEL_BG)
            wdg['action_btn'].configure(
                text='↺ Retry', state='normal',
                bg=SURF1, fg=TEXT,
                activebackground=SURF2,
                command=lambda iid=item.id: self._retry_item(iid))

        elif item.status == DownloadItem.DOWNLOADING:
            wdg['action_btn'].configure(
                text='Cancel', state='normal',
                bg=wdg['card_bg'], fg=SUBT0,
                activebackground=SURF1,
                command=lambda: self.items.get(item.id) and self.items[item.id].cancel())

    def _tint(self, wdg: dict, color: str):
        """Paint the card and its non-ttk children with a background colour."""
        wdg['card_bg'] = color
        for widget in [wdg['card']] + wdg['card'].winfo_children():
            try:
                widget.configure(bg=color)
            except Exception:
                pass
            for child in widget.winfo_children():
                try:
                    child.configure(bg=color)
                except Exception:
                    pass

    # ─── Queue operations ─────────────────────────────────────────────────────
    def _select_all(self):
        for w in self._q_widgets.values():
            w['check_var'].set(True)

    def _deselect_all(self):
        for w in self._q_widgets.values():
            w['check_var'].set(False)

    def _clear_done(self):
        done = {DownloadItem.DONE, DownloadItem.ERROR, DownloadItem.CANCELLED}
        for iid in [i for i, it in self.items.items() if it.status in done]:
            self._remove_card(iid)
            del self.items[iid]

    def _remove_selected(self):
        inactive = {DownloadItem.PENDING, DownloadItem.DONE,
                    DownloadItem.ERROR, DownloadItem.CANCELLED}
        for iid in [i for i, w in self._q_widgets.items()
                    if w['check_var'].get()
                    and self.items.get(i)
                    and self.items[i].status in inactive]:
            self._remove_card(iid)
            self.items.pop(iid, None)

    def _cancel_all(self):
        for item in self.items.values():
            if item.status in (DownloadItem.PENDING, DownloadItem.DOWNLOADING):
                item.cancel()

    def _retry_item(self, item_id: str):
        item = self.items.get(item_id)
        if not item:
            return
        item.reset()
        wdg = self._q_widgets.get(item_id)
        if wdg:
            # Restore neutral card appearance
            self._tint(wdg, self._CARD_BG)
            wdg['prog']['value'] = 0
            wdg['prog'].configure(style='Horizontal.TProgressbar')
            wdg['info_lbl'].configure(text='', fg=SUBT0, wraplength=0)
            wdg['action_btn'].configure(
                text='Cancel', state='normal',
                bg=self._CARD_BG, fg=SUBT0,
                activebackground=SURF1,
                command=lambda: item.cancel())
            wdg['status_lbl'].configure(text='PENDING', fg=YELLOW)
        self._start_downloads([item_id])

    def _open_folder(self):
        path = self.outdir_var.get()
        if os.path.isdir(path):
            if sys.platform == 'win32':
                os.startfile(path)
            else:
                subprocess.Popen(['xdg-open', path])

    def _clear_log(self):
        self.log_txt.configure(state='normal')
        self.log_txt.delete('1.0', 'end')
        self.log_txt.configure(state='disabled')

    # ─── Download logic ────────────────────────────────────────────────────────
    def _collect_settings(self) -> dict:
        s = self.settings
        s['format_preset']        = self.fmt_preset_var.get()
        s['custom_format']        = self.custom_fmt_var.get()
        s['audio_extract']        = self.audio_extract_var.get()
        s['audio_format']         = self.audio_fmt_var.get()
        s['audio_quality']        = self.audio_q_var.get()
        s['audio_normalize']      = self.audio_norm_var.get()
        s['audio_sample_rate']    = self.audio_sr_var.get()
        s['output_dir']           = self.outdir_var.get()
        s['output_template']      = self.tmpl_var.get()
        s['embed_thumbnail']      = self.embed_thumb_var.get()
        s['write_thumbnail']      = self.write_thumb_var.get()
        s['embed_metadata']       = self.embed_meta_var.get()
        s['write_infojson']       = self.write_json_var.get()
        s['write_subs']           = self.write_subs_var.get()
        s['auto_subs']            = self.auto_subs_var.get()
        s['embed_subs']           = self.embed_subs_var.get()
        s['sub_langs']            = self.sub_langs_var.get()
        s['sponsorblock_enabled'] = self.sb_var.get()
        s['sponsorblock_cats']    = self.sb_cats_var.get()
        s['rate_limit']           = self.rate_limit_var.get()
        s['proxy']                = self.proxy_var.get()
        s['retries']              = self.retries_var.get()
        s['concurrent_fragments'] = self.concurrent_var.get()
        s['no_playlist']          = self.no_playlist_var.get()
        s['max_filesize']         = self.maxfs_var.get()
        s['date_after']           = self.date_after_var.get()
        s['date_before']          = self.date_before_var.get()
        s['cookie_browser']       = self.cookie_browser_var.get()
        s['cookie_file']          = self.cookie_file_var.get()
        self._save_settings()
        return s

    def _build_ydl_opts(self, item: DownloadItem, s: dict) -> dict:
        # Format
        preset = s['format_preset']
        if s['audio_extract']:
            fmt = 'bestaudio/best'
        elif preset == 'Custom…':
            fmt = s['custom_format'] or 'bestvideo+bestaudio/best'
        else:
            fmt = FORMAT_PRESETS.get(preset, 'bestvideo+bestaudio/best')

        outtmpl = os.path.join(s['output_dir'],
                               s['output_template'] or '%(title)s.%(ext)s')

        # Post-processors
        pp = []
        if s['audio_extract']:
            quality = s['audio_quality']
            if quality == 'best':
                quality = '0'
            pp.append({'key': 'FFmpegExtractAudio',
                       'preferredcodec': s['audio_format'],
                       'preferredquality': quality})
        if s['embed_thumbnail']:
            pp.append({'key': 'EmbedThumbnail'})
        if s['embed_metadata']:
            pp.append({'key': 'FFmpegMetadata', 'add_metadata': True})
        if s['embed_subs'] and (s['write_subs'] or s['auto_subs']):
            pp.append({'key': 'FFmpegEmbedSubtitle'})
        if s['sponsorblock_enabled']:
            cats = s['sponsorblock_cats']
            if cats == 'all':
                cats = 'sponsor,intro,outro,selfpromo,interaction,music_offtopic,preview,filler'
            pp.append({'key': 'SponsorBlock', 'categories': cats.split(',')})
            pp.append({'key': 'ModifyChapters',
                       'sponsorblock_chapter_title': '[SponsorBlock]: %(category_names)l',
                       'remove_sponsor_segments': [], 'force_keyframes': False})

        sub_langs = [ln.strip() for ln in s['sub_langs'].split(',') if ln.strip()]

        opts: dict = {
            'format':            fmt,
            'outtmpl':           outtmpl,
            'postprocessors':    pp,
            'writethumbnail':    s['write_thumbnail'],
            'writeinfojson':     s['write_infojson'],
            'writesubtitles':    s['write_subs'],
            'writeautomaticsub': s['auto_subs'],
            'subtitleslangs':    sub_langs if (s['write_subs'] or s['auto_subs']) else [],
            'noplaylist':        s['no_playlist'],
            'quiet':             True,
            'no_warnings':       False,
            'noprogress':        True,
            'logger':            _GUILogger(self.msg_q, item.id),
            'progress_hooks':    [lambda d, iid=item.id: self.msg_q.put(('prog', iid, d))],
            'postprocessor_hooks': [lambda d, iid=item.id: self.msg_q.put(('pp', iid, d))],
        }

        # Audio postprocessor extra args (normalize / sample rate)
        if s['audio_extract']:
            pp_extra: list[str] = []
            if s.get('audio_normalize'):
                pp_extra += ['-af', 'loudnorm']
            if s.get('audio_sample_rate'):
                pp_extra += ['-ar', s['audio_sample_rate']]
            if pp_extra:
                opts['postprocessor_args'] = {'FFmpegExtractAudio': pp_extra}

        # FFmpeg location
        if self._ffmpeg_path:
            opts['ffmpeg_location'] = os.path.dirname(self._ffmpeg_path)

        if s['rate_limit']:
            opts['ratelimit'] = s['rate_limit']
        if s['proxy']:
            opts['proxy'] = s['proxy']
        try:
            opts['retries'] = int(s['retries'])
        except ValueError:
            pass
        try:
            opts['concurrent_fragment_downloads'] = int(s['concurrent_fragments'])
        except ValueError:
            pass
        if s['max_filesize']:
            opts['max_filesize'] = s['max_filesize']
        if s['date_after']:
            opts['dateafter'] = s['date_after']
        if s['date_before']:
            opts['datebefore'] = s['date_before']
        if s['cookie_browser']:
            opts['cookiesfrombrowser'] = (s['cookie_browser'],)
        if s['cookie_file'] and os.path.exists(s['cookie_file']):
            opts['cookiefile'] = s['cookie_file']

        return opts

    def _download_worker(self, item: DownloadItem, opts: dict):
        item.status = DownloadItem.DOWNLOADING
        self.msg_q.put(('update', item))
        try:
            with YoutubeDL(opts) as ydl:
                ret = ydl.download([item.url])
            if item.is_cancelled:
                item.status = DownloadItem.CANCELLED
            elif ret == 0:
                item.status   = DownloadItem.DONE
                item.progress = 1.0
                item.speed    = ''
                item.eta      = ''
            else:
                item.status = DownloadItem.ERROR
                if not item.error:
                    item.error = 'Download returned non-zero exit code.'
        except Exception as exc:
            if item.is_cancelled:
                item.status = DownloadItem.CANCELLED
            else:
                item.status = DownloadItem.ERROR
                if not item.error:
                    item.error = str(exc)
        finally:
            self.msg_q.put(('update', item))
            tag = 'green' if item.status == DownloadItem.DONE else 'red'
            self.msg_q.put(('log', f'[{item.status.upper()}] {item.title}\n', tag))

    def _start_downloads(self, item_ids: list):
        s = self._collect_settings()
        started = 0
        for iid in item_ids:
            item = self.items.get(iid)
            if not item or item.status != DownloadItem.PENDING:
                continue
            opts = self._build_ydl_opts(item, s)
            t = threading.Thread(target=self._download_worker,
                                 args=(item, opts), daemon=True)
            self._threads[iid] = t
            t.start()
            started += 1
        if started:
            self.status_var.set(f'Started {started} download(s)…')
            self._log(f'Starting {started} download(s).\n', 'yellow')

    def _download_selected(self):
        ids = [iid for iid, w in self._q_widgets.items() if w['check_var'].get()]
        self._start_downloads(ids)

    def _download_all(self):
        self._start_downloads(list(self.items.keys()))

    # ─── Message queue ────────────────────────────────────────────────────────
    def _poll(self):
        try:
            while True:
                self._handle(self.msg_q.get_nowait())
        except queue.Empty:
            pass
        finally:
            self.after(80, self._poll)

    def _handle(self, msg):
        kind = msg[0]

        if kind == 'update':
            self._update_card(msg[1])

        elif kind == 'prog':
            _, iid, d = msg
            item = self.items.get(iid)
            if not item:
                return
            status = d.get('status')

            if status == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                dl    = d.get('downloaded_bytes', 0)
                item.progress = (dl / total) if total else 0.0
                spd   = d.get('speed')
                eta   = d.get('eta')
                item.speed    = (format_bytes(spd) + '/s') if spd else ''
                item.eta      = f'{int(eta)}s' if eta else ''
                item.size_str = (f'{format_bytes(dl)}/{format_bytes(total)}'
                                 if total else format_bytes(dl)) if dl else ''
                if d.get('info_dict', {}).get('title'):
                    item.title = d['info_dict']['title']
                item.status = DownloadItem.DOWNLOADING
                self._update_card(item)
                pct = f'{item.progress * 100:.1f}%'
                self._log(f'{self._trunc(item.title, 28)}: {pct}'
                          f'{" @ " + item.speed if item.speed else ""}'
                          f'{" ETA " + item.eta if item.eta else ""}\n')

            elif status == 'finished':
                item.progress = 1.0
                item.status   = DownloadItem.CONVERTING
                item.speed    = ''
                item.eta      = ''
                self._update_card(item)

            elif status == 'error':
                item.status = DownloadItem.ERROR
                if not item.error:
                    item.error = str(d.get('error', 'Unknown error'))
                self._update_card(item)

        elif kind == 'pp':
            _, iid, d = msg
            item = self.items.get(iid)
            if item and d.get('status') == 'started':
                item.status   = DownloadItem.CONVERTING
                item.size_str = f'Post-processing: {d.get("postprocessor", "")}'
                self._update_card(item)

        elif kind == 'set_error':
            _, iid, errmsg = msg
            item = self.items.get(iid)
            if item and item.status != DownloadItem.DONE:
                item.error  = errmsg
                item.status = DownloadItem.ERROR
                self._update_card(item)

        elif kind == 'status':
            self.status_var.set(msg[1])

        elif kind == 'log':
            tag = msg[2] if len(msg) > 2 else ''
            self._log(msg[1], tag)

        elif kind == 'show_info':
            self._show_info_popup(msg[1])

        elif kind == 'conv_update':
            self._update_conv_card(msg[1])

    def _log(self, text: str, tag: str = ''):
        self.log_txt.configure(state='normal')
        if tag:
            self.log_txt.insert('end', text, tag)
        else:
            self.log_txt.insert('end', text)
        self.log_txt.see('end')
        lines = int(self.log_txt.index('end-1c').split('.')[0])
        if lines > 500:
            self.log_txt.delete('1.0', f'{lines - 500}.0')
        self.log_txt.configure(state='disabled')

    # ─── Info popup ───────────────────────────────────────────────────────────
    def _show_info_popup(self, info: dict):
        popup = tk.Toplevel(self)
        popup.title('Media Info')
        popup.geometry('700x580')
        popup.configure(bg=BASE)
        popup.transient(self)
        popup.grab_set()
        self._apply_dark_titlebar(popup)

        title = info.get('title', 'Unknown')

        tk.Label(popup, text=title, bg=BASE, fg=TEXT,
                 font=('Segoe UI', 12, 'bold'), wraplength=660,
                 justify='left').pack(anchor='w', padx=16, pady=(14, 4))

        meta = []
        if info.get('uploader'):   meta.append(f'by {info["uploader"]}')
        if info.get('duration'):
            m, s = divmod(int(info['duration']), 60)
            h, m = divmod(m, 60)
            meta.append(f'{h:02d}:{m:02d}:{s:02d}')
        if info.get('view_count'): meta.append(f'{info["view_count"]:,} views')
        if info.get('upload_date'):
            d = info['upload_date']
            meta.append(f'{d[:4]}-{d[4:6]}-{d[6:]}')
        if info.get('extractor_key'): meta.append(info['extractor_key'])

        tk.Label(popup, text='  ·  '.join(meta), bg=BASE, fg=SUBT0,
                 font=('Segoe UI', 9)).pack(anchor='w', padx=16, pady=(0, 8))

        tk.Frame(popup, bg=SURF1, height=1).pack(fill='x', padx=16)
        tk.Label(popup, text='Available Formats', bg=BASE, fg=MAUVE,
                 font=('Segoe UI', 10, 'bold')).pack(anchor='w', padx=16, pady=(10, 4))

        cols = ('ID', 'Ext', 'Resolution', 'FPS', 'VCodec', 'ACodec', 'Bitrate', 'Size')
        widths = (52, 52, 110, 48, 110, 110, 80, 80)

        tf = tk.Frame(popup, bg=BASE)
        tf.pack(fill='both', expand=True, padx=16)
        tree = ttk.Treeview(tf, columns=cols, show='headings', height=14)
        for col, w in zip(cols, widths):
            tree.heading(col, text=col)
            tree.column(col, width=w, anchor='center', stretch=False)
        tree.tag_configure('video',    foreground=BLUE)
        tree.tag_configure('audio',    foreground=GREEN)
        tree.tag_configure('combined', foreground=MAUVE)

        for fmt in reversed(info.get('formats', [])):
            vc   = (fmt.get('vcodec') or 'none')
            ac   = (fmt.get('acodec') or 'none')
            w, h = fmt.get('width'), fmt.get('height')
            res  = f'{w}×{h}' if (w and h) else (fmt.get('format_note', '') or 'audio only')
            fps  = str(int(fmt.get('fps', 0) or 0)) or ''
            tbr  = fmt.get('tbr')
            fs   = fmt.get('filesize') or fmt.get('filesize_approx')
            has_v = vc not in ('none', 'None', '', None)
            has_a = ac not in ('none', 'None', '', None)
            tag   = 'combined' if (has_v and has_a) else ('video' if has_v else 'audio')
            tree.insert('', 'end', tags=(tag,),
                        values=(fmt.get('format_id', ''), fmt.get('ext', ''),
                                res, fps, vc[:12], ac[:12],
                                f'{tbr:.0f}k' if tbr else '',
                                format_bytes(fs) if fs else ''))

        tsb = ttk.Scrollbar(tf, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=tsb.set)
        tsb.pack(side='right', fill='y')
        tree.pack(side='left', fill='both', expand=True)

        desc = info.get('description', '')
        if desc:
            tk.Frame(popup, bg=SURF1, height=1).pack(fill='x', padx=16, pady=(8, 0))
            db = tk.Text(popup, height=3, bg=MANTLE, fg=SUBT0,
                         font=('Segoe UI', 9), relief='flat', bd=0,
                         wrap='word', state='normal', padx=8, pady=4)
            db.insert('1.0', desc[:400] + ('…' if len(desc) > 400 else ''))
            db.configure(state='disabled')
            db.pack(fill='x', padx=16, pady=(4, 0))

        btn_row = tk.Frame(popup, bg=BASE)
        btn_row.pack(fill='x', padx=16, pady=10)
        _bkw = {'font': ('Segoe UI', 9), 'relief': 'flat', 'bd': 0,
                 'padx': 14, 'pady': 6, 'cursor': 'hand2'}

        def add_to_queue():
            url = info.get('webpage_url') or info.get('url', '')
            if url:
                new = DownloadItem(url)
                new.title = title
                self.items[new.id] = new
                self._add_card(new)
                self._q_widgets[new.id]['title_lbl'].configure(
                    text=self._trunc(title, 44))
            popup.destroy()

        tk.Button(btn_row, text='Add to Queue', command=add_to_queue,
                  bg=MAUVE, fg=CRUST, activebackground='#b89be6',
                  font=('Segoe UI', 9, 'bold'), relief='flat', bd=0,
                  padx=14, pady=6, cursor='hand2').pack(side='left')
        tk.Button(btn_row, text='Copy URL',
                  command=lambda: (self.clipboard_clear(),
                                   self.clipboard_append(info.get('webpage_url', ''))),
                  bg=SURF0, fg=TEXT, activebackground=SURF1,
                  **_bkw).pack(side='left', padx=(6, 0))
        tk.Button(btn_row, text='Close', command=popup.destroy,
                  bg=SURF0, fg=TEXT, activebackground=SURF1,
                  **_bkw).pack(side='left', padx=(6, 0))
        self.status_var.set(f'Info: {self._trunc(title, 50)}')

    # ─── Converter tab ────────────────────────────────────────────────────────
    def _build_converter_tab(self, parent: tk.Frame):
        content = tk.Frame(parent, bg=BASE)
        content.pack(fill='both', expand=True)
        self._build_conv_queue_panel(content)
        tk.Frame(content, bg=SURF1, width=1).pack(side='left', fill='y')
        self._build_conv_settings_panel(content)

    def _build_conv_queue_panel(self, parent: tk.Frame):
        frame = tk.Frame(parent, bg=MANTLE)
        frame.pack(side='left', fill='both', expand=True)

        hdr = tk.Frame(frame, bg=MANTLE)
        hdr.pack(fill='x', padx=12, pady=(10, 6))
        tk.Label(hdr, text='FILES TO CONVERT', bg=MANTLE, fg=SUBT0,
                 font=('Segoe UI', 9, 'bold')).pack(side='left')

        _bkw = {'bg': SURF0, 'fg': SUBT0, 'font': ('Segoe UI', 8),
                'relief': 'flat', 'bd': 0, 'padx': 8, 'pady': 3,
                'cursor': 'hand2', 'activebackground': SURF1, 'activeforeground': TEXT}
        tk.Button(hdr, text='Add Files',    command=self._add_conv_files,    **_bkw).pack(side='right', padx=(2, 0))
        tk.Button(hdr, text='Clear Done',   command=self._clear_done_conv,   **_bkw).pack(side='right', padx=2)
        tk.Button(hdr, text='Remove Sel.',  command=self._remove_selected_conv, **_bkw).pack(side='right', padx=2)

        outer = tk.Frame(frame, bg=MANTLE)
        outer.pack(fill='both', expand=True, padx=8, pady=(0, 8))

        vsb = ttk.Scrollbar(outer, orient='vertical')
        vsb.pack(side='right', fill='y')

        self._conv_canvas = tk.Canvas(outer, bg=MANTLE, highlightthickness=0, bd=0,
                                      yscrollcommand=vsb.set)
        self._conv_canvas.pack(side='left', fill='both', expand=True)
        vsb.configure(command=self._conv_canvas.yview)

        self._conv_frame = tk.Frame(self._conv_canvas, bg=MANTLE)
        self._conv_win = self._conv_canvas.create_window((0, 0), window=self._conv_frame, anchor='nw')
        self._conv_frame.bind('<Configure>',
                              lambda _e: self._conv_canvas.configure(
                                  scrollregion=self._conv_canvas.bbox('all')))
        self._conv_canvas.bind('<Configure>',
                               lambda e: self._conv_canvas.itemconfig(self._conv_win, width=e.width))
        self._conv_canvas.bind('<Enter>', lambda _e: self._set_wheel_target(self._conv_canvas))
        self._conv_canvas.bind('<Leave>', lambda _e: self._set_wheel_target(None))

        self._conv_empty_lbl = tk.Label(
            self._conv_frame,
            text='No files added.\nClick "Add Files" to browse for audio or video files.',
            bg=MANTLE, fg=OVL0, font=('Segoe UI', 11), justify='center')
        self._conv_empty_lbl.pack(pady=80)

        # Convert button at bottom
        btn_bar = tk.Frame(frame, bg=MANTLE)
        btn_bar.pack(fill='x', padx=12, pady=(0, 8))
        tk.Button(btn_bar, text='⚙  Convert All', command=self._start_conversions,
                  bg=MAUVE, fg=CRUST, activebackground='#b89be6',
                  font=('Segoe UI', 9, 'bold'), relief='flat', bd=0,
                  padx=14, pady=6, cursor='hand2').pack(side='left')
        tk.Button(btn_bar, text='📁  Open Folder', command=self._open_conv_folder,
                  bg=SURF0, fg=TEXT, font=('Segoe UI', 9), relief='flat', bd=0,
                  padx=14, pady=6, cursor='hand2', activebackground=SURF1,
                  activeforeground=TEXT).pack(side='left', padx=(6, 0))

    def _build_conv_settings_panel(self, parent: tk.Frame):
        frame = tk.Frame(parent, bg=MANTLE, width=380)
        frame.pack(side='left', fill='both')
        frame.pack_propagate(False)

        vsb = ttk.Scrollbar(frame, orient='vertical')
        vsb.pack(side='right', fill='y')

        canvas = tk.Canvas(frame, bg=MANTLE, highlightthickness=0, bd=0,
                           yscrollcommand=vsb.set)
        canvas.pack(side='left', fill='both', expand=True)
        vsb.configure(command=canvas.yview)

        inner = tk.Frame(canvas, bg=MANTLE)
        win = canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>',
                   lambda _e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(win, width=e.width))
        canvas.bind('<Enter>', lambda _e: self._set_wheel_target(canvas))
        canvas.bind('<Leave>', lambda _e: self._set_wheel_target(None))

        P = {'padx': 16, 'pady': 3}

        # ── FORMAT ────────────────────────────────────────────────────────────
        self._sec(inner, 'OUTPUT FORMAT')

        type_row = tk.Frame(inner, bg=MANTLE)
        type_row.pack(fill='x', **P)
        tk.Label(type_row, text='Format Type', bg=MANTLE, fg=SUBT0,
                 font=('Segoe UI', 9)).pack(anchor='w')
        self.conv_type_var = tk.StringVar(value=self.settings['conv_format_type'])
        type_cb = ttk.Combobox(type_row, textvariable=self.conv_type_var,
                               values=['Audio', 'Video'], state='readonly',
                               font=('Segoe UI', 10))
        type_cb.pack(fill='x', pady=(2, 0))
        type_cb.bind('<<ComboboxSelected>>', self._on_conv_type_change)

        fmt_row = tk.Frame(inner, bg=MANTLE)
        fmt_row.pack(fill='x', **P)
        tk.Label(fmt_row, text='Output Format', bg=MANTLE, fg=SUBT0,
                 font=('Segoe UI', 9)).pack(anchor='w')
        self.conv_fmt_var = tk.StringVar(value=self.settings['conv_output_format'])
        self._conv_fmt_cb = ttk.Combobox(fmt_row, textvariable=self.conv_fmt_var,
                                         state='readonly', font=('Segoe UI', 10))
        self._conv_fmt_cb.pack(fill='x', pady=(2, 0))
        self._conv_fmt_cb.bind('<<ComboboxSelected>>', self._on_conv_format_change)

        # ── QUALITY ───────────────────────────────────────────────────────────
        self._sec(inner, 'QUALITY')

        self._conv_audio_q_frame = tk.Frame(inner, bg=MANTLE)
        self._conv_audio_q_frame.pack(fill='x', **P)
        self.conv_audio_q_var = tk.StringVar(value=self.settings['conv_audio_quality'])
        tk.Label(self._conv_audio_q_frame, text='Audio Bitrate (kbps)',
                 bg=MANTLE, fg=SUBT0, font=('Segoe UI', 9)).pack(anchor='w')
        ttk.Combobox(self._conv_audio_q_frame, textvariable=self.conv_audio_q_var,
                     values=['best', '320', '256', '192', '128', '96', '64', '32'],
                     state='readonly', font=('Segoe UI', 10)).pack(fill='x', pady=(2, 0))

        self._conv_video_q_frame = tk.Frame(inner, bg=MANTLE)
        self.conv_video_crf_var = tk.StringVar(value=self.settings['conv_video_crf'])
        tk.Label(self._conv_video_q_frame, text='Video Quality (CRF, 0=lossless → 51=worst)',
                 bg=MANTLE, fg=SUBT0, font=('Segoe UI', 9)).pack(anchor='w')
        e = self._entry(self._conv_video_q_frame, self.conv_video_crf_var)
        e.configure(width=8)
        e.pack(anchor='w', ipady=4, pady=(2, 0))

        # ── OUTPUT ────────────────────────────────────────────────────────────
        self._sec(inner, 'OUTPUT')

        self.conv_outdir_var = tk.StringVar(value=self.settings['conv_output_dir'])
        self._browse_row(inner, 'Save To', self.conv_outdir_var, self._browse_conv_dir)

        self.conv_overwrite_var = tk.BooleanVar(value=self.settings['conv_overwrite'])
        self._chk(inner, 'Overwrite existing files (-y)', self.conv_overwrite_var)

        tk.Label(inner, text='vorbis → .ogg · alac → .m4a · aac → .m4a',
                 bg=MANTLE, fg=OVL0, font=('Segoe UI', 8)).pack(anchor='w', padx=16, pady=(8, 0))

        tk.Frame(inner, bg=MANTLE, height=24).pack()

        # Initialize format list and quality visibility
        self._refresh_conv_fmt_list()
        self.after(100, lambda: self._bind_scroll_on(inner, canvas))

    def _on_conv_type_change(self, _e=None):
        self._refresh_conv_fmt_list()

    def _refresh_conv_fmt_list(self):
        t = self.conv_type_var.get()
        if t == 'Audio':
            self._conv_fmt_cb.configure(values=CONV_AUDIO_FORMATS)
            cur = self.conv_fmt_var.get()
            if cur not in CONV_AUDIO_FORMATS:
                self.conv_fmt_var.set(CONV_AUDIO_FORMATS[0])
        else:
            self._conv_fmt_cb.configure(values=CONV_VIDEO_FORMATS)
            cur = self.conv_fmt_var.get()
            if cur not in CONV_VIDEO_FORMATS:
                self.conv_fmt_var.set(CONV_VIDEO_FORMATS[0])
        self._on_conv_format_change()

    def _on_conv_format_change(self, _e=None):
        fmt = self.conv_fmt_var.get()
        info = CONV_FORMAT_INFO.get(fmt)
        kind = info[1] if info else 'audio'
        if kind == 'video':
            self._conv_audio_q_frame.pack_forget()
            self._conv_video_q_frame.pack(fill='x', padx=16, pady=3)
        else:
            self._conv_video_q_frame.pack_forget()
            if fmt in CONV_LOSSLESS:
                self._conv_audio_q_frame.pack_forget()
            else:
                self._conv_audio_q_frame.pack(fill='x', padx=16, pady=3)

    def _add_conv_files(self):
        paths = filedialog.askopenfilenames(
            title='Select audio or video files',
            filetypes=[
                ('Audio/Video files',
                 '*.mp3 *.wav *.flac *.aac *.m4a *.ogg *.opus *.alac '
                 '*.mp4 *.mkv *.webm *.mov *.avi *.wmv *.ts *.flv *.3gp'),
                ('All files', '*.*'),
            ])
        for path in paths:
            item = ConvItem(path)
            self.conv_items[item.id] = item
            self._add_conv_card(item)

    def _browse_conv_dir(self):
        path = filedialog.askdirectory(
            initialdir=self.conv_outdir_var.get(), title='Select Output Directory')
        if path:
            self.conv_outdir_var.set(path)

    def _open_conv_folder(self):
        path = self.conv_outdir_var.get()
        if os.path.isdir(path):
            if sys.platform == 'win32':
                os.startfile(path)
            else:
                subprocess.Popen(['xdg-open', path])

    # ── Conv card management ──────────────────────────────────────────────────
    _CONV_CARD_BG  = SURF0
    _CONV_DONE_BG  = '#1c2f1c'
    _CONV_ERROR_BG = '#2f1c1c'

    def _add_conv_card(self, item: ConvItem):
        if self._conv_empty_lbl and self._conv_empty_lbl.winfo_ismapped():
            self._conv_empty_lbl.pack_forget()

        card = tk.Frame(self._conv_frame, bg=self._CONV_CARD_BG, padx=10, pady=8)
        card.pack(fill='x', padx=4, pady=3)

        top = tk.Frame(card, bg=self._CONV_CARD_BG)
        top.pack(fill='x')

        chk_var = tk.BooleanVar(value=True)
        tk.Checkbutton(top, variable=chk_var, bg=self._CONV_CARD_BG,
                       activebackground=self._CONV_CARD_BG, fg=TEXT,
                       selectcolor=SURF1, relief='flat', bd=0).pack(side='left')

        name_lbl = tk.Label(top, text=self._trunc(item.filename, 48),
                            bg=self._CONV_CARD_BG, fg=TEXT,
                            font=('Segoe UI', 10), anchor='w')
        name_lbl.pack(side='left', fill='x', expand=True, padx=(4, 0))

        ext_badge = tk.Label(top, text=os.path.splitext(item.filename)[1].upper().lstrip('.') or '?',
                             bg=SURF1, fg=SUBT0, font=('Segoe UI', 8, 'bold'), padx=6, pady=2)
        ext_badge.pack(side='right', padx=(4, 0))

        status_lbl = tk.Label(top, text='PENDING', bg=self._CONV_CARD_BG,
                              fg=YELLOW, font=('Segoe UI', 8, 'bold'))
        status_lbl.pack(side='right')

        prog = ttk.Progressbar(card, style='Horizontal.TProgressbar',
                               mode='determinate', maximum=100, value=0)
        prog.pack(fill='x', pady=(6, 4))

        bot = tk.Frame(card, bg=self._CONV_CARD_BG)
        bot.pack(fill='x')

        info_lbl = tk.Label(bot, text='', bg=self._CONV_CARD_BG, fg=SUBT0,
                            font=('Segoe UI', 9), anchor='w', justify='left',
                            wraplength=0)
        info_lbl.pack(side='left', fill='x', expand=True)

        action_btn = tk.Button(bot, text='Cancel',
                               bg=self._CONV_CARD_BG, fg=SUBT0,
                               font=('Segoe UI', 8), relief='flat', bd=0,
                               padx=6, pady=2, cursor='hand2',
                               activebackground=SURF1, activeforeground=TEXT,
                               command=lambda: self.conv_items.get(item.id) and self.conv_items[item.id].cancel())
        action_btn.pack(side='right')

        wdg = {
            'card': card, 'chk_var': chk_var,
            'name_lbl': name_lbl, 'ext_badge': ext_badge,
            'status_lbl': status_lbl, 'prog': prog,
            'info_lbl': info_lbl, 'action_btn': action_btn,
            'card_bg': self._CONV_CARD_BG,
        }
        self._conv_widgets[item.id] = wdg
        self._bind_scroll_on(card, self._conv_canvas)

    def _update_conv_card(self, item: ConvItem):
        wdg = self._conv_widgets.get(item.id)
        if not wdg:
            return

        wdg['prog']['value'] = item.progress * 100

        STATUS = {
            ConvItem.PENDING:   (YELLOW, 'PENDING'),
            ConvItem.RUNNING:   (MAUVE,  'RUNNING'),
            ConvItem.DONE:      (GREEN,  'DONE'),
            ConvItem.ERROR:     (RED,    'ERROR'),
            ConvItem.CANCELLED: (SUBT0,  'CANCELLED'),
        }
        col, label = STATUS.get(item.status, (SUBT0, item.status.upper()))
        wdg['status_lbl'].configure(text=label, fg=col)

        if item.error:
            wdg['info_lbl'].configure(text=item.error, fg=RED, wraplength=340)
        else:
            pct = f'{item.progress * 100:.1f}%' if item.status == ConvItem.RUNNING else ''
            wdg['info_lbl'].configure(text=pct, fg=SUBT0, wraplength=0)

        if item.status == ConvItem.DONE:
            self._conv_tint(wdg, self._CONV_DONE_BG)
            wdg['prog'].configure(style='Green.Horizontal.TProgressbar')
            wdg['action_btn'].configure(text='✓ Done', state='disabled',
                                        bg=self._CONV_DONE_BG, fg=GREEN,
                                        activebackground=self._CONV_DONE_BG)
        elif item.status == ConvItem.ERROR:
            self._conv_tint(wdg, self._CONV_ERROR_BG)
            wdg['action_btn'].configure(text='Dismiss', state='normal',
                                        bg='#4a2020', fg=PEACH,
                                        activebackground='#5a2828',
                                        command=lambda iid=item.id: self._remove_conv_card(iid))
        elif item.status == ConvItem.CANCELLED:
            wdg['action_btn'].configure(text='Remove', state='normal',
                                        bg=SURF1, fg=TEXT,
                                        activebackground=SURF2,
                                        command=lambda iid=item.id: self._remove_conv_card(iid))

    def _conv_tint(self, wdg: dict, color: str):
        wdg['card_bg'] = color
        for widget in [wdg['card']] + wdg['card'].winfo_children():
            try:
                widget.configure(bg=color)
            except Exception:
                pass
            for child in widget.winfo_children():
                try:
                    child.configure(bg=color)
                except Exception:
                    pass

    def _remove_conv_card(self, item_id: str):
        wdg = self._conv_widgets.pop(item_id, None)
        if wdg:
            wdg['card'].destroy()
        self.conv_items.pop(item_id, None)
        if not self._conv_widgets and self._conv_empty_lbl:
            self._conv_empty_lbl.pack(pady=80)
        if self._conv_canvas:
            self._conv_canvas.configure(scrollregion=self._conv_canvas.bbox('all'))

    def _clear_done_conv(self):
        done = {ConvItem.DONE, ConvItem.ERROR, ConvItem.CANCELLED}
        for iid in [i for i, it in self.conv_items.items() if it.status in done]:
            self._remove_conv_card(iid)

    def _remove_selected_conv(self):
        inactive = {ConvItem.PENDING, ConvItem.DONE, ConvItem.ERROR, ConvItem.CANCELLED}
        for iid in [i for i, w in self._conv_widgets.items()
                    if w['chk_var'].get()
                    and self.conv_items.get(i)
                    and self.conv_items[i].status in inactive]:
            self._remove_conv_card(iid)

    # ── Conversion logic ──────────────────────────────────────────────────────
    def _collect_conv_settings(self) -> dict:
        s = self.settings
        s['conv_format_type']   = self.conv_type_var.get()
        s['conv_output_format'] = self.conv_fmt_var.get()
        s['conv_audio_quality'] = self.conv_audio_q_var.get()
        s['conv_video_crf']     = self.conv_video_crf_var.get()
        s['conv_output_dir']    = self.conv_outdir_var.get()
        s['conv_overwrite']     = self.conv_overwrite_var.get()
        self._save_settings()
        return s

    def _start_conversions(self):
        if not self._ffmpeg_path:
            self._log('[ERR]  FFmpeg not found. Set its path in ⚙ Settings.\n', 'red')
            return
        s = self._collect_conv_settings()
        fmt = s['conv_output_format']
        info = CONV_FORMAT_INFO.get(fmt)
        if not info:
            self._log(f'[ERR]  Unknown output format: {fmt}\n', 'red')
            return

        base_args, kind, ext = info
        quality_args: list[str] = []
        if kind == 'audio' and fmt not in CONV_LOSSLESS:
            q = s['conv_audio_quality']
            if q == 'best':
                if fmt == 'MP3':
                    quality_args = ['-q:a', '0']
                elif fmt in ('OGG', 'Opus'):
                    quality_args = ['-q:a', '6']
                # else: leave encoder to choose default
            else:
                if fmt == 'MP3':
                    quality_args = ['-b:a', f'{q}k']
                elif fmt == 'Opus':
                    quality_args = ['-b:a', f'{q}k']
                else:
                    quality_args = ['-b:a', f'{q}k']
        elif kind == 'video':
            try:
                quality_args = ['-crf', str(int(s['conv_video_crf']))]
            except ValueError:
                quality_args = ['-crf', '23']

        ffmpeg_args = base_args + quality_args
        overwrite_flag = ['-y'] if s['conv_overwrite'] else ['-n']
        out_dir = s['conv_output_dir']
        os.makedirs(out_dir, exist_ok=True)

        started = 0
        for item in self.conv_items.values():
            if item.status != ConvItem.PENDING:
                continue
            stem = os.path.splitext(item.filename)[0]
            out_path = os.path.join(out_dir, f'{stem}.{ext}')
            item.status = ConvItem.RUNNING
            self.msg_q.put(('conv_update', item))
            t = threading.Thread(
                target=self._conv_worker,
                args=(item, self._ffmpeg_path, ffmpeg_args, overwrite_flag, out_path),
                daemon=True)
            self._conv_threads[item.id] = t
            t.start()
            started += 1

        if started:
            self._log(f'Starting {started} conversion(s) → {fmt}\n', 'yellow')
        else:
            self._log('No pending files to convert.\n', '')

    def _probe_duration(self, ffmpeg_bin: str, path: str) -> float:
        """Return file duration in seconds by running `ffmpeg -i <path>`."""
        try:
            kw: dict = {'stderr': subprocess.PIPE, 'text': True, 'timeout': 15}
            if sys.platform == 'win32':
                kw['creationflags'] = subprocess.CREATE_NO_WINDOW
            # ffmpeg exits with code 1 (no output specified) but prints full
            # media info including "Duration: HH:MM:SS.ms" to stderr.
            r = subprocess.run([ffmpeg_bin, '-i', path], **kw)
            m = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.\d+)', r.stderr)
            if m:
                return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        except Exception:
            pass
        return 0.0

    def _conv_worker(self, item: ConvItem, ffmpeg_bin: str,
                     ffmpeg_args: list, overwrite: list, out_path: str):
        try:
            # Probe duration for progress tracking
            item.duration = self._probe_duration(ffmpeg_bin, item.path)

            kw = {}
            if sys.platform == 'win32':
                kw['creationflags'] = subprocess.CREATE_NO_WINDOW

            cmd = ([ffmpeg_bin] + overwrite +
                   ['-i', item.path] + ffmpeg_args + [out_path])
            proc = subprocess.Popen(
                cmd, stderr=subprocess.PIPE, text=True,
                encoding='utf-8', errors='replace', **kw)
            item._proc = proc

            for line in proc.stderr:
                if item._cancel.is_set():
                    proc.terminate()
                    break
                m = re.search(r'time=(\d+):(\d+):(\d+\.\d+)', line)
                if m and item.duration > 0:
                    elapsed = (int(m.group(1)) * 3600 +
                               int(m.group(2)) * 60 +
                               float(m.group(3)))
                    item.progress = min(elapsed / item.duration, 0.99)
                    self.msg_q.put(('conv_update', item))

            proc.wait()

            if item._cancel.is_set():
                item.status = ConvItem.CANCELLED
            elif proc.returncode == 0:
                item.status   = ConvItem.DONE
                item.progress = 1.0
            else:
                item.status = ConvItem.ERROR
                item.error  = f'FFmpeg exited with code {proc.returncode}'

        except Exception as exc:
            if item._cancel.is_set():
                item.status = ConvItem.CANCELLED
            else:
                item.status = ConvItem.ERROR
                item.error  = str(exc)
        finally:
            self.msg_q.put(('conv_update', item))
            tag = 'green' if item.status == ConvItem.DONE else 'red'
            self.msg_q.put(('log', f'[{item.status.upper()}] {item.filename}\n', tag))

    # ─── Utilities ────────────────────────────────────────────────────────────
    @staticmethod
    def _trunc(text: str, n: int) -> str:
        return text if len(text) <= n else text[:n - 1] + '…'

    def destroy(self):
        self._save_settings()
        super().destroy()


if __name__ == '__main__':
    App().mainloop()
