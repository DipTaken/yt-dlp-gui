"""Building the yt-dlp options dict.

Deliberately free of tkinter: everything the UI supplies — the ffmpeg path, a
`warn` callback, the logger and the two hooks — arrives as an argument. That
keeps these functions directly testable without spawning a window.

The yt-dlp API gotchas encoded here are documented in CLAUDE.md; read them
before changing anything.
"""
import os
import re

from yt_dlp.dependencies import mutagen
from yt_dlp.postprocessor import MetadataParserPP
from yt_dlp.postprocessor.ffmpeg import ACODECS, FFmpegExtractAudioPP
from yt_dlp.postprocessor.sponsorblock import SponsorBlockPP
from yt_dlp.utils import (
    DateRange, download_range_func, parse_bytes, parse_duration, parse_filesize,
)

from .audio_filters import build_audio_filter_chain, practice_suffix
from .config import (
    AUDIO_CHANNEL_MODES, AUDIO_ENCODER_OVERRIDES, CHAPTER_SUFFIX_TMPL,
    CLIP_SUFFIX_TMPL, COMPAT_MODES, DEFAULT_COMPAT_MODE, FORMAT_PRESETS,
)

# The tail every sane output template ends with. Suffixes have to go *before*
# it or they would land after the file extension.
_EXT_TAIL_RE = re.compile(r'\.%\(ext\)s$')


def insert_before_ext(template: str, suffix: str, warn=None) -> str:
    """Splice `suffix` in ahead of the `.%(ext)s` tail.

    A hand-written template need not end that way; appending is then the only
    thing we can do, and it is worth saying so, because the result will not
    have a usable extension.
    """
    if not suffix:
        return template
    if _EXT_TAIL_RE.search(template):
        return _EXT_TAIL_RE.sub(suffix + '.%(ext)s', template)
    if warn:
        warn(f'Filename template does not end in ".%(ext)s" — appending '
             f'"{suffix.strip()}" to the end instead.')
    return template + suffix


def audio_encoder_for(fmt: str) -> str:
    """The ffmpeg encoder yt-dlp would use for an audio-extraction target.

    ACODECS leaves the encoder empty for the two formats it treats specially,
    so those are filled in here. Needed because any `-af`/`-ar` of ours has to
    be paired with an explicit `-c:a` — see build_ydl_opts.
    """
    if fmt in AUDIO_ENCODER_OVERRIDES:
        return AUDIO_ENCODER_OVERRIDES[fmt]
    entry = ACODECS.get(fmt)
    return (entry[1] if entry else '') or ''


def int_or_none(value) -> 'int | None':
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def build_format_opts(item, s: dict, warn) -> dict:
    """Format selector + codec/container policy.

    Returns the subset of ydl_opts governing what gets downloaded and what
    container it ends up in. Kept separate from _build_ydl_opts so it can be
    reasoned about (and tested) on its own.
    """
    preset = s['format_preset']
    selector, height_cap = FORMAT_PRESETS.get(
        preset, FORMAT_PRESETS['Best (Video + Audio)'])

    # With "keep the original video" on, the video has to be downloaded too;
    # an audio-only selector would leave nothing to keep.
    audio_only = ((bool(s['audio_extract']) and not s.get('keep_video'))
                  or preset == 'Audio Only (Best)')

    if item.format_override:
        # Chosen explicitly in the info popup; respect it verbatim and skip
        # every preference below, otherwise sorting could override the pick.
        return {'format': item.format_override}

    if audio_only:
        # Prefer the source that needs the least work for the target: AAC for
        # the MP4-family targets (and plain audio-only downloads), Opus for
        # Opus, yt-dlp's own ranking for anything else.
        fmt = s.get('audio_format')
        if fmt in ('m4a', 'aac', 'alac') or not s['audio_extract']:
            sort = ['acodec:aac', 'abr']
        elif fmt == 'opus':
            sort = ['acodec:opus', 'abr']
        else:
            sort = []
        out = {'format': 'ba/b'}
        if sort:
            out['format_sort'] = sort
        return out

    if preset == 'Custom…':
        custom = (s['custom_format'] or '').strip()
        if not custom:
            warn('Custom format string is empty — using "bv*+ba/b".')
            custom = 'bv*+ba/b'
        # A hand-written selector is an explicit instruction: don't layer a
        # container preference on top of it.
        return {'format': custom}

    mode = COMPAT_MODES.get(s.get('compat_mode') or '',
                            COMPAT_MODES[DEFAULT_COMPAT_MODE])
    # Resolution cap goes first so it outranks the codec preference; see the
    # FORMAT_PRESETS comment for why this is a sort field, not a filter.
    sort = ([f'res:{height_cap}'] if height_cap else []) + list(mode['sort'])

    out: dict = {'format': selector}
    if sort:
        out['format_sort'] = sort
    if mode['merge']:
        out['merge_output_format'] = mode['merge']
    return out

