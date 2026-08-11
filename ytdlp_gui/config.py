"""Settings schema, presets and the format/codec tables.

Pure data plus load/save/migrate. Imports nothing from the UI, so tests and
tooling can read it without a display.
"""
import json
import os
import sys
from pathlib import Path


def _project_root() -> str:
    """Directory holding gui.py — where gui_settings.json lives.

    Kept stable across the package split so existing settings files are still
    found. Under PyInstaller the package is unpacked to a temp dir, so the
    executable's own directory is the right anchor there.
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


ROOT = _project_root()

SETTINGS_FILE = os.path.join(ROOT, 'gui_settings.json')
SETTINGS_SCHEMA_VERSION = 2

# Resolution caps are expressed through yt-dlp's format *sorting* (`-S res:N`)
# rather than through a `[height<=N]` filter. A filter makes the download fail
# outright ("Requested format is not available") when every available format is
# above the cap; sorting instead picks the closest match and always succeeds.
#
#   label -> (format selector, height cap or None)
FORMAT_PRESETS: dict[str, tuple[str, 'int | None']] = {
    'Best (Video + Audio)': ('bv*+ba/b',  None),
    '4K (2160p)':           ('bv*+ba/b',  2160),
    '1440p':                ('bv*+ba/b',  1440),
    '1080p':                ('bv*+ba/b',  1080),
    '720p':                 ('bv*+ba/b',  720),
    '480p':                 ('bv*+ba/b',  480),
    '360p':                 ('bv*+ba/b',  360),
    'Audio Only (Best)':    ('ba/b',      None),
    'Custom…':              ('',          None),
}

# Codec/container policy. `sort` fields are prepended to yt-dlp's default format
# ordering; `merge` is passed as --merge-output-format.
#
# 'mp4/mkv' (rather than plain 'mp4') matters: with a single preference yt-dlp
# will force the container even when the codecs don't fit it, producing e.g.
# VP9+Opus inside .mp4 — a file most players choke on. The '/mkv' fallback lets
# it pick a container that can actually hold the streams.
COMPAT_MODES: dict[str, dict] = {
    'Compatible (H.264 / AAC / MP4)': {
        'sort':  ['vcodec:h264', 'acodec:aac', 'ext:mp4:m4a'],
        'merge': 'mp4/mkv',
        'help':  'Plays everywhere (Windows Media Player, QuickTime, editors, '
                 'phones, TVs). Caps out at 1080p on YouTube, which serves '
                 'higher resolutions only as VP9/AV1.',
    },
    'Balanced (best resolution, MP4 if possible)': {
        'sort':  ['res', 'vcodec:h264', 'acodec:aac', 'ext:mp4:m4a'],
        'merge': 'mp4/mkv',
        'help':  'Highest resolution available, preferring H.264/AAC at that '
                 'resolution. Falls back to .mkv when the codecs cannot go in '
                 'an MP4 container.',
    },
    'Max quality (any codec, MKV)': {
        'sort':  [],
        'merge': 'mkv',
        'help':  'Whatever yt-dlp rates highest (often AV1/VP9 + Opus), always '
                 'in Matroska. Best quality per byte; needs VLC/mpv or a recent '
                 'player.',
    },
    'yt-dlp default (no override)': {
        'sort':  [],
        'merge': '',
        'help':  'No container or codec preference at all — yt-dlp decides, '
                 'which may yield .webm or .mkv.',
    },
}
DEFAULT_COMPAT_MODE = 'Balanced (best resolution, MP4 if possible)'

SB_CATEGORIES = ['all', 'sponsor', 'intro', 'outro', 'selfpromo',
                  'interaction', 'music_offtopic', 'preview', 'filler']

# 'Mark' writes chapters you can skip manually; 'Remove' physically cuts the
# segments out (needs a re-encode at the cut points to stay in sync).
SB_MODES = ['Mark as chapters', 'Remove from file']

# Subtitle conversion targets. SRT is what every NLE and transcription tool
# reads; YouTube hands out VTT by default.
SUB_FORMATS = ['(keep original)', 'srt', 'ass', 'vtt', 'lrc']

# Filename layouts. yt-dlp template syntax: `a,b` = use first available field,
# `|x` = literal fallback, `>fmt` = strftime for date fields.
OUTPUT_TEMPLATES: dict[str, str] = {
    'Default — Title [id]':
        '%(title)s [%(id)s].%(ext)s',
    'Plain title':
        '%(title)s.%(ext)s',
    'Music library — Artist/Album/## Track':
        '%(artist,uploader|Unknown Artist)s/%(album,playlist_title|Singles)s/'
        '%(track_number,playlist_index|0)02d %(track,title)s.%(ext)s',
    'Music flat — Artist - Title':
        '%(artist,uploader|Unknown Artist)s - %(track,title)s.%(ext)s',
    'Playlist folders — ###  Title':
        '%(playlist_title|Downloads)s/%(playlist_index|0)03d %(title)s.%(ext)s',
    'Editor — Title [1080p60]':
        '%(title)s [%(height|0)sp%(fps|0)s].%(ext)s',
    'Date — YYYY-MM-DD Uploader - Title':
        '%(upload_date>%Y-%m-%d|no-date)s %(uploader|Unknown)s - %(title)s.%(ext)s',
    'Custom…':
        '',
}

# ─────────────────────────────────────────────────────────────────────────────
# Converter constants
# ─────────────────────────────────────────────────────────────────────────────
CONV_AUDIO_FORMATS = ['MP3', 'WAV', 'FLAC', 'AAC', 'M4A', 'OGG', 'Opus', 'ALAC']
CONV_VIDEO_FORMATS = ['MP4 (H.264)', 'MP4 (H.265/HEVC)', 'MKV', 'WebM', 'MOV', 'AVI',
                      'ProRes (MOV)', 'DNxHR (MOV)']
CONV_LOSSLESS      = {'WAV', 'FLAC', 'ALAC'}

# Intra-frame editing codecs. Every frame is a keyframe, so scrubbing and
# trimming in an NLE is instant — unlike the long-GOP H.264/VP9/AV1 that
# download sites serve, which editors must decode sequentially.
# Profile flags verified against ffmpeg 8.1; each profile pins the pixel format
# its encoder actually requires.
CONV_INTRA_PROFILES: dict[str, list] = {
    # label, -profile:v value, -pix_fmt
    'ProRes (MOV)': [
        ('Proxy — smallest, offline edit', '0', 'yuv422p10le'),
        ('LT — light delivery',            '1', 'yuv422p10le'),
        ('422 — standard',                 '2', 'yuv422p10le'),
        ('422 HQ — mastering',             '3', 'yuv422p10le'),
        ('4444 — with alpha',              '4', 'yuva444p10le'),
    ],
    'DNxHR (MOV)': [
        ('LB — low bandwidth proxy', 'dnxhr_lb',  'yuv422p'),
        ('SQ — standard quality',    'dnxhr_sq',  'yuv422p'),
        ('HQ — high quality',        'dnxhr_hq',  'yuv422p'),
        ('HQX — 10-bit mastering',   'dnxhr_hqx', 'yuv422p10le'),
    ],
}

# Constant-frame-rate targets. Downloads are often variable-frame-rate, which
# NLEs handle badly — audio drifts out of sync over long clips. Forcing CFR at
# import time is the standard fix.
CONV_FPS_CHOICES = ['(keep original)', '23.976', '24', '25', '29.97', '30',
                    '50', '59.94', '60']

# Downscale targets, by width. '-2' keeps the aspect ratio and guarantees an
# even height, which every h264/prores encoder requires.
CONV_SCALE_CHOICES = {
    '(keep original)': '',
    '3840 (4K UHD)':   '3840',
    '2560 (1440p)':    '2560',
    '1920 (1080p)':    '1920',
    '1280 (720p)':     '1280',
    '854 (480p)':      '854',
    '640 (360p)':      '640',
}

# EBU R128 loudness targets. Streaming services normalise to roughly -14 LUFS;
# -16 is the podcast/spoken-word convention, -23 is EU broadcast.
CONV_LOUDNESS = {
    'Off':                        '',
    '-14 LUFS (streaming)':       'I=-14:TP=-1:LRA=11',
    '-16 LUFS (podcast)':         'I=-16:TP=-1.5:LRA=11',
    '-23 LUFS (EBU broadcast)':   'I=-23:TP=-2:LRA=7',
}

# Quick converter presets. Any key omitted keeps the panel's current value.
#   type/fmt  — format type + output format
#   aq/crf/profile — the quality control for that format kind
#   scale/fps/loud — optional filters
CONV_PRESETS: dict[str, dict] = {
    # ── Music ────────────────────────────────────────────────────────────────
    'MP3 — High (320 kbps)':        dict(type='Audio', fmt='MP3',  aq='320'),
    'MP3 — Standard (192 kbps)':    dict(type='Audio', fmt='MP3',  aq='192'),
    'MP3 — Small (128 kbps)':       dict(type='Audio', fmt='MP3',  aq='128'),
    'AAC / M4A — Standard':         dict(type='Audio', fmt='M4A',  aq='192'),
    'FLAC — Lossless archive':      dict(type='Audio', fmt='FLAC', aq='best'),
    'Opus — Voice (64 kbps)':       dict(type='Audio', fmt='Opus', aq='64'),
    'MP3 — Streaming loudness':     dict(type='Audio', fmt='MP3',  aq='320',
                                         loud='-14 LUFS (streaming)'),
    'MP3 — Podcast loudness':       dict(type='Audio', fmt='MP3',  aq='192',
                                         loud='-16 LUFS (podcast)'),
    # ── Delivery ─────────────────────────────────────────────────────────────
    'MP4 H.264 — High Quality':     dict(type='Video', fmt='MP4 (H.264)',      crf='18'),
    'MP4 H.264 — Balanced':         dict(type='Video', fmt='MP4 (H.264)',      crf='23'),
    'MP4 H.264 — Small File':       dict(type='Video', fmt='MP4 (H.264)',      crf='28'),
    'MP4 H.265 — Efficient':        dict(type='Video', fmt='MP4 (H.265/HEVC)', crf='24'),
    'WebM — Web-friendly':          dict(type='Video', fmt='WebM',             crf='32'),
    # ── Editing ──────────────────────────────────────────────────────────────
    'ProRes 422 — edit-ready':      dict(type='Video', fmt='ProRes (MOV)',
                                         profile='422 — standard'),
    'ProRes 422 HQ — mastering':    dict(type='Video', fmt='ProRes (MOV)',
                                         profile='422 HQ — mastering'),
    'ProRes Proxy — 1080p offline': dict(type='Video', fmt='ProRes (MOV)',
                                         profile='Proxy — smallest, offline edit',
                                         scale='1920 (1080p)'),
    'DNxHR SQ — Resolve/Avid':      dict(type='Video', fmt='DNxHR (MOV)',
                                         profile='SQ — standard quality'),
    'DNxHR HQX — 10-bit master':    dict(type='Video', fmt='DNxHR (MOV)',
                                         profile='HQX — 10-bit mastering'),
    'H.264 proxy — 720p @30 CFR':   dict(type='Video', fmt='MP4 (H.264)', crf='26',
                                         scale='1280 (720p)', fps='30'),
    'Timeline-safe — 1080p @24 CFR': dict(type='Video', fmt='MP4 (H.264)', crf='20',
                                          scale='1920 (1080p)', fps='24'),
}

# (base_ffmpeg_args, kind, output_ext)
#
# Notes on the args below:
#  · Audio targets get `-vn` at build time. Verified against ffmpeg 8.1: without
#    it, an M4A/AAC/OGG/ALAC "audio" conversion of a video file keeps the video
#    stream and re-encodes it, so the result is a video file wearing an audio
#    extension. (MP3/WAV/FLAC/Opus happened to drop it on their own.)
#  · H.264 targets pin `-pix_fmt yuv420p`. libx264 otherwise inherits the
#    source's pixel format, so a 10-bit or 4:2:2 input yields High 4:2:2 10-bit
#    output that most hardware decoders and players refuse — verified: the old
#    args turned a yuv422p10le source into a yuv422p10le MP4.
#  · `-sn -dn` on video targets is defensive; ffmpeg dropped incompatible
#    subtitle streams on its own in testing, but being explicit costs nothing.
#  · AVI uses `libmp3lame` rather than `mp3` for explicitness — the bare `mp3`
#    alias does resolve on current ffmpeg builds, so this is a clarity change.
CONV_FORMAT_INFO: dict[str, tuple[list, str, str]] = {
    'MP3':              (['-c:a', 'libmp3lame'],                             'audio', 'mp3'),
    'WAV':              (['-c:a', 'pcm_s16le'],                              'audio', 'wav'),
    'FLAC':             (['-c:a', 'flac'],                                   'audio', 'flac'),
    'AAC':              (['-c:a', 'aac'],                                    'audio', 'm4a'),
    'M4A':              (['-c:a', 'aac', '-movflags', '+faststart'],         'audio', 'm4a'),
    'OGG':              (['-c:a', 'libvorbis'],                              'audio', 'ogg'),
    'Opus':             (['-c:a', 'libopus'],                                'audio', 'opus'),
    'ALAC':             (['-c:a', 'alac', '-movflags', '+faststart'],        'audio', 'm4a'),
    'MP4 (H.264)':      (['-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                          '-c:a', 'aac', '-movflags', '+faststart'],         'video', 'mp4'),
    'MP4 (H.265/HEVC)': (['-c:v', 'libx265', '-pix_fmt', 'yuv420p',
                          '-tag:v', 'hvc1',
                          '-c:a', 'aac', '-movflags', '+faststart'],         'video', 'mp4'),
    'MKV':              (['-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                          '-c:a', 'aac'],                                    'video', 'mkv'),
    # yuv420p pins VP9 to profile 0. Without it a 10-bit source yields profile
    # 2/3, which browsers and most hardware decoders refuse to play.
    'WebM':             (['-c:v', 'libvpx-vp9', '-pix_fmt', 'yuv420p', '-b:v', '0',
                          '-c:a', 'libopus'],                                'video', 'webm'),
    'MOV':              (['-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                          '-c:a', 'aac'],                                    'video', 'mov'),
    'AVI':              (['-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                          '-c:a', 'libmp3lame'],                             'video', 'avi'),
    # Editing codecs: uncompressed PCM audio too, since NLEs prefer it and the
    # file is already large enough that compressing the audio saves nothing.
    'ProRes (MOV)':     (['-c:v', 'prores_ks', '-c:a', 'pcm_s16le'],         'intra', 'mov'),
    'DNxHR (MOV)':      (['-c:v', 'dnxhd', '-c:a', 'pcm_s16le'],             'intra', 'mov'),
}

DEFAULT_SETTINGS = {
    'output_dir':           str(Path.home() / 'Downloads'),
    'output_template':      '%(title)s [%(id)s].%(ext)s',
    'template_preset':      'Default — Title [id]',
    'format_preset':        'Best (Video + Audio)',
    'compat_mode':          DEFAULT_COMPAT_MODE,
    'custom_format':        '',
    'audio_extract':        False,
    'audio_format':         'mp3',
    'audio_quality':        '192',
    'audio_normalize':      False,
    'audio_sample_rate':    '',
    'keep_video':           False,
    'music_tags':           False,
    'embed_thumbnail':      True,
    'write_thumbnail':      False,
    'embed_metadata':       True,
    'write_infojson':       False,
    'write_subs':           False,
    'auto_subs':            False,
    'embed_subs':           True,
    'sub_langs':            'en',
    'sub_convert':          '(keep original)',
    'sponsorblock_enabled': False,
    'sponsorblock_cats':    'all',
    'sponsorblock_mode':    'Mark as chapters',
    'clip_start':           '',
    'clip_end':             '',
    'clip_precise':         True,
    'split_chapters':       False,
    'archive_enabled':      False,
    'archive_file':         '',
    'rate_limit':           '',
    'proxy':                '',
    'retries':              '3',
    'concurrent_fragments': '4',
    'max_concurrent':       '3',
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
    'conv_intra_profile':   '',        # '' = first profile for the codec
    'conv_fps':             '(keep original)',
    'conv_scale':           '(keep original)',
    'conv_loudness':        'Off',
    'conv_trim_start':      '',
    'conv_trim_end':        '',
    'conv_overwrite':       True,
    # Window geometry (WxH+X+Y); empty = use default
    'window_geometry':      '',
    'schema_version':       SETTINGS_SCHEMA_VERSION,
}


# ─────────────────────────────────────────────────────────────────────────────


def migrate_settings(data: dict, from_version: int) -> dict:
    """Translate older settings shapes forward. Add new migration cases here
    as the schema evolves; each should bump `from_version` until it matches
    SETTINGS_SCHEMA_VERSION."""
    # v0 → v1: no field renames yet; just record the new version.
    if from_version < 1:
        from_version = 1
    # v1 → v2: format presets no longer encode container/codec preferences —
    # that moved to the separate `compat_mode` setting. The old 'Best MP4'
    # preset becomes "Best" + the compatible codec policy.
    if from_version < 2:
        if data.get('format_preset') == 'Best MP4':
            data['format_preset'] = 'Best (Video + Audio)'
            data.setdefault('compat_mode', 'Compatible (H.264 / AAC / MP4)')
        if data.get('format_preset') not in FORMAT_PRESETS:
            data['format_preset'] = DEFAULT_SETTINGS['format_preset']
        from_version = 2
    # Future: if from_version < 3: ...
    return data


def load_settings() -> dict:
    """Read gui_settings.json, migrate it forward and fill in any new keys.

    Any failure falls back to defaults rather than refusing to start — a
    corrupt settings file should never be a reason the app won't open.
    """
    if not os.path.exists(SETTINGS_FILE):
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as f:
            raw = json.load(f)
    except Exception:
        return dict(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return dict(DEFAULT_SETTINGS)

    raw = migrate_settings(raw, raw.get('schema_version', 0))
    merged = {**DEFAULT_SETTINGS, **raw}
    merged['schema_version'] = SETTINGS_SCHEMA_VERSION
    return merged


def save_settings(settings: dict):
    """Best-effort write; a failure here must never interrupt a download."""
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2)
    except OSError:
        pass
