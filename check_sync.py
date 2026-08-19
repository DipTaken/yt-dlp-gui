#!/usr/bin/env python3
"""Post-sync smoke test: does the GUI still agree with the vendored yt-dlp?

Run this after merging upstream yt-dlp. It asserts the yt-dlp internals the
option builder depends on, all of which are documented in CLAUDE.md. Each has
broken silently before — a wrong postprocessor_args key or a renamed option is
accepted without complaint and simply does nothing.

    python check_sync.py

Exits non-zero if anything regressed. Needs no network. The ffmpeg section is
skipped when ffmpeg is not installed.
"""
import itertools
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from yt_dlp import YoutubeDL
from yt_dlp.postprocessor import FFmpegExtractAudioPP, MetadataParserPP
from yt_dlp.version import __version__ as YTDLP_VERSION

from ytdlp_gui import config
from ytdlp_gui.convert import build_conv_args
from ytdlp_gui.download import build_ydl_opts, parse_clip_range
from ytdlp_gui.ffmpeg_utils import find_ffmpeg, no_window_kwargs
from ytdlp_gui.models import DownloadItem

FAILS = []
WARNINGS = []


def check(label, cond, detail=''):
    print(f'{"PASS" if cond else "FAIL"}  {label}{"  -- " + str(detail) if detail else ""}')
    if not cond:
        FAILS.append(label)


def opts_for(**over):
    """Build ydl opts from defaults plus overrides, capturing warnings."""
    s = {**config.DEFAULT_SETTINGS, **over}
    return build_ydl_opts(
        DownloadItem('https://example.com/v'), s,
        ffmpeg_path='', logger=None, progress_hook=None, pp_hook=None,
        warn=WARNINGS.append)


U = 'https://example.com/v'
FORMATS = [
    dict(format_id='av1-2160', ext='mp4',  vcodec='av01.0.12M.08', acodec='none',
         height=2160, width=3840, tbr=16000, url=U, protocol='https'),
    dict(format_id='vp9-2160', ext='webm', vcodec='vp09.00.50.08', acodec='none',
         height=2160, width=3840, tbr=13000, url=U, protocol='https'),
    dict(format_id='h264-1080', ext='mp4', vcodec='avc1.640028', acodec='none',
         height=1080, width=1920, tbr=2500, url=U, protocol='https'),
    dict(format_id='h264-720', ext='mp4',  vcodec='avc1.4d401f', acodec='none',
         height=720, width=1280, tbr=1200, url=U, protocol='https'),
    dict(format_id='opus', ext='webm', vcodec='none', acodec='opus',
         abr=160, url=U, protocol='https'),
    dict(format_id='m4a', ext='m4a', vcodec='none', acodec='mp4a.40.2',
         abr=128, url=U, protocol='https'),
]


def select(o):
    """Run yt-dlp's real format selection over a synthetic format list."""
    keep = {k: v for k, v in o.items()
            if k in ('format', 'format_sort', 'merge_output_format')}
    ydl = YoutubeDL({**keep, 'quiet': True, 'simulate': True})
    try:
        r = ydl.process_ie_result(
            dict(id='x', title='t', formats=[dict(f) for f in FORMATS],
                 extractor='test', extractor_key='Test', webpage_url=U,
                 _type='video'), download=False)
        return r.get('format_id'), r.get('ext')
    finally:
        ydl.close()


print(f'yt-dlp {YTDLP_VERSION}   python {sys.version.split()[0]}')
print()
print('=== option names yt-dlp still honours ===')
o = opts_for(rate_limit='2M', max_filesize='500M', date_after='20240101',
             date_before='20241231', retries='5')
check('ratelimit parsed to bytes', o.get('ratelimit') == 2097152, o.get('ratelimit'))
check('max_filesize parsed to bytes', o.get('max_filesize') == 524288000, o.get('max_filesize'))
check('daterange is a DateRange', 'daterange' in o and '20240615' in o['daterange'])
check('no CLI-only date keys leaked', not {'dateafter', 'datebefore'} & o.keys())
# These are read straight off params; a rename upstream would silently disable them.
for key in ('format', 'outtmpl', 'postprocessors', 'writethumbnail', 'noplaylist',
            'concurrent_fragment_downloads', 'retries', 'fragment_retries'):
    check(f'YoutubeDL documents "{key}"', key in YoutubeDL.__doc__, '')