# Sample rates each lossy encoder will actually accept.
_ENCODER_RATES = {
    'opus': {48000, 24000, 16000, 12000, 8000},
    'mp3':  {8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000},
}


def _audio_quality_args(fmt: str, quality) -> list:
    """Bitrate/quality flags for the practice re-encode.

    The practice pass re-encodes after ExtractAudio, so ExtractAudio's own
    quality setting no longer applies to the final file; restate it here.
    """
    if fmt in ('flac', 'wav', 'alac'):
        return []
    q = str(quality or '').strip()
    if q in ('', 'best', '0'):
        return {'mp3': ['-q:a', '0'], 'vorbis': ['-q:a', '10']}.get(
            fmt, ['-b:a', '256k'])
    if q.isdigit():
        return ['-b:a', f'{q}k']
    return []


def build_practice_args(s: dict, warn=None) -> list:
    """ffmpeg arguments for the practice / loudness / sample-rate pass.

    Returned as the `copystream` postprocessor_args. They go to a dedicated
    FFmpegCopyStream PP rather than to ExtractAudio because ExtractAudio
    returns early (doing nothing at all) when the source is already in the
    target codec, which silently discarded the filters for m4a/aac and for an
    mp3 source. The args are always paired with an explicit `-c:a`: the copy
    PP starts from `-c copy`, and filters against a copied stream make ffmpeg
    exit with "Invalid argument". Ours win, being appended last.
    """
    if not s['audio_extract']:
        return []
    fmt = s['audio_format']
    chain = build_audio_filter_chain(
        s.get('audio_tempo', '100'), s.get('audio_transpose', '0'),
        AUDIO_CHANNEL_MODES.get(s.get('audio_channels', 'Stereo'), ''),
        'loudnorm' if s.get('audio_normalize') else '')
    rate = str(s.get('audio_sample_rate') or '').strip()
    if rate:
        try:
            r = int(rate)
        except ValueError:
            r = None
        key = fmt if fmt in _ENCODER_RATES else None
        if r is not None and key and r not in _ENCODER_RATES[key]:
            if warn:
                warn(f'{fmt} cannot be {r} Hz — using 48000.')
            rate = '48000'
    if not chain and not rate:
        return []

    quality = s.get('audio_quality')
    if quality == 'best' and fmt == 'opus':
        quality = '256'
    args: list = []
    encoder = audio_encoder_for(fmt)
    if encoder:
        args += ['-c:a', encoder]
    args += _audio_quality_args(fmt, quality)
    if chain:
        args += ['-af', chain]
    if rate:
        args += ['-ar', rate]
    return args


def _tempo_changed(s: dict) -> bool:
    try:
        tempo = float(str(s.get('audio_tempo', '100')).strip().rstrip('%'))
    except ValueError:
        return False
    return bool(s['audio_extract']) and abs(tempo - 100.0) > 1e-9


