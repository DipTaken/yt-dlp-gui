"""A LosslessCut-style clip editor for picking precise in/out points.

There is no video widget in tkinter, so the preview is built the way a scrubber
actually gets used: ffmpeg extracts a single frame at the requested timestamp
and Tk renders it. Tk 8.6 reads PNG natively, so no Pillow dependency — ffmpeg
writes PNG to a pipe and the bytes go straight into a PhotoImage.

Frame extraction lands in well under a tenth of a second for a local file, so
scrubbing, frame stepping and keyframe hopping all feel immediate. Continuous
playback is deliberately not attempted; decoding 30 fps through a pipe would
not keep up, and precise cutting is a stepping-and-scrubbing job anyway.

Audio is shown as a waveform (ffmpeg's showwavespic) behind the timeline, which
is what makes it possible to find an edit point by eye — a beat, a word
boundary, the gap before a chorus.

Keyframe positions come from a packet scan (no decoding), so "snap to keyframe"
can show you where a cut would be free rather than requiring a re-encode.
"""
import base64
import json
import os
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import ttk

from .ffmpeg_utils import no_window_kwargs
from .theme import (
    BASE, BLUE, CRUST, GREEN, MANTLE, MAUVE, OVL0, PEACH, RED, SUBT0, SURF0,
    SURF1, SURF2, TEXT, YELLOW, apply_dark_titlebar,
)

WAVE_H = 74          # waveform strip height
PREVIEW_W = 640      # frame decode width; the canvas scales to fit


def fmt_tc(seconds: float, fps: float = 0.0) -> str:
    """HH:MM:SS.mmm, with a frame number when the fps is known."""
    if seconds is None or seconds < 0:
        seconds = 0.0
    h, rem = divmod(float(seconds), 3600)
    m, s = divmod(rem, 60)
    base = f'{int(h):02d}:{int(m):02d}:{s:06.3f}'
    if fps:
        return f'{base}  (f{int(round(seconds * fps))})'
    return base


