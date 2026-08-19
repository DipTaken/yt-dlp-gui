"""The Preferences window.

Holds everything that is configured once and then left alone — network limits,
cookies, filters, the raw filename template, the download archive, and the
external tool paths. Keeping these out of the sidebar leaves that panel showing
only what actually changes between downloads.

Every control here binds to a Var owned by App (see App._SETTING_VARS), so
values survive this window being closed and reopened, and the normal
save-on-collect path picks them up with no special casing.
"""
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from .config import SETTINGS_FILE
from .ffmpeg_utils import find_ffmpeg
from .theme import (
    BASE, GREEN, MANTLE, MAUVE, OVL0, RED, SUBT0, SURF0, SURF1, SURF2, TEXT,
    YELLOW, apply_dark_titlebar,
)
from .widgets import WheelRouter, browse_row, check, entry, labeled_combo, labeled_entry, mini_entry


class PreferencesWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title('Preferences')
        self.geometry('620x560')
        self.minsize(560, 460)
        self.configure(bg=BASE)
        self.transient(app)
        apply_dark_titlebar(self)

        self._wheel = WheelRouter(self)

        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=10, pady=(10, 0))
        for label, builder in (
            ('  Files  ',    self._build_files),
            ('  Network  ',  self._build_network),
            ('  Cookies  ',  self._build_cookies),
            ('  Filters  ',  self._build_filters),
            ('  Tools  ',    self._build_tools),
        ):
            nb.add(self._scrollable(nb, builder), text=label)

        bar = tk.Frame(self, bg=BASE)
        bar.pack(fill='x', padx=16, pady=12)
        tk.Label(bar, text=f'Stored in {SETTINGS_FILE}', bg=BASE, fg=OVL0,
                 font=('Segoe UI', 8)).pack(side='left')
        tk.Button(bar, text='Done', command=self._close,
                  bg=MAUVE, fg='#11111b', activebackground='#b89be6',
                  font=('Segoe UI', 9, 'bold'), relief='flat', bd=0,
                  padx=18, pady=6, cursor='hand2').pack(side='right')

        self.protocol('WM_DELETE_WINDOW', self._close)
        self.bind('<Escape>', lambda _e: self._close())

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _scrollable(self, parent, builder):
        """A tab page that scrolls, since some of these lists are long."""
        outer = tk.Frame(parent, bg=MANTLE)
        vsb = ttk.Scrollbar(outer, orient='vertical')
        vsb.pack(side='right', fill='y')
        canvas = tk.Canvas(outer, bg=MANTLE, highlightthickness=0, bd=0,
                           yscrollcommand=vsb.set)
        canvas.pack(side='left', fill='both', expand=True)
        vsb.configure(command=canvas.yview)

        inner = tk.Frame(canvas, bg=MANTLE)
        win = canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>',
                   lambda _e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(win, width=e.width))
        canvas.bind('<Enter>', lambda _e: self._wheel.set_target(canvas))
        canvas.bind('<Leave>', lambda _e: self._wheel.set_target(None))

        builder(inner)
        self.after(80, lambda: self._wheel.bind_tree(inner, canvas))
        return outer

    def _close(self):
        # Collecting writes every Var back into settings and saves to disk, so
        # closing the window is what commits the changes.
        self.app._collect_settings()
        self.app._refresh_ffmpeg_badge()
        self.destroy()

    @staticmethod
    def _head(parent, text, blurb=''):
        tk.Label(parent, text=text, bg=MANTLE, fg=MAUVE,
                 font=('Segoe UI', 9, 'bold')).pack(anchor='w', padx=16, pady=(16, 0))
        if blurb:
            tk.Label(parent, text=blurb, bg=MANTLE, fg=SURF2,
                     font=('Segoe UI', 8), wraplength=520,
                     justify='left').pack(anchor='w', padx=16, pady=(1, 4))
        tk.Frame(parent, bg=SURF1, height=1).pack(fill='x', padx=16, pady=(2, 6))

    # ── tabs ─────────────────────────────────────────────────────────────────
    def _build_files(self, p):
        a = self.app
        self._head(p, 'FILENAME TEMPLATE',
                   'The layout dropdown in the sidebar fills this in. Edit it here '
                   'for anything the presets do not cover.')
        f = tk.Frame(p, bg=MANTLE)
        f.pack(fill='x', padx=16)
        entry(f, a.tmpl_var).pack(fill='x', ipady=4)
        a._tmpl_hint = tk.Label(p, text='', bg=MANTLE, fg=SURF2,
                                font=('Segoe UI', 8), wraplength=520,
                                justify='left', anchor='w')
        a._tmpl_hint.pack(anchor='w', fill='x', padx=16, pady=(3, 0))
        a._refresh_tmpl_hint()

        tk.Label(p, text='%(title)s  %(id)s  %(uploader)s  %(artist)s  %(album)s  '
                         '%(track_number)02d  %(height)sp  %(fps)s  %(ext)s',
                 bg=MANTLE, fg=OVL0, font=('Consolas', 8),
                 wraplength=520, justify='left').pack(anchor='w', padx=16, pady=(4, 0))

        self._head(p, 'DOWNLOAD ARCHIVE',
                   'Records every completed video id. Re-running the same playlist '
                   'then fetches only what is new — the usual way to keep a music '
                   'library in sync.')
        check(p, 'Skip anything already downloaded', a.archive_var)
        browse_row(p, 'Archive file (blank = archive.txt in the output folder)',
                   a.archive_file_var, self._browse_archive)

    def _build_network(self, p):
        a = self.app
        self._head(p, 'BANDWIDTH')
        labeled_entry(p, 'Rate Limit (e.g. 2M, 500K — blank = unlimited)',
                      a.rate_limit_var, width=16)

        self._head(p, 'CONNECTION')
        labeled_entry(p, 'Proxy (http://… or socks5://…)', a.proxy_var)
        row = tk.Frame(p, bg=MANTLE)
        row.pack(fill='x', padx=16, pady=3)
        mini_entry(row, 'Retries', a.retries_var, width=7)
        tk.Frame(row, bg=MANTLE, width=14).pack(side='left')
        mini_entry(row, 'Concurrent Fragments', a.concurrent_var, width=7)
        tk.Frame(row, bg=MANTLE, width=14).pack(side='left')
        mini_entry(row, 'Parallel Downloads', a.max_conc_var, width=7)
        tk.Label(p, text='Concurrent fragments splits one video across several '
                         'connections. Parallel downloads is how many queue items '
                         'run at once — raising both multiplies the load on the site.',
                 bg=MANTLE, fg=SURF2, font=('Segoe UI', 8),
                 wraplength=520, justify='left').pack(anchor='w', padx=16, pady=(4, 0))

    def _build_cookies(self, p):
        a = self.app
        self._head(p, 'COOKIES',
                   'Needed for age-restricted, members-only and private videos. '
                   'Importing from a browser requires that browser to be closed.')
        labeled_combo(p, 'Import from Browser', a.cookie_browser_var,
                      ['', 'chrome', 'firefox', 'edge', 'brave', 'safari',
                       'chromium', 'opera', 'vivaldi'])
        browse_row(p, 'Cookie File (Netscape format)', a.cookie_file_var,
                   self._browse_cookies)

    def _build_filters(self, p):
        a = self.app
        self._head(p, 'WHAT TO SKIP',
                   'Applied to every download. Left at their defaults, nothing is '
                   'filtered out.')
        check(p, 'Single Video (ignore playlist)', a.no_playlist_var)
        labeled_entry(p, 'Max Filesize (e.g. 500M, 2G)', a.maxfs_var, width=16)
        row = tk.Frame(p, bg=MANTLE)
        row.pack(fill='x', padx=16, pady=3)
        mini_entry(row, 'Uploaded After  (YYYYMMDD)', a.date_after_var, width=13)
        tk.Frame(row, bg=MANTLE, width=14).pack(side='left')
        mini_entry(row, 'Uploaded Before (YYYYMMDD)', a.date_before_var, width=13)

    def _build_tools(self, p):
        a = self.app
        self._head(p, 'FFMPEG',
                   'Required for merging video with audio, extracting audio, '
                   'embedding thumbnails and subtitles, and everything in the '
                   'Converter tab.')
        f = tk.Frame(p, bg=MANTLE)
        f.pack(fill='x', padx=16)
        tk.Label(f, text='Path to ffmpeg (blank = auto-detect)', bg=MANTLE,
                 fg=SUBT0, font=('Segoe UI', 9)).pack(anchor='w')
        row = tk.Frame(f, bg=MANTLE)
        row.pack(fill='x', pady=(2, 0))
        self._ffmpeg_var = tk.StringVar(value=a.settings.get('ffmpeg_path', ''))
        entry(row, self._ffmpeg_var).pack(side='left', fill='x', expand=True, ipady=4)
        tk.Button(row, text='Browse', command=self._browse_ffmpeg,
                  bg=SURF1, fg=TEXT, font=('Segoe UI', 9), relief='flat', bd=0,
                  padx=10, pady=4, cursor='hand2',
                  activebackground=SURF2).pack(side='left', padx=(6, 0))
        tk.Button(row, text='Apply', command=self._apply_ffmpeg,
                  bg=SURF0, fg=TEXT, font=('Segoe UI', 9), relief='flat', bd=0,
                  padx=10, pady=4, cursor='hand2',
                  activebackground=SURF1).pack(side='left', padx=(4, 0))

        detected = find_ffmpeg()
        tk.Label(p, text=(f'Auto-detected: {detected}' if detected
                          else 'Not found on PATH or in the usual install folders'),
                 bg=MANTLE, fg=GREEN if detected else YELLOW, font=('Segoe UI', 8),
                 wraplength=520, justify='left').pack(anchor='w', padx=16, pady=(4, 0))
        self._ffmpeg_status = tk.Label(p, text='', bg=MANTLE, fg=SUBT0,
                                       font=('Segoe UI', 8), wraplength=520,
                                       justify='left')
        self._ffmpeg_status.pack(anchor='w', padx=16)

        self._head(p, 'YT-DLP')
        try:
            from yt_dlp.version import __version__ as ver
        except Exception:
            ver = 'unknown'
        vrow = tk.Frame(p, bg=MANTLE)
        vrow.pack(fill='x', padx=16)
        tk.Label(vrow, text=f'Bundled version: {ver}', bg=MANTLE, fg=SUBT0,
                 font=('Segoe UI', 9)).pack(side='left')
        self._update_btn = tk.Button(vrow, text='Update yt-dlp',
                                     command=self._run_update,
                                     bg=SURF0, fg=TEXT, font=('Segoe UI', 9),
                                     relief='flat', bd=0, padx=10, pady=4,
                                     cursor='hand2', activebackground=SURF1,
                                     activeforeground=TEXT)
        self._update_btn.pack(side='right')
        self._update_status = tk.Label(p, text='', bg=MANTLE, fg=SUBT0,
                                       font=('Segoe UI', 8), wraplength=520,
                                       justify='left')
        self._update_status.pack(anchor='w', padx=16, pady=(4, 0))

    # ── actions ──────────────────────────────────────────────────────────────
    def _browse_archive(self):
        path = filedialog.asksaveasfilename(
            parent=self, title='Download archive file', defaultextension='.txt',
            initialfile='archive.txt',
            filetypes=[('Text files', '*.txt'), ('All files', '*.*')])
        if path:
            self.app.archive_file_var.set(path)

    def _browse_cookies(self):
        path = filedialog.askopenfilename(
            parent=self, title='Select Cookie File',
            filetypes=[('Cookie files', '*.txt'), ('All files', '*.*')])
        if path:
            self.app.cookie_file_var.set(path)

    def _browse_ffmpeg(self):
        path = filedialog.askopenfilename(
            parent=self, title='Locate ffmpeg',
            filetypes=[('FFmpeg executable', 'ffmpeg ffmpeg.exe'), ('All', '*.*')])
        if path:
            self._ffmpeg_var.set(path)
            self._apply_ffmpeg()

    def _apply_ffmpeg(self):
        self.app.settings['ffmpeg_path'] = self._ffmpeg_var.get().strip()
        self.app._ffmpeg_path = self.app._resolve_ffmpeg()
        self.app._refresh_ffmpeg_badge()
        resolved = self.app._ffmpeg_path
        self._ffmpeg_status.configure(
            text=f'Using: {resolved}' if resolved else 'No usable ffmpeg found.',
            fg=GREEN if resolved else RED)

    def _run_update(self):
        self._update_btn.configure(state='disabled', text='Updating…')
        self._update_status.configure(text='Running: pip install -U yt-dlp',
                                      fg=YELLOW)

        def worker():
            try:
                kw = {}
                if sys.platform == 'win32':
                    kw['creationflags'] = subprocess.CREATE_NO_WINDOW
                proc = subprocess.run(
                    [sys.executable, '-m', 'pip', 'install', '-U', 'yt-dlp'],
                    capture_output=True, text=True, timeout=180, **kw)
                ok = proc.returncode == 0
                tail = (proc.stdout or proc.stderr or '').strip().splitlines()[-1:]
                msg = tail[0] if tail else ('Updated.' if ok else 'Failed.')
            except Exception as exc:
                ok, msg = False, str(exc)
            self.app.msg_q.put(('update_result', ok, msg))

        def on_done(ok, msg):
            # The window may already be gone by the time pip finishes.
            try:
                self._update_btn.configure(state='normal', text='Update yt-dlp')
                self._update_status.configure(text=msg, fg=GREEN if ok else RED)
            except tk.TclError:
                pass
            self.app._log(f'[{"OK" if ok else "FAIL"}] yt-dlp update: {msg}\n',
                          'green' if ok else 'red')

        self.app._pending_update_callback = on_done
        threading.Thread(target=worker, daemon=True).start()