def plan_postprocessors(s: dict, warn=None) -> dict:
    """Build the postprocessor chain and the data that goes with it.

    Order matters and mirrors yt-dlp's own CLI (yt_dlp/__init__.py,
    get_postprocessors): SponsorBlock → extract audio → embed subs → modify
    chapters → [practice re-encode] → metadata → embed thumbnail → split.
    Running EmbedThumbnail before FFmpegMetadata makes the metadata pass remux
    the file and drop the artwork that was just embedded.

    Returns a dict: `pp`, `want_thumb_file`, `forced_thumb` (the thumbnail is
    written only so it can be embedded) and `practice_args`.
    """
    def say(msg):
        if warn:
            warn(msg)

    pp: list[dict] = []
    want_thumb_file = bool(s['write_thumbnail'])
    embed_thumb     = bool(s['embed_thumbnail'])
    want_subs       = bool(s['write_subs'] or s['auto_subs'])
    embed_subs      = bool(s['embed_subs'] and want_subs)
    sped            = _tempo_changed(s)

    # Some audio containers cannot carry cover art, and a failed embed fails
    # the whole item (no post_hooks fire, the card goes red). Skip it up front.
    if embed_thumb and s['audio_extract']:
        tgt = ACODECS.get(s['audio_format'], (None,))[0]
        if tgt == 'wav':
            say('Cannot embed a thumbnail in .wav — skipping.')
            embed_thumb = False
        elif tgt in ('ogg', 'opus', 'flac') and not mutagen:
            say(f'Cannot embed a thumbnail in .{tgt} (mutagen not installed) '
                '— skipping.')
            embed_thumb = False

    sb_cats: list = []
    # SponsorBlock queries the API before download so later passes can act
    # on the chapter data ('after_filter' is what the CLI uses).
    if s['sponsorblock_enabled']:
        valid = [c for c in SponsorBlockPP.CATEGORIES
                 if c not in SponsorBlockPP.NON_SKIPPABLE_CATEGORIES]
        typed = [c.strip() for c in (s['sponsorblock_cats'] or '').split(',')
                 if c.strip()]
        if not typed or 'all' in typed:
            sb_cats = valid
        else:
            sb_cats = [c for c in typed if c in valid]
            unknown = [c for c in typed if c not in valid]
            if unknown:
                say(f'Unknown SponsorBlock categories ignored: {", ".join(unknown)}.')
            if not sb_cats:
                sb_cats = valid
        pp.append({'key': 'SponsorBlock', 'categories': sb_cats,
                   'when': 'after_filter'})

    # Parse "Artist - Title" out of the video title into real tag fields.
    # FFmpegMetadata later maps artist/track onto ID3/MP4 tags, so this is
    # what turns a downloaded music video into a properly tagged track.
    if s.get('music_tags'):
        pp.append({'key': 'MetadataParser',
                   'when': 'pre_process',
                   'actions': [(MetadataParserPP.Actions.INTERPRET,
                                'title', r'(?P<artist>.+?) [-–—] (?P<track>.+)')]})

    # Subtitle conversion has to happen before anything embeds them.
    sub_fmt = (s.get('sub_convert') or '').strip()
    if want_subs and sub_fmt and not sub_fmt.startswith('('):
        pp.append({'key': 'FFmpegSubtitlesConvertor',
                   'format': sub_fmt, 'when': 'before_dl'})

    # A thumbnail kept on disk can be normalised to jpg (opt-in).
    if s['write_thumbnail'] and s.get('thumbnail_to_jpg'):
        pp.append({'key': 'FFmpegThumbnailsConvertor',
                   'format': 'jpg', 'when': 'before_dl'})

    if s['audio_extract']:
        quality = s['audio_quality']
        if quality == 'best':
            # 0 = best VBR for the target encoder; libopus has no VBR scale
            # and would fall back to ~96k, so ask for a real bitrate there.
            quality = '256' if s['audio_format'] == 'opus' else '0'
        pp.append({'key': 'FFmpegExtractAudio',
                   'preferredcodec': s['audio_format'],
                   'preferredquality': quality})

    if embed_subs:
        # already_have_subtitle keeps the standalone .srt/.vtt when the user
        # asked to *save* subtitles as well as embed them.
        pp.append({'key': 'FFmpegEmbedSubtitle',
                   'already_have_subtitle': bool(s['write_subs'])})

    # ModifyChapters must precede FFmpegMetadata so the rewritten chapter
    # list is what gets written into the container.
    if s['sponsorblock_enabled']:
        removing = s.get('sponsorblock_mode') == 'Remove from file'
        pp.append({'key': 'ModifyChapters',
                   'sponsorblock_chapter_title': '[SponsorBlock]: %(category_names)l',
                   # Non-empty = physically cut these categories out.
                   'remove_sponsor_segments': sb_cats if removing else [],
                   'remove_ranges': [],
                   'force_keyframes': bool(s.get('clip_precise')) if removing else False})

    # The practice re-encode runs after every pass that works in original
    # timestamps (SponsorBlock cuts) and before the metadata pass.
    practice_args = build_practice_args(s, warn)
    if practice_args:
        if sped:
            # Chapter times describe the original; after a speed change they
            # would all be wrong, so drop them rather than write bad ones.
            practice_args += ['-map_chapters', '-1']
        pp.append({'key': 'FFmpegCopyStream'})

    # Mark-as-chapters needs FFmpegMetadata too, even with tags switched off:
    # it is the only postprocessor that writes chapters into the file.
    marking = (s['sponsorblock_enabled']
               and s.get('sponsorblock_mode') != 'Remove from file')
    add_chapters = not sped
    if s['embed_metadata'] or (marking and add_chapters):
        pp.append({'key': 'FFmpegMetadata',
                   'add_metadata': bool(s['embed_metadata']),
                   'add_chapters': add_chapters})

    forced_thumb = False
    if embed_thumb:
        # already_have_thumbnail=True stops yt-dlp deleting a thumbnail the
        # user explicitly asked to keep.
        pp.append({'key': 'EmbedThumbnail',
                   'already_have_thumbnail': want_thumb_file})
        # The thumbnail has to be on disk before it can be embedded. yt-dlp's
        # CLI force-enables it; doing it by hand is required here, and its
        # absence is why "Embed Thumbnail" alone silently did nothing.
        forced_thumb = not want_thumb_file
        want_thumb_file = True

    # Splitting runs last so each piece inherits the finished file's tags,
    # chapters and artwork.
    if s.get('split_chapters'):
        if sped:
            say('Split chapters is off for a speed-changed copy.')
        else:
            pp.append({'key': 'FFmpegSplitChapters',
                       'force_keyframes': bool(s.get('clip_precise'))})

    return {'pp': pp, 'want_thumb_file': want_thumb_file,
            'forced_thumb': forced_thumb, 'practice_args': practice_args}


