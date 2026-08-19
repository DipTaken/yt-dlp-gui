"""Building ffmpeg argument lists for the converter tab.

Like download.py this is tkinter-free — `warn` and `error` are callbacks — so
argument construction can be exercised directly against a real ffmpeg.

The ffmpeg gotchas encoded here are documented in CLAUDE.md.
"""
from yt_dlp.utils import parse_duration

from .config import (
    CONV_FORMAT_INFO,
    CONV_INTRA_PROFILES,
    CONV_LOSSLESS,
    CONV_LOUDNESS,
    CONV_SCALE_CHOICES,
)
from .download import int_or_none


def parse_time(raw, label: str, warn) -> 'float | None':
    raw = (raw or '').strip()
    if not raw:
        return None
    secs = parse_duration(raw)
    if secs is None:
        warn(f'Could not read {label} {raw!r} — ignoring it.')
        return None
    return secs

def build_conv_args(s: dict, warn, error) -> 'tuple[list, list, str] | None':
    """Assemble the ffmpeg argument list for the current converter settings.

    Returns (input_args, output_args, output_ext), or None if the settings
    are unusable. `input_args` go before -i (that's where -ss belongs, so
    ffmpeg seeks instead of decoding and discarding).
    """
    fmt  = s['conv_output_format']
    info = CONV_FORMAT_INFO.get(fmt)
    if not info:
        error(f'Unknown output format: {fmt}')
        return None
    base_args, kind, ext = info
    out_args = list(base_args)

    # ── Quality, per format kind ─────────────────────────────────────────
    if kind == 'audio' and fmt not in CONV_LOSSLESS:
        q = s['conv_audio_quality']
        if q == 'best':
            if fmt == 'MP3':
                out_args += ['-q:a', '0']
            elif fmt == 'OGG':
                out_args += ['-q:a', '8']
            elif fmt == 'Opus':
                out_args += ['-b:a', '192k']   # libopus has no -q:a
        else:
            out_args += ['-b:a', f'{q}k']
    elif kind == 'video':
        crf = int_or_none(s['conv_video_crf'])
        if crf is None or not 0 <= crf <= 63:
            warn(f'CRF {s["conv_video_crf"]!r} out of range — using 23.')
            crf = 23
        out_args += ['-crf', str(crf)]
    elif kind == 'intra':
        profiles = CONV_INTRA_PROFILES.get(fmt, [])
        if not profiles:
            # dnxhd in particular refuses to encode without an explicit
            # profile, so fail loudly rather than shipping a broken command.
            error(f'No editing profiles defined for {fmt}.')
            return None
        chosen = next((p for p in profiles if p[0] == s.get('conv_intra_profile')),
                      profiles[min(2, len(profiles) - 1)])
        _label, profile, pix_fmt = chosen
        out_args += ['-profile:v', profile, '-pix_fmt', pix_fmt]

    # ── Video filters ────────────────────────────────────────────────────
    filters: list = []
    scale = CONV_SCALE_CHOICES.get(s.get('conv_scale', ''), '')
    if scale and kind != 'audio':
        # min(iw,N) makes this downscale-only: picking "1920" for a 720p
        # source should leave it at 720p, not blow it up to 1080p and waste
        # bitrate inventing detail. -2 keeps the aspect ratio and forces an
        # even height, which every one of these encoders requires.
        filters.append(f"scale=w='min(iw,{scale})':h=-2:flags=lanczos")
    if filters:
        out_args += ['-vf', ','.join(filters)]

    fps = s.get('conv_fps', '')
    if fps and not fps.startswith('(') and kind != 'audio':
        out_args += ['-r', fps, '-fps_mode', 'cfr']

    # ── Audio filters ────────────────────────────────────────────────────
    loud = CONV_LOUDNESS.get(s.get('conv_loudness', 'Off'), '')
    if loud:
        out_args += ['-af', f'loudnorm={loud}']

    # ── Stream selection ─────────────────────────────────────────────────
    # Audio targets must drop the video stream, otherwise converting a video
    # file to M4A/AAC/OGG/ALAC re-encodes and keeps the picture.
    out_args += ['-vn', '-sn', '-dn'] if kind == 'audio' else ['-sn', '-dn']

    # ── Trim ─────────────────────────────────────────────────────────────
    in_args: list = []
    start = parse_time(s.get('conv_trim_start'), 'trim start', warn)
    end   = parse_time(s.get('conv_trim_end'), 'trim end', warn)
    if start is not None:
        in_args += ['-ss', str(start)]
    if end is not None:
        if start is not None and end <= start:
            warn('Trim end is not after trim start — ignoring trim.')
            in_args, end = [], None
        else:
            # -to after -i is relative to the trimmed start, so pass the
            # duration instead of an absolute stamp.
            out_args += ['-t', str(end - (start or 0))]

    return in_args, out_args, ext
