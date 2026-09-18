"""Per-row state objects, error translation and the yt-dlp -> GUI log adapter."""
import os
import queue
import re
import subprocess
import threading
import uuid

# ─────────────────────────────────────────────────────────────────────────────
# Turning yt-dlp's raw error text into something a person can act on.
#
# Each entry is (pattern, advice). The raw message is still logged and still
# shown underneath — this only adds the sentence that says what to *do*, which
# is the difference between "it broke" and "open Preferences". First match
# wins, so the specific patterns come before the general ones.
_ERROR_HINTS = [
    (r'confirm you.{0,3}re not a bot|sign in to confirm|not a bot',
     'YouTube wants a signed-in session. Set a cookie source in '
     'Preferences → Cookies, then retry.'),
    (r'age.?restricted|inappropriate for some users|age.?gate',
     'This video is age-restricted. Import cookies from a signed-in browser '
     'in Preferences → Cookies.'),
    (r'private video|members.?only|join this channel|paid members',
     'This video is private or members-only. It needs cookies from an account '
     'that can watch it (Preferences → Cookies).'),
    (r'requested format is not available|no video formats found',
     'That format does not exist for this video. Try the "Best" preset, or '
     'pick a row in Fetch Info.'),
    (r'ffprobe and ffmpeg not found|ffmpeg not found|is not installed',
     'FFmpeg is missing. Set its path in Preferences → Tools.'),
    (r'unsupported url',
     'yt-dlp has no extractor for this site — check the link is a media page, '
     'not a search or a profile.'),
    (r'video unavailable|has been removed|no longer available|account.*terminated',
     'The video is gone, or blocked in your region.'),
    (r'http error 429|too many requests',
     'The site is rate-limiting you. Wait a few minutes, and lower Parallel '
     'Downloads in Preferences → Network.'),
    (r'http error 40[13]|forbidden',
     'The site refused the request. Cookies (Preferences → Cookies) or a '
     'proxy usually fixes this.'),
    (r'no space left|disk.*full',
     'The drive is full — free some space or pick another output folder.'),
    (r'permission denied|access is denied|being used by another process',
     'Windows would not let the file be written. Close whatever has it open, '
     'or pick a different output folder.'),
    (r'getaddrinfo|name or service not known|unable to download webpage|'
     r'connection (reset|refused|aborted)|timed out|ssl',
     'Could not reach the site. Check your connection, or the proxy in '
     'Preferences → Network.'),
    (r'unable to rename file|unable to move file',
     'The finished file could not be moved into place — something else has '
     'it open.'),
]
_ERROR_HINTS = [(re.compile(pat, re.I), advice) for pat, advice in _ERROR_HINTS]


def friendly_error(message: str) -> str:
    """One actionable sentence for a yt-dlp error, or '' when we have nothing
    useful to add. Never replaces the raw text — it sits above it."""
    if not message:
        return ''
    for pattern, advice in _ERROR_HINTS:
        if pattern.search(message):
            return advice
    return ''


# ─────────────────────────────────────────────────────────────────────────────
class NullLogger:
    """Swallows yt-dlp output entirely.

    For background work the user did not ask for — the courtesy title lookup —
    where a failure is not worth reporting. Without it yt-dlp writes straight
    to stderr, which turns a dead link in the queue into console noise.
    """

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


class GUILogger:
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
        # Record the text only. yt-dlp logs errors for recoverable situations
        # too (a missing subtitle track, one dead entry in a playlist), so the
        # worker's return value — not this callback — decides the final status.
        self._q.put(('note_error', self._id, msg.rstrip('\n')))


# ─────────────────────────────────────────────────────────────────────────────
# DownloadItem
# ─────────────────────────────────────────────────────────────────────────────
class DownloadItem:
    PENDING     = 'pending'
    QUEUED      = 'queued'       # accepted, waiting for a download slot
    FETCHING    = 'fetching'
    DOWNLOADING = 'downloading'
    CONVERTING  = 'converting'
    DONE        = 'done'
    ERROR       = 'error'
    CANCELLED   = 'cancelled'

    # States from which a fresh download may be started.
    STARTABLE = (PENDING,)
    # States where a worker thread may still be running.
    ACTIVE    = (QUEUED, FETCHING, DOWNLOADING, CONVERTING)

    def __init__(self, url: str, clip_range=None):
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
        # Explicit format selector chosen in the info popup. Already a complete
        # yt-dlp selector (e.g. '137+bestaudio/137'), not a bare format_id.
        self.format_override: str = ''
        # In/out points for *this* item, in seconds, or None for the whole
        # video. Per-item rather than global so two sections of one video can
        # sit in the queue at once — the filename carries the range, so the two
        # downloads do not land on the same name.
        self.clip_range: 'tuple[float, float] | None' = clip_range
        # Converter preset to run over the finished file, snapshotted when the
        # download starts so changing the sidebar mid-queue changes nothing.
        self.post_convert: str = ''

        # ── filled in as the download proceeds ──
        self.paths: list = []           # final files, from yt-dlp's post_hooks
        self.duration: float = 0.0
        self.uploader: str = ''
        self.note: str = ''             # secondary line, e.g. partial playlist
        self.entry_count: int = 0       # >0 once we know this is a playlist
        self.entry_index: int = 0
        self.playlist_title: str = ''
        self.height = None
        self.fps = None
        self.vcodec = ''
        self.acodec = ''
        self.ext = ''
        self.filesize: int = 0

    def cancel(self):
        self._cancel.set()

    def reset(self):
        """Reset for a retry. Keeps what the *user* chose — the clip range, an
        explicit format pick, the follow-up conversion — and clears only what
        the last attempt produced."""
        self._cancel = threading.Event()
        self.status   = self.PENDING
        self.error    = ''
        self.progress = 0.0
        self.speed    = ''
        self.eta      = ''
        self.size_str = ''
        self.note     = ''
        self.paths    = []
        self.entry_index = 0
        self.filesize = 0

    @property
    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    # ── queue persistence ────────────────────────────────────────────────────
    def to_snapshot(self) -> dict:
        """The parts worth surviving a restart: what was asked for, not what
        happened. Plain JSON scalars only."""
        return {'url': self.url, 'title': self.title,
                'clip_range': list(self.clip_range) if self.clip_range else None,
                'format_override': self.format_override,
                'post_convert': self.post_convert}

    @classmethod
    def from_snapshot(cls, data: dict):
        """Rebuild an item from to_snapshot(). Returns None for anything
        unusable, so a hand-edited settings file cannot stop the app opening."""
        if not isinstance(data, dict) or not data.get('url'):
            return None
        rng = data.get('clip_range')
        try:
            clip = (float(rng[0]), float(rng[1])) if rng and len(rng) == 2 else None
        except (TypeError, ValueError):
            clip = None
        item = cls(str(data['url']), clip_range=clip)
        item.title = str(data.get('title') or data['url'])
        item.format_override = str(data.get('format_override') or '')
        item.post_convert = str(data.get('post_convert') or '')
        return item

    @property
    def clip_label(self) -> str:
        """'1:30 – 2:45' for the card badge, '' when there is no range."""
        if not self.clip_range:
            return ''

        def clock(t):
            m, s = divmod(int(t), 60)
            h, m = divmod(m, 60)
            return f'{h:d}:{m:02d}:{s:02d}' if h else f'{m:d}:{s:02d}'

        start, end = self.clip_range
        tail = 'end' if end == float('inf') else clock(end)
        return f'{clock(start)} – {tail}'


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

    @property
    def is_cancelled(self) -> bool:
        return self._cancel.is_set()


# ─────────────────────────────────────────────────────────────────────────────
# App