def build_postprocessors(s: dict, warn=None) -> tuple:
    """Compatibility wrapper: (postprocessors, needs_thumbnail_download)."""
    plan = plan_postprocessors(s, warn)
    return plan['pp'], plan['want_thumb_file']


def network_opts(s: dict, warn=None) -> dict:
    """Proxy + cookie options, shared by every place that talks to yt-dlp."""
    opts: dict = {}
    if s.get('proxy'):
        opts['proxy'] = s['proxy']
    browser = (s.get('cookie_browser') or '').strip()
    if browser:
        # "chrome", "chrome:Profile 1" or "firefox+gnomekeyring:default"
        name, _, profile = browser.partition(':')
        name, _, keyring = name.partition('+')
        name = name.strip().lower()
        from yt_dlp.cookies import SUPPORTED_BROWSERS
        if name in SUPPORTED_BROWSERS:
            opts['cookiesfrombrowser'] = (
                name, profile.strip() or None, keyring.strip().upper() or None)
        elif warn:
            warn(f'Unknown cookie browser, ignoring: {name or browser}')
    cookie_file = s.get('cookie_file')
    if cookie_file:
        if os.path.isfile(cookie_file):
            opts['cookiefile'] = cookie_file
        elif warn:
            warn(f'Cookie file not found, ignoring: {cookie_file}')
    return opts


def _bytes_or_none(raw):
    raw = str(raw).strip()
    return parse_bytes(raw) or parse_filesize(raw)


def _pos_number(value):
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def parse_clip_range(s: dict, warn) -> 'tuple[float, float] | None':
    """Turn the Start/End boxes into (start_secs, end_secs), or None.

    Accepts '90', '1:30' and '00:01:30'. An empty End means "to the end of
    the video", which download_range_func expresses as infinity.
    """
    raw_start = (s.get('clip_start') or '').strip()
    raw_end   = (s.get('clip_end') or '').strip()
    if not raw_start and not raw_end:
        return None

    start = 0.0
    if raw_start:
        parsed = parse_duration(raw_start)
        if parsed is None:
            warn(f'Could not read clip start {raw_start!r} — ignoring clip range.')
            return None
        start = parsed

    end = float('inf')
    if raw_end:
        parsed = parse_duration(raw_end)
        if parsed is None:
            warn(f'Could not read clip end {raw_end!r} — ignoring clip range.')
            return None
        end = parsed

    if end <= start:
        warn(f'Clip end ({raw_end}) is not after start ({raw_start}) — ignoring clip range.')
        return None
    return start, end