class ClipEditor(tk.Toplevel):
    """Pick in/out points against a real preview.

    `source` is a local path or a direct media URL. `resolver`, when given, is
    called on a worker thread to turn a page URL into a playable stream URL.
    `on_apply(start, end)` receives the chosen range in seconds.
    """

    def __init__(self, master, *, source, title, ffmpeg, on_apply,
                 initial_in=0.0, initial_out=None, resolver=None):
        super().__init__(master)
        self.title(f'Clip — {title}')
        self.geometry('900x740')
        self.minsize(760, 620)
        self.configure(bg=BASE)
        self.transient(master)
        apply_dark_titlebar(self)

        self._ffmpeg = ffmpeg
        self._source = source
        self._resolver = resolver
        self._on_apply = on_apply
        self._title = title

        self.duration = 0.0
        self.fps = 0.0
        self.pos = float(initial_in or 0.0)
        self.mark_in = float(initial_in or 0.0)
        self.mark_out = initial_out
        self.keyframes: list = []

        self._msg_q: queue.Queue = queue.Queue()
        self._render_req: queue.Queue = queue.Queue()
        self._frame_img = None      # keep a reference or Tk drops the image
        self._wave_img = None
        self._closing = False
        self._pending_pos = None
        # Cached on the main thread so the waveform worker never has to ask a
        # widget for its size.
        self._timeline_w = 800

        self._build_ui()
        self._poll()
        threading.Thread(target=self._prepare, daemon=True).start()

    # ── layout ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        self._status = tk.Label(self, text='Loading…', bg=BASE, fg=SUBT0,
                                font=('Segoe UI', 9), anchor='w')
        self._status.pack(fill='x', padx=14, pady=(10, 4))

        self._preview = tk.Canvas(self, bg=CRUST, highlightthickness=0, bd=0,
                                  height=360)
        self._preview.pack(fill='both', expand=True, padx=14)
        self._preview.bind('<Configure>', lambda _e: self._redraw_frame())

        # Timecode row
        tc = tk.Frame(self, bg=BASE)
        tc.pack(fill='x', padx=14, pady=(6, 0))
        self._tc_lbl = tk.Label(tc, text='00:00:00.000', bg=BASE, fg=TEXT,
                                font=('Consolas', 12, 'bold'))
        self._tc_lbl.pack(side='left')
        self._dur_lbl = tk.Label(tc, text='', bg=BASE, fg=OVL0,
                                 font=('Consolas', 10))
        self._dur_lbl.pack(side='left', padx=(8, 0))
        self._range_lbl = tk.Label(tc, text='', bg=BASE, fg=MAUVE,
                                   font=('Consolas', 10))
        self._range_lbl.pack(side='right')

        # Timeline: waveform + in/out shading + playhead
        self._timeline = tk.Canvas(self, bg=MANTLE, highlightthickness=0, bd=0,
                                   height=WAVE_H, cursor='sb_h_double_arrow')
        self._timeline.pack(fill='x', padx=14, pady=(4, 0))
        self._timeline.bind('<Configure>', self._on_timeline_resize)
        for ev in ('<Button-1>', '<B1-Motion>'):
            self._timeline.bind(ev, self._on_timeline_click)

        self._build_controls()
        self._build_footer()
        self._bind_keys()

    def _build_controls(self):
        bar = tk.Frame(self, bg=BASE)
        bar.pack(fill='x', padx=14, pady=(8, 0))
        b = {'bg': SURF0, 'fg': TEXT, 'font': ('Segoe UI', 9), 'relief': 'flat',
             'bd': 0, 'padx': 9, 'pady': 5, 'cursor': 'hand2',
             'activebackground': SURF1, 'activeforeground': TEXT}

        def mk(parent, text, cmd, tip, **over):
            btn = tk.Button(parent, text=text, command=cmd, **{**b, **over})
            btn.pack(side='left', padx=2)
            _tip(btn, tip)
            return btn

        mk(bar, '⏮', lambda: self.seek_to(0), 'Start  (Home)')
        mk(bar, '◀◀ key', self.prev_keyframe, 'Previous keyframe  (Shift+←)')
        mk(bar, '◀ frame', lambda: self.step(-1), 'Previous frame  (←)')
        mk(bar, '−1s', lambda: self.nudge(-1), 'Back 1 second')
        mk(bar, '+1s', lambda: self.nudge(1), 'Forward 1 second')
        mk(bar, 'frame ▶', lambda: self.step(1), 'Next frame  (→)')
        mk(bar, 'key ▶▶', self.next_keyframe, 'Next keyframe  (Shift+→)')
        mk(bar, '⏭', lambda: self.seek_to(self.duration), 'End  (End)')

        marks = tk.Frame(self, bg=BASE)
        marks.pack(fill='x', padx=14, pady=(6, 0))
        mk(marks, '[  Set In', self.set_in, 'Mark in point  (I)',
           bg=SURF1, activebackground=SURF2)
        mk(marks, 'Set Out  ]', self.set_out, 'Mark out point  (O)',
           bg=SURF1, activebackground=SURF2)
        mk(marks, '⟲ In', lambda: self.seek_to(self.mark_in), 'Jump to in point')
        mk(marks, '⟲ Out', lambda: self.seek_to(self.mark_out if self.mark_out
                                                else self.duration),
           'Jump to out point')
        mk(marks, '↺ Reset', self.reset_marks, 'Clear both marks')

        self._snap_var = tk.BooleanVar(value=False)
        chk = ttk.Checkbutton(marks, text='Snap marks to keyframes',
                              variable=self._snap_var)
        chk.pack(side='left', padx=(14, 0))
        _tip(chk, 'Cuts on a keyframe need no re-encode, so they are exact and '
                  'instant — but they may sit up to a second from where you '
                  'clicked.')

    def _build_footer(self):
        foot = tk.Frame(self, bg=BASE)
        foot.pack(fill='x', padx=14, pady=12)
        tk.Button(foot, text='Use This Range', command=self._apply,
                  bg=MAUVE, fg=CRUST, activebackground='#b89be6',
                  font=('Segoe UI', 9, 'bold'), relief='flat', bd=0,
                  padx=18, pady=7, cursor='hand2').pack(side='left')
        tk.Button(foot, text='Cancel', command=self.destroy,
                  bg=SURF0, fg=TEXT, activebackground=SURF1,
                  font=('Segoe UI', 9), relief='flat', bd=0,
                  padx=14, pady=7, cursor='hand2').pack(side='left', padx=(8, 0))
        self._hint = tk.Label(
            foot,
            text='←/→ frame · Shift+←/→ keyframe · I/O set marks · Home/End',
            bg=BASE, fg=OVL0, font=('Segoe UI', 8))
        self._hint.pack(side='right')

    def _bind_keys(self):
        self.bind('<Left>',        lambda _e: self.step(-1))
        self.bind('<Right>',       lambda _e: self.step(1))
        self.bind('<Shift-Left>',  lambda _e: self.prev_keyframe())
        self.bind('<Shift-Right>', lambda _e: self.next_keyframe())
        self.bind('<Home>',        lambda _e: self.seek_to(0))
        self.bind('<End>',         lambda _e: self.seek_to(self.duration))
        self.bind('<i>',           lambda _e: self.set_in())
        self.bind('<o>',           lambda _e: self.set_out())
        self.bind('<Escape>',      lambda _e: self.destroy())
        self.focus_set()

    # ── background preparation ───────────────────────────────────────────────
    def _prepare(self):
        """Resolve the stream, probe it, then kick off waveform + keyframes."""
        try:
            if self._resolver:
                self._post('status', 'Resolving stream…')
                resolved = self._resolver()
                if not resolved:
                    self._post('status', 'Could not resolve a preview stream.', RED)
                    return
                self._source = resolved

            self._post('status', 'Reading media…')
            info = self._probe()
            if not info:
                self._post('status', 'Could not read this media.', RED)
                return
            self._post('probed', info)

            # Start the render worker only once we know the duration.
            threading.Thread(target=self._render_worker, daemon=True).start()
            self._request_frame(self.pos)

            self._post('status', 'Building waveform…')
            self._post('wave', self._waveform_png())
            self._post('status', 'Scanning keyframes…')
            self._post('keys', self._keyframe_times())
            self._post('status', '')
        except Exception as exc:
            self._post('status', f'{type(exc).__name__}: {exc}', RED)

    def _run(self, cmd, **kw):
        return subprocess.run(cmd, capture_output=True, **no_window_kwargs(), **kw)

    def _probe(self) -> dict:
        r = self._run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                       '-show_entries',
                       'stream=r_frame_rate,avg_frame_rate,width,height:'
                       'format=duration',
                       '-of', 'json', self._source], text=True)
        if r.returncode:
            return {}
        data = json.loads(r.stdout or '{}')
        st = (data.get('streams') or [{}])[0]
        dur = float((data.get('format') or {}).get('duration') or 0)

        def rate(v):
            try:
                n, d = v.split('/')
                return float(n) / float(d) if float(d) else 0.0
            except Exception:
                return 0.0
        fps = rate(st.get('avg_frame_rate') or '') or rate(st.get('r_frame_rate') or '')
        return {'duration': dur, 'fps': fps,
                'w': st.get('width') or 0, 'h': st.get('height') or 0}

    def _waveform_png(self) -> bytes:
        # Uses the cached width, never winfo_width(): this runs on a worker
        # thread, and touching a widget from off the main thread is exactly
        # what breaks (silently, via the caller's except) in tkinter.
        width = max(self._timeline_w, 600)
        r = self._run([
            self._ffmpeg, '-hide_banner', '-nostdin', '-i', self._source,
            '-filter_complex',
            f'aformat=channel_layouts=mono,'
            f'showwavespic=s={width}x{WAVE_H}:colors=0x7aa2f7',
            '-frames:v', '1', '-f', 'image2pipe', '-c:v', 'png', '-'])
        return r.stdout if r.returncode == 0 else b''

    def _keyframe_times(self) -> list:
        """Keyframe timestamps via a packet scan — no decoding, so it's quick."""
        r = self._run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                       '-show_packets', '-show_entries', 'packet=pts_time,flags',
                       '-of', 'csv=p=0', self._source], text=True)
        if r.returncode:
            return []
        out = []
        for line in (r.stdout or '').splitlines():
            parts = line.split(',')
            if len(parts) >= 2 and 'K' in parts[1]:
                try:
                    out.append(float(parts[0]))
                except ValueError:
                    pass
        return sorted(out)

    # ── frame rendering ──────────────────────────────────────────────────────
    def _render_worker(self):
        """Serialise frame extraction, always rendering the newest request.

        Scrubbing generates far more requests than ffmpeg can service; draining
        to the latest keeps the preview tracking the cursor instead of playing
        catch-up through a backlog.
        """
        while not self._closing:
            t = self._render_req.get()
            if t is None:
                return
            while not self._render_req.empty():       # drop stale requests
                t = self._render_req.get_nowait()
                if t is None:
                    return
            try:
                r = self._run([
                    self._ffmpeg, '-hide_banner', '-nostdin', '-ss', f'{t:.3f}',
                    '-i', self._source, '-frames:v', '1',
                    '-vf', f'scale={PREVIEW_W}:-2', '-f', 'image2pipe',
                    '-c:v', 'png', '-'])
                if r.returncode == 0 and r.stdout:
                    self._post('frame', r.stdout)
            except Exception:
                pass

    def _request_frame(self, t: float):
        self._render_req.put(max(0.0, min(t, max(self.duration - 0.05, 0))))

    # ── thread -> UI ─────────────────────────────────────────────────────────
    def _post(self, kind, payload=None, colour=None):
        self._msg_q.put((kind, payload, colour))

    def _poll(self):
        if self._closing:
            return
        try:
            while True:
                kind, payload, colour = self._msg_q.get_nowait()
                if kind == 'status':
                    self._status.configure(text=payload or '',
                                           fg=colour or SUBT0)
                elif kind == 'probed':
                    self.duration = payload['duration']
                    self.fps = payload['fps']
                    if self.mark_out is None:
                        self.mark_out = self.duration
                    self._dur_lbl.configure(
                        text=f'/ {fmt_tc(self.duration)}   '
                             f'{payload["w"]}×{payload["h"]} @ {self.fps:.3f} fps')
                    self._refresh_labels()
                    self._redraw_timeline()
                elif kind == 'frame':
                    self._frame_img = tk.PhotoImage(
                        data=base64.b64encode(payload), master=self)
                    self._redraw_frame()
                elif kind == 'wave':
                    if payload:
                        self._wave_img = tk.PhotoImage(
                            data=base64.b64encode(payload), master=self)
                    self._redraw_timeline()
                elif kind == 'keys':
                    self.keyframes = payload or []
                    self._redraw_timeline()
        except queue.Empty:
            pass
        except tk.TclError:
            return
        self.after(60, self._poll)

    # ── drawing ──────────────────────────────────────────────────────────────
    def _redraw_frame(self):
        c = self._preview
        c.delete('all')
        w, h = c.winfo_width(), c.winfo_height()
        if not self._frame_img or w < 2:
            c.create_text(w // 2, h // 2, text='no preview',
                          fill=SURF2, font=('Segoe UI', 11))
            return
        # Fit by integer subsample; PhotoImage has no smooth scaling, and
        # integer factors at least keep it sharp.
        iw, ih = self._frame_img.width(), self._frame_img.height()
        img = self._frame_img
        if iw > w or ih > h:
            factor = max((iw + w - 1) // max(w, 1), (ih + h - 1) // max(h, 1), 1)
            img = self._frame_img.subsample(factor, factor)
        c.create_image(w // 2, h // 2, image=img, anchor='center')
        c.image = img          # keep the scaled copy alive too

    def _x_for(self, t: float, w: int) -> float:
        return (t / self.duration) * w if self.duration else 0.0

    def _redraw_timeline(self):
        c = self._timeline
        c.delete('all')
        w = c.winfo_width()
        if w < 2:
            return
        if self._wave_img:
            c.create_image(0, 0, image=self._wave_img, anchor='nw')
        else:
            c.create_rectangle(0, 0, w, WAVE_H, fill=MANTLE, outline='')
            c.create_text(w // 2, WAVE_H // 2, text='waveform loading…',
                          fill=SURF2, font=('Segoe UI', 8))

        if self.duration:
            # keyframe ticks along the bottom
            for kt in self.keyframes:
                x = self._x_for(kt, w)
                c.create_line(x, WAVE_H - 6, x, WAVE_H, fill=SURF2)

            a = self._x_for(self.mark_in, w)
            b = self._x_for(self.mark_out if self.mark_out is not None
                            else self.duration, w)
            # dim everything outside the selection
            c.create_rectangle(0, 0, a, WAVE_H, fill=CRUST, outline='', stipple='gray50')
            c.create_rectangle(b, 0, w, WAVE_H, fill=CRUST, outline='', stipple='gray50')
            c.create_line(a, 0, a, WAVE_H, fill=GREEN, width=2)
            c.create_line(b, 0, b, WAVE_H, fill=PEACH, width=2)

            x = self._x_for(self.pos, w)
            c.create_line(x, 0, x, WAVE_H, fill=MAUVE, width=2)

    def _refresh_labels(self):
        self._tc_lbl.configure(text=fmt_tc(self.pos, self.fps))
        out = self.mark_out if self.mark_out is not None else self.duration
        length = max(out - self.mark_in, 0)
        self._range_lbl.configure(
            text=f'in {fmt_tc(self.mark_in)}   out {fmt_tc(out)}   '
                 f'length {fmt_tc(length)}')

    # ── interaction ──────────────────────────────────────────────────────────
    def _on_timeline_resize(self, event):
        self._timeline_w = max(int(event.width), 1)
        self._redraw_timeline()

    def _on_timeline_click(self, event):
        w = self._timeline.winfo_width()
        if w > 1 and self.duration:
            self.seek_to(event.x / w * self.duration)

    def seek_to(self, t: float):
        if not self.duration:
            return
        self.pos = max(0.0, min(float(t), self.duration))
        self._refresh_labels()
        self._redraw_timeline()
        self._request_frame(self.pos)

    def step(self, frames: int):
        self.seek_to(self.pos + frames / (self.fps or 25.0))

    def nudge(self, seconds: float):
        self.seek_to(self.pos + seconds)

    def prev_keyframe(self):
        earlier = [k for k in self.keyframes if k < self.pos - 0.001]
        self.seek_to(earlier[-1] if earlier else 0.0)

    def next_keyframe(self):
        later = [k for k in self.keyframes if k > self.pos + 0.001]
        self.seek_to(later[0] if later else self.duration)

    def _snapped(self, t: float) -> float:
        if not self._snap_var.get() or not self.keyframes:
            return t
        return min(self.keyframes, key=lambda k: abs(k - t))

    def set_in(self):
        t = self._snapped(self.pos)
        out = self.mark_out if self.mark_out is not None else self.duration
        if t >= out:
            self._status.configure(text='In point must come before the out point.',
                                   fg=YELLOW)
            return
        self.mark_in = t
        self._status.configure(text='', fg=SUBT0)
        self._refresh_labels()
        self._redraw_timeline()

    def set_out(self):
        t = self._snapped(self.pos)
        if t <= self.mark_in:
            self._status.configure(text='Out point must come after the in point.',
                                   fg=YELLOW)
            return
        self.mark_out = t
        self._status.configure(text='', fg=SUBT0)
        self._refresh_labels()
        self._redraw_timeline()

    def reset_marks(self):
        self.mark_in, self.mark_out = 0.0, self.duration
        self._refresh_labels()
        self._redraw_timeline()

    def _apply(self):
        out = self.mark_out if self.mark_out is not None else self.duration
        # A full-length selection means "no clip" rather than an explicit range.
        if self.mark_in <= 0.001 and out >= self.duration - 0.001:
            self._on_apply(None, None)
        else:
            self._on_apply(self.mark_in, out)
        self.destroy()

    def destroy(self):
        self._closing = True
        self._render_req.put(None)
        super().destroy()


def _tip(widget, text):
    """Local tooltip — widgets here are not the settings-panel kind."""
    from .widgets import tooltip
    return tooltip(widget, text)
