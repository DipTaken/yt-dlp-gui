"""The main application window.

UI assembly and the message-queue dispatch loop. The heavy lifting — yt-dlp
option building and ffmpeg argument building — lives in download.py and
convert.py so it can be tested without a display.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadCancelled, format_bytes
from yt_dlp.version import __version__ as YTDLP_VERSION

from .clip_editor import ClipEditor
from .config import (
    AUDIO_CHANNEL_MODES, AUDIO_FORMATS, AUDIO_TEMPO_CHOICES,
    AUDIO_TRANSPOSE_CHOICES, COMPAT_MODES, CONV_AUDIO_FORMATS,
    CONV_FORMAT_INFO, CONV_FPS_CHOICES, CONV_INTRA_PROFILES, CONV_LOSSLESS,
    CONV_LOUDNESS, CONV_PRESETS, CONV_SCALE_CHOICES, CONV_VIDEO_FORMATS,
    DEFAULT_COMPAT_MODE, DEFAULT_SETTINGS, FORMAT_PRESETS, OUTPUT_TEMPLATES,
    ROOT, SB_CATEGORIES, SB_MODES, SETTINGS_FILE, SUB_FORMATS,
    apply_conv_preset, load_settings, save_settings,
)
from .convert import build_conv_args, conv_name_suffix, parse_time
from .download import (
    build_format_opts, build_postprocessors, build_ydl_opts, int_or_none,
    network_opts, parse_clip_range,
)
from .ffmpeg_utils import (
    effective_duration, find_ffmpeg, iter_ffmpeg_chunks, probe_duration,
    same_file,
)
from .models import (
    ConvItem, DownloadItem, GUILogger, NullLogger, friendly_error,
)
from .preferences import PreferencesWindow
from .theme import (
    BASE, BLUE, CRUST, GREEN, MANTLE, MAUVE, OVL0, PEACH, RED, SUBT0, SURF0,
    SURF1, SURF2, TEXT, YELLOW, apply_dark_titlebar, build_styles,
)
from .widgets import (
    WheelRouter, browse_row, check, entry, labeled_combo, labeled_entry,
    mini_combo, mini_entry, section, tooltip,
)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('YT-DLP GUI')
        self.minsize(900, 620)
        self.configure(bg=BASE)
        self._apply_dark_titlebar(self)

        # The default icon is inherited by every Toplevel too. Frozen builds
        # unpack icon.ico to _MEIPASS; from source it sits in the project root.
        try:
            self.iconbitmap(default=os.path.join(
                getattr(sys, '_MEIPASS', ROOT), 'icon.ico'))
        except Exception:
            pass

        self.settings = self._load_settings()
        self.geometry(self._saved_geometry())
        self.items: dict[str, DownloadItem] = {}
        self.msg_q: queue.Queue = queue.Queue()
        self._threads: dict[str, threading.Thread] = {}
        # Open clip editors by item id, so a second double-click raises the
        # existing one instead of stacking another.
        self._clip_editors: dict = {}
        self._fetching = False
        self._was_busy = False
        self._dl_slot: 'threading.Semaphore | None' = None
        self._dl_slot_size = 0
        self._conv_slot: 'threading.Semaphore | None' = None
        self._conv_slot_size = 0
        self._poll_after_id: 'str | None' = None
        self._clip_after_id: 'str | None' = None
        # Set in destroy(); the repeating timers check it before rescheduling,
        # so a callback already in flight cannot queue another one against a
        # window that is going away.
        self._closing = False

        # Converter state
        self.conv_items: dict[str, ConvItem] = {}
        self._conv_widgets: dict[str, dict] = {}
        self._conv_threads: dict[str, threading.Thread] = {}
        self._conv_canvas: 'tk.Canvas | None' = None
        self._conv_empty_lbl: 'tk.Label | None' = None

        # FFmpeg state (resolved at startup; may be overridden by settings)
        self._ffmpeg_path = self._resolve_ffmpeg()

        # One-shot callback for the "Update yt-dlp" worker; set by the
        # settings dialog, consumed once by _handle('update_result').
        self._pending_update_callback = None

        # Throttling state for progress log lines, keyed by item id.
        self._last_logged_pct: dict[str, float] = {}
        self._last_status_summary = ''
        # Title lookups are courtesy work; two at a time is plenty and keeps a
        # pasted playlist of 50 links from hammering the site before the user
        # has even pressed Download.
        self._title_slot = threading.Semaphore(2)
        # Clipboard watcher state (opt-in, see Preferences → Files).
        # Seeded with whatever is already on the clipboard, so enabling the
        # watcher does not queue a link copied long before.
        try:
            self._clip_last = self.clipboard_get().strip()
        except Exception:
            self._clip_last = ''
        self._clip_ignore = ''

        # Routes the wheel to whichever scrollable canvas the cursor is over.
        self._wheel = WheelRouter(self)

        self._build_styles()
        # A Combobox's class binding changes its value on every wheel tick, so
        # scrolling a sidebar past one silently edited a setting.
        self.bind_class('TCombobox', '<MouseWheel>', lambda _e: None)
        self._create_setting_vars()
        self._build_ui()
        self._bind_shortcuts()
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        if self.settings.get('window_zoomed'):
            try:
                self.state('zoomed')
            except tk.TclError:
                pass
        self._restore_queue()
        self._poll()
        self._watch_clipboard()

    def _saved_geometry(self) -> str:
        """The remembered window geometry, pulled back on screen.

        A monitor that was unplugged since the last run would otherwise
        restore the window somewhere unreachable.
        """
        default = '1200x760'
        m = re.match(r'^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$',
                     self.settings.get('window_geometry') or '')
        if not m:
            return default
        w, h, x, y = (int(g) for g in m.groups())
        vx, vy = self.winfo_vrootx(), self.winfo_vrooty()
        vw, vh = self.winfo_vrootwidth(), self.winfo_vrootheight()
        w, h = min(w, vw), min(h, vh)
        x = min(max(x, vx), max(vx + vw - 200, vx))
        y = min(max(y, vy), max(vy + vh - 120, vy))
        return f'{w}x{h}+{x}+{y}'

    def _bind_shortcuts(self):
        """App-wide keyboard shortcuts. Most ignore Entry/Text focus so typing
        in a field doesn't trigger them, and all of them ignore other windows
        (Preferences, popups) — bind_all would otherwise fire Delete there."""
        def is_text_widget(w):
            try:
                return w.winfo_class() in ('Entry', 'TEntry', 'Text', 'TCombobox',
                                           'Spinbox', 'TSpinbox', 'Treeview',
                                           'Listbox')
            except Exception:
                return False

        def guarded(action):
            def handler(e):
                try:
                    w = self.focus_get()
                except KeyError:        # focus is on a popdown listbox
                    return None
                if (w is None or w.winfo_toplevel() is not self
                        or is_text_widget(w)):
                    return None
                action()
                return 'break'
            return handler

        def focus_url():
            self._url_focus_in(None)
            self.url_entry.focus_set()
            self.url_entry.select_range(0, 'end')
            return 'break'

        def add_files():
            if self._nb.select() == str(self._conv_tab):
                self._add_conv_files()

        # Ctrl+L → focus URL bar (works even from a text widget)
        self.bind_all('<Control-l>', lambda _e: focus_url())
        # Ctrl+D → download selected
        self.bind_all('<Control-d>', guarded(self._download_selected))
        # Delete / Ctrl+Backspace → remove selected
        self.bind_all('<Delete>',           guarded(self._remove_selected))
        # Ctrl+A in queue area → select-all checkboxes (when not in a text widget)
        self.bind_all('<Control-a>',        guarded(self._select_all))
        # Ctrl+V outside a field → queue whatever links are on the clipboard
        self.bind_all('<Control-v>',        guarded(self._queue_clipboard))
        self.bind_all('<Control-r>',        guarded(self._retry_failed))
        self.bind_all('<Control-o>',        guarded(add_files))

    def _queue_clipboard(self):
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            text = ''
        urls = [u for u in self._split_urls(text) if re.match(r'^https?://', u)]
        if not urls:
            self.status_var.set('No links on the clipboard.')
            return
        ids = self._queue_urls(urls)
        self._log(f'Added {len(ids)} URL(s) from the clipboard.\n', 'blue')

    # ── helpers ───────────────────────────────────────────────────────────────
    def _apply_dark_titlebar(self, win):
        apply_dark_titlebar(win)

    def _resolve_ffmpeg(self) -> str:
        """Return the effective ffmpeg path from settings or auto-detection."""
        saved = self.settings.get('ffmpeg_path', '').strip()
        if saved and os.path.isfile(saved):
            return saved
        return find_ffmpeg()

    # ── settings persistence ──────────────────────────────────────────────────
    def _load_settings(self) -> dict:
        return load_settings()

    def _save_settings(self):
        save_settings(self.settings)

    def _save_settings_now(self):
        """Collect current UI values, save to disk, flash status."""
        self._collect_settings()
        self.status_var.set('Settings saved.')
        self._log('Settings saved.\n', 'blue')

    # Configured once in Preferences, so the sidebar's Reset leaves them alone:
    # wiping a proxy or cookie source from a button that looks like it only
    # touches format options would be a nasty surprise.
    _PREFS_KEYS = frozenset({
        'archive_enabled', 'archive_file', 'rate_limit', 'proxy', 'retries',
        'concurrent_fragments', 'max_concurrent', 'no_playlist', 'max_filesize',
        'date_after', 'date_before', 'cookie_browser', 'cookie_file',
        'watch_clipboard', 'sleep_interval', 'max_sleep_interval',
        'sleep_interval_requests', 'playlist_items', 'socket_timeout',
        'force_ipv4', 'extractor_retries', 'trim_file_name',
        'break_on_existing', 'thumbnail_to_jpg'})

    def _reset_settings(self):
        """Restore the sidebar's settings to DEFAULT_SETTINGS values."""
        if not messagebox.askyesno(
                'Reset settings',
                'Reset the sidebar options to their defaults?\n\n'
                'Network, cookies, filters and other Preferences are kept.',
                parent=self):
            return
        for key, var in self._setting_var_map():
            if key in self._PREFS_KEYS or key == 'template_preset':
                continue    # the preset follows the template via its trace
            value = DEFAULT_SETTINGS[key]
            if key == 'post_convert_preset' and not value:
                value = '(nothing)'
            var.set(value)
        # Refresh dependent sub-frames
        self._on_format_change()
        self._on_compat_change()
        self._on_audio_toggle()
        self._on_sb_toggle()
        self._collect_settings()
        self.status_var.set('Settings reset to defaults.')
        self._log('Settings reset to defaults.\n', 'yellow')

    # ── styles ────────────────────────────────────────────────────────────────
    def _build_styles(self):
        build_styles(self)
    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self._build_header()
        tk.Frame(self, bg=SURF1, height=1).pack(fill='x')

        # The log sits in its own pane so it can be dragged taller when a
        # download is misbehaving.
        paned = tk.PanedWindow(self, orient='vertical', bg=SURF1, bd=0,
                               sashwidth=5, sashrelief='flat')
        paned.pack(fill='both', expand=True)
        top = tk.Frame(paned, bg=BASE)
        log_pane = tk.Frame(paned, bg=MANTLE)
        paned.add(top, stretch='always', minsize=360)
        paned.add(log_pane, stretch='never', minsize=60)

        nb = ttk.Notebook(top)
        nb.pack(fill='both', expand=True)
        self._nb = nb          # kept so actions can switch tabs

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
        self._conv_tab = conv_tab
        self._build_converter_tab(conv_tab)

        tk.Frame(top, bg=SURF1, height=1).pack(fill='x')
        self._build_bottom(top, log_pane)

    # ─── Header ──────────────────────────────────────────────────────────────
    def _build_header(self):
        hdr = tk.Frame(self, bg=MANTLE)
        hdr.pack(fill='x')

        # Logo
        tk.Label(hdr, text='YT-DLP GUI', bg=MANTLE, fg=MAUVE,
                 font=('Segoe UI', 14, 'bold')).pack(side='left', padx=(16, 6), pady=12)
        tk.Label(hdr, text=f'yt-dlp {YTDLP_VERSION}', bg=MANTLE, fg=OVL0,
                 font=('Segoe UI', 9)).pack(side='left', pady=12)

        # Settings gear button (right side, packed before URL so it's rightmost)
        tk.Button(hdr, text=' ⚙ ', command=self._open_preferences,
                  bg=SURF0, fg=SUBT0, font=('Segoe UI', 11),
                  relief='flat', bd=0, padx=10, pady=6,
                  cursor='hand2', activebackground=SURF1,
                  activeforeground=TEXT).pack(side='right', padx=(4, 12))

        # FFmpeg badge
        self._ffmpeg_badge = tk.Label(hdr, bg=MANTLE, font=('Segoe UI', 8, 'bold'),
                                      cursor='hand2')
        self._ffmpeg_badge.pack(side='right', padx=(4, 2))
        self._ffmpeg_badge.bind('<Button-1>', lambda _e: self._open_preferences())
        self._refresh_ffmpeg_badge()

        # URL entry
        url_wrap = tk.Frame(hdr, bg=MANTLE)
        url_wrap.pack(side='left', fill='x', expand=True, padx=16, pady=10)

        self._url_placeholder = ('Paste a link and press Enter to download  ·  '
                                 'Shift+Enter to queue it  ·  Ctrl+L')
        self._url_is_placeholder = True
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
        # Enter is the whole point of pasting a link, so it downloads. Queueing
        # without starting is still one keystroke away.
        self.url_entry.bind('<Return>',       lambda _e: self._add_urls(start=True))
        self.url_entry.bind('<Shift-Return>', lambda _e: self._add_urls(start=False))

        _bkw = {'bg': SURF0, 'fg': TEXT, 'font': ('Segoe UI', 9),
                 'relief': 'flat', 'bd': 0, 'padx': 10, 'pady': 5,
                 'cursor': 'hand2', 'activebackground': SURF1, 'activeforeground': TEXT}

        self._tooltip(
            tk.Button(url_wrap, text='⬇  Download',
                      command=lambda: self._add_urls(start=True),
                      **{**_bkw, 'bg': MAUVE, 'fg': CRUST,
                         'activebackground': '#b89be6',
                         'font': ('Segoe UI', 9, 'bold')}),
            'Add and start straight away  ·  Enter').pack(side='left', padx=(0, 2))
        self._tooltip(
            tk.Button(url_wrap, text='Add', command=lambda: self._add_urls(start=False),
                      **_bkw),
            'Queue without starting  ·  Shift+Enter').pack(side='left', padx=2)
        tk.Button(url_wrap, text='Paste', command=self._paste_url,
                  **_bkw).pack(side='left', padx=2)
        self._fetch_btn = tk.Button(url_wrap, text='Fetch Info',
                                    command=self._fetch_info_btn, **_bkw)
        self._fetch_btn.pack(side='left', padx=2)

    def _refresh_ffmpeg_badge(self):
        if self._ffmpeg_path:
            self._ffmpeg_badge.configure(
                text=' FFmpeg ✓ ', bg='#1a3a1a', fg=GREEN)
            tip = f'Using {self._ffmpeg_path}\nClick to change it.'
        else:
            self._ffmpeg_badge.configure(
                text=' FFmpeg ✗ ', bg='#3a1a1a', fg=RED)
            tip = ('FFmpeg not found — merging, audio extraction, the clip '
                   'editor and the Converter all need it.\n'
                   'Click to set the path or download it.')
        # Replace rather than stack: this runs again every time the path
        # changes, and add='+' bindings would pile up tooltips.
        self._ffmpeg_badge.unbind('<Enter>')
        self._ffmpeg_badge.unbind('<Leave>')
        self._tooltip(self._ffmpeg_badge, tip)

    def _url_focus_in(self, _e):
        if self._url_is_placeholder:
            self.url_entry.delete(0, 'end')
            self.url_entry.configure(fg=TEXT)
            self._url_is_placeholder = False

    def _url_focus_out(self, _e):
        if not self.url_var.get().strip():
            self.url_entry.insert(0, self._url_placeholder)
            self.url_entry.configure(fg=SUBT0)
            self._url_is_placeholder = True

    def _url_text(self) -> str:
        """The URL bar's real content: '' when it holds only the placeholder.
        Also strips a placeholder that got left in front of pasted text."""
        raw = self.url_var.get().strip()
        if raw.startswith(self._url_placeholder):
            raw = raw[len(self._url_placeholder):].strip()
        return raw

    def _paste_url(self):
        try:
            text = self.clipboard_get().strip()
            self.url_entry.delete(0, 'end')
            self.url_entry.configure(fg=TEXT)
            self.url_entry.insert(0, text)
            self._url_is_placeholder = False
        except Exception:
            pass

    # ─── Queue persistence ───────────────────────────────────────────────────
    def _restore_queue(self):
        """Re-add whatever was still unfinished at the last close.

        Never auto-started: coming back to downloads already running, possibly
        overwriting files, is not something the app should decide on its own.
        """
        snaps = self.settings.get('queue_snapshot') or []
        if not isinstance(snaps, list):
            return
        restored = 0
        for snap in snaps[:200]:
            item = DownloadItem.from_snapshot(snap)
            if item is None:
                continue
            self.items[item.id] = item
            self._add_card(item)
            restored += 1
        if restored:
            self._log(f'Restored {restored} unfinished item(s) from last time — '
                      f'press Download when ready.\n', 'blue')

    def _snapshot_queue(self):
        """Store everything not finished, so closing mid-queue is not a loss."""
        try:
            self.settings['queue_snapshot'] = [
                it.to_snapshot() for it in list(self.items.values())[:200]
                if it.status != DownloadItem.DONE]
        except Exception:
            self.settings['queue_snapshot'] = []

    # ─── Clipboard watching ──────────────────────────────────────────────────
    def _watch_clipboard(self):
        """Poll the clipboard for links, when the user has asked us to.

        Opt-in, and it only *queues* — silently starting a download because
        something got copied would be a surprise nobody wants. Reschedules
        itself regardless of the setting so the toggle takes effect live.
        """
        if self._closing:
            return
        try:
            if self.watch_clip_var.get():
                text = ''
                try:
                    text = self.clipboard_get().strip()
                except Exception:
                    text = ''
                if (text and text != self._clip_last
                        and text != self._clip_ignore and len(text) < 4000):
                    self._clip_last = text
                    urls = [u for u in self._split_urls(text)
                            if re.match(r'^https?://', u)]
                    known = {i.url for i in self.items.values()}
                    fresh = [u for u in urls if u not in known]
                    if fresh:
                        self._queue_urls(fresh)
                        self._log(f'Clipboard: queued {len(fresh)} link(s).\n',
                                  'blue')
        except tk.TclError:
            return                      # window is going away
        except Exception:
            pass
        if not self._closing:
            self._clip_after_id = self.after(1000, self._watch_clipboard)

    # ─── Clip editor ─────────────────────────────────────────────────────────
    def _open_clip_editor(self, item_id: str):
        """Pick in/out points for a queued download.

        The queue holds page URLs, not media, so the editor is handed a
        resolver that asks yt-dlp for a direct stream. A small format is
        requested deliberately: every scrub is an HTTP range request, and a
        360p stream seeks in a fraction of the time a 4K one takes.
        """
        item = self.items.get(item_id)
        if not item:
            return
        if self._raise_clip_editor(item_id) or not self._need_ffmpeg('clip editor'):
            return

        def resolve():
            opts = {'quiet': True, 'no_warnings': True, 'skip_download': True,
                    'noplaylist': True,
                    # Prefer a small progressive stream: one URL, cheap to seek.
                    'format': 'best[height<=480][acodec!=none][vcodec!=none]'
                              '/best[height<=720]/best'}
            s = self.settings
            opts.update(network_opts(s, self._warn))
            try:
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(item.url, download=False)
                if not info:
                    return ''
                if info.get('_type') in ('playlist', 'multi_video'):
                    entries = [e for e in (info.get('entries') or []) if e]
                    if not entries:
                        return ''
                    info = entries[0]
                # Some sites refuse a bare request, so the editor gets the
                # headers yt-dlp would have sent along with the stream URL.
                return (info.get('url') or '',
                        info.get('http_headers') or None)
            except Exception as exc:
                self.msg_q.put(('log', f'[ERR]  Clip preview: {exc}\n', 'red'))
                return ''

        # Seed from this item's own range, not the sidebar's default.
        rng = item.clip_range
        self._clip_editors[item_id] = ClipEditor(
            self, source=item.url, title=self._trunc(item.title, 60),
            ffmpeg=self._ffmpeg_path, resolver=resolve,
            initial_in=(rng[0] if rng else 0.0),
            initial_out=(rng[1] if rng and rng[1] != float('inf') else None),
            on_apply=lambda s, e, iid=item_id: self._set_item_clip(iid, s, e))

    def _raise_clip_editor(self, key: str) -> bool:
        """Bring an already-open editor for this item forward. True if there was one."""
        editor = self._clip_editors.get(key)
        try:
            if editor is not None and editor.winfo_exists():
                editor.lift()
                editor.focus_force()
                return True
        except tk.TclError:
            pass
        self._clip_editors.pop(key, None)
        return False

    def _need_ffmpeg(self, what: str) -> bool:
        """True when ffmpeg is available; otherwise say so and offer the fix."""
        if self._ffmpeg_path:
            return True
        self._log(f'[ERR]  The {what} needs FFmpeg. '
                  'Set its path in Preferences → Tools.\n', 'red')
        self.status_var.set(f'The {what} needs FFmpeg.')
        if messagebox.askyesno(
                'FFmpeg not found',
                f'The {what} needs FFmpeg, which was not found.\n\n'
                'Open Preferences to set its path?', parent=self):
            self._open_preferences()
        return False

    @staticmethod
    def _clock(seconds: float) -> str:
        h, rem = divmod(float(seconds or 0), 3600)
        m, s = divmod(rem, 60)
        return f'{int(h):02d}:{int(m):02d}:{s:06.3f}'

    def _open_conv_clip_editor(self, item_id: str):
        """Pick trim points for a local converter file — no resolving needed."""
        item = self.conv_items.get(item_id)
        if not item:
            return
        if self._raise_clip_editor(item_id) or not self._need_ffmpeg('clip editor'):
            return
        if not os.path.isfile(item.path):
            self._log(f'[ERR]  File is gone: {item.path}\n', 'red')
            return

        def apply_trim(start, end):
            # Per file: the panel's own trim boxes stay the default for items
            # that were not trimmed here.
            item.trim = None if (start is None and end is None) else (start, end)
            self._update_conv_card(item)
            self._log(f'Trim for {self._trunc(item.filename, 40)}: '
                      f'{item.trim_label or "cleared"}\n', 'blue')

        trim = item.trim or (None, None)
        self._clip_editors[item_id] = ClipEditor(
            self, source=item.path, title=item.filename,
            ffmpeg=self._ffmpeg_path, on_apply=apply_trim,
            initial_in=(trim[0] or 0.0), initial_out=trim[1])

    def _open_preferences(self):
        """Open (or re-focus) the Preferences window."""
        existing = getattr(self, '_prefs_win', None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return
        self._prefs_win = PreferencesWindow(self)

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
        tk.Button(hdr, text='Retry Failed', command=self._retry_failed, **_bkw).pack(side='right', padx=2)
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
        self._wheel.set_target(canvas)

    def _bind_scroll_on(self, widget, canvas):
        self._wheel.bind_tree(widget, canvas)

    def _populate_settings(self, inner):
        """The sidebar holds only what changes between downloads.

        Everything configured once and then forgotten — network, cookies,
        filters, the raw filename template, the archive — lives in the
        Preferences window instead. Groups that are toggled on and off rather
        than tuned per download start collapsed.
        """
        P = {'padx': 16, 'pady': 3}

        # ── FORMAT ───────────────────────────────────────────────────────────
        body = self._sec(inner, 'FORMAT')
        preset_anchor = self._labeled_combo(
            body, 'Format Preset', self.fmt_preset_var,
            list(FORMAT_PRESETS.keys()), on_select=self._on_format_change)

        self._custom_fmt_frame = tk.Frame(body, bg=MANTLE)
        self._custom_fmt_anchor = preset_anchor
        tk.Label(self._custom_fmt_frame, text='Custom Format String',
                 bg=MANTLE, fg=SUBT0, font=('Segoe UI', 9)).pack(anchor='w')
        self._entry(self._custom_fmt_frame, self.custom_fmt_var).pack(
            fill='x', pady=(2, 0), ipady=4)
        tk.Label(self._custom_fmt_frame,
                 text='Raw yt-dlp -f selector. The codec preference below is '
                      'not applied to custom selectors.',
                 bg=MANTLE, fg=SURF2, font=('Segoe UI', 8),
                 wraplength=330, justify='left').pack(anchor='w', pady=(2, 0))

        self._compat_anchor = self._labeled_combo(
            body, 'Codec / Container Preference', self.compat_var,
            list(COMPAT_MODES.keys()), on_select=self._on_compat_change)
        self._compat_help = tk.Label(body, text='', bg=MANTLE, fg=SURF2,
                                     font=('Segoe UI', 8), wraplength=330,
                                     justify='left')
        self._compat_help.pack(anchor='w', padx=16, pady=(0, 2))
        self._on_compat_change()

        ttk.Checkbutton(body, text='Extract Audio Only',
                        variable=self.audio_extract_var,
                        command=self._on_audio_toggle).pack(anchor='w', **P)

        self._audio_opts_frame = tk.Frame(body, bg=MANTLE)
        arow = tk.Frame(self._audio_opts_frame, bg=MANTLE)
        arow.pack(fill='x')
        # AUDIO_FORMATS, not a hand-written list: 'ogg' used to be offered here
        # and is not a codec yt-dlp knows, so picking it lost the download.
        self._mini_combo(arow, 'Audio Format', self.audio_fmt_var,
                         AUDIO_FORMATS)
        tk.Frame(arow, bg=MANTLE, width=12).pack(side='left')
        self._mini_combo(arow, 'Quality (kbps)', self.audio_q_var,
                         ['best', '320', '256', '192', '128', '96', '64', '32'])

        arow2 = tk.Frame(self._audio_opts_frame, bg=MANTLE)
        arow2.pack(fill='x', pady=(6, 0))
        self._mini_combo(arow2, 'Sample Rate', self.audio_sr_var,
                         ['', '22050', '44100', '48000', '96000'])
        tk.Label(arow2, text='(blank = keep original)', bg=MANTLE, fg=OVL0,
                 font=('Segoe UI', 8)).pack(side='left', padx=(8, 0), pady=(16, 0))

        # ── Practice controls ────────────────────────────────────────────────
        tk.Label(self._audio_opts_frame, text='PRACTICE', bg=MANTLE, fg=MAUVE,
                 font=('Segoe UI', 8, 'bold')).pack(anchor='w', pady=(10, 0))
        arow3 = tk.Frame(self._audio_opts_frame, bg=MANTLE)
        arow3.pack(fill='x', pady=(2, 0))
        self._mini_combo(arow3, 'Speed %', self.audio_tempo_var,
                         AUDIO_TEMPO_CHOICES)
        tk.Frame(arow3, bg=MANTLE, width=10).pack(side='left')
        self._mini_combo(arow3, 'Transpose', self.audio_transpose_var,
                         AUDIO_TRANSPOSE_CHOICES)
        tk.Frame(arow3, bg=MANTLE, width=10).pack(side='left')
        self._mini_combo(arow3, 'Channels', self.audio_channels_var,
                         list(AUDIO_CHANNEL_MODES.keys()))
        tk.Label(self._audio_opts_frame,
                 text='Slows a solo down without dropping its pitch, or shifts '
                      'the key for a capo or a Bb instrument. "Left only" '
                      'isolates an instrument panned to one side of an old mix. '
                      'The speed and key end up in the filename.',
                 bg=MANTLE, fg=SURF2, font=('Segoe UI', 8),
                 wraplength=330, justify='left').pack(anchor='w', pady=(2, 0))

        ttk.Checkbutton(self._audio_opts_frame,
                        text='Normalize Audio Volume (FFmpeg loudnorm)',
                        variable=self.audio_norm_var).pack(anchor='w', pady=(4, 0))
        ttk.Checkbutton(self._audio_opts_frame,
                        text='Keep the original video file as well',
                        variable=self.keep_video_var).pack(anchor='w')
        ttk.Checkbutton(self._audio_opts_frame,
                        text='Tag from "Artist - Title" in the video title',
                        variable=self.music_tags_var).pack(anchor='w')
        tk.Label(self._audio_opts_frame,
                 text='Splits a title like "Daft Punk - Around the World" into '
                      'separate artist and track tags.',
                 bg=MANTLE, fg=SURF2, font=('Segoe UI', 8),
                 wraplength=330, justify='left').pack(anchor='w')

        self._on_format_change()
        self._on_audio_toggle()

        # ── SAVE TO ──────────────────────────────────────────────────────────
        body = self._sec(inner, 'SAVE TO')
        self._browse_row(body, 'Folder', self.outdir_var, self._browse_dir)
        self._labeled_combo(body, 'Filename Layout', self.tmpl_preset_var,
                            list(OUTPUT_TEMPLATES.keys()),
                            on_select=self._on_template_preset_change)
        tk.Label(body, text='Edit the raw template in Preferences → Files.',
                 bg=MANTLE, fg=SURF2, font=('Segoe UI', 8)).pack(anchor='w', padx=16)

        # ── CLIPS & CHAPTERS ─────────────────────────────────────────────────
        body = self._sec(inner, 'CLIPS & CHAPTERS')
        tk.Label(body, text='Download only part of a video — no need to pull a '
                            'two-hour stream for a ten-second cutaway.',
                 bg=MANTLE, fg=SURF2, font=('Segoe UI', 8),
                 wraplength=330, justify='left').pack(anchor='w', padx=16, pady=(0, 4))
        tk.Label(body, text='Default range for new queue items',
                 bg=MANTLE, fg=SUBT0, font=('Segoe UI', 9)).pack(anchor='w', padx=16)
        cr = tk.Frame(body, bg=MANTLE)
        cr.pack(fill='x', **P)
        self._mini_entry(cr, 'Start (1:30)', self.clip_start_var, width=13)
        tk.Frame(cr, bg=MANTLE, width=10).pack(side='left')
        self._mini_entry(cr, 'End (blank = to end)', self.clip_end_var, width=13)
        tk.Label(body, text='Each queued item keeps its own range, shown as a ✂ '
                            'badge on the card — so one video can be queued '
                            'twice for two different sections. Double-click a '
                            'card to set it visually.',
                 bg=MANTLE, fg=SURF2, font=('Segoe UI', 8),
                 wraplength=330, justify='left').pack(anchor='w', padx=16)
        self._chk(body, 'Frame-accurate cuts (slower, re-encodes)',
                  self.clip_precise_var)
        self._chk(body, 'Split into one file per chapter', self.split_chapters_var)
        tk.Label(body, text='Splitting turns an album upload, DJ set or long '
                            'tutorial into separate numbered files.',
                 bg=MANTLE, fg=SURF2, font=('Segoe UI', 8),
                 wraplength=330, justify='left').pack(anchor='w', padx=16)

        # ── AFTER DOWNLOAD (collapsed) ───────────────────────────────────────
        body = self._sec(inner, 'AFTER DOWNLOAD', collapsed=True)
        self._labeled_combo(body, 'Convert finished files with preset',
                            self.post_conv_var,
                            ['(nothing)'] + list(CONV_PRESETS.keys()))
        tk.Label(body, text='Runs the finished file through the Converter '
                            'automatically — a download straight to a ProRes '
                            'proxy, or to a practice-speed MP3. The original '
                            'is kept, and the new file lands beside it.',
                 bg=MANTLE, fg=SURF2, font=('Segoe UI', 8),
                 wraplength=330, justify='left').pack(anchor='w', padx=16)

        # ── SUBTITLES (collapsed) ────────────────────────────────────────────
        body = self._sec(inner, 'SUBTITLES', collapsed=True)
        self._chk(body, 'Download Subtitles', self.write_subs_var)
        self._chk(body, 'Include Auto-generated', self.auto_subs_var)
        self._chk(body, 'Embed into Video (requires FFmpeg)', self.embed_subs_var)
        f2 = tk.Frame(body, bg=MANTLE)
        f2.pack(fill='x', **P)
        tk.Label(f2, text='Languages (comma-separated, e.g. en,es)',
                 bg=MANTLE, fg=SUBT0, font=('Segoe UI', 9)).pack(anchor='w')
        self._entry(f2, self.sub_langs_var).pack(fill='x', pady=(2, 0), ipady=4)
        self._labeled_combo(body, 'Convert Subtitles To', self.sub_convert_var,
                            SUB_FORMATS)
        tk.Label(body, text='Sites usually serve VTT; SRT is what editing and '
                            'transcription tools expect.',
                 bg=MANTLE, fg=SURF2, font=('Segoe UI', 8),
                 wraplength=330, justify='left').pack(anchor='w', padx=16)

        # ── METADATA & ARTWORK (collapsed) ───────────────────────────────────
        body = self._sec(inner, 'METADATA & ARTWORK', collapsed=True)
        self._chk(body, 'Embed Thumbnail (requires FFmpeg)', self.embed_thumb_var)
        self._chk(body, 'Save Thumbnail File', self.write_thumb_var)
        self._chk(body, 'Embed Metadata / ID3 (requires FFmpeg)', self.embed_meta_var)
        self._chk(body, 'Write Info JSON', self.write_json_var)

        # ── SPONSORBLOCK (collapsed) ─────────────────────────────────────────
        body = self._sec(inner, 'SPONSORBLOCK', collapsed=True)
        sb_chk = ttk.Checkbutton(body, text='Enable SponsorBlock',
                                 variable=self.sb_var, command=self._on_sb_toggle)
        sb_chk.pack(anchor='w', **P)
        self._sb_anchor = sb_chk
        self._sb_frame = tk.Frame(body, bg=MANTLE)
        self._labeled_combo(self._sb_frame, 'Categories', self.sb_cats_var,
                            SB_CATEGORIES)
        self._labeled_combo(self._sb_frame, 'Action', self.sb_mode_var, SB_MODES)
        tk.Label(self._sb_frame,
                 text='Remove physically cuts the segments out — useful for '
                      'music uploads padded with intros and self-promo.',
                 bg=MANTLE, fg=SURF2, font=('Segoe UI', 8),
                 wraplength=330, justify='left').pack(anchor='w', padx=16)
        self._on_sb_toggle()

        # ── Footer actions ───────────────────────────────────────────────────
        df = tk.Frame(inner, bg=MANTLE)
        df.pack(fill='x', padx=16, pady=(18, 4))
        _bkw = {'font': ('Segoe UI', 9), 'relief': 'flat', 'bd': 0,
                'padx': 10, 'pady': 5, 'cursor': 'hand2', 'activeforeground': TEXT}
        tk.Button(df, text='⚙  Preferences…', command=self._open_preferences,
                  bg=SURF1, fg=TEXT, activebackground=SURF2, **_bkw).pack(side='left')
        tk.Button(df, text='Save now', command=self._save_settings_now,
                  bg=SURF0, fg=TEXT, activebackground=SURF1, **_bkw
                  ).pack(side='left', padx=(6, 0))
        tk.Button(df, text='Reset', command=self._reset_settings,
                  bg=SURF0, fg=SUBT0, activebackground=SURF1, **_bkw
                  ).pack(side='left', padx=(6, 0))

        tk.Frame(inner, bg=MANTLE, height=24).pack()

        # Bind scroll activation to every widget in the settings panel
        self.after(100, lambda: self._bind_scroll_on(inner, self._s_canvas))


    # ─── Bottom bar ───────────────────────────────────────────────────────────
    def _build_bottom(self, parent, log_pane):
        bar = tk.Frame(parent, bg=BASE)
        bar.pack(fill='x', padx=12, pady=6)

        _bkw = {'font': ('Segoe UI', 9), 'relief': 'flat', 'bd': 0,
                 'padx': 14, 'pady': 6, 'cursor': 'hand2',
                 'activeforeground': TEXT}
        self._tooltip(
            tk.Button(bar, text='⬇  Download Selected', command=self._download_selected,
                      bg=MAUVE, fg=CRUST, activebackground='#b89be6',
                      font=('Segoe UI', 9, 'bold'), relief='flat', bd=0,
                      padx=14, pady=6, cursor='hand2'),
            'Ctrl+D').pack(side='left')
        tk.Button(bar, text='⬇  Download All',   command=self._download_all,
                  bg=SURF0, activebackground=SURF1, **_bkw).pack(side='left', padx=(4, 0))
        tk.Button(bar, text='⏹  Cancel All',      command=self._cancel_all,
                  bg=SURF0, activebackground=SURF1, **_bkw).pack(side='left', padx=(4, 0))
        tk.Button(bar, text='📁  Open Folder',    command=self._open_folder,
                  bg=SURF0, activebackground=SURF1, **_bkw).pack(side='left', padx=(4, 0))
        self._tooltip(
            tk.Button(bar, text='🗑  Remove Selected', command=self._remove_selected,
                      bg=SURF0, activebackground=SURF1, **_bkw),
            'Delete').pack(side='left', padx=(4, 0))

        self.status_var = tk.StringVar(value='Ready')
        tk.Label(bar, textvariable=self.status_var, bg=BASE, fg=SUBT0,
                 font=('Segoe UI', 9)).pack(side='right')

        # Aggregate progress bar (hidden until something is active)
        self._total_prog = ttk.Progressbar(bar, style='Horizontal.TProgressbar',
                                           mode='determinate', maximum=100,
                                           value=0, length=160)
        # Packed/unpacked on demand in _update_total_progress()

        # Log strip
        log_hdr = tk.Frame(log_pane, bg=MANTLE)
        log_hdr.pack(fill='x')
        tk.Label(log_hdr, text='LOG', bg=MANTLE, fg=MAUVE,
                 font=('Segoe UI', 9, 'bold')).pack(side='left', padx=(12, 0), pady=4)
        tk.Button(log_hdr, text='Clear', command=self._clear_log,
                  bg=MANTLE, fg=SUBT0, font=('Segoe UI', 8), relief='flat',
                  bd=0, padx=8, pady=2, cursor='hand2',
                  activebackground=SURF0).pack(side='right', padx=8, pady=2)
        tk.Button(log_hdr, text='Copy', command=self._copy_log,
                  bg=MANTLE, fg=SUBT0, font=('Segoe UI', 8), relief='flat',
                  bd=0, padx=8, pady=2, cursor='hand2',
                  activebackground=SURF0).pack(side='right', pady=2)

        self.log_txt = tk.Text(log_pane, height=5, bg=MANTLE, fg=SUBT0,
                               font=('Consolas', 9), relief='flat', bd=0,
                               state='disabled', wrap='word',
                               insertbackground=TEXT,
                               selectbackground=SURF1, selectforeground=TEXT)
        self.log_txt.pack(fill='both', expand=True, padx=8, pady=(0, 6))
        self.log_txt.tag_config('green',  foreground=GREEN)
        self.log_txt.tag_config('red',    foreground=RED)
        self.log_txt.tag_config('yellow', foreground=YELLOW)
        self.log_txt.tag_config('blue',   foreground=BLUE)

    # ─── Settings widget helpers ──────────────────────────────────────────────
    def _sec(self, parent, text, collapsed=False):
        return section(parent, text, collapsed)

    def _entry(self, parent, var):
        return entry(parent, var)

    def _chk(self, parent, text, var):
        check(parent, text, var)

    def _labeled_combo(self, parent, label, var, values, on_select=None):
        return labeled_combo(parent, label, var, values, on_select)

    def _labeled_entry(self, parent, label, var, width=None):
        labeled_entry(parent, label, var, width)

    def _mini_combo(self, parent, label, var, values):
        mini_combo(parent, label, var, values)

    def _mini_entry(self, parent, label, var, width=8):
        mini_entry(parent, label, var, width)

    def _browse_row(self, parent, label, var, cmd):
        browse_row(parent, label, var, cmd)
    # ─── Toggle handlers ──────────────────────────────────────────────────────
    # Each of these re-shows a frame with an explicit `after=` anchor. Without
    # one, pack() appends to the end of the parent, so toggling any of these
    # options shuffled the settings panel into a different order.
    def _on_format_change(self, _e=None):
        preset = self.fmt_preset_var.get()
        if preset == 'Custom…':
            self._custom_fmt_frame.pack(fill='x', padx=16, pady=3,
                                        after=self._custom_fmt_anchor)
        else:
            self._custom_fmt_frame.pack_forget()
        # A raw selector and "audio only" both bypass the codec policy, so hide
        # the control rather than let it imply an effect it does not have.
        bypassed = preset == 'Custom…' or self.audio_extract_var.get()
        if bypassed:
            self._compat_anchor.pack_forget()
            self._compat_help.pack_forget()
        else:
            self._compat_anchor.pack(fill='x', padx=16, pady=3,
                                     after=self._custom_fmt_anchor)
            self._compat_help.pack(anchor='w', padx=16, pady=(0, 2),
                                   after=self._compat_anchor)

    def _on_compat_change(self, _e=None):
        mode = COMPAT_MODES.get(self.compat_var.get())
        self._compat_help.configure(text=mode['help'] if mode else '')

    def _on_audio_toggle(self):
        if self.audio_extract_var.get():
            self._audio_opts_frame.pack(fill='x', padx=16, pady=3)
        else:
            self._audio_opts_frame.pack_forget()
        self._on_format_change()

    def _on_sb_toggle(self):
        if self.sb_var.get():
            self._sb_frame.pack(fill='x', padx=16, pady=3,
                                after=self._sb_anchor)
        else:
            self._sb_frame.pack_forget()

    def _on_template_preset_change(self, _e=None, initial=False):
        name = self.tmpl_preset_var.get()
        tmpl = OUTPUT_TEMPLATES.get(name)
        if tmpl and not initial:
            self._suppress_tmpl_trace = True
            try:
                self.tmpl_var.set(tmpl)
            finally:
                self._suppress_tmpl_trace = False
        self._refresh_tmpl_hint()

    def _on_template_edited(self, *_a):
        if not self._suppress_tmpl_trace:
            current = self.tmpl_var.get()
            match = next((k for k, v in OUTPUT_TEMPLATES.items() if v and v == current), None)
            self.tmpl_preset_var.set(match or 'Custom…')
        self._refresh_tmpl_hint()

    def _refresh_tmpl_hint(self):
        """Validate the template live and show what it will produce.

        The hint label lives in the Preferences window, which may not be open —
        the trace on tmpl_var fires regardless.
        """
        hint = getattr(self, '_tmpl_hint', None)
        if hint is None or not hint.winfo_exists():
            return
        self._tmpl_hint = hint
        tmpl = self.tmpl_var.get().strip()
        if not tmpl:
            self._tmpl_hint.configure(
                text='%(title)s  %(id)s  %(uploader)s  %(height)sp  %(ext)s',
                fg=SURF2)
            return
        err = YoutubeDL.validate_outtmpl(tmpl)
        if err:
            self._tmpl_hint.configure(text=f'Invalid template: {err}', fg=RED)
            return
        self._tmpl_hint.configure(text=f'e.g.  {self._preview_template(tmpl)}', fg=SURF2)

    _PREVIEW_INFO = {
        'title': 'Around the World', 'id': 'dQw4w9WgXcQ', 'ext': 'mp4',
        'uploader': 'Daft Punk', 'artist': 'Daft Punk', 'track': 'Around the World',
        'album': 'Homework', 'track_number': 7, 'height': 1080, 'fps': 60,
        'upload_date': '20240115', 'playlist_title': 'Homework', 'playlist_index': 7,
        'duration': 428, 'epoch': 0,
    }

    def _preview_template(self, tmpl: str) -> str:
        try:
            ydl = YoutubeDL({'quiet': True, 'outtmpl': tmpl, 'simulate': True})
            try:
                return ydl.prepare_filename(dict(self._PREVIEW_INFO))
            finally:
                ydl.close()
        except Exception:
            return '(preview unavailable)'

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
    @staticmethod
    def _split_urls(raw: str) -> list:
        """Split pasted input into URLs.

        Splits on whitespace/newlines. Commas are only treated as separators
        when every resulting piece looks like its own URL — a blanket
        `replace(',', '\\n')` mangled query strings that legitimately contain
        commas (YouTube `list=`, timestamps, many CDN signatures).
        """
        tokens = [t.strip() for t in raw.split() if t.strip()]
        out: list = []
        for tok in tokens:
            if ',' in tok:
                parts = [p.strip() for p in tok.split(',') if p.strip()]
                if len(parts) > 1 and all(
                        re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', p) for p in parts):
                    out.extend(parts)
                    continue
            out.append(tok)
        return out

    def _add_urls(self, start: bool = False):
        """Queue whatever is in the URL bar. `start` also begins downloading.

        Returns the new item ids so callers (the clipboard watcher, the info
        popup) can act on them.
        """
        raw = self._url_text()
        if not raw:
            return []
        urls = self._split_urls(raw)
        if not urls:
            return []
        self.url_entry.delete(0, 'end')
        # Focus normally stays in the box after Enter. Writing the placeholder
        # back then would leave it there for good (FocusIn never fires again),
        # so the next paste landed after it.
        if self.focus_get() is self.url_entry:
            self.url_entry.configure(fg=TEXT)
            self._url_is_placeholder = False
        else:
            self._url_is_placeholder = False
            self._url_focus_out(None)
        ids = self._queue_urls(urls)
        self._log(f'Added {len(ids)} URL(s) to queue.\n', 'blue')
        if start:
            self._start_downloads(ids)
        return ids

    def _queue_urls(self, urls: list) -> list:
        """Create a card per URL, snapshotting the sidebar's default clip
        range onto each one, and look the titles up in the background."""
        default_clip = self._parse_clip_range(self._collect_settings())
        ids = []
        for url in urls:
            item = DownloadItem(url, clip_range=default_clip)
            self.items[item.id] = item
            self._add_card(item)
            ids.append(item.id)
            self._fetch_title_async(item)
        return ids

    def _fetch_title_async(self, item: DownloadItem):
        """Fill a pending card's title in the background.

        A queue of raw URLs is unreadable, and the title only arrives with the
        first progress hook otherwise — which never happens for an item that
        is sitting waiting. Best effort on purpose: failures stay silent,
        because the download itself will report them properly, and the status
        is left alone so this can never block Download.
        """
        slot = self._title_slot
        s = self.settings
        opts = {'quiet': True, 'no_warnings': True, 'skip_download': True,
                'noplaylist': bool(s.get('no_playlist')),
                # Do not walk an entire channel just to label a card.
                'extract_flat': 'in_playlist',
                # Nobody asked for this lookup, so its failures stay silent
                # instead of being written to stderr.
                'logger': NullLogger()}
        opts.update(network_opts(s))
        iid, url = item.id, item.url

        def worker():
            with slot:
                if iid not in self.items:
                    return
                try:
                    with YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                except Exception:
                    return
                if not info:
                    return
                entries = [e for e in (info.get('entries') or []) if e]
                self.msg_q.put(('meta', iid, {
                    'title': info.get('title'),
                    'duration': info.get('duration'),
                    'uploader': info.get('uploader') or info.get('channel'),
                    'entry_count': (info.get('playlist_count') or len(entries)
                                    if entries else 0),
                    'playlist_title': info.get('title') if entries else '',
                }))

        threading.Thread(target=worker, daemon=True).start()

    def _fetch_info_btn(self):
        if self._fetching:
            return
        raw = self._url_text()
        if not raw:
            return
        urls = self._split_urls(raw)
        url = urls[0] if urls else raw
        s = self._collect_settings()

        # Reuse the download credentials/network settings. Without these, info
        # fetching failed on anything age-gated, private or geo-blocked even
        # though cookies and a proxy were configured for downloads.
        # 'in_playlist' keeps a 200-video channel from being fully extracted
        # just to list it; a single video is still resolved in full, so the
        # format table is unaffected.
        opts: dict = {'quiet': True, 'no_warnings': True, 'skip_download': True,
                      'noplaylist': s['no_playlist'],
                      'extract_flat': 'in_playlist'}
        opts.update(network_opts(s, self._warn))
        if self._ffmpeg_path:
            opts['ffmpeg_location'] = os.path.dirname(self._ffmpeg_path)

        self.status_var.set('Fetching info…')
        self._fetching = True
        self._fetch_btn.configure(state='disabled')
        threading.Thread(target=self._fetch_info_thread, args=(url, opts),
                         daemon=True).start()

    def _fetch_info_thread(self, url: str, opts: dict):
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                # A playlist/channel URL has no formats of its own. Showing the
                # first entry's format table — what this used to do — answered
                # a question nobody asked and hid the other 26 videos.
                if info and info.get('_type') in ('playlist', 'multi_video'):
                    entries = [e for e in (info.get('entries') or []) if e]
                    if not entries:
                        self.msg_q.put(('status', 'No entries found at that URL.'))
                        return
                    self.msg_q.put(('show_playlist', {
                        'title': info.get('title') or 'Playlist',
                        'url': info.get('webpage_url') or url,
                        'entries': [{'title': e.get('title'),
                                     'duration': e.get('duration'),
                                     'url': e.get('webpage_url') or e.get('url')}
                                    for e in entries],
                    }))
                    return
            if info:
                self.msg_q.put(('show_info', info))
            else:
                self.msg_q.put(('status', 'Could not fetch info.'))
        except Exception as exc:
            self.msg_q.put(('status', f'Fetch error: {exc}'))
            self.msg_q.put(('log', f'[ERR]  Fetch failed: {exc}\n', 'red'))
        finally:
            self.msg_q.put(('fetch_done',))

    # ─── Queue card management ────────────────────────────────────────────────
    _CARD_BG   = SURF0
    _DONE_BG   = '#1c2f1c'
    _ERROR_BG  = '#2f1c1c'
    _CANCEL_BG = '#26262e'

    def _add_card(self, item: DownloadItem):
        if self._empty_lbl.winfo_manager():
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

        # Per-item clip range. Packed only when there is one, so an ordinary
        # download carries no extra furniture.
        clip_lbl = tk.Label(top, text='', bg=self._CARD_BG, fg=MAUVE,
                            font=('Segoe UI', 8, 'bold'))
        self._tooltip(clip_lbl, 'This item downloads only part of the video')

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
                               command=lambda iid=item.id: self._cancel_item(iid))
        action_btn.pack(side='right')

        clip_btn = tk.Button(bot, text='✂', bg=self._CARD_BG, fg=SUBT0,
                             font=('Segoe UI', 9), relief='flat', bd=0,
                             padx=6, pady=2, cursor='hand2',
                             activebackground=SURF1, activeforeground=TEXT,
                             command=lambda iid=item.id: self._open_clip_editor(iid))
        clip_btn.pack(side='right', padx=(0, 4))
        self._tooltip(clip_btn, 'Set in/out points visually (or double-click the card)')

        # Pause is cancel + resume: yt-dlp continues from the .part file.
        # Packed only while a download is actually running.
        pause_btn = tk.Button(bot, text='⏸', bg=self._CARD_BG, fg=SUBT0,
                              font=('Segoe UI', 9), relief='flat', bd=0,
                              padx=6, pady=2, cursor='hand2',
                              activebackground=SURF1, activeforeground=TEXT,
                              command=lambda iid=item.id: self._pause_item(iid))
        self._tooltip(pause_btn, 'Pause — Resume continues from where it stopped')

        # Secondary line: partial-playlist summary, or the error hint.
        note_lbl = tk.Label(card, text='', bg=self._CARD_BG, fg=YELLOW,
                            font=('Segoe UI', 8), anchor='w', justify='left',
                            wraplength=360)

        wdg = {
            'card': card, 'check_var': check_var, 'clip_btn': clip_btn,
            'pause_btn': pause_btn, 'title_lbl': title_lbl, 'status_lbl': status_lbl,
            'clip_lbl': clip_lbl, 'note_lbl': note_lbl,
            'prog': prog, 'info_lbl': info_lbl,
            'action_btn': action_btn,
            'card_bg': self._CARD_BG,  # current bg, updated on tint
        }
        self._q_widgets[item.id] = wdg

        # Bind scroll events so hovering over a card still scrolls the queue
        self._bind_scroll_on(card, self._q_canvas)
        # Double-click anywhere on the card opens the clip editor; right-click
        # is the way to everything else the item can do.
        for w in (card, top, bot, title_lbl, info_lbl, note_lbl):
            w.bind('<Double-Button-1>',
                   lambda _e, iid=item.id: self._open_clip_editor(iid))
            w.bind('<Button-3>',
                   lambda e, iid=item.id: self._post_card_menu(e, iid))
        self._update_card(item)

    # ── Card context menu ────────────────────────────────────────────────────
    def _popup_menu(self, attr: str, event, entries: list):
        """Show a right-click menu built from (label, command, enabled) tuples
        (None = separator). One Menu per kind is kept and repopulated —
        building a fresh one on every click leaked a widget each time."""
        menu = getattr(self, attr, None)
        if menu is None:
            menu = tk.Menu(self, tearoff=0, bg=SURF0, fg=TEXT,
                           activebackground=MAUVE, activeforeground=CRUST,
                           bd=0, font=('Segoe UI', 9))
            setattr(self, attr, menu)
        menu.delete(0, 'end')
        for entry_ in entries:
            if entry_ is None:
                menu.add_separator()
            else:
                label, cmd, enabled = entry_
                menu.add_command(label=label, command=cmd,
                                 state='normal' if enabled else 'disabled')
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _post_card_menu(self, event, item_id: str):
        """Right-click menu — the single route to every per-item action.

        Putting these on the card itself would need six more buttons per row;
        a menu keeps the queue readable and still makes them reachable.
        """
        item = self.items.get(item_id)
        if not item:
            return
        has_file = bool(item.paths and os.path.exists(item.paths[0]))
        order = list(self.items)
        pos = order.index(item_id)
        self._popup_menu('_card_menu', event, [
            ('Open file', lambda: self._reveal(item.paths[0]), has_file),
            ('Show in folder', lambda: self._show_in_folder(item.paths[0]), has_file),
            ('Send to Converter', lambda: self._send_to_converter(item), has_file),
            None,
            ('Copy URL', lambda: self._copy(item.url), True),
            ('Edit clip range…', lambda: self._open_clip_editor(item_id), True),
            ('Clear clip range',
             lambda: self._set_item_clip(item_id, None, None), bool(item.clip_range)),
            ('Duplicate', lambda: self._duplicate_item(item), True),
            None,
            ('Move to top', lambda: self._move_item(item_id, 'top'), pos > 0),
            ('Move up', lambda: self._move_item(item_id, 'up'), pos > 0),
            ('Move down', lambda: self._move_item(item_id, 'down'),
             pos < len(order) - 1),
            None,
            ('Pause', lambda: self._pause_item(item_id),
             item.status == DownloadItem.DOWNLOADING),
            ('Resume' if item.paused else 'Retry',
             lambda: self._retry_item(item_id), item.status not in DownloadItem.ACTIVE),
            ('Remove', lambda: self._remove_one(item_id), True),
        ])

    def _move_item(self, item_id: str, where: str):
        """Reorder a queue card. The dict order is the visible order, and it is
        what Download All / Download Selected start items in."""
        order = list(self.items)
        if item_id not in order:
            return
        i = order.index(item_id)
        order.remove(item_id)
        j = {'top': 0, 'up': max(i - 1, 0), 'down': min(i + 1, len(order))}[where]
        order.insert(j, item_id)
        # Swap the dict objects rather than mutating them: the title-lookup
        # threads read self.items, and a half-rebuilt dict would look empty.
        self.items = {k: self.items[k] for k in order}
        self._q_widgets = {k: self._q_widgets[k] for k in order if k in self._q_widgets}
        prev = None
        for k in order:                       # repack in the new order
            wdg = self._q_widgets.get(k)
            if wdg:
                if prev is None:
                    first = next(iter(self._q_frame.pack_slaves()), None)
                    if first is not wdg['card']:
                        wdg['card'].pack(fill='x', padx=4, pady=3, before=first)
                else:
                    wdg['card'].pack(fill='x', padx=4, pady=3, after=prev)
                prev = wdg['card']
        self._q_sync_scroll()

    def _pause_item(self, item_id: str):
        item = self.items.get(item_id)
        if not item or item.status != DownloadItem.DOWNLOADING:
            return
        self._cancel_item(item_id)
        item.paused = True      # after the cancel, which clears it

    def _copy(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)
        # Stops the clipboard watcher from re-queueing what we just copied.
        self._clip_ignore = text

    def _remove_one(self, item_id: str):
        self._remove_card(item_id)
        self.items.pop(item_id, None)

    @staticmethod
    def _show_in_folder(path: str):
        """Open the folder with the file highlighted (Explorer), else just the folder."""
        try:
            if sys.platform == 'win32' and os.path.exists(path):
                subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
                return
        except Exception:
            pass
        App._reveal(os.path.dirname(path) or path)

    def _duplicate_item(self, item: DownloadItem):
        """Queue the same URL again as a fresh PENDING item.

        This is how a second section of one video gets queued: duplicate, then
        give the copy its own clip range. The filenames carry the range, so the
        two downloads do not collide.
        """
        copy = DownloadItem(item.url, clip_range=item.clip_range)
        copy.title = item.title
        copy.format_override = item.format_override
        copy.duration = item.duration
        copy.uploader = item.uploader
        copy.title_locked = item.title_locked
        self.items[copy.id] = copy
        self._add_card(copy)
        self._log(f'Duplicated {self._trunc(item.title, 40)}.\n', 'blue')

    def _send_to_converter(self, item: DownloadItem):
        added = 0
        for path in item.paths:
            if os.path.isfile(path):
                conv = ConvItem(path)
                self.conv_items[conv.id] = conv
                self._add_conv_card(conv)
                added += 1
        if added:
            self._nb.select(self._conv_tab)
            self._log(f'Added {added} file(s) to the Converter.\n', 'blue')

    def _set_item_clip(self, item_id: str, start, end):
        item = self.items.get(item_id)
        if not item:
            return
        item.clip_range = None if start is None else (start, end)
        self._update_card(item)
        if start is None:
            self._log('Clip range cleared (full length).\n', 'blue')
        else:
            self._log(f'Clip range set: {self._clock(start)} → '
                      f'{self._clock(end)}\n', 'blue')

    def _remove_card(self, item_id: str):
        # Cancel the item first so any still-running daemon thread stops
        # touching widgets that are about to be destroyed.
        item = self.items.get(item_id)
        if item and item.status in DownloadItem.ACTIVE:
            item.cancel()
        wdg = self._q_widgets.pop(item_id, None)
        if wdg:
            wdg['card'].destroy()
        if not self._q_widgets:
            self._empty_lbl.pack(pady=80)
        self._q_sync_scroll()

    def _update_card(self, item: DownloadItem):
        wdg = self._q_widgets.get(item.id)
        if not wdg:
            self._update_total_progress()
            return

        wdg['title_lbl'].configure(text=self._trunc(item.title, 44))
        wdg['prog']['value'] = item.progress * 100
        self._update_total_progress()

        # Clip badge — only present when this item has its own range.
        label = item.clip_label
        if label:
            wdg['clip_lbl'].configure(text=f'✂ {label}  ')
            if not wdg['clip_lbl'].winfo_manager():
                wdg['clip_lbl'].pack(side='right', after=wdg['status_lbl'])
        elif wdg['clip_lbl'].winfo_manager():
            wdg['clip_lbl'].pack_forget()

        # Secondary note (partial playlist, error hint).
        note = item.note
        if not note and item.status == DownloadItem.ERROR:
            note = friendly_error(item.error)
        if note:
            wdg['note_lbl'].configure(
                text=note, fg=RED if item.status == DownloadItem.ERROR else YELLOW)
            if not wdg['note_lbl'].winfo_manager():
                wdg['note_lbl'].pack(fill='x', after=wdg['prog'])
        elif wdg['note_lbl'].winfo_manager():
            wdg['note_lbl'].pack_forget()

        STATUS_COLORS = {
            DownloadItem.PENDING:     (YELLOW, 'PENDING'),
            DownloadItem.QUEUED:      (BLUE,   'QUEUED'),
            DownloadItem.FETCHING:    (BLUE,   'FETCHING'),
            DownloadItem.DOWNLOADING: (MAUVE,  'DOWNLOADING'),
            DownloadItem.CONVERTING:  (PEACH,  'CONVERTING'),
            DownloadItem.DONE:        (GREEN,  'DONE'),
            DownloadItem.ERROR:       (RED,    'ERROR'),
            DownloadItem.CANCELLED:   (SUBT0,  'CANCELLED'),
        }
        col, label = STATUS_COLORS.get(item.status, (SUBT0, item.status.upper()))
        if item.paused and item.status == DownloadItem.CANCELLED:
            col, label = BLUE, 'PAUSED'
        wdg['status_lbl'].configure(text=label, fg=col)

        # Info / error text. Only surface the error on a card that actually
        # failed — otherwise a recoverable warning logged mid-download stayed
        # painted red under a finished item.
        if item.error and item.status == DownloadItem.ERROR:
            wdg['info_lbl'].configure(text=self._trunc(item.error, 220),
                                      fg=RED, wraplength=360)
        elif item.status == DownloadItem.QUEUED:
            wdg['info_lbl'].configure(text='Waiting for a free slot…',
                                      fg=SUBT0, wraplength=0)
        elif item.status == DownloadItem.DONE:
            wdg['info_lbl'].configure(text=self._done_summary(item),
                                      fg=SUBT0, wraplength=0)
        elif item.status == DownloadItem.PENDING:
            wdg['info_lbl'].configure(text=self._pending_summary(item),
                                      fg=SUBT0, wraplength=0)
        else:
            parts = [p for p in [self._playlist_progress(item), item.size_str,
                                 item.speed,
                                 f'ETA {item.eta}' if item.eta else ''] if p]
            wdg['info_lbl'].configure(text='   '.join(parts), fg=SUBT0, wraplength=0)

        # Pause only makes sense while bytes are actually coming down.
        if item.status == DownloadItem.DOWNLOADING:
            if not wdg['pause_btn'].winfo_manager():
                wdg['pause_btn'].pack(side='right', padx=(0, 4),
                                      after=wdg['action_btn'])
        elif wdg['pause_btn'].winfo_manager():
            wdg['pause_btn'].pack_forget()

        # Tint + action button
        if item.status == DownloadItem.PENDING:
            # Also the state after Retry/Resume's reset: back to a neutral card.
            if wdg['card_bg'] != self._CARD_BG:
                self._tint(wdg, self._CARD_BG)
                wdg['prog'].configure(style='Horizontal.TProgressbar')
            wdg['action_btn'].configure(
                text='Start', state='normal',
                bg=self._CARD_BG, fg=GREEN,
                activebackground=SURF1,
                command=lambda iid=item.id: self._start_downloads([iid]))

        elif item.status == DownloadItem.DONE:
            self._tint(wdg, self._DONE_BG)
            wdg['prog'].configure(style='Green.Horizontal.TProgressbar')
            # "Done" was a dead end: the file was on disk and the app offered
            # no way to reach it. Open is the obvious next thing to want.
            wdg['action_btn'].configure(
                text='Open', state='normal',
                bg=self._DONE_BG, fg=GREEN,
                activebackground=SURF1,
                command=lambda iid=item.id: self._open_item(iid))

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
                text='▶ Resume' if item.paused else '↺ Retry', state='normal',
                bg=SURF1, fg=TEXT,
                activebackground=SURF2,
                command=lambda iid=item.id: self._retry_item(iid))

        elif item.status in DownloadItem.ACTIVE:
            wdg['action_btn'].configure(
                text='Cancel', state='normal',
                bg=wdg['card_bg'], fg=SUBT0,
                activebackground=SURF1,
                command=lambda iid=item.id: self._cancel_item(iid))

    @staticmethod
    def _playlist_progress(item: DownloadItem) -> str:
        if item.entry_count > 1 and item.entry_index:
            return f'{item.entry_index}/{item.entry_count}'
        return ''

    def _pending_summary(self, item: DownloadItem) -> str:
        """What we know before anything is downloaded — filled in by the
        background title lookup, so a waiting queue is readable."""
        if item.entry_count > 1:
            return f'Playlist · {item.entry_count} videos'
        parts = []
        if item.duration:
            parts.append(self._fmt_duration(item.duration))
        if item.uploader:
            parts.append(item.uploader)
        return ' · '.join(parts)

    def _done_summary(self, item: DownloadItem) -> str:
        """'1080p60 · avc1 / mp4a · 245 MB' for a finished card."""
        parts = []
        if item.entry_count > 1:
            parts.append(f'{len(item.paths)} files')
        elif item.height:
            fps = f'{int(item.fps)}' if item.fps else ''
            parts.append(f'{item.height}p{fps}')
        codecs = [c.split('.')[0] for c in (item.vcodec, item.acodec)
                  if c and c != 'none']
        if codecs:
            parts.append(' / '.join(codecs))
        if item.filesize:
            parts.append(format_bytes(item.filesize))
        return ' · '.join(parts) or 'Finished'

    def _open_item(self, item_id: str):
        """Open the finished file, or fall back to the folder it went to."""
        item = self.items.get(item_id)
        if not item:
            return
        for path in item.paths:
            if os.path.exists(path):
                self._reveal(path)
                return
        self._log('The finished file could not be found — opening the output '
                  'folder instead.\n', 'yellow')
        self._open_folder()

    @staticmethod
    def _fmt_duration(seconds) -> str:
        try:
            secs = int(seconds)
        except (TypeError, ValueError):
            return ''
        m, s = divmod(secs, 60)
        h, m = divmod(m, 60)
        return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'

    @staticmethod
    def _tint_widgets(card: tk.Widget, color: str, skip=()):
        """Paint a card frame and its non-ttk descendants with a background
        colour. `skip` lists widgets with a colour of their own (badges)."""
        def paint(widget):
            if widget in skip:
                return
            try:
                widget.configure(bg=color)
                if widget.winfo_class() == 'Checkbutton':
                    widget.configure(activebackground=color)
            except Exception:
                pass

        for widget in [card] + card.winfo_children():
            paint(widget)
            for child in widget.winfo_children():
                paint(child)

    def _tint(self, wdg: dict, color: str):
        wdg['card_bg'] = color
        self._tint_widgets(wdg['card'], color, skip=(wdg.get('ext_badge'),))

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

    def _retry_failed(self):
        """Restart everything that failed or was cancelled.

        A flaky connection or an expired cookie tends to take several items
        down at once, and clicking Retry on each card is busywork.
        """
        failed = [i for i, it in self.items.items()
                  if it.status in (DownloadItem.ERROR, DownloadItem.CANCELLED)]
        if not failed:
            self._log('Nothing to retry.\n', 'yellow')
            return
        for iid in failed:
            self._reset_item(iid)
        self._start_downloads(failed)

    def _remove_selected(self):
        inactive = {DownloadItem.PENDING, DownloadItem.DONE,
                    DownloadItem.ERROR, DownloadItem.CANCELLED}
        doomed = [i for i, w in self._q_widgets.items()
                  if w['check_var'].get()
                  and self.items.get(i)
                  and self.items[i].status in inactive]
        # Every card starts ticked, so Delete with nothing in mind could take
        # out the whole queue in one keystroke.
        if len(doomed) > 3 and not messagebox.askyesno(
                'Remove items', f'Remove {len(doomed)} items from the queue?',
                parent=self):
            return
        for iid in doomed:
            self._remove_card(iid)
            self.items.pop(iid, None)

    def _cancel_all(self):
        cancelled = 0
        for item in self.items.values():
            if item.status in DownloadItem.ACTIVE:
                item.cancel()
                cancelled += 1
        self._log(f'Cancel requested for {cancelled} active download(s).\n', 'yellow')

    def _cancel_item(self, item_id: str):
        item = self.items.get(item_id)
        # A PENDING item has no worker; setting its cancel flag would only
        # poison it, so the next Start would cancel itself immediately.
        if not item or item.status not in DownloadItem.ACTIVE:
            return
        item.paused = False
        item.cancel()
        wdg = self._q_widgets.get(item_id)
        if wdg:
            wdg['action_btn'].configure(text='Cancelling…', state='disabled')

    def _reset_item(self, item_id: str) -> bool:
        """Put an item back to PENDING (the card repaints itself)."""
        item = self.items.get(item_id)
        if not item:
            return False
        item.reset()
        wdg = self._q_widgets.get(item_id)
        if wdg:
            wdg['prog']['value'] = 0
            wdg['info_lbl'].configure(text='', fg=SUBT0, wraplength=0)
        self._update_card(item)
        self._last_logged_pct.pop(item_id, None)
        return True

    def _retry_item(self, item_id: str):
        if self._reset_item(item_id):
            self._start_downloads([item_id])

    @staticmethod
    def _reveal(path: str):
        try:
            if sys.platform == 'win32':
                os.startfile(path)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', path])
            else:
                subprocess.Popen(['xdg-open', path])
        except Exception:
            pass

    def _open_folder(self):
        path = self.outdir_var.get()
        if os.path.isdir(path):
            self._reveal(path)
        else:
            self._log(f'Output folder does not exist: {path}\n', 'yellow')

    def _copy_log(self):
        """Copy the selection if there is one, else the whole log."""
        try:
            text = self.log_txt.get('sel.first', 'sel.last')
        except tk.TclError:
            text = self.log_txt.get('1.0', 'end-1c')
        self._copy(text)
        self.status_var.set('Log copied.')

    def _clear_log(self):
        self.log_txt.configure(state='normal')
        self.log_txt.delete('1.0', 'end')
        self.log_txt.configure(state='disabled')

    # ─── Download logic ────────────────────────────────────────────────────────
    # Widgets — sidebar or Preferences window — bind to these, so a setting
    # keeps its value while Preferences is closed and _setting_var_map no
    # longer depends on which panels happen to have been built.
    _SETTING_VARS = [
        # (attribute,           settings key)
        ('fmt_preset_var',      'format_preset'),
        ('compat_var',          'compat_mode'),
        ('custom_fmt_var',      'custom_format'),
        ('audio_extract_var',   'audio_extract'),
        ('audio_fmt_var',       'audio_format'),
        ('audio_q_var',         'audio_quality'),
        ('audio_norm_var',      'audio_normalize'),
        ('audio_sr_var',        'audio_sample_rate'),
        ('audio_tempo_var',     'audio_tempo'),
        ('audio_transpose_var', 'audio_transpose'),
        ('audio_channels_var',  'audio_channels'),
        ('keep_video_var',      'keep_video'),
        ('music_tags_var',      'music_tags'),
        ('outdir_var',          'output_dir'),
        ('tmpl_var',            'output_template'),
        ('tmpl_preset_var',     'template_preset'),
        ('archive_var',         'archive_enabled'),
        ('archive_file_var',    'archive_file'),
        ('embed_thumb_var',     'embed_thumbnail'),
        ('write_thumb_var',     'write_thumbnail'),
        ('embed_meta_var',      'embed_metadata'),
        ('write_json_var',      'write_infojson'),
        ('write_subs_var',      'write_subs'),
        ('auto_subs_var',       'auto_subs'),
        ('embed_subs_var',      'embed_subs'),
        ('sub_langs_var',       'sub_langs'),
        ('sub_convert_var',     'sub_convert'),
        ('sb_var',              'sponsorblock_enabled'),
        ('sb_cats_var',         'sponsorblock_cats'),
        ('sb_mode_var',         'sponsorblock_mode'),
        ('clip_start_var',      'clip_start'),
        ('clip_end_var',        'clip_end'),
        ('clip_precise_var',    'clip_precise'),
        ('split_chapters_var',  'split_chapters'),
        ('rate_limit_var',      'rate_limit'),
        ('proxy_var',           'proxy'),
        ('retries_var',         'retries'),
        ('concurrent_var',      'concurrent_fragments'),
        ('max_conc_var',        'max_concurrent'),
        ('no_playlist_var',     'no_playlist'),
        ('maxfs_var',           'max_filesize'),
        ('date_after_var',      'date_after'),
        ('date_before_var',     'date_before'),
        ('cookie_browser_var',  'cookie_browser'),
        ('cookie_file_var',     'cookie_file'),
        ('post_conv_var',       'post_convert_preset'),
        ('watch_clip_var',      'watch_clipboard'),
        ('sleep_int_var',       'sleep_interval'),
        ('sleep_max_var',       'max_sleep_interval'),
        ('sleep_req_var',       'sleep_interval_requests'),
        ('playlist_items_var',  'playlist_items'),
        ('socket_timeout_var',  'socket_timeout'),
        ('force_ipv4_var',      'force_ipv4'),
        ('extractor_retries_var', 'extractor_retries'),
        ('trim_name_var',       'trim_file_name'),
        ('break_existing_var',  'break_on_existing'),
        ('thumb_jpg_var',       'thumbnail_to_jpg'),
    ]

    def _create_setting_vars(self):
        """Build every download-tab Var up front. Type follows the default."""
        for attr, key in self._SETTING_VARS:
            default = DEFAULT_SETTINGS[key]
            cls = tk.BooleanVar if isinstance(default, bool) else tk.StringVar
            setattr(self, attr, cls(value=self.settings.get(key, default)))
        # Lives on the Var, not on a panel: the template box is in Preferences
        # and the layout dropdown is in the sidebar, so either can change it and
        # both need the dropdown/preview kept in sync.
        # The flag stops a programmatic write (layout dropdown -> template) from
        # bouncing the dropdown straight back to "Custom…".
        self._suppress_tmpl_trace = False
        self.tmpl_var.trace_add('write', self._on_template_edited)
        # The combo's "off" entry is a readable label rather than a blank row.
        if not self.post_conv_var.get():
            self.post_conv_var.set('(nothing)')

    def _setting_var_map(self) -> list:
        """(settings_key, tk_var) for every download-tab setting."""
        return [(key, getattr(self, attr)) for attr, key in self._SETTING_VARS]

    def _collect_settings(self) -> dict:
        for key, var in self._setting_var_map():
            self.settings[key] = var.get()
        self._save_settings()
        return self.settings

    def _warn(self, text: str):
        """Thread-safe warning into the log strip."""
        self.msg_q.put(('log', f'[WARN] {text}\n', 'yellow'))

    def _log_error(self, text: str):
        """Thread-safe error into the log strip. Passed to the option builders
        so they can report without knowing anything about the UI."""
        self.msg_q.put(('log', f'[ERR]  {text}\n', 'red'))

    def _build_format_opts(self, item, s):
        return build_format_opts(item, s, self._warn)

    def _build_postprocessors(self, s):
        return build_postprocessors(s, self._warn)

    def _build_ydl_opts(self, item, s: dict) -> dict:
        return build_ydl_opts(
            item, s,
            ffmpeg_path=self._ffmpeg_path,
            logger=GUILogger(self.msg_q, item.id),
            progress_hook=self._make_progress_hook(item),
            pp_hook=self._make_pp_hook(item),
            warn=self._warn,
            post_hook=self._make_post_hook(item))

    def _make_post_hook(self, item: DownloadItem):
        """Records the final path of every finished file.

        This is the only place yt-dlp reveals where a file actually landed
        after the postprocessor chain has renamed and re-containered it, and it
        fires once per file — so a playlist or a chapter split reports each
        piece. It is what "Open" on a done card and the follow-up conversion
        both depend on.
        """
        iid = item.id

        def hook(filepath):
            # Recorded here, on the worker thread, not when the message is
            # drained: the worker checks item.paths the moment download()
            # returns, which can be before the main loop has seen the message.
            if filepath and filepath not in item.paths:
                item.paths.append(filepath)
            self.msg_q.put(('file', iid, filepath))
        return hook

    @staticmethod
    def _note_playlist(item: DownloadItem, d: dict):
        """Copy playlist position off a progress message onto the item.

        A playlist is one queue card covering many videos; without this it
        shows a bar that resets to zero over and over with no indication that
        anything is being counted.
        """
        if d.get('n_entries'):
            item.entry_count = d['n_entries']
        if d.get('playlist_index'):
            item.entry_index = d['playlist_index']
        if d.get('playlist_title'):
            item.playlist_title = d['playlist_title']
            item.title = d['playlist_title']

    def _chain_convert(self, item: DownloadItem, path: str):
        """Hand a just-finished download to the Converter, if the item asked.

        The preset was snapshotted onto the item when the download started, so
        a queue can carry several different follow-ups at once. The output
        lands beside the download rather than in the Converter's own folder —
        the file the user just asked for should not move somewhere else.
        """
        preset = getattr(item, 'post_convert', '')
        if not preset or preset not in CONV_PRESETS:
            return
        if not os.path.isfile(path):
            return
        if not self._ffmpeg_path:
            self._log('[ERR]  Cannot convert the finished file — FFmpeg is '
                      'missing (Preferences → Tools).\n', 'red')
            return
        s = apply_conv_preset(self.settings, preset)
        s['conv_output_dir'] = os.path.dirname(path) or s['conv_output_dir']
        # Whatever trim the Converter panel happens to be holding belongs to
        # the file the user picked there, not to this one — inheriting it would
        # quietly truncate every automatic conversion.
        s['conv_trim_start'] = s['conv_trim_end'] = ''
        s['conv_overwrite'] = True
        conv = ConvItem(path)
        conv.retry_settings = s
        self.conv_items[conv.id] = conv
        self._add_conv_card(conv)
        self._log(f'Converting {self._trunc(os.path.basename(path), 40)} '
                  f'→ {preset}\n', 'blue')
        self._start_conversion_items([conv], s)

    def _parse_clip_range(self, s):
        return parse_clip_range(s, self._warn)

    @staticmethod
    def _int_or_none(value):
        return int_or_none(value)

    def _make_progress_hook(self, item: DownloadItem):
        """Progress hook that also implements cancellation.

        Raising DownloadCancelled from inside a hook is the only way to stop a
        running yt-dlp download; simply setting a flag (what this app did
        before) left the transfer running to completion and merely relabelled
        the card afterwards.
        """
        iid = item.id

        def hook(d):
            if item.is_cancelled:
                raise DownloadCancelled('Cancelled by user')
            info = d.get('info_dict') or {}
            # Only ever plain scalars over the queue — never the live
            # info_dict, which yt-dlp keeps mutating under the worker.
            self.msg_q.put(('prog', iid, {
                'status':           d.get('status'),
                'downloaded_bytes': d.get('downloaded_bytes'),
                'total_bytes':      d.get('total_bytes'),
                'total_bytes_estimate': d.get('total_bytes_estimate'),
                'speed':            d.get('speed'),
                'eta':              d.get('eta'),
                'title':            info.get('title'),
                'error':            d.get('error'),
                # Playlist position, so one card can narrate 27 downloads.
                'playlist_index':   info.get('playlist_index'),
                'n_entries':        info.get('n_entries') or info.get('playlist_count'),
                'playlist_title':   info.get('playlist_title'),
                # What actually got downloaded, for the finished card.
                'height':           info.get('height'),
                'fps':              info.get('fps'),
                'vcodec':           info.get('vcodec'),
                'acodec':           info.get('acodec'),
                'ext':              info.get('ext'),
                'duration':         info.get('duration'),
            }))
        return hook

    def _make_pp_hook(self, item: DownloadItem):
        iid = item.id

        def hook(d):
            if item.is_cancelled:
                raise DownloadCancelled('Cancelled by user')
            self.msg_q.put(('pp', iid, {
                'status':        d.get('status'),
                'postprocessor': d.get('postprocessor'),
            }))
        return hook

    def _download_worker(self, item: DownloadItem, opts: dict, slot: threading.Semaphore):
        acquired = False
        try:
            # Wait for a free download slot without blocking the UI. Polling
            # rather than a plain acquire() so Cancel works while queued.
            while not slot.acquire(timeout=0.25):
                if item.is_cancelled:
                    item.status = DownloadItem.CANCELLED
                    return
            acquired = True
            if item.is_cancelled:
                item.status = DownloadItem.CANCELLED
                return

            item.status = DownloadItem.DOWNLOADING
            self.msg_q.put(('update', item))

            with YoutubeDL(opts) as ydl:
                ret = ydl.download([item.url])

            if item.is_cancelled:
                item.status = DownloadItem.CANCELLED
            elif ret == 0:
                item.status   = DownloadItem.DONE
                item.progress = 1.0
                item.speed    = ''
                item.eta      = ''
                item.error    = ''   # clear warnings logged along the way
            elif item.paths:
                # Something did come down. With ignoreerrors this is the normal
                # shape of a playlist containing a private or deleted entry —
                # calling the whole thing a failure would be a lie, and would
                # hide the 26 files that are sitting on disk.
                item.status   = DownloadItem.DONE
                item.progress = 1.0
                got = len(item.paths)
                total = item.entry_count or got
                item.note = (f'{got} of {total} downloaded — '
                             f'{max(total - got, 1)} failed (see the log)')
                item.error = ''
            else:
                item.status = DownloadItem.ERROR
                if not item.error:
                    item.error = 'Download returned non-zero exit code.'
        except DownloadCancelled:
            item.status = DownloadItem.CANCELLED
            item.error  = ''
        except Exception as exc:
            if item.is_cancelled:
                item.status = DownloadItem.CANCELLED
                item.error  = ''
            else:
                item.status = DownloadItem.ERROR
                item.error  = str(exc) or item.error or 'Unknown error'
        finally:
            if acquired:
                slot.release()
            item.speed = ''
            item.eta   = ''
            # Only drop our own entry — a Retry may already have registered
            # its replacement thread under the same id.
            if self._threads.get(item.id) is threading.current_thread():
                self._threads.pop(item.id, None)
            self.msg_q.put(('update', item))
            tag = {DownloadItem.DONE: 'green',
                   DownloadItem.CANCELLED: 'yellow'}.get(item.status, 'red')
            self.msg_q.put(('log', f'[{item.status.upper()}] {item.title}\n', tag))

    def _download_slot(self) -> threading.Semaphore:
        """Semaphore limiting simultaneous downloads, rebuilt when the setting
        changes. Unbounded threads used to let 'Download All' on a large queue
        spawn one yt-dlp session per URL."""
        want = self._int_or_none(self.settings.get('max_concurrent')) or 3
        want = max(1, min(want, 16))
        if self._dl_slot is None or self._dl_slot_size != want:
            self._dl_slot = threading.Semaphore(want)
            self._dl_slot_size = want
        return self._dl_slot

    def _start_downloads(self, item_ids: list):
        s = self._collect_settings()
        slot = self._download_slot()
        started = 0
        for iid in item_ids:
            item = self.items.get(iid)
            if not item or item.status not in DownloadItem.STARTABLE:
                continue
            # Claim the item on the main thread *before* spawning. The worker
            # used to set this itself, so two quick clicks on "Download" both
            # passed the check and ran the same URL twice.
            if item.is_cancelled:
                item._cancel = threading.Event()
            item.status = DownloadItem.QUEUED
            item.error  = ''
            item.note   = ''
            # Snapshot the follow-up conversion now, so changing the sidebar
            # while a queue runs does not retroactively alter what is running.
            preset = self.post_conv_var.get()
            item.post_convert = preset if preset in CONV_PRESETS else ''
            self._update_card(item)
            try:
                opts = self._build_ydl_opts(item, s)
            except Exception as exc:
                # Without this the item sat at QUEUED forever with no worker.
                item.status = DownloadItem.ERROR
                item.error = f'Could not prepare the download: {exc}'
                self._update_card(item)
                continue
            t = threading.Thread(target=self._download_worker,
                                 args=(item, opts, slot), daemon=True)
            self._threads[iid] = t
            t.start()
            started += 1
        if started:
            self.status_var.set(f'Started {started} download(s)…')
            self._log(f'Starting {started} download(s) '
                      f'(max {self._dl_slot_size} at once).\n', 'yellow')
        else:
            # Silence here looked like a crash; say why nothing happened.
            self._log('Nothing to download — select queued items, or use '
                      '↺ Retry on finished/failed ones.\n', 'yellow')
            self.status_var.set('Nothing to start.')

    def _download_selected(self):
        ids = [iid for iid, w in self._q_widgets.items() if w['check_var'].get()]
        if not ids:
            self._log('No items selected.\n', 'yellow')
            return
        self._start_downloads(ids)

    def _download_all(self):
        self._start_downloads(list(self.items.keys()))

    # ─── Message queue ────────────────────────────────────────────────────────
    def _poll(self):
        try:
            while True:
                try:
                    msg = self.msg_q.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(msg)
                except tk.TclError:
                    return          # window is going away
                except Exception as exc:
                    # One malformed message must not kill the poll loop, or the
                    # whole UI silently stops updating.
                    self._log(f'[GUI] {type(exc).__name__}: {exc}\n', 'red')
        finally:
            if not self._closing:
                try:
                    self._check_idle()
                except Exception:
                    pass
                self._poll_after_id = self.after(80, self._poll)

    def _check_idle(self):
        """Nudge the user when the queue empties while they are elsewhere."""
        busy = (any(i.status in DownloadItem.ACTIVE for i in self.items.values())
                or any(c.status in (ConvItem.QUEUED, ConvItem.RUNNING)
                       for c in self.conv_items.values()))
        if self._was_busy and not busy and self.focus_displayof() is None:
            try:
                self.bell()
            except tk.TclError:
                pass
            if sys.platform == 'win32':
                try:
                    import ctypes
                    user32 = ctypes.windll.user32
                    user32.FlashWindow(user32.GetParent(self.winfo_id()), True)
                except Exception:
                    pass
        self._was_busy = busy

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

            # A late progress message for an item that already finished,
            # failed or was cancelled must not drag it back to life.
            if status in ('downloading', 'error') and item.status not in DownloadItem.ACTIVE:
                return

            if status == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                dl    = d.get('downloaded_bytes') or 0
                item.progress = min(dl / total, 1.0) if total else 0.0
                spd   = d.get('speed')
                eta   = d.get('eta')
                item.speed    = (format_bytes(spd) + '/s') if spd else ''
                item.eta      = self._fmt_eta(eta)
                item.size_str = (f'{format_bytes(dl)}/{format_bytes(total)}'
                                 if total else format_bytes(dl)) if dl else ''
                self._note_playlist(item, d)
                if (d.get('title') and not item.entry_count
                        and not item.title_locked):
                    item.title = d['title']
                item.status = DownloadItem.DOWNLOADING
                self._update_card(item)
                # Progress hooks fire many times a second; logging every one
                # flooded the strip and made the whole app stutter.
                last = self._last_logged_pct.get(iid, -1.0)
                pct  = item.progress * 100
                if pct - last >= 5.0 or last < 0:
                    self._last_logged_pct[iid] = pct
                    self._log(f'{self._trunc(item.title, 28)}: {pct:.1f}%'
                              f'{" @ " + item.speed if item.speed else ""}'
                              f'{" ETA " + item.eta if item.eta else ""}\n')

            elif status == 'finished':
                self._last_logged_pct.pop(iid, None)
                item.progress = 1.0
                item.speed    = ''
                item.eta      = ''
                self._note_playlist(item, d)
                # Remember what was actually fetched, so the done card can say
                # "1080p60 · avc1 / mp4a" instead of nothing at all.
                for attr in ('height', 'fps', 'vcodec', 'acodec', 'ext'):
                    if d.get(attr):
                        setattr(item, attr, d[attr])
                if d.get('duration'):
                    item.duration = d['duration']
                if d.get('total_bytes'):
                    item.filesize += d['total_bytes']
                # Fires once per stream, so a merge passes through here between
                # the video and audio downloads. Never resurrect a cancelled or
                # already-finished item.
                if item.status == DownloadItem.DOWNLOADING:
                    item.status = DownloadItem.CONVERTING
                self._update_card(item)

            elif status == 'error':
                item.status = DownloadItem.ERROR
                if not item.error:
                    item.error = str(d.get('error', 'Unknown error'))
                self._update_card(item)

        elif kind == 'pp':
            _, iid, d = msg
            item = self.items.get(iid)
            if item and d.get('status') == 'started' and item.status in (
                    DownloadItem.DOWNLOADING, DownloadItem.CONVERTING):
                item.status   = DownloadItem.CONVERTING
                item.size_str = f'Post-processing: {d.get("postprocessor", "")}'
                self._update_card(item)

        elif kind == 'file':
            # One finished file on disk (post_hooks). A playlist or a chapter
            # split reports several.
            _, iid, path = msg
            item = self.items.get(iid)
            if item and path and path not in item._chained:
                item._chained.add(path)
                self._chain_convert(item, path)

        elif kind == 'meta':
            # Title/duration fetched for a *queued* item, purely cosmetic.
            _, iid, info = msg
            item = self.items.get(iid)
            if item:
                if info.get('title') and not item.title_locked:
                    item.title = info['title']
                item.duration       = info.get('duration') or 0.0
                item.uploader       = info.get('uploader') or ''
                item.entry_count    = info.get('entry_count') or 0
                item.playlist_title = info.get('playlist_title') or ''
                self._update_card(item)

        elif kind == 'note_error':
            # Remember the message so a genuine failure can show something
            # useful, but leave the status alone — see GUILogger.error.
            _, iid, errmsg = msg
            item = self.items.get(iid)
            if item and item.status not in (DownloadItem.DONE,
                                            DownloadItem.CANCELLED):
                item.error = errmsg

        elif kind == 'status':
            self.status_var.set(msg[1])

        elif kind == 'log':
            tag = msg[2] if len(msg) > 2 else ''
            self._log(msg[1], tag)

        elif kind == 'show_info':
            self._show_info_popup(msg[1])

        elif kind == 'show_playlist':
            self._show_playlist_popup(msg[1])

        elif kind == 'conv_update':
            self._update_conv_card(msg[1])

        elif kind == 'fetch_done':
            self._fetching = False
            self._fetch_btn.configure(state='normal')

        elif kind == 'update_result':
            cb = self._pending_update_callback
            if cb:
                try:
                    cb(msg[1], msg[2])
                except Exception:
                    pass
                self._pending_update_callback = None

    _LOG_MAX_LINES = 500

    def _log(self, text: str, tag: str = ''):
        # Follow the output only if the reader is already at the bottom;
        # otherwise a busy download yanks the view away from what they scrolled
        # back to read.
        at_end = self.log_txt.yview()[1] > 0.999
        self.log_txt.configure(state='normal')
        if tag:
            self.log_txt.insert('end', text, tag)
        else:
            self.log_txt.insert('end', text)
        if at_end:
            self.log_txt.see('end')
        # Only check line count when a newline was actually inserted.
        if '\n' in text:
            lines = int(self.log_txt.index('end-1c').split('.', 1)[0])
            if lines > self._LOG_MAX_LINES:
                self.log_txt.delete('1.0', f'{lines - self._LOG_MAX_LINES}.0')
        self.log_txt.configure(state='disabled')

    # ─── Info popup ───────────────────────────────────────────────────────────
    def _show_info_popup(self, info: dict):
        popup = tk.Toplevel(self)
        popup.title('Media Info')
        popup.geometry('700x580')
        popup.configure(bg=BASE)
        popup.transient(self)
        popup.grab_set()
        popup.bind('<Escape>', lambda _e: popup.destroy())
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

        # Formats and chapters are two different questions about the same
        # video, so they get a tab each rather than one very long dialog.
        tabs = ttk.Notebook(popup)
        tabs.pack(fill='both', expand=True, padx=16, pady=(8, 0))
        fmt_page = tk.Frame(tabs, bg=BASE)
        tabs.add(fmt_page, text='  Formats  ')

        cols = ('ID', 'Ext', 'Resolution', 'FPS', 'VCodec', 'ACodec', 'Bitrate', 'Size')
        widths = (52, 52, 110, 48, 110, 110, 80, 80)

        tf = tk.Frame(fmt_page, bg=BASE)
        tf.pack(fill='both', expand=True)
        tree = ttk.Treeview(tf, columns=cols, show='headings', height=14)

        # Sort handler: numeric columns sort numerically, text columns lexically.
        # Resolution like "1920×1080" sorts by the leading int.
        size_raw: dict[str, int] = {}     # row iid -> bytes, for a numeric Size sort

        def sort_by(col, descending):
            def keyfn(s):
                if not s:
                    return (0, '')
                t = s.rstrip('k×').replace(',', '')
                t = t.split('×')[0].split('×')[0]
                try:
                    return (1, float(t))
                except ValueError:
                    return (2, s)

            def row_key(k):
                if col == 'Size':       # "1.5MiB" text sorts wrongly
                    return (1, float(size_raw[k])) if size_raw.get(k) else (0, '')
                return keyfn(tree.set(k, col))
            rows = sorted(tree.get_children(''), key=row_key, reverse=descending)
            for idx, k in enumerate(rows):
                tree.move(k, '', idx)
            tree.heading(col, command=lambda c=col: sort_by(c, not descending))

        for col, w in zip(cols, widths):
            tree.heading(col, text=col, command=lambda c=col: sort_by(c, False))
            tree.column(col, width=w, anchor='center', stretch=False)
        tree.tag_configure('video',    foreground=BLUE)
        tree.tag_configure('audio',    foreground=GREEN)
        tree.tag_configure('combined', foreground=MAUVE)

        # row iid -> ('combined' | 'video' | 'audio'), needed to build a correct
        # selector when the user picks a row.
        row_kind: dict[str, str] = {}
        for fmt in reversed(info.get('formats', [])):
            vc   = (fmt.get('vcodec') or 'none')
            ac   = (fmt.get('acodec') or 'none')
            w, h = fmt.get('width'), fmt.get('height')
            res  = f'{w}×{h}' if (w and h) else (fmt.get('format_note', '') or 'audio only')
            fps_val = fmt.get('fps') or 0
            fps  = str(int(fps_val)) if fps_val else ''
            tbr  = fmt.get('tbr')
            fs   = fmt.get('filesize') or fmt.get('filesize_approx')
            has_v = vc not in ('none', 'None', '', None)
            has_a = ac not in ('none', 'None', '', None)
            tag   = 'combined' if (has_v and has_a) else ('video' if has_v else 'audio')
            iid = tree.insert('', 'end', tags=(tag,),
                              values=(fmt.get('format_id', ''), fmt.get('ext', ''),
                                      res, fps, vc[:12], ac[:12],
                                      f'{tbr:.0f}k' if tbr else '',
                                      format_bytes(fs) if fs else ''))
            row_kind[iid] = tag
            size_raw[iid] = int(fs) if fs else 0

        tsb = ttk.Scrollbar(tf, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=tsb.set)
        tsb.pack(side='right', fill='y')
        tree.pack(side='left', fill='both', expand=True)

        tk.Label(fmt_page,
                 text='Blue = video only (audio is merged in automatically)  ·  '
                      'Green = audio only  ·  Purple = already combined',
                 bg=BASE, fg=SURF2, font=('Segoe UI', 8)).pack(anchor='w', pady=(4, 0))

        url_final = info.get('webpage_url') or info.get('url', '')
        self._build_chapter_tab(tabs, info, url_final, title, popup)

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

        def make_item(selector: str = '') -> 'DownloadItem | None':
            if not url_final:
                self._log('[ERR]  No usable URL in the fetched info.\n', 'red')
                return None
            new = DownloadItem(url_final,
                               clip_range=self._parse_clip_range(self.settings))
            new.title = title
            new.title_locked = True
            new.duration = info.get('duration') or 0.0
            new.uploader = info.get('uploader') or ''
            new.format_override = selector
            self.items[new.id] = new
            self._add_card(new)
            self._q_widgets[new.id]['title_lbl'].configure(
                text=self._trunc(title, 44))
            return new

        def selector_for(row_iid: str) -> str:
            """Turn a picked table row into a complete yt-dlp selector.

            A bare format_id was used before. For the video-only rows that make
            up most of the table on YouTube and similar sites, that downloads a
            silent video — the single most common way this dialog produced a
            'broken' file. Video-only rows now get audio merged in, with a
            fallback to the bare id if no audio stream exists.
            """
            fid = tree.set(row_iid, 'ID')
            if not fid:
                return ''
            if row_kind.get(row_iid) == 'video':
                return f'{fid}+bestaudio/{fid}'
            return fid

        def add_to_queue():
            sel = tree.selection()
            make_item(selector_for(sel[0]) if sel else '')
            popup.destroy()

        def download_selected_format():
            sel = tree.selection()
            if not sel:
                self.status_var.set('Pick a format row first.')
                return
            selector = selector_for(sel[0])
            if not selector:
                return
            item = make_item(selector)
            if item:
                self._start_downloads([item.id])
            popup.destroy()

        add_btn = tk.Button(btn_row, text='Add to Queue', command=add_to_queue,
                            bg=MAUVE, fg=CRUST, activebackground='#b89be6',
                            font=('Segoe UI', 9, 'bold'), relief='flat', bd=0,
                            padx=14, pady=6, cursor='hand2')
        add_btn.pack(side='left')
        tk.Button(btn_row, text='Download Selected Format',
                  command=download_selected_format,
                  bg=SURF0, fg=TEXT, activebackground=SURF1,
                  **_bkw).pack(side='left', padx=(6, 0))

        # Make it obvious that selecting a row changes what "Add" will queue.
        tree.bind('<<TreeviewSelect>>', lambda _e: add_btn.configure(
            text='Queue Selected Format' if tree.selection() else 'Add to Queue'))
        tree.bind('<Double-1>', lambda e: download_selected_format()
                  if tree.identify_region(e.x, e.y) == 'cell' else None)
        tk.Button(btn_row, text='Copy URL',
                  command=lambda: self._copy(url_final),
                  bg=SURF0, fg=TEXT, activebackground=SURF1,
                  **_bkw).pack(side='left', padx=(6, 0))
        tk.Button(btn_row, text='Close', command=popup.destroy,
                  bg=SURF0, fg=TEXT, activebackground=SURF1,
                  **_bkw).pack(side='left', padx=(6, 0))
        self.status_var.set(f'Info: {self._trunc(title, 50)}')

    def _build_chapter_tab(self, tabs, info: dict, url: str, title: str, popup):
        """A Chapters tab that queues chapters as individual clip downloads.

        Splitting after the fact needs the whole video first. Picking the two
        chapters you actually want and downloading only those is the thing an
        editor pulling cutaways — or a transcriber after one track of an album
        upload — is really trying to do.
        """
        chapters = [c for c in (info.get('chapters') or [])
                    if c.get('start_time') is not None]
        if not chapters or not url:
            return

        page = tk.Frame(tabs, bg=BASE)
        tabs.add(page, text=f'  Chapters ({len(chapters)})  ')

        holder = tk.Frame(page, bg=BASE)
        holder.pack(fill='both', expand=True)
        cols = ('#', 'Title', 'Start', 'End', 'Length')
        tree = ttk.Treeview(holder, columns=cols, show='headings', height=13,
                            selectmode='extended')
        for col, width, anchor in (('#', 40, 'center'), ('Title', 330, 'w'),
                                   ('Start', 90, 'center'), ('End', 90, 'center'),
                                   ('Length', 90, 'center')):
            tree.heading(col, text=col)
            tree.column(col, width=width, anchor=anchor, stretch=(col == 'Title'))

        ranges: dict = {}
        for idx, chapter in enumerate(chapters, 1):
            start = float(chapter.get('start_time') or 0)
            end   = chapter.get('end_time')
            end   = float(end) if end is not None else None
            row = tree.insert('', 'end', values=(
                idx, chapter.get('title') or f'Chapter {idx}',
                self._fmt_duration(start),
                self._fmt_duration(end) if end else 'end',
                self._fmt_duration(end - start) if end else ''))
            ranges[row] = (start, end, chapter.get('title') or f'Chapter {idx}')

        sb = ttk.Scrollbar(holder, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        tree.pack(side='left', fill='both', expand=True)

        bar = tk.Frame(page, bg=BASE)
        bar.pack(fill='x', pady=(6, 0))

        def queue_chapters():
            rows = tree.selection() or tuple(tree.get_children(''))
            added = 0
            for row in rows:
                start, end, name = ranges[row]
                item = DownloadItem(
                    url, clip_range=(start, end if end is not None else float('inf')))
                item.title = f'{title} — {name}'
                item.title_locked = True
                self.items[item.id] = item
                self._add_card(item)
                added += 1
            self._log(f'Queued {added} chapter(s) as clips.\n', 'blue')
            popup.destroy()

        tk.Button(bar, text='Queue selected chapters as clips',
                  command=queue_chapters,
                  bg=MAUVE, fg=CRUST, activebackground='#b89be6',
                  font=('Segoe UI', 9, 'bold'), relief='flat', bd=0,
                  padx=14, pady=6, cursor='hand2').pack(side='left')
        tk.Label(bar, text='Nothing selected queues them all. Each becomes its '
                           'own queue item with its own range.',
                 bg=BASE, fg=SURF2, font=('Segoe UI', 8)).pack(side='left', padx=(8, 0))

    # ─── Playlist popup ───────────────────────────────────────────────────────
    def _show_playlist_popup(self, data: dict):
        """Entry list for a playlist or channel URL.

        Fetch Info used to silently throw the playlist away and show the first
        video's formats, which told you nothing about the other 26.
        """
        entries = data.get('entries') or []
        popup = tk.Toplevel(self)
        popup.title('Playlist')
        popup.geometry('700x560')
        popup.configure(bg=BASE)
        popup.transient(self)
        popup.grab_set()
        popup.bind('<Escape>', lambda _e: popup.destroy())
        self._apply_dark_titlebar(popup)

        tk.Label(popup, text=data.get('title') or 'Playlist', bg=BASE, fg=TEXT,
                 font=('Segoe UI', 12, 'bold'), wraplength=660,
                 justify='left').pack(anchor='w', padx=16, pady=(14, 2))
        tk.Label(popup, text=f'{len(entries)} videos', bg=BASE, fg=SUBT0,
                 font=('Segoe UI', 9)).pack(anchor='w', padx=16, pady=(0, 8))

        holder = tk.Frame(popup, bg=BASE)
        holder.pack(fill='both', expand=True, padx=16)
        tree = ttk.Treeview(holder, columns=('#', 'Title', 'Duration'),
                            show='headings', height=15, selectmode='extended')
        for col, width, anchor in (('#', 44, 'center'), ('Title', 480, 'w'),
                                   ('Duration', 90, 'center')):
            tree.heading(col, text=col)
            tree.column(col, width=width, anchor=anchor, stretch=(col == 'Title'))
        urls: dict = {}
        for idx, entry in enumerate(entries, 1):
            row = tree.insert('', 'end', values=(
                idx, entry.get('title') or entry.get('url') or f'Video {idx}',
                self._fmt_duration(entry.get('duration'))))
            urls[row] = entry.get('url') or ''
        sb = ttk.Scrollbar(holder, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        tree.pack(side='left', fill='both', expand=True)

        bar = tk.Frame(popup, bg=BASE)
        bar.pack(fill='x', padx=16, pady=10)
        _bkw = {'font': ('Segoe UI', 9), 'relief': 'flat', 'bd': 0,
                'padx': 14, 'pady': 6, 'cursor': 'hand2',
                'bg': SURF0, 'fg': TEXT, 'activebackground': SURF1}

        def queue_whole():
            # One item for the whole playlist keeps %(playlist_index)s
            # templates meaningful, which is the point of the music layouts.
            self._queue_urls([data['url']])
            popup.destroy()

        def queue_separately():
            rows = tree.selection() or tuple(tree.get_children(''))
            picked = [urls[r] for r in rows if urls.get(r)]
            if not picked:
                self.status_var.set('No usable per-video URLs in this playlist.')
                return
            self._queue_urls(picked)
            popup.destroy()

        tk.Button(bar, text='Queue as one playlist item', command=queue_whole,
                  bg=MAUVE, fg=CRUST, activebackground='#b89be6',
                  font=('Segoe UI', 9, 'bold'), relief='flat', bd=0,
                  padx=14, pady=6, cursor='hand2').pack(side='left')
        tk.Button(bar, text='Queue selected separately',
                  command=queue_separately, **_bkw).pack(side='left', padx=(6, 0))
        tk.Button(bar, text='Close', command=popup.destroy,
                  **_bkw).pack(side='left', padx=(6, 0))
        self.status_var.set(f'Playlist: {len(entries)} videos')

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

        # ── PRESET ────────────────────────────────────────────────────────────
        body = self._sec(inner, 'PRESET')
        preset_row = tk.Frame(body, bg=MANTLE)
        preset_row.pack(fill='x', **P)
        tk.Label(preset_row, text='Quick Preset', bg=MANTLE, fg=SUBT0,
                 font=('Segoe UI', 9)).pack(anchor='w')
        self.conv_preset_var = tk.StringVar(value='— custom —')
        preset_cb = ttk.Combobox(preset_row, textvariable=self.conv_preset_var,
                                 values=['— custom —'] + list(CONV_PRESETS.keys()),
                                 state='readonly', font=('Segoe UI', 10))
        preset_cb.pack(fill='x', pady=(2, 0))
        preset_cb.bind('<<ComboboxSelected>>', self._on_conv_preset_change)

        # ── FORMAT ────────────────────────────────────────────────────────────
        body = self._sec(inner, 'OUTPUT FORMAT')

        type_row = tk.Frame(body, bg=MANTLE)
        type_row.pack(fill='x', **P)
        tk.Label(type_row, text='Format Type', bg=MANTLE, fg=SUBT0,
                 font=('Segoe UI', 9)).pack(anchor='w')
        self.conv_type_var = tk.StringVar(value=self.settings['conv_format_type'])
        type_cb = ttk.Combobox(type_row, textvariable=self.conv_type_var,
                               values=['Audio', 'Video'], state='readonly',
                               font=('Segoe UI', 10))
        type_cb.pack(fill='x', pady=(2, 0))
        type_cb.bind('<<ComboboxSelected>>', self._on_conv_type_change)

        fmt_row = tk.Frame(body, bg=MANTLE)
        fmt_row.pack(fill='x', **P)
        tk.Label(fmt_row, text='Output Format', bg=MANTLE, fg=SUBT0,
                 font=('Segoe UI', 9)).pack(anchor='w')
        self.conv_fmt_var = tk.StringVar(value=self.settings['conv_output_format'])
        self._conv_fmt_cb = ttk.Combobox(fmt_row, textvariable=self.conv_fmt_var,
                                         state='readonly', font=('Segoe UI', 10))
        self._conv_fmt_cb.pack(fill='x', pady=(2, 0))
        self._conv_fmt_cb.bind('<<ComboboxSelected>>', self._on_conv_format_change)

        # ── QUALITY ───────────────────────────────────────────────────────────
        body = self._sec(inner, 'QUALITY')

        self._conv_audio_q_frame = tk.Frame(body, bg=MANTLE)
        self._conv_audio_q_frame.pack(fill='x', **P)
        self.conv_audio_q_var = tk.StringVar(value=self.settings['conv_audio_quality'])
        tk.Label(self._conv_audio_q_frame, text='Audio Bitrate (kbps)',
                 bg=MANTLE, fg=SUBT0, font=('Segoe UI', 9)).pack(anchor='w')
        ttk.Combobox(self._conv_audio_q_frame, textvariable=self.conv_audio_q_var,
                     values=['best', '320', '256', '192', '128', '96', '64', '32'],
                     state='readonly', font=('Segoe UI', 10)).pack(fill='x', pady=(2, 0))

        self._conv_video_q_frame = tk.Frame(body, bg=MANTLE)
        self.conv_video_crf_var = tk.StringVar(value=self.settings['conv_video_crf'])
        tk.Label(self._conv_video_q_frame, text='Video Quality (CRF, 0=lossless → 51=worst)',
                 bg=MANTLE, fg=SUBT0, font=('Segoe UI', 9)).pack(anchor='w')
        e = self._entry(self._conv_video_q_frame, self.conv_video_crf_var)
        e.configure(width=8)
        e.pack(anchor='w', ipady=4, pady=(2, 0))

        # Intra-frame codecs use a named profile, not CRF.
        self._conv_intra_q_frame = tk.Frame(body, bg=MANTLE)
        self.conv_intra_var = tk.StringVar(value=self.settings['conv_intra_profile'])
        tk.Label(self._conv_intra_q_frame, text='Editing Profile',
                 bg=MANTLE, fg=SUBT0, font=('Segoe UI', 9)).pack(anchor='w')
        self._conv_intra_cb = ttk.Combobox(
            self._conv_intra_q_frame, textvariable=self.conv_intra_var,
            state='readonly', font=('Segoe UI', 10))
        self._conv_intra_cb.pack(fill='x', pady=(2, 0))
        tk.Label(self._conv_intra_q_frame,
                 text='Every frame is a keyframe, so scrubbing and trimming are '
                      'instant in Premiere, Resolve, Final Cut and Avid. Files '
                      'are large — that is the trade.',
                 bg=MANTLE, fg=SURF2, font=('Segoe UI', 8),
                 wraplength=320, justify='left').pack(anchor='w', pady=(2, 0))

        # ── TIMELINE / FILTERS ────────────────────────────────────────────────
        body = self._sec(inner, 'FRAME RATE, SIZE & TRIM')

        self.conv_fps_var = tk.StringVar(value=self.settings['conv_fps'])
        self._labeled_combo(body, 'Constant Frame Rate', self.conv_fps_var,
                            CONV_FPS_CHOICES)
        tk.Label(body, text='Downloads are often variable-frame-rate, which makes '
                            'audio drift out of sync on an NLE timeline. Forcing '
                            'CFR on import is the standard fix.',
                 bg=MANTLE, fg=SURF2, font=('Segoe UI', 8),
                 wraplength=320, justify='left').pack(anchor='w', padx=16)

        self.conv_scale_var = tk.StringVar(value=self.settings['conv_scale'])
        self._labeled_combo(body, 'Resize (width, keeps aspect)',
                            self.conv_scale_var, list(CONV_SCALE_CHOICES.keys()))

        self.conv_trim_start_var = tk.StringVar(value=self.settings['conv_trim_start'])
        self.conv_trim_end_var   = tk.StringVar(value=self.settings['conv_trim_end'])
        tr = tk.Frame(body, bg=MANTLE)
        tr.pack(fill='x', padx=16, pady=3)
        self._mini_entry(tr, 'Trim from', self.conv_trim_start_var, width=12)
        tk.Frame(tr, bg=MANTLE, width=12).pack(side='left')
        self._mini_entry(tr, 'Trim to',   self.conv_trim_end_var,   width=12)
        tk.Label(body, text='Blank = whole file.  Accepts 90, 1:30 or 00:01:30.',
                 bg=MANTLE, fg=SURF2, font=('Segoe UI', 8)).pack(anchor='w', padx=16)

        # ── AUDIO ─────────────────────────────────────────────────────────────
        body = self._sec(inner, 'AUDIO')
        self.conv_loud_var = tk.StringVar(value=self.settings['conv_loudness'])
        self._labeled_combo(body, 'Loudness Normalization (EBU R128)',
                            self.conv_loud_var, list(CONV_LOUDNESS.keys()))
        tk.Label(body, text='Evens out volume across a mixed library so tracks '
                            'do not jump in level between songs.',
                 bg=MANTLE, fg=SURF2, font=('Segoe UI', 8),
                 wraplength=320, justify='left').pack(anchor='w', padx=16)

        # Practice controls. Audio targets only — retiming the audio under a
        # video stream that keeps its own timing just walks the two apart —
        # so the frame is packed and unpacked by _on_conv_format_change.
        self._conv_practice_frame = tk.Frame(body, bg=MANTLE)
        self.conv_tempo_var     = tk.StringVar(value=self.settings['conv_tempo'])
        self.conv_transpose_var = tk.StringVar(value=self.settings['conv_transpose'])
        self.conv_channels_var  = tk.StringVar(value=self.settings['conv_channels'])
        tk.Label(self._conv_practice_frame, text='PRACTICE', bg=MANTLE, fg=MAUVE,
                 font=('Segoe UI', 8, 'bold')).pack(anchor='w', pady=(8, 0))
        prow = tk.Frame(self._conv_practice_frame, bg=MANTLE)
        prow.pack(fill='x', pady=(2, 0))
        self._mini_combo(prow, 'Speed %', self.conv_tempo_var, AUDIO_TEMPO_CHOICES)
        tk.Frame(prow, bg=MANTLE, width=10).pack(side='left')
        self._mini_combo(prow, 'Transpose', self.conv_transpose_var,
                         AUDIO_TRANSPOSE_CHOICES)
        tk.Frame(prow, bg=MANTLE, width=10).pack(side='left')
        self._mini_combo(prow, 'Channels', self.conv_channels_var,
                         list(AUDIO_CHANNEL_MODES.keys()))
        tk.Label(self._conv_practice_frame,
                 text='Slow a phrase down without dropping its pitch, or shift '
                      'the key. The speed and key are written into the output '
                      'filename, so the slow copy never overwrites the original.',
                 bg=MANTLE, fg=SURF2, font=('Segoe UI', 8),
                 wraplength=320, justify='left').pack(anchor='w', pady=(2, 0))
        self._conv_audio_anchor = body

        # ── OUTPUT ────────────────────────────────────────────────────────────
        body = self._sec(inner, 'OUTPUT')

        self.conv_outdir_var = tk.StringVar(value=self.settings['conv_output_dir'])
        self._browse_row(body, 'Save To', self.conv_outdir_var, self._browse_conv_dir)

        self.conv_overwrite_var = tk.BooleanVar(value=self.settings['conv_overwrite'])
        self._chk(body, 'Overwrite existing files (-y)', self.conv_overwrite_var)

        tk.Label(body, text='vorbis → .ogg · alac → .m4a · aac → .m4a',
                 bg=MANTLE, fg=OVL0, font=('Segoe UI', 8)).pack(anchor='w', padx=16, pady=(8, 0))

        tk.Frame(inner, bg=MANTLE, height=24).pack()

        # Initialize format list and quality visibility
        self._refresh_conv_fmt_list()
        self.after(100, lambda: self._bind_scroll_on(inner, canvas))

    def _on_conv_preset_change(self, _e=None):
        """Apply a preset to the panel.

        The rules live in config.apply_conv_preset so the after-download chain
        can use exactly the same ones against a throwaway dict; this method is
        only the part that pushes the result into the widgets.
        """
        name = self.conv_preset_var.get()
        if name not in CONV_PRESETS:
            return
        s = apply_conv_preset(self._settings_from_conv_panel(), name)
        self.conv_type_var.set(s['conv_format_type'])
        self._refresh_conv_fmt_list(keep_preset=True)
        self.conv_fmt_var.set(s['conv_output_format'])
        self._refresh_conv_intra_list()
        self.conv_audio_q_var.set(s['conv_audio_quality'])
        self.conv_video_crf_var.set(s['conv_video_crf'])
        if s['conv_intra_profile']:
            self.conv_intra_var.set(s['conv_intra_profile'])
        self.conv_scale_var.set(s['conv_scale'])
        self.conv_fps_var.set(s['conv_fps'])
        self.conv_loud_var.set(s['conv_loudness'])
        self.conv_tempo_var.set(s['conv_tempo'])
        self.conv_transpose_var.set(s['conv_transpose'])
        self.conv_channels_var.set(s['conv_channels'])
        self._on_conv_format_change()

    def _settings_from_conv_panel(self) -> dict:
        """The converter panel's current values as a plain dict, without
        writing them to disk the way _collect_conv_settings does."""
        return {**self.settings,
                'conv_format_type':   self.conv_type_var.get(),
                'conv_output_format': self.conv_fmt_var.get(),
                'conv_audio_quality': self.conv_audio_q_var.get(),
                'conv_video_crf':     self.conv_video_crf_var.get(),
                'conv_intra_profile': self.conv_intra_var.get()}

    def _refresh_conv_intra_list(self):
        """Populate the editing-profile list for the selected intra codec."""
        profiles = CONV_INTRA_PROFILES.get(self.conv_fmt_var.get())
        if not profiles:
            return
        labels = [p[0] for p in profiles]
        self._conv_intra_cb.configure(values=labels)
        if self.conv_intra_var.get() not in labels:
            # Default to the middle-of-the-road profile, not the extreme one.
            self.conv_intra_var.set(labels[min(2, len(labels) - 1)])

    def _on_conv_type_change(self, _e=None):
        self._refresh_conv_fmt_list()
        self.conv_preset_var.set('— custom —')

    def _refresh_conv_fmt_list(self, keep_preset=False):
        choices = (CONV_AUDIO_FORMATS if self.conv_type_var.get() == 'Audio'
                   else CONV_VIDEO_FORMATS)
        self._conv_fmt_cb.configure(values=choices)
        if self.conv_fmt_var.get() not in choices:
            self.conv_fmt_var.set(choices[0])
        self._refresh_conv_intra_list()
        self._on_conv_format_change(keep_preset=keep_preset)

    def _on_conv_format_change(self, _e=None, keep_preset=False):
        # Manual format change → no longer matches any named preset
        if _e is not None and not keep_preset:
            self.conv_preset_var.set('— custom —')
        fmt = self.conv_fmt_var.get()
        info = CONV_FORMAT_INFO.get(fmt)
        kind = info[1] if info else 'audio'
        self._refresh_conv_intra_list()

        for frame in (self._conv_audio_q_frame, self._conv_video_q_frame,
                      self._conv_intra_q_frame):
            frame.pack_forget()
        if kind == 'video':
            self._conv_video_q_frame.pack(fill='x', padx=16, pady=3)
        elif kind == 'intra':
            self._conv_intra_q_frame.pack(fill='x', padx=16, pady=3)
        elif fmt not in CONV_LOSSLESS:
            self._conv_audio_q_frame.pack(fill='x', padx=16, pady=3)

        # Practice filters are audio-only; showing them for a video target
        # would promise something build_conv_args deliberately does not do.
        practice = getattr(self, '_conv_practice_frame', None)
        if practice is not None:
            if kind == 'audio':
                practice.pack(fill='x', padx=16, pady=(0, 3))
            else:
                practice.pack_forget()

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
            self._reveal(path)
        else:
            self._log(f'Output folder does not exist: {path}\n', 'yellow')

    # ── Conv card management ──────────────────────────────────────────────────
    _CONV_CARD_BG  = SURF0
    _CONV_DONE_BG  = '#1c2f1c'
    _CONV_ERROR_BG = '#2f1c1c'

    def _add_conv_card(self, item: ConvItem):
        if self._conv_empty_lbl and self._conv_empty_lbl.winfo_manager():
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

        # Label and command are set per status by _update_conv_card.
        action_btn = tk.Button(bot, text='Remove',
                               bg=self._CONV_CARD_BG, fg=SUBT0,
                               font=('Segoe UI', 8), relief='flat', bd=0,
                               padx=6, pady=2, cursor='hand2',
                               activebackground=SURF1, activeforeground=TEXT)
        action_btn.pack(side='right')

        clip_btn = tk.Button(bot, text='✂', bg=self._CONV_CARD_BG, fg=SUBT0,
                             font=('Segoe UI', 9), relief='flat', bd=0,
                             padx=6, pady=2, cursor='hand2',
                             activebackground=SURF1, activeforeground=TEXT,
                             command=lambda iid=item.id: self._open_conv_clip_editor(iid))
        clip_btn.pack(side='right', padx=(0, 4))
        self._tooltip(clip_btn, 'Pick trim points visually (or double-click the card)')

        # Per-file trim badge; packed only when this file has its own range.
        trim_lbl = tk.Label(top, text='', bg=self._CONV_CARD_BG, fg=MAUVE,
                            font=('Segoe UI', 8, 'bold'))
        self._tooltip(trim_lbl, 'This file is trimmed to a range of its own')

        wdg = {
            'card': card, 'chk_var': chk_var, 'clip_btn': clip_btn,
            'trim_lbl': trim_lbl,
            'name_lbl': name_lbl, 'ext_badge': ext_badge,
            'status_lbl': status_lbl, 'prog': prog,
            'info_lbl': info_lbl, 'action_btn': action_btn,
            'card_bg': self._CONV_CARD_BG,
        }
        self._conv_widgets[item.id] = wdg
        self._bind_scroll_on(card, self._conv_canvas)
        for w in (card, top, bot, name_lbl, info_lbl):
            w.bind('<Double-Button-1>',
                   lambda _e, iid=item.id: self._open_conv_clip_editor(iid))
            w.bind('<Button-3>',
                   lambda e, iid=item.id: self._post_conv_menu(e, iid))
        self._update_conv_card(item)

    def _post_conv_menu(self, event, item_id: str):
        """Right-click menu for a converter card — without it a failed
        conversion was a dead end."""
        item = self.conv_items.get(item_id)
        if not item:
            return
        has_out = bool(item.status == ConvItem.DONE and item.out_path
                       and os.path.exists(item.out_path))
        idle = item.status not in (ConvItem.QUEUED, ConvItem.RUNNING)
        self._popup_menu('_conv_menu', event, [
            ('Open output', lambda: self._reveal(item.out_path), has_out),
            ('Show in folder', lambda: self._show_in_folder(item.out_path), has_out),
            None,
            ('Edit trim…', lambda: self._open_conv_clip_editor(item_id), True),
            ('Clear trim', lambda: self._set_conv_trim(item, None, None),
             bool(item.trim)),
            None,
            ('Retry', lambda: self._retry_conv(item_id),
             item.status in (ConvItem.ERROR, ConvItem.CANCELLED, ConvItem.DONE)),
            ('Remove', lambda: self._remove_conv_card(item_id), True),
        ])

    def _set_conv_trim(self, item: ConvItem, start, end):
        item.trim = None if (start is None and end is None) else (start, end)
        self._update_conv_card(item)

    def _retry_conv(self, item_id: str):
        item = self.conv_items.get(item_id)
        if not item or item.status in (ConvItem.QUEUED, ConvItem.RUNNING):
            return
        item.reset()
        self._update_conv_card(item)
        s = item.retry_settings or self._collect_conv_settings()
        self._start_conversion_items([item], s)

    def _update_conv_card(self, item: ConvItem):
        wdg = self._conv_widgets.get(item.id)
        if not wdg:
            return

        wdg['prog']['value'] = item.progress * 100

        STATUS = {
            ConvItem.PENDING:   (YELLOW, 'PENDING'),
            ConvItem.QUEUED:    (BLUE,   'QUEUED'),
            ConvItem.RUNNING:   (MAUVE,  'RUNNING'),
            ConvItem.DONE:      (GREEN,  'DONE'),
            ConvItem.ERROR:     (RED,    'ERROR'),
            ConvItem.CANCELLED: (SUBT0,  'CANCELLED'),
        }
        col, label = STATUS.get(item.status, (SUBT0, item.status.upper()))
        wdg['status_lbl'].configure(text=label, fg=col)

        if item.trim_label:
            wdg['trim_lbl'].configure(text=f'✂ {item.trim_label}  ')
            if not wdg['trim_lbl'].winfo_manager():
                wdg['trim_lbl'].pack(side='right', after=wdg['status_lbl'])
        elif wdg['trim_lbl'].winfo_manager():
            wdg['trim_lbl'].pack_forget()

        if item.error:
            wdg['info_lbl'].configure(text=item.error, fg=RED, wraplength=340)
        elif item.status == ConvItem.QUEUED:
            wdg['info_lbl'].configure(text='Waiting for a free slot…',
                                      fg=SUBT0, wraplength=0)
        else:
            pct = f'{item.progress * 100:.1f}%' if item.status == ConvItem.RUNNING else ''
            wdg['info_lbl'].configure(text=pct, fg=SUBT0, wraplength=0)

        if item.status in (ConvItem.PENDING, ConvItem.QUEUED, ConvItem.RUNNING):
            # Also the state after a Retry: back to a neutral card.
            if wdg['card_bg'] != self._CONV_CARD_BG:
                self._conv_tint(wdg, self._CONV_CARD_BG)
                wdg['prog'].configure(style='Horizontal.TProgressbar')
            if item.status == ConvItem.PENDING:
                # No worker exists yet, so there is nothing to cancel —
                # cancelling here used to poison the item for its next run.
                wdg['action_btn'].configure(
                    text='Remove', state='normal',
                    bg=self._CONV_CARD_BG, fg=SUBT0, activebackground=SURF1,
                    command=lambda iid=item.id: self._remove_conv_card(iid))
            else:
                wdg['action_btn'].configure(
                    text='Cancel', state='normal',
                    bg=self._CONV_CARD_BG, fg=SUBT0, activebackground=SURF1,
                    command=item.cancel)
        elif item.status == ConvItem.DONE:
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
        self._tint_widgets(wdg['card'], color, skip=(wdg.get('ext_badge'),))

    def _remove_conv_card(self, item_id: str):
        item = self.conv_items.get(item_id)
        if item and item.status in (ConvItem.QUEUED, ConvItem.RUNNING):
            item.cancel()           # stop the ffmpeg process before its card goes
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
        s['conv_intra_profile'] = self.conv_intra_var.get()
        s['conv_fps']           = self.conv_fps_var.get()
        s['conv_scale']         = self.conv_scale_var.get()
        s['conv_loudness']      = self.conv_loud_var.get()
        s['conv_tempo']         = self.conv_tempo_var.get()
        s['conv_transpose']     = self.conv_transpose_var.get()
        s['conv_channels']      = self.conv_channels_var.get()
        s['conv_trim_start']    = self.conv_trim_start_var.get()
        s['conv_trim_end']      = self.conv_trim_end_var.get()
        s['conv_output_dir']    = self.conv_outdir_var.get()
        s['conv_overwrite']     = self.conv_overwrite_var.get()
        self._save_settings()
        return s

    def _build_conv_args(self, s: dict):
        return build_conv_args(s, self._warn, self._log_error)

    def _parse_time(self, raw, label):
        return parse_time(raw, label, self._warn)

    def _start_conversions(self):
        """The Convert All button: everything pending, with what the panel
        currently shows."""
        if not self._ffmpeg_path:
            self._log('[ERR]  FFmpeg not found. Set its path in Preferences → Tools.\n', 'red')
            return
        s = self._collect_conv_settings()
        pending = [i for i in self.conv_items.values()
                   if i.status == ConvItem.PENDING]
        if not pending:
            self._log('No pending files to convert.\n', 'yellow')
            return
        self._start_conversion_items(pending, s)

    def _start_conversion_items(self, items: list, s: dict):
        """Convert an explicit set of items with an explicit settings dict.

        Split out from the button handler so the after-download chain can run a
        preset that has nothing to do with whatever the Converter panel is
        showing at the time.
        """
        if not self._ffmpeg_path:
            self._log('[ERR]  FFmpeg not found. Set its path in Preferences → Tools.\n', 'red')
            return
        fmt = s['conv_output_format']
        built = self._build_conv_args(s)
        if not built:
            return
        in_args, ffmpeg_args, ext = built
        suffix = conv_name_suffix(s)
        overwrite = bool(s['conv_overwrite'])
        overwrite_flag = ['-y'] if overwrite else ['-n']
        out_dir = s['conv_output_dir']
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            self._log(f'[ERR]  Cannot create output folder: {exc}\n', 'red')
            return

        slot = self._conversion_slot()
        started = skipped = 0
        used: set = set()
        for item in items:
            if item.status != ConvItem.PENDING:
                continue
            if item.is_cancelled:
                item._cancel = threading.Event()
            # A trim chosen for this file wins over the panel's trim boxes.
            item_in, item_out = in_args, ffmpeg_args
            if item.trim:
                t_start, t_end = item.trim
                trimmed = self._build_conv_args({
                    **s,
                    'conv_trim_start': '' if t_start is None else self._clock(t_start),
                    'conv_trim_end':   '' if t_end is None else self._clock(t_end)})
                if not trimmed:
                    continue
                item_in, item_out = trimmed[0], trimmed[1]
            # The practice suffix keeps a 75%-speed copy from overwriting the
            # full-speed one when both are made from the same source.
            stem = os.path.splitext(item.filename)[0] + suffix
            out_path = os.path.join(out_dir, f'{stem}.{ext}')

            # Guard against ffmpeg reading and writing the same file, which
            # truncates the source to a zero-byte file. Happens whenever the
            # output folder is the source folder and the extension matches.
            if self._same_file(out_path, item.path):
                out_path = os.path.join(out_dir, f'{stem} (converted).{ext}')
            # Two inputs with the same stem would otherwise race on one output.
            norm = os.path.normcase(os.path.abspath(out_path))
            if norm in used:
                base, dot_ext = os.path.splitext(out_path)
                n = 2
                while os.path.normcase(os.path.abspath(f'{base} ({n}){dot_ext}')) in used:
                    n += 1
                out_path = f'{base} ({n}){dot_ext}'
                norm = os.path.normcase(os.path.abspath(out_path))
            used.add(norm)

            if not overwrite and os.path.exists(out_path):
                item.status = ConvItem.ERROR
                item.error  = 'Output already exists (overwrite is off).'
                self.msg_q.put(('conv_update', item))
                skipped += 1
                continue

            item.status = ConvItem.QUEUED
            item.error  = ''
            item.out_path = out_path
            self.msg_q.put(('conv_update', item))
            t = threading.Thread(
                target=self._conv_worker,
                args=(item, self._ffmpeg_path, item_in, item_out,
                      overwrite_flag, out_path, slot),
                daemon=True)
            self._conv_threads[item.id] = t
            t.start()
            started += 1

        if started:
            self._log(f'Starting {started} conversion(s) → {fmt} '
                      f'(max {self._conv_slot_size} at once).\n', 'yellow')
        if skipped:
            self._log(f'Skipped {skipped} file(s) — output exists.\n', 'yellow')


    @staticmethod
    def _same_file(a, b):
        return same_file(a, b)

    def _conversion_slot(self) -> threading.Semaphore:
        """Limit simultaneous ffmpeg processes. Each one happily saturates every
        core, so running a whole queue at once made the machine unusable."""
        want = max(1, min((os.cpu_count() or 4) // 2, 4))
        if self._conv_slot is None or self._conv_slot_size != want:
            self._conv_slot = threading.Semaphore(want)
            self._conv_slot_size = want
        return self._conv_slot

    _TIME_RE     = re.compile(r'time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)')

    def _probe_duration(self, ffmpeg_bin, path):
        return probe_duration(ffmpeg_bin, path)

    @staticmethod
    def _effective_duration(probed, in_args, out_args):
        return effective_duration(probed, in_args, out_args)

    @staticmethod
    def _iter_ffmpeg_chunks(stream):
        return iter_ffmpeg_chunks(stream)

    def _conv_worker(self, item: ConvItem, ffmpeg_bin: str, in_args: list,
                     ffmpeg_args: list, overwrite: list, out_path: str,
                     slot: threading.Semaphore):
        acquired = False
        launched = False
        preexisting = True
        tail: list = []
        try:
            while not slot.acquire(timeout=0.25):
                if item.is_cancelled:
                    item.status = ConvItem.CANCELLED
                    return
            acquired = True
            if item.is_cancelled:
                item.status = ConvItem.CANCELLED
                return
            item.status = ConvItem.RUNNING
            self.msg_q.put(('conv_update', item))

            # Probe duration for progress tracking. With a trim applied, ffmpeg
            # reports time= relative to the trimmed output, so the progress
            # denominator has to be the trimmed length, not the file's.
            item.duration = self._probe_duration(ffmpeg_bin, item.path)
            item.duration = self._effective_duration(item.duration, in_args, ffmpeg_args)

            kw = {}
            if sys.platform == 'win32':
                kw['creationflags'] = subprocess.CREATE_NO_WINDOW

            # in_args (the trim -ss) must precede -i so ffmpeg seeks to the
            # start instead of decoding and throwing away everything before it.
            cmd = ([ffmpeg_bin, '-hide_banner', '-nostdin'] + overwrite +
                   in_args + ['-i', item.path] + ffmpeg_args + [out_path])
            preexisting = os.path.exists(out_path)
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='replace', **kw)
            item._proc = proc
            launched = True

            for piece in self._iter_ffmpeg_chunks(proc.stderr):
                if item.is_cancelled:
                    proc.terminate()
                    break
                m = self._TIME_RE.search(piece)
                if m and item.duration > 0:
                    elapsed = (int(m.group(1)) * 3600 +
                               int(m.group(2)) * 60 +
                               float(m.group(3)))
                    item.progress = min(elapsed / item.duration, 0.99)
                    self.msg_q.put(('conv_update', item))
                elif not m:
                    # Keep the last few non-progress lines; ffmpeg's actual
                    # error message lives here and used to be thrown away,
                    # leaving only a useless "exited with code 1".
                    stripped = piece.strip()
                    if stripped:
                        tail.append(stripped)
                        del tail[:-6]

            proc.wait()

            if item.is_cancelled:
                item.status = ConvItem.CANCELLED
            elif proc.returncode == 0:
                item.status   = ConvItem.DONE
                item.progress = 1.0
            else:
                item.status = ConvItem.ERROR
                reason = next((t for t in reversed(tail)
                               if not t.startswith(('frame=', 'size=', 'video:'))), '')
                item.error = (f'FFmpeg failed (code {proc.returncode}): {reason}'
                              if reason else
                              f'FFmpeg exited with code {proc.returncode}')

        except Exception as exc:
            if item.is_cancelled:
                item.status = ConvItem.CANCELLED
            else:
                item.status = ConvItem.ERROR
                item.error  = str(exc)
        finally:
            if acquired:
                slot.release()
            item._proc = None
            # A cancelled or failed run leaves a truncated output behind, which
            # with overwrite off then blocks the re-run ("Output already
            # exists"). Only if ffmpeg actually started: otherwise any file
            # there is the user's, not ours.
            if launched and not preexisting and item.status != ConvItem.DONE:
                try:
                    os.remove(out_path)
                except OSError:
                    pass
            if self._conv_threads.get(item.id) is threading.current_thread():
                self._conv_threads.pop(item.id, None)
            self.msg_q.put(('conv_update', item))
            tag = {ConvItem.DONE: 'green',
                   ConvItem.CANCELLED: 'yellow'}.get(item.status, 'red')
            self.msg_q.put(('log', f'[{item.status.upper()}] {item.filename}\n', tag))
            if item.status == ConvItem.ERROR and item.error:
                self.msg_q.put(('log', f'       {item.error}\n', 'red'))

    # ─── Tooltip ──────────────────────────────────────────────────────────────
    def _tooltip(self, widget, text):
        return tooltip(widget, text)
    # ─── Total progress ───────────────────────────────────────────────────────
    def _update_total_progress(self):
        """Show aggregate progress for active downloads in the bottom bar."""
        active_states = (DownloadItem.DOWNLOADING, DownloadItem.CONVERTING,
                         DownloadItem.FETCHING)
        active = [i for i in self.items.values() if i.status in active_states]
        if not active:
            if self._total_prog.winfo_manager():
                self._total_prog.pack_forget()
            done = sum(1 for i in self.items.values()
                       if i.status == DownloadItem.DONE)
            summary = f'{done}/{len(self.items)} done' if (done and self.items) else ''
            # Only rewrite the status line when the summary actually changes;
            # this runs on every card update and used to wipe out transient
            # messages like "Settings saved" within 80 ms.
            if summary and summary != self._last_status_summary:
                self.status_var.set(summary)
            self._last_status_summary = summary
            return
        self._last_status_summary = ''
        pct = sum(i.progress for i in active) / len(active) * 100
        self._total_prog.configure(value=pct)
        if not self._total_prog.winfo_manager():
            self._total_prog.pack(side='right', padx=(0, 10))
        self.status_var.set(f'{len(active)} active · {pct:.0f}%')

    # ─── Utilities ────────────────────────────────────────────────────────────
    @staticmethod
    def _trunc(text: str, n: int) -> str:
        return text if len(text) <= n else text[:n - 1] + '…'

    @staticmethod
    def _fmt_eta(seconds) -> str:
        """'45s' / '3m12s' / '1h04m' — plain seconds got unreadable past a
        couple of minutes."""
        try:
            secs = int(seconds)
        except (TypeError, ValueError):
            return ''
        if secs < 0:
            return ''
        if secs < 60:
            return f'{secs}s'
        mins, secs = divmod(secs, 60)
        if mins < 60:
            return f'{mins}m{secs:02d}s'
        hours, mins = divmod(mins, 60)
        return f'{hours}h{mins:02d}m'

    def _on_close(self):
        """Window-close handler: confirm if work is running, stop it cleanly.

        Daemon threads die with the process, but an ffmpeg child they started
        does not — closing mid-conversion used to orphan it.
        """
        running = ([i for i in self.items.values() if i.status in DownloadItem.ACTIVE]
                   + [c for c in self.conv_items.values()
                      if c.status in (ConvItem.QUEUED, ConvItem.RUNNING)])
        if running and not messagebox.askyesno(
                'Quit', f'{len(running)} download(s)/conversion(s) are still '
                        f'running. Cancel them and quit?\n\n'
                        f'Unfinished downloads stay in the queue for next time.',
                parent=self):
            return
        for item in running:
            item.cancel()
        deadline = time.monotonic() + 3.0
        for t in list(self._threads.values()) + list(self._conv_threads.values()):
            t.join(timeout=max(deadline - time.monotonic(), 0))
        self.destroy()

    def destroy(self):
        self._closing = True
        # Anything typed into a sidebar box but never "applied" would be lost,
        # since settings are otherwise only collected when a download starts.
        try:
            for key, var in self._setting_var_map():
                self.settings[key] = var.get()
            self._collect_conv_settings()
        except (tk.TclError, AttributeError):
            pass
        try:
            zoomed = self.state() == 'zoomed'
            self.settings['window_zoomed'] = zoomed
            geo = self.winfo_geometry()
            # Closing while minimised/withdrawn reports a degenerate size like
            # '1x1+0+0'; persisting that reopens the app as an unusable sliver.
            # A maximized window reports the full-screen size, so keep the
            # restored size from before instead.
            m = re.match(r'^(\d+)x(\d+)\+', geo)
            if (not zoomed and m and int(m.group(1)) >= 400
                    and int(m.group(2)) >= 300):
                self.settings['window_geometry'] = geo
        except Exception:
            pass
        self._snapshot_queue()
        self._save_settings()
        for attr in ('_poll_after_id', '_clip_after_id'):
            after_id = getattr(self, attr, None)
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
        for item in self.items.values():
            if item.status in DownloadItem.ACTIVE:
                item.cancel()
        for item in self.conv_items.values():
            if item.status in (ConvItem.QUEUED, ConvItem.RUNNING):
                item.cancel()
        super().destroy()