def build_ydl_opts(item, s: dict, *, ffmpeg_path: str, logger,
                   progress_hook, pp_hook, warn, post_hook=None) -> dict:
    plan = plan_postprocessors(s, warn)
    pp, want_thumb_file = plan['pp'], plan['want_thumb_file']
    practice_args = plan['practice_args']

    template = (s['output_template'] or '').strip() or '%(title)s [%(id)s].%(ext)s'

    # A clip range has to show up in the name. yt-dlp renders every range
    # through the *default* template, so without this two sections of one
    # video resolve to the same path and the second overwrites the first.
    clip = getattr(item, 'clip_range', None)
    if clip:
        template = insert_before_ext(template, CLIP_SUFFIX_TMPL, warn)

    # Same reasoning for a practice copy: a 75% speed version must not land on
    # top of the normal one. '%' is doubled because this is a template.
    if s['audio_extract']:
        practice = practice_suffix(s.get('audio_tempo', '100'),
                                   s.get('audio_transpose', '0'))
        if practice:
            template = insert_before_ext(template, practice.replace('%', '%%'),
                                         warn)

    sub_langs = [ln.strip() for ln in s['sub_langs'].split(',') if ln.strip()]
    want_subs = bool(s['write_subs'] or s['auto_subs'])
    if want_subs and not sub_langs:
        sub_langs = ['en']

    opts: dict = {
        # 'chapter' is what FFmpegSplitChaptersPP renders each piece through.
        # Leaving it at yt-dlp's default meant split files ignored the chosen
        # filename layout *and* landed in the process working directory.
        'outtmpl': {
            'default': template,
            'chapter': insert_before_ext(template, CHAPTER_SUFFIX_TMPL),
        },
        'postprocessors':    pp,
        'writethumbnail':    want_thumb_file,
        'writeinfojson':     s['write_infojson'],
        'writesubtitles':    s['write_subs'],
        'writeautomaticsub': s['auto_subs'],
        'subtitleslangs':    sub_langs if want_subs else [],
        'noplaylist':        s['no_playlist'],
        # The API default is False, which aborts a whole playlist on the first
        # private or deleted entry. The CLI uses 'only_download'; so do we, and
        # the worker reports "26 of 27" rather than one red card.
        'ignoreerrors':      'only_download',
        'quiet':             True,
        'no_warnings':       False,
        'noprogress':        True,
        # Escape codes would otherwise leak into the log and error text when
        # the app is launched from a colour terminal.
        'color':             {'stdout': 'never', 'stderr': 'never'},
        'logger':            logger,
        'progress_hooks':      [progress_hook],
        'postprocessor_hooks': [pp_hook],
    }
    # An absolute template carries its own location and yt-dlp ignores `paths`
    # for it; otherwise 'home' is what makes *every* output — including the
    # per-chapter files above — land in the chosen folder.
    if not os.path.isabs(template):
        opts['paths'] = {'home': s['output_dir']}
    if post_hook is not None:
        opts['post_hooks'] = [post_hook]
    opts.update(build_format_opts(item, s, warn))

    # Practice filters / normalize / sample rate. Keyed 'copystream' (the
    # lower-cased PP name without 'FFmpeg' or 'PP'), and carried by a dedicated
    # FFmpegCopyStream pass because ExtractAudio does nothing when the source is
    # already in the target codec. See build_practice_args.
    if practice_args:
        opts['postprocessor_args'] = {'copystream': practice_args}

    if plan['forced_thumb']:
        # The thumbnail is only on disk to be embedded; like the CLI, don't let
        # yt-dlp also write the playlist's thumbnail through the default template.
        opts['outtmpl']['pl_thumbnail'] = ''

    # Mirrors the CLI: lets yt-dlp recognise an existing `Title [id].mp3` as
    # already downloaded instead of fetching the source again.
    if s['audio_extract'] and s['audio_format'] in FFmpegExtractAudioPP.SUPPORTED_EXTS:
        opts['final_ext'] = s['audio_format']

    # FFmpeg location. A file path is fine: yt-dlp finds ffprobe beside it, and
    # unlike the bare folder this still works for a custom-named executable.
    if ffmpeg_path:
        opts['ffmpeg_location'] = ffmpeg_path
    elif pp or opts.get('merge_output_format'):
        warn('FFmpeg not found — merging, audio extraction, '
             'thumbnail and subtitle embedding will not work.')

    # ── Numeric / typed options ──────────────────────────────────────────
    # ratelimit and max_filesize are bytes *numbers* in yt-dlp; handing them
    # the raw '2M' / '500M' strings from the UI raised a TypeError mid-run.
    if s['rate_limit']:
        limit = _bytes_or_none(s['rate_limit'])
        if limit:
            opts['ratelimit'] = limit
        else:
            warn(f'Could not parse rate limit {s["rate_limit"]!r} — ignoring.')
    if s['max_filesize']:
        maxfs = _bytes_or_none(s['max_filesize'])
        if maxfs:
            opts['max_filesize'] = maxfs
        else:
            warn(f'Could not parse max filesize {s["max_filesize"]!r} — ignoring.')

    opts.update(network_opts(s, warn))

    retries = int_or_none(s['retries'])
    if retries is not None:
        opts['retries'] = retries
        opts['fragment_retries'] = retries
    frags = int_or_none(s['concurrent_fragments'])
    if frags is not None and frags > 0:
        opts['concurrent_fragment_downloads'] = frags

    # yt-dlp takes a single DateRange under 'daterange'. The old
    # 'dateafter'/'datebefore' keys are CLI-only names that YoutubeDL never
    # reads, so both date filters used to do nothing at all.
    if s['date_after'] or s['date_before']:
        def _date(v):
            # 2024-01-01 -> 20240101; yt-dlp's own parser rejects the dashes.
            v = (v or '').strip()
            return re.sub(r'^(\d{4})-(\d{2})-(\d{2})$', r'\1\2\3', v) or None
        try:
            opts['daterange'] = DateRange(_date(s['date_after']),
                                          _date(s['date_before']))
        except Exception as exc:
            warn(f'Invalid date filter ({exc}) — ignoring.')

    # ── Clip range ───────────────────────────────────────────────────────
    # Read off the item, never the settings: the sidebar boxes are only the
    # default handed to *new* queue entries, so that one video can be queued
    # twice with two different ranges.
    if clip:
        start, end = clip
        opts['download_ranges'] = download_range_func([], [(start, end)])
        # Without this the cut snaps to the nearest keyframe, which on a
        # 2-second GOP can land seconds away from what the user typed.
        opts['force_keyframes_at_cuts'] = bool(s.get('clip_precise'))

    if s.get('keep_video') and s['audio_extract']:
        opts['keepvideo'] = True

    # ── Download archive ─────────────────────────────────────────────────
    if s.get('archive_enabled'):
        archive = (s.get('archive_file') or '').strip()
        if not archive:
            archive = os.path.join(s['output_dir'], 'archive.txt')
        try:
            parent = os.path.dirname(os.path.abspath(archive))
            if parent:
                os.makedirs(parent, exist_ok=True)
            opts['download_archive'] = archive
        except OSError as exc:
            warn(f'Cannot use archive file {archive!r} ({exc}) — ignoring.')

    # ── Optional politeness / robustness knobs (all default off) ─────────
    # Read with .get so a settings dict from before these existed still works.
    for key in ('sleep_interval', 'max_sleep_interval', 'sleep_interval_requests'):
        v = _pos_number(s.get(key))
        if v:
            opts[key] = v
    if opts.get('max_sleep_interval') and not opts.get('sleep_interval'):
        # yt-dlp only honours the upper bound alongside a lower one.
        del opts['max_sleep_interval']
    items = (s.get('playlist_items') or '').strip()
    if items:
        opts['playlist_items'] = items
    timeout = _pos_number(s.get('socket_timeout'))
    if timeout:
        opts['socket_timeout'] = timeout
    if s.get('force_ipv4'):
        opts['source_address'] = '0.0.0.0'
    ex_retries = int_or_none(s.get('extractor_retries'))
    if ex_retries is not None and ex_retries >= 0 and s.get('extractor_retries') != '':
        opts['extractor_retries'] = ex_retries
    trim = int_or_none(s.get('trim_file_name'))
    if trim and trim > 0:
        opts['trim_file_name'] = trim
    if s.get('break_on_existing') and 'download_archive' in opts:
        opts['break_on_existing'] = True

    return opts
