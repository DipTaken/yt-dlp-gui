"""Building the yt-dlp options dict.

Deliberately free of tkinter: everything the UI supplies — the ffmpeg path, a
`warn` callback, the logger and the two hooks — arrives as an argument. That
keeps these functions directly testable without spawning a window.

The yt-dlp API gotchas encoded here are documented in CLAUDE.md; read them
before changing anything.
"""
import os
import re

from yt_dlp.postprocessor import MetadataParserPP
from yt_dlp.postprocessor.ffmpeg import ACODECS
from yt_dlp.utils import DateRange, download_range_func, parse_bytes, parse_duration

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

    audio_only = bool(s['audio_extract']) or preset == 'Audio Only (Best)'

    if item.format_override:
        # Chosen explicitly in the info popup; respect it verbatim and skip
        # every preference below, otherwise sorting could override the pick.
        return {'format': item.format_override}

    if audio_only:
        return {'format': 'ba/b', 'format_sort': ['acodec:aac', 'abr']}

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

def build_postprocessors(s: dict) -> tuple:
    """Build the postprocessor chain.

    Order matters and mirrors yt-dlp's own CLI (yt_dlp/__init__.py,
    get_postprocessors): extract audio → embed subs → modify chapters →
    metadata → embed thumbnail. Running EmbedThumbnail before FFmpegMetadata
    (as this file used to) makes the metadata pass remux the file and drop
    the artwork that was just embedded.

    Returns (postprocessors, needs_thumbnail_download).
    """
    pp: list[dict] = []
    want_thumb_file = bool(s['write_thumbnail'])
    embed_thumb     = bool(s['embed_thumbnail'])
    want_subs       = bool(s['write_subs'] or s['auto_subs'])
    embed_subs      = bool(s['embed_subs'] and want_subs)

    sb_cats: list = []
    # SponsorBlock queries the API before download so later passes can act
    # on the chapter data ('after_filter' is what the CLI uses).
    if s['sponsorblock_enabled']:
        sb_cats = [c.strip() for c in s['sponsorblock_cats'].split(',') if c.strip()]
        if not sb_cats or 'all' in sb_cats:
            sb_cats = ['sponsor', 'intro', 'outro', 'selfpromo',
                       'interaction', 'music_offtopic', 'preview', 'filler']
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

    if s['audio_extract']:
        quality = s['audio_quality']
        if quality == 'best':
            quality = '0'          # 0 = best VBR for the target encoder
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

    if s['embed_metadata']:
        pp.append({'key': 'FFmpegMetadata',
                   'add_metadata': True,
                   'add_chapters': True})

    if embed_thumb:
        # already_have_thumbnail=True stops yt-dlp deleting a thumbnail the
        # user explicitly asked to keep.
        pp.append({'key': 'EmbedThumbnail',
                   'already_have_thumbnail': want_thumb_file})
        # The thumbnail has to be on disk before it can be embedded. yt-dlp's
        # CLI force-enables this; doing it by hand is required here, and its
        # absence is why "Embed Thumbnail" alone silently did nothing.
        want_thumb_file = True

    # Splitting runs last so each piece inherits the finished file's tags,
    # chapters and artwork.
    if s.get('split_chapters'):
        pp.append({'key': 'FFmpegSplitChapters',
                   'force_keyframes': bool(s.get('clip_precise'))})

    return pp, want_thumb_file

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
    pp, want_thumb_file = build_postprocessors(s)

    template = s['output_template'] or '%(title)s [%(id)s].%(ext)s'

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

    # Audio postprocessor extra args (practice filters / normalize / rate).
    # Keys are matched case-insensitively against a *lower-cased* pp key, so
    # 'FFmpegExtractAudio' — the old value here — never matched anything and
    # both options were silently discarded.
    if s['audio_extract']:
        pp_extra: list[str] = []
        chain = build_audio_filter_chain(
            s.get('audio_tempo', '100'), s.get('audio_transpose', '0'),
            AUDIO_CHANNEL_MODES.get(s.get('audio_channels', 'Stereo'), ''),
            'loudnorm' if s.get('audio_normalize') else '')
        if chain:
            pp_extra += ['-af', chain]
        if s.get('audio_sample_rate'):
            pp_extra += ['-ar', str(s['audio_sample_rate'])]
        if pp_extra:
            # ExtractAudio stream-copies whenever the source is already in the
            # target codec (every m4a/opus download from YouTube), emitting
            # `-acodec copy`. Filters against a copied stream make ffmpeg exit
            # with "Invalid argument" and lose the file, so an explicit encoder
            # — which wins, being last on the command line — has to come with
            # them. Verified against ffmpeg 8.1.
            encoder = audio_encoder_for(s['audio_format'])
            prefix = ['-c:a', encoder] if encoder else []
            opts['postprocessor_args'] = {'extractaudio': prefix + pp_extra}

    # FFmpeg location
    if ffmpeg_path:
        opts['ffmpeg_location'] = os.path.dirname(ffmpeg_path)
    elif pp or opts.get('merge_output_format'):
        warn('FFmpeg not found — merging, audio extraction, '
                   'thumbnail and subtitle embedding will not work.')

    # ── Numeric / typed options ──────────────────────────────────────────
    # ratelimit and max_filesize are bytes *numbers* in yt-dlp; handing them
    # the raw '2M' / '500M' strings from the UI raised a TypeError mid-run.
    if s['rate_limit']:
        limit = parse_bytes(s['rate_limit'])
        if limit:
            opts['ratelimit'] = limit
        else:
            warn(f'Could not parse rate limit {s["rate_limit"]!r} — ignoring.')
    if s['max_filesize']:
        maxfs = parse_bytes(s['max_filesize'])
        if maxfs:
            opts['max_filesize'] = maxfs
        else:
            warn(f'Could not parse max filesize {s["max_filesize"]!r} — ignoring.')

    if s['proxy']:
        opts['proxy'] = s['proxy']

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
        try:
            opts['daterange'] = DateRange(s['date_after'] or None,
                                          s['date_before'] or None)
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

    if s['cookie_browser']:
        opts['cookiesfrombrowser'] = (s['cookie_browser'],)
    if s['cookie_file']:
        if os.path.isfile(s['cookie_file']):
            opts['cookiefile'] = s['cookie_file']
        else:
            warn(f'Cookie file not found, ignoring: {s["cookie_file"]}')

    return opts
