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
from yt_dlp.postprocessor.ffmpeg import ACODECS
from yt_dlp.version import __version__ as YTDLP_VERSION

from ytdlp_gui import config
from ytdlp_gui.audio_filters import build_audio_filter_chain
from ytdlp_gui.convert import build_conv_args
from ytdlp_gui.download import audio_encoder_for, build_ydl_opts, parse_clip_range
from ytdlp_gui.ffmpeg_utils import find_ffmpeg, no_window_kwargs
from ytdlp_gui.models import DownloadItem

FAILS = []
WARNINGS = []


def check(label, cond, detail=''):
    print(f'{"PASS" if cond else "FAIL"}  {label}{"  -- " + str(detail) if detail else ""}')
    if not cond:
        FAILS.append(label)


def opts_for(clip=None, **over):
    """Build ydl opts from defaults plus overrides, capturing warnings.

    `clip` goes on the *item*, not the settings: a clip range is per queue
    entry, which is what lets one video be queued twice for two sections.
    """
    s = {**config.DEFAULT_SETTINGS, **over}
    return build_ydl_opts(
        DownloadItem('https://example.com/v', clip_range=clip), s,
        ffmpeg_path='', logger=None, progress_hook=None, pp_hook=None,
        warn=WARNINGS.append)


def name_for(opts, info, tmpl_type=''):
    """Render a filename exactly the way YoutubeDL would for these opts."""
    ydl = YoutubeDL({'quiet': True, 'simulate': True,
                     'outtmpl': opts['outtmpl'], 'paths': opts.get('paths', {})})
    try:
        return ydl.prepare_filename(dict(info), tmpl_type)
    finally:
        ydl.close()


def ffmpeg_accepts(ff, args, source_args=('-f', 'lavfi', '-i', 'sine=f=440:d=1')):
    """Run a real encode with `args` and report (ok, last stderr line)."""
    r = subprocess.run([ff, '-hide_banner', '-nostdin', '-y', *source_args,
                        *args, '-f', 'null', '-'],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', **no_window_kwargs())
    tail = ((r.stderr or '').strip().splitlines() or [''])[-1]
    return r.returncode == 0, tail[:90]


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
            'concurrent_fragment_downloads', 'retries', 'fragment_retries',
            'post_hooks', 'paths', 'ignoreerrors', 'extract_flat'):
    check(f'YoutubeDL documents "{key}"', key in YoutubeDL.__doc__, '')
o = opts_for()
check('ignoreerrors lets a playlist survive one dead entry',
      o.get('ignoreerrors') == 'only_download', o.get('ignoreerrors'))
check('paths.home carries the output folder',
      o.get('paths', {}).get('home') == config.DEFAULT_SETTINGS['output_dir'],
      o.get('paths'))
check('post_hooks is accepted and registered',
      len(YoutubeDL({'quiet': True, 'post_hooks': [lambda p: None]})._post_hooks) == 1)

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
      got == ['-c:a', 'libmp3lame', '-af', 'loudnorm', '-ar', '48000'], got)

print()
print('=== audio extraction targets exist upstream ===')
missing = [f for f in config.AUDIO_FORMATS
           if f not in FFmpegExtractAudioPP.SUPPORTED_EXTS]
check('every offered audio format is one yt-dlp knows', not missing, missing)
no_encoder = [f for f in config.AUDIO_FORMATS if not audio_encoder_for(f)]
check('every audio format resolves to an ffmpeg encoder', not no_encoder, no_encoder)
check('ogg is not offered (the codec is called vorbis)',
      'ogg' not in config.AUDIO_FORMATS)
check('an old "ogg" setting migrates to vorbis',
      config.migrate_settings({'audio_format': 'ogg'}, 2)['audio_format'] == 'vorbis')
# Every format must pair its own encoder with the filters; ACODECS leaves the
# encoder blank for wav/alac, and a blank -c:a would be a broken command line.
for fmt in config.AUDIO_FORMATS:
    args = opts_for(audio_extract=True, audio_format=fmt,
                    audio_tempo='75')['postprocessor_args']['extractaudio']
    check(f'{fmt} filters carry an explicit encoder',
          args[0] == '-c:a' and args[1] == audio_encoder_for(fmt), args[:2])

print()
print('=== clip ranges land in the filename ===')
o = opts_for(clip=(90.0, 165.0))
got = name_for(o, {'title': 'T', 'id': 'x', 'ext': 'mp4',
                   'section_start': 90.0, 'section_end': 165.0})
check('a clip range is written into the name',
      got.endswith('T [x] [clip 00h01m30s-00h02m45s].mp4'), got)