print()
print('=== format selection still behaves ===')
fid, ext = select(opts_for(format_preset='Best (Video + Audio)',
                           compat_mode='Compatible (H.264 / AAC / MP4)'))
check('compat mode picks H.264 + AAC in mp4',
      fid == 'h264-1080+m4a' and ext == 'mp4', f'{fid} .{ext}')
fid, ext = select(opts_for(format_preset='Best (Video + Audio)',
                           compat_mode='Balanced (best resolution, MP4 if possible)'))
check('balanced falls back to mkv for VP9+AAC', ext == 'mkv', f'{fid} .{ext}')
fid, _ = select(opts_for(format_preset='720p'))
check('720p cap respected', fid.startswith('h264-720'), fid)
# A cap with nothing under it must still download rather than error.
ydl = YoutubeDL({**{k: v for k, v in opts_for(format_preset='360p').items()
                    if k in ('format', 'format_sort', 'merge_output_format')},
                 'quiet': True, 'simulate': True})
try:
    r = ydl.process_ie_result(
        dict(id='x', title='t', extractor='test', extractor_key='Test',
             webpage_url=U, _type='video',
             formats=[dict(f) for f in FORMATS if (f.get('height') or 0) >= 2160
                      or f['format_id'] == 'm4a']), download=False)
    check('cap with nothing below it still resolves', bool(r.get('format_id')),
          r.get('format_id'))
except Exception as exc:
    check('cap with nothing below it still resolves', False, f'{type(exc).__name__}: {exc}')
finally:
    ydl.close()

print()
print('=== postprocessor chain ===')
o = opts_for(embed_thumbnail=True, write_thumbnail=False, embed_metadata=True,
             write_subs=True, embed_subs=True, sponsorblock_enabled=True,
             split_chapters=True, music_tags=True, audio_extract=True)
keys = [p['key'] for p in o['postprocessors']]
print('  chain:', keys)
check('EmbedThumbnail forces writethumbnail', o['writethumbnail'] is True)
check('ModifyChapters before FFmpegMetadata',
      keys.index('ModifyChapters') < keys.index('FFmpegMetadata'))
check('EmbedThumbnail after FFmpegMetadata',
      keys.index('EmbedThumbnail') > keys.index('FFmpegMetadata'))
check('SplitChapters runs last', keys[-1] == 'FFmpegSplitChapters')
# Every PP key must still exist upstream, and accept the kwargs we pass.
for pp in o['postprocessors']:
    kwargs = {k: v for k, v in pp.items() if k not in ('key', 'when')}
    try:
        YoutubeDL({'quiet': True}).add_post_processor  # noqa: B018  (attr exists)
        from yt_dlp.postprocessor import get_postprocessor
        cls = get_postprocessor(pp['key'])
        y = YoutubeDL({'quiet': True})
        cls(y, **kwargs)
        y.close()
        ok, detail = True, ''
    except Exception as exc:
        ok, detail = False, f'{type(exc).__name__}: {exc}'
    check(f'{pp["key"]} accepts our arguments', ok, detail)

print()
print('=== postprocessor_args key lookup ===')
o = opts_for(audio_extract=True, audio_normalize=True, audio_sample_rate='48000')
y = YoutubeDL({'quiet': True, 'postprocessor_args': o.get('postprocessor_args', {})})
got = FFmpegExtractAudioPP(y)._configuration_args('ffmpeg')
y.close()
check('yt-dlp actually receives the audio args',
      got == ['-af', 'loudnorm', '-ar', '48000'], got)

