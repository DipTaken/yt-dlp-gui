"""Locating ffmpeg and the low-level helpers for driving it."""
import os
import re
import shutil
import subprocess
import sys

from .config import ROOT

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

_DURATION_RE = re.compile(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)')
TIME_RE      = re.compile(r'time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)')


def no_window_kwargs() -> dict:
    """Keep ffmpeg/ffprobe from flashing a console window on Windows."""
    if sys.platform == 'win32':
        return {'creationflags': subprocess.CREATE_NO_WINDOW}
    return {}


def probe_duration(ffmpeg_bin: str, path: str) -> float:
    """Return file duration in seconds by running `ffmpeg -i <path>`."""
    try:
        kw: dict = {'stderr': subprocess.PIPE, 'stdout': subprocess.DEVNULL,
                    'text': True, 'encoding': 'utf-8', 'errors': 'replace',
                    'timeout': 20}
        kw.update(no_window_kwargs())
        # ffmpeg exits with code 1 (no output specified) but prints full
        # media info including "Duration: HH:MM:SS.ms" to stderr.
        r = subprocess.run([ffmpeg_bin, '-hide_banner', '-i', path], **kw)
        m = _DURATION_RE.search(r.stderr or '')
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return 0.0


def effective_duration(probed: float, in_args: list, out_args: list) -> float:
    """Length of the output once -ss / -t are taken into account."""
    def after(args, flag):
        try:
            return float(args[args.index(flag) + 1])
        except (ValueError, IndexError):
            return None
    if probed > 0:
        ss = after(in_args, '-ss')
        if ss:
            probed = max(probed - ss, 0.0)
    t = after(out_args, '-t')
    if t:
        probed = min(probed, t) if probed > 0 else t
    return probed


def iter_ffmpeg_chunks(stream):
    """Yield ffmpeg stderr fragments split on CR *and* LF.

    ffmpeg rewrites its progress line in place using carriage returns, so
    iterating the stream line-by-line (`for line in proc.stderr`) yields
    nothing until the process exits — which is why the converter's progress
    bar sat at zero and then jumped straight to done.
    """
    buf = ''
    while True:
        chunk = stream.read(256)
        if not chunk:
            break
        buf += chunk
        buf = buf.replace('\r\n', '\n')
        while True:
            idx = min((i for i in (buf.find('\r'), buf.find('\n')) if i >= 0),
                      default=-1)
            if idx < 0:
                break
            piece, buf = buf[:idx], buf[idx + 1:]
            if piece:
                yield piece
    if buf:
        yield buf


def same_file(a: str, b: str) -> bool:
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
