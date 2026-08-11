"""Per-row state objects and the yt-dlp -> GUI log adapter."""
import os
import queue
import subprocess
import threading
import uuid

# ─────────────────────────────────────────────────────────────────────────────
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
        # Explicit format selector chosen in the info popup. Already a complete
        # yt-dlp selector (e.g. '137+bestaudio/137'), not a bare format_id.
        self.format_override: str = ''

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

    @property
    def is_cancelled(self) -> bool:
        return self._cancel.is_set()


# ─────────────────────────────────────────────────────────────────────────────
# App
