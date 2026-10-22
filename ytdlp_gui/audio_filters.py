"""Building the ffmpeg `-af` chain for the practice / loudness controls.

tkinter-free like download.py and convert.py: every input is a plain value, so
the chain can be exercised against a real ffmpeg without opening a window.

Filter order is not arbitrary:

    pan  →  pitch (asetrate + aresample)  →  atempo  →  loudnorm

* `pan` goes first so everything downstream sees the layout that was asked for.
  Isolating one side of a stereo mix is how you pull a guitar that was hard-
  panned in a 60s recording out from under the vocal.
* Pitch is shifted by *resampling*: `asetrate` reinterprets the sample rate,
  which moves pitch and speed together, and the speed part is then folded into
  the `atempo` factor below. `rubberband` sounds better but is missing from
  many ffmpeg builds, so it is deliberately not used.
* `atempo` only accepts a factor in [0.5, 2.0]. Anything outside that range is
  split into several chained stages.
* `loudnorm` is last because it measures whatever it is handed; running it
  before a tempo or pitch change would normalise the wrong signal.

Note that `-af` can only be given once — a second one silently replaces the
first — which is why every effect has to be merged here rather than appended
by each feature separately.
"""

_PITCH_RATE = 48000      # fixed working rate, so asetrate has a known base


def _atempo_stages(factor: float) -> list:
    """Split a tempo factor into stages ffmpeg's atempo will accept.

    atempo clamps to [0.5, 2.0]; 0.25x has to be expressed as 0.5 × 0.5.
    """
    stages: list = []
    x = float(factor)
    if x <= 0:
        return []
    while x > 2.0 + 1e-9:
        stages.append(2.0)
        x /= 2.0
    while x < 0.5 - 1e-9:
        stages.append(0.5)
        x *= 2.0
    if abs(x - 1.0) > 1e-6:
        stages.append(x)
    return stages


def _num(value, default):
    try:
        return float(str(value).strip().rstrip('%'))
    except (TypeError, ValueError):
        return default


def build_audio_filter_chain(tempo_pct=100, semitones=0, channel_filter='',
                             loudnorm='') -> str:
    """Return a single `-af` value, or '' when nothing is active.

    `channel_filter` is a ready-made `pan=…` string (see
    config.AUDIO_CHANNEL_MODES); `loudnorm` is the parameter tail such as
    'I=-16:TP=-1.5:LRA=11', or 'loudnorm' on its own for plain defaults.
    """
    tempo = _num(tempo_pct, 100.0)
    steps = _num(semitones, 0.0)
    parts: list = []

    if channel_filter:
        # Force stereo first: `pan=mono|c0=c1` against a mono source has no
        # right channel to take, and an upmix is cheaper than a special case.
        parts += ['aformat=channel_layouts=stereo', channel_filter]

    ratio = 2.0 ** (steps / 12.0)          # pitch multiplier
    if abs(steps) > 1e-9:
        parts += [f'aresample={_PITCH_RATE}',
                  f'asetrate={int(round(_PITCH_RATE * ratio))}',
                  f'aresample={_PITCH_RATE}']

    # asetrate already sped the audio up by `ratio`, so the tempo correction
    # has to divide it back out on top of whatever the user asked for.
    for stage in _atempo_stages((tempo / 100.0) / ratio):
        parts.append(f'atempo={stage:.6f}'.rstrip('0').rstrip('.'))

    if loudnorm:
        parts.append(loudnorm if loudnorm.startswith('loudnorm')
                     else f'loudnorm={loudnorm}')
        # loudnorm upsamples its output to 192 kHz internally; bring it back
        # to something sane so FLAC/WAV/AAC/ProRes files aren't hugely inflated.
        parts.append(f'aresample={_PITCH_RATE}')

    return ','.join(parts)


def practice_suffix(tempo_pct=100, semitones=0) -> str:
    """' [75% speed] [+2 st]' — keeps a slowed copy from overwriting the normal
    one, and makes the file self-describing once it is out of the app."""
    tempo = _num(tempo_pct, 100.0)
    steps = _num(semitones, 0.0)
    bits = []
    if abs(tempo - 100.0) > 1e-9:
        bits.append(f'[{tempo:g}% speed]')
    if abs(steps) > 1e-9:
        bits.append(f'[{steps:+g} st]')
    return (' ' + ' '.join(bits)) if bits else ''