got = name_for(o, {'title': 'T', 'id': 'x', 'ext': 'mp4',
                   'section_start': 90.0, 'section_end': None})
check('an open-ended clip says "end"', got.endswith('-end].mp4'), got)
got = name_for(opts_for(), {'title': 'T', 'id': 'x', 'ext': 'mp4'})
check('no clip range means no suffix', got.endswith('T [x].mp4'), got)
got = name_for(opts_for(audio_extract=True, audio_tempo='75', audio_transpose='2'),
               {'title': 'T', 'id': 'x', 'ext': 'mp3'})
check('a practice copy is named so it cannot overwrite the original',
      got.endswith('T [x] [75% speed] [+2 st].mp3'), got)

print()
print('=== split chapters land in the output folder ===')
o = opts_for(split_chapters=True)
got = name_for(o, {'title': 'Album', 'id': 'x', 'ext': 'mp3',
                   'section_number': 1, 'section_title': 'Track One'}, 'chapter')
check('the chapter template is set at all', 'chapter' in o['outtmpl'])
check('a chapter file goes to the chosen folder, not the CWD',
      os.path.dirname(got) == config.DEFAULT_SETTINGS['output_dir'], got)
check('a chapter file keeps the chosen filename layout',
      got.endswith('Album [x] - 001 Track One.mp3'), os.path.basename(got))

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
o = opts_for(clip=(90.0, 165.0), clip_precise=True)
rng = list(o['download_ranges']({'duration': 600}, None))
check('download_ranges yields the window',
      rng == [{'start_time': 90.0, 'end_time': 165.0}], rng)
check('force_keyframes_at_cuts travels with it',
      o.get('force_keyframes_at_cuts') is True)
check('the sidebar boxes alone do not clip anything',
      'download_ranges' not in opts_for(clip_start='1:30', clip_end='2:45'))

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

    print()
    print('=== every converter preset encodes ===')
    for name in config.CONV_PRESETS:
        s = config.apply_conv_preset(config.DEFAULT_SETTINGS, name)
        built = build_conv_args(s, WARNINGS.append, WARNINGS.append)
        if not built:
            check(name, False, 'builder returned nothing')
            continue
        in_args, out_args, ext = built
        out = os.path.join(work, f'p{abs(hash(name))}.{ext}')
        r = subprocess.run([FF, '-hide_banner', '-nostdin', '-y'] + in_args +
                           ['-i', src] + out_args + [out],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', **no_window_kwargs())
        ok = r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0
        check(name, ok, '' if ok else
              ((r.stderr or '').strip().splitlines() or ['?'])[-1][:90])

    print()
    print('=== practice filter chains are valid ffmpeg ===')
    for tempo, steps, mode in ((100, 0, 'Stereo'), (50, 0, 'Stereo'),
                               (75, 5, 'Mono (mix)'), (100, -12, 'Left only'),
                               (150, 0, 'Right only'), (50, 12, 'Stereo')):
        chain = build_audio_filter_chain(
            tempo, steps, config.AUDIO_CHANNEL_MODES[mode],
            'I=-16:TP=-1.5:LRA=11')
        ok, tail = ffmpeg_accepts(FF, ['-af', chain] if chain else [])
        check(f'chain {tempo}% {steps:+d}st {mode}', ok, tail)

    print()
    print('=== audio filters survive a stream copy ===')
    # ExtractAudio emits `-acodec copy` whenever the source is already in the
    # target codec. Filters against a copied stream fail outright unless an
    # explicit encoder comes with them — the exact shape yt-dlp builds.
    for fmt in config.AUDIO_FORMATS:
        ext, encoder, more = ACODECS[fmt]
        probe_src = os.path.join(work, f'copy_src.{ext}')
        subprocess.run([FF, '-y', '-hide_banner', '-loglevel', 'error',
                        '-f', 'lavfi', '-i', 'sine=f=440:d=1']
                       + (['-c:a', encoder] if encoder else []) + [probe_src],
                       check=True, **no_window_kwargs())
        extra = opts_for(audio_extract=True, audio_format=fmt,
                         audio_normalize=True, audio_tempo='75'
                         )['postprocessor_args']['extractaudio']
        out = os.path.join(work, f'copy_out.{ext}')
        r = subprocess.run([FF, '-hide_banner', '-nostdin', '-y', '-i', probe_src,
                            '-vn', '-acodec', 'copy', *more,
                            '-movflags', '+faststart', *extra, out],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', **no_window_kwargs())
        ok = r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0
        check(f'{fmt}: filters + stream copy', ok, '' if ok else
              ((r.stderr or '').strip().splitlines() or ['?'])[-1][:90])

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