print()
print('=== music tagging still parses ===')
o = opts_for(music_tags=True, audio_extract=True)
cfg = next(p for p in o['postprocessors'] if p['key'] == 'MetadataParser')
y = YoutubeDL({'quiet': True})
info = {'title': 'Daft Punk - Around the World', 'ext': 'mp3'}
MetadataParserPP(y, cfg['actions']).run(info)
y.close()
check('artist/track parsed from title',
      info.get('artist') == 'Daft Punk' and info.get('track') == 'Around the World',
      f"{info.get('artist')!r} / {info.get('track')!r}")

print()
print('=== clip range ===')
s = {**config.DEFAULT_SETTINGS, 'clip_start': '1:30', 'clip_end': '2:45'}
check('timestamps parse', parse_clip_range(s, WARNINGS.append) == (90.0, 165.0))
o = opts_for(clip_start='1:30', clip_end='2:45', clip_precise=True)
rng = list(o['download_ranges']({'duration': 600}, None))
check('download_ranges yields the window',
      rng == [{'start_time': 90.0, 'end_time': 165.0}], rng)

print()
print('=== every option combination constructs ===')
bad = []
for audio, split, sb, tags, clip in itertools.product((0, 1), repeat=5):
    try:
        o = opts_for(audio_extract=bool(audio), split_chapters=bool(split),
                     sponsorblock_enabled=bool(sb), music_tags=bool(tags),
                     clip_start='0:10' if clip else '',
                     clip_end='0:20' if clip else '')
        YoutubeDL({**o, 'simulate': True, 'quiet': True}).close()
    except Exception as exc:
        bad.append(f'{audio}{split}{sb}{tags}{clip}: {type(exc).__name__}: {exc}')
check('all 32 combinations accepted by YoutubeDL', not bad, '; '.join(bad[:2]))

# ── ffmpeg side ──────────────────────────────────────────────────────────────
FF = find_ffmpeg()
print()
if not FF:
    print('=== ffmpeg checks SKIPPED (ffmpeg not found) ===')
else:
    print('=== converter formats encode for real ===')
    work = tempfile.mkdtemp(prefix='synccheck_')
    src = os.path.join(work, 'src.mkv')
    subprocess.run(
        [FF, '-y', '-hide_banner', '-loglevel', 'error',
         '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=30:duration=2',
         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
         '-c:v', 'libx264', '-pix_fmt', 'yuv422p10le', '-c:a', 'aac', src],
        check=True, **no_window_kwargs())

    for fmt, (_args, kind, _ext) in config.CONV_FORMAT_INFO.items():
        s = {**config.DEFAULT_SETTINGS, 'conv_output_format': fmt}
        built = build_conv_args(s, WARNINGS.append, WARNINGS.append)
        if not built:
            check(f'{fmt}', False, 'builder returned nothing')
            continue
        in_args, out_args, ext = built
        out = os.path.join(work, f'o{abs(hash(fmt))}.{ext}')
        r = subprocess.run([FF, '-hide_banner', '-nostdin', '-y'] + in_args +
                           ['-i', src] + out_args + [out],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', **no_window_kwargs())
        ok = r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0
        detail = ''
        if ok:
            p = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries',
                 'stream=codec_type,codec_name,pix_fmt', '-of', 'json', out],
                capture_output=True, text=True, **no_window_kwargs())
            streams = json.loads(p.stdout or '{}').get('streams', [])
            if kind == 'audio' and any(x['codec_type'] == 'video' for x in streams):
                ok, detail = False, 'video stream leaked into an audio output'
            elif kind == 'video':
                v = next((x for x in streams if x['codec_type'] == 'video'), None)
                if v and v.get('pix_fmt') not in ('yuv420p', None):
                    ok, detail = False, f'not 8-bit 4:2:0 — {v.get("pix_fmt")}'
        else:
            detail = ((r.stderr or '').strip().splitlines() or ['?'])[-1][:100]
        check(fmt, ok, detail)

    import shutil
    shutil.rmtree(work, ignore_errors=True)

print()
if FAILS:
    print(f'{len(FAILS)} FAILED: {FAILS}')
    print('\nSee the "gotchas" sections in CLAUDE.md — upstream most likely '
          'renamed or re-ordered something the option builder relies on.')
else:
    print('All checks passed.')
sys.exit(1 if FAILS else 0)
