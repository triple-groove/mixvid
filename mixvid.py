#!/usr/bin/env python3
"""
mixvid.py - Render a "DJ mix" style visualizer video (album art + tracklist +
            full-mix waveform + moving playhead + live timecode) from a single
            audio file and a JSON tracklist. No video editor required.

Pipeline:
  1. Read the tracklist + audio.
  2. Decode the audio once to a peak envelope (the bottom waveform).
  3. Render ONE static layout per track with Pillow.
  4. Stream raw frames to ffmpeg, redrawing only the moving parts
     (played waveform, playhead, timecode). Audio is muxed in directly.

Usage:
  python3 mixvid.py config.json out.mp4 [--fps 15] [--res 1920x1080]

Config (JSON):
{
  "title":    "Rainsongs Vol.8 | Liquid Drum & Bass Mix",
  "subtitle": "f1rstpers0n #236",
  "date":     "Mar 2026",
  "audio":    "mix.flac",
  "tracks": [
    {"start": "0:00",   "title": "...", "artist": "...", "release": "...", "cover": "covers/01.jpg"},
    {"start": "5:14",   "title": "...", "artist": "...", "release": "...", "cover": "covers/02.jpg"}
  ]
}
"start" accepts "H:MM:SS", "MM:SS", or a plain number of seconds.
Any missing "cover" is replaced by a generated placeholder.
"""

import sys, os, json, subprocess, argparse, hashlib, re, tempfile, glob, shutil, colorsys, time
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# Resolved at startup (see resolve_tools); may be overridden with --ffmpeg/--ffprobe
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# --------------------------------------------------------------------------- #
#  Theme  (tweak everything here)
# --------------------------------------------------------------------------- #
# Named color palettes, selectable with --theme-color. Every theme picks a
# default (rain -> blue, bokeh -> warm); --theme-color overrides it.
PALETTES = {
    # cool blue "rain on window" (the original look)
    "blue": {
        "bg_top":     (12, 22, 34),     # background gradient (top)
        "bg_bottom":  (4, 7, 14),       # background gradient (bottom)
        "panel":      (8, 14, 24),      # bottom bar
        "white":      (240, 244, 248),
        "gray":       (138, 151, 166),
        "dim_gray":   (96, 110, 126),
        "accent":     (63, 160, 216),   # artist names / "blue" text
        "accent2":    (41, 196, 240),   # bright cyan (counter bottom number, playhead)
        "wave_off":   (108, 122, 138),  # un-played waveform bars
        "wave_on":    (224, 234, 242),  # played waveform bars
        "wave_mark":  (60, 190, 235),   # track-start tick on the waveform timeline
        "playhead":   (191, 233, 255),
        "spectrum":   (41, 196, 240),   # live spectrogram bars by the counter
        "dot":        (130, 180, 230),  # rain/ripple grid dots
        "fog":        (30, 70, 116),    # drifting fog aura tint
    },
    # warm amber "bokeh lounge"
    "warm": {
        "bg_top":     (28, 16, 10),
        "bg_bottom":  (6, 3, 3),
        "panel":      (14, 8, 6),
        "white":      (245, 230, 215),
        "gray":       (205, 172, 142),
        "dim_gray":   (150, 124, 100),
        "accent":     (232, 196, 150),  # amber artist / accent text
        "accent2":    (255, 236, 196),  # bright amber (counter, playhead)
        "wave_off":   (120, 98, 74),
        "wave_on":    (240, 214, 168),
        "wave_mark":  (255, 200, 140),
        "playhead":   (255, 236, 196),
        "spectrum":   (240, 184, 124),
        "dot":        (235, 180, 120),
        "fog":        (70, 45, 30),
    },
    # pink / magenta neon
    "pink": {
        "bg_top":     (22, 12, 26),     # dark purple background
        "bg_bottom":  (6, 4, 12),
        "panel":      (16, 8, 18),
        "white":      (246, 240, 248),
        "gray":       (176, 150, 178),
        "dim_gray":   (124, 104, 126),
        "accent":     (236, 92, 182),   # pink artist / accent text
        "accent2":    (255, 122, 212),  # bright magenta (counter, playhead)
        "wave_off":   (112, 92, 116),
        "wave_on":    (242, 186, 222),
        "wave_mark":  (255, 110, 200),
        "playhead":   (255, 170, 226),
        "spectrum":   (255, 116, 204),
        "dot":        (236, 120, 202),  # pink ripple/grid dots
        "fog":        (78, 30, 78),     # magenta fog aura tint
    },
}
# THEME is the *active* palette; main() points it at PALETTES[<chosen>] before
# anything renders, so functions can keep reading THEME["..."] at call time.
THEME = PALETTES["blue"]

# Font files. Sans (Liberation ~ Arial) for the default look; a serif face for
# the bokeh theme. main() repoints FONT_REG/BOLD/THIN to the serifs for --theme
# bokeh, so render code keeps reading the same names.
FONT_REG  = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
FONT_MONO = "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"
# thin/geometric face for the big track counter (lower-right)
FONT_THIN = "/usr/share/fonts/truetype/dejavu/DejaVuSans-ExtraLight.ttf"
# serif faces (bokeh theme)
FONT_SERIF        = "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"
FONT_SERIF_BOLD   = "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"
FONT_SERIF_ITALIC = "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf"
for _f in (FONT_REG, FONT_BOLD, FONT_MONO):
    if not os.path.exists(_f):                      # fall back to DejaVu
        FONT_REG  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        FONT_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
        break
if not os.path.exists(FONT_THIN):
    FONT_THIN = FONT_REG
if not os.path.exists(FONT_SERIF):                  # fall back to DejaVu Serif
    FONT_SERIF        = "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"
    FONT_SERIF_BOLD   = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"
    FONT_SERIF_ITALIC = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf"
if not os.path.exists(FONT_SERIF):                  # last resort: the sans face
    FONT_SERIF = FONT_REG
if not os.path.exists(FONT_SERIF_BOLD):
    FONT_SERIF_BOLD = FONT_BOLD
if not os.path.exists(FONT_SERIF_ITALIC):
    FONT_SERIF_ITALIC = FONT_SERIF

# Glyph fallback for scripts the Latin faces lack (CJK: Japanese/Chinese/Korean).
# Pillow has no automatic per-glyph fallback, so text() splits a string into
# Latin/CJK runs and draws the CJK runs with this face (see _runs / text()).
FONT_FALLBACK = "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"
for _fb in (FONT_FALLBACK,
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"):
    if os.path.exists(_fb):
        FONT_FALLBACK = _fb
        break
else:
    FONT_FALLBACK = None


# --------------------------------------------------------------------------- #
#  Small helpers
# --------------------------------------------------------------------------- #
def parse_time(v):
    if isinstance(v, (int, float)):
        return float(v)
    parts = str(v).strip().split(":")
    parts = [float(p) for p in parts]
    s = 0.0
    for p in parts:
        s = s * 60 + p
    return s


def fmt_time(sec):
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:01d}:{m:02d}:{s:02d}"


def yt_time(sec):
    """Timestamp in YouTube's accepted chapter style: MM:SS, or HH:MM:SS past an
    hour (zero-padded, e.g. 00:00, 03:10, 01:02:03)."""
    sec = max(0, int(round(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def youtube_chapters(tracks):
    """Build YouTube-description chapter markers from parsed cue/JSON tracks.

    Returns one "M:SS Artist - Title" line per track. YouTube's rules for the
    chapters to register: the first marker must be 0:00, there must be at least
    three, and each chapter must be >= 10s. The first marker is forced to 0:00;
    a warning is printed (to stderr) if the source data would violate the
    >=3 / >=10s rules, but the lines are still returned.
    """
    lines, prev = [], None
    for i, t in enumerate(tracks):
        sec = 0.0 if i == 0 else parse_time(t["start"])
        artist = (t.get("artist") or "").strip()
        title = (t.get("title") or "").strip()
        label = f"{artist} - {title}" if artist else (title or "Untitled")
        lines.append(f"{yt_time(sec)} {label}")
        if prev is not None and sec - prev < 10:
            print(f"  NOTE: chapter {i+1} ({yt_time(sec)}) is < 10s after the "
                  f"previous one; YouTube may not register it.", file=sys.stderr)
        prev = sec
    if len(tracks) < 3:
        print("  NOTE: YouTube needs at least 3 chapters to enable chapter markers.",
              file=sys.stderr)
    return "\n".join(lines)


def audio_duration(path):
    out = subprocess.check_output([
        FFPROBE, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path])
    return float(out.strip())


def _find_tool(name, hint):
    """Return a runnable path for ffmpeg/ffprobe, or None. hint may be an exe or a bin dir."""
    cands = []
    if hint:
        cands += ([os.path.join(hint, name), os.path.join(hint, name + ".exe")]
                  if os.path.isdir(hint) else [hint, hint + ".exe"])
    env = os.environ.get(name.upper())          # FFMPEG / FFPROBE env vars
    if env:
        cands += [env, env + ".exe"]
    cands.append(name)                          # bare name -> resolved via PATH
    for c in cands:
        if c and os.path.isfile(c):
            return c
        w = shutil.which(c) if c else None
        if w:
            return w
    return None


def resolve_tools(args):
    """Locate ffmpeg + ffprobe; exit with guidance if missing."""
    global FFMPEG, FFPROBE
    ffmpeg = _find_tool("ffmpeg", getattr(args, "ffmpeg", None))
    ffprobe = _find_tool("ffprobe", getattr(args, "ffprobe", None))
    # if only one was found, look for the other next to it
    if ffmpeg and not ffprobe:
        ffprobe = _find_tool("ffprobe", os.path.dirname(ffmpeg))
    if ffprobe and not ffmpeg:
        ffmpeg = _find_tool("ffmpeg", os.path.dirname(ffprobe))
    if not ffmpeg or not ffprobe:
        win = sys.platform.startswith("win")
        msg = [
            "ERROR: couldn't find ffmpeg/ffprobe. mixvid needs ffmpeg "
            "(which bundles ffprobe) available to run.", ""]
        if win:
            msg += [
                "Install it, then open a NEW terminal so PATH refreshes:",
                "    winget install Gyan.FFmpeg",
                "    (or)  choco install ffmpeg   /   scoop install ffmpeg", "",
                "Or download a build from https://www.gyan.dev/ffmpeg/builds/, unzip it,",
                "and point mixvid at its bin folder:",
                '    python mixvid.py ... --ffmpeg "C:\\ffmpeg\\bin"']
        else:
            msg += ["Install it, e.g.:  sudo dnf install ffmpeg   /   sudo apt install ffmpeg",
                    "or point mixvid at it:  --ffmpeg /path/to/bin"]
        print("\n".join(msg), file=sys.stderr)
        sys.exit(1)
    FFMPEG, FFPROBE = ffmpeg, ffprobe


_font_cache = {}


def font(path, size):
    key = (path, int(size))
    f = _font_cache.get(key)
    if f is None:
        f = ImageFont.truetype(path, int(size))
        _font_cache[key] = f
    return f


def _needs_fallback(ch):
    """True for codepoints the Latin faces don't cover (CJK / fullwidth forms)."""
    o = ord(ch)
    return (0x3000 <= o <= 0x9FFF       # CJK symbols, Hiragana, Katakana, ideographs
            or 0xAC00 <= o <= 0xD7A3    # Hangul syllables
            or 0xF900 <= o <= 0xFAFF    # CJK compatibility ideographs
            or 0xFF00 <= o <= 0xFFEF    # halfwidth / fullwidth forms
            or 0x20000 <= o <= 0x2FA1F) # CJK extension planes


def _runs(s):
    """Split s into consecutive (segment, use_fallback) runs by glyph coverage."""
    out, cur, curfb = [], [], None
    for ch in s:
        fb = _needs_fallback(ch)
        if curfb is None or fb == curfb:
            cur.append(ch)
            curfb = fb
        else:
            out.append(("".join(cur), curfb))
            cur, curfb = [ch], fb
    if cur:
        out.append(("".join(cur), curfb))
    return out


def _run_font(fnt, fb):
    """The face to draw a run with: the fallback (sized to match) for CJK runs."""
    if not fb or not FONT_FALLBACK:
        return fnt
    return font(FONT_FALLBACK, getattr(fnt, "size", 26))


def text_len(draw, s, fnt):
    """draw.textlength, but measuring CJK runs with the fallback face."""
    if not FONT_FALLBACK or not any(_needs_fallback(c) for c in s):
        return draw.textlength(s, font=fnt)
    return sum(draw.textlength(seg, font=_run_font(fnt, fb)) for seg, fb in _runs(s))


def text(draw, xy, s, fnt, fill, anchor="la"):
    if not FONT_FALLBACK or not any(_needs_fallback(c) for c in s):
        draw.text(xy, s, font=fnt, fill=fill, anchor=anchor)
        return
    x, y = xy
    hanch = anchor[0] if anchor else "l"
    vanch = anchor[1] if len(anchor) > 1 else "a"
    runs = _runs(s)
    total = sum(draw.textlength(seg, font=_run_font(fnt, fb)) for seg, fb in runs)
    if hanch == "r":
        x -= total
    elif hanch == "m":
        x -= total / 2
    for seg, fb in runs:                                # draw each run left-anchored
        f = _run_font(fnt, fb)
        draw.text((x, y), seg, font=f, fill=fill, anchor="l" + vanch)
        x += draw.textlength(seg, font=f)


def fit_text(draw, s, fnt_path, size, max_w):
    """Return a font shrunk until s fits in max_w (min size 60% of original)."""
    sz = size
    while sz > size * 0.6:
        f = font(fnt_path, sz)
        if text_len(draw, s, f) <= max_w:
            return f
        sz -= 2
    return font(fnt_path, sz)


def uniform_fit(draw, strings, fnt_path, size, max_w, floor=30):
    """Largest size (<= `size`, >= floor) at which EVERY string fits in max_w.

    Used so the now-playing title is one consistent size on every track, chosen
    so the longest title isn't cut off.
    """
    sz = size
    while sz > floor:
        f = font(fnt_path, sz)
        if all(text_len(draw, s, f) <= max_w for s in strings):
            return sz
        sz -= 2
    return floor


def ellipsize(draw, s, fnt, max_w):
    if text_len(draw, s, fnt) <= max_w:
        return s
    while s and text_len(draw, s + "\u2026", fnt) > max_w:
        s = s[:-1]
    return s + "\u2026"


# --------------------------------------------------------------------------- #
#  Audio -> waveform envelope
# --------------------------------------------------------------------------- #
def extract_envelope(audio, n_bars):
    """Decode audio to mono and return a 0..1 peak value per bar."""
    sr = 8000
    raw = subprocess.run(
        [FFMPEG, "-v", "error", "-i", audio, "-ac", "1", "-ar", str(sr),
         "-f", "s16le", "-"],
        stdout=subprocess.PIPE, check=True).stdout
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if samples.size == 0:
        return np.zeros(n_bars)
    # bucket into n_bars and take the peak of each bucket
    idx = np.linspace(0, samples.size, n_bars + 1).astype(int)
    env = np.array([np.abs(samples[idx[i]:idx[i + 1]]).max(initial=0.0)
                    for i in range(n_bars)], dtype=np.float32)
    env /= (env.max() or 1.0)
    env = env ** 0.7                       # gamma: lift quiet bars a little
    return np.clip(env, 0.02, 1.0)


def extract_spectrum(audio, total, fps, n_bands=16):
    """Decode audio and return per-video-frame frequency-band magnitudes.

    Returns a (n_frames, n_bands) array in 0..1, log-spaced bands (bass -> treble),
    sampled once per output frame. Drives the live spectrogram glyph by the counter.
    """
    sr = 16000
    raw = subprocess.run(
        [FFMPEG, "-v", "error", "-i", audio, "-ac", "1", "-ar", str(sr),
         "-f", "s16le", "-"],
        stdout=subprocess.PIPE, check=True).stdout
    s = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if s.size == 0:
        return np.zeros((1, n_bands), np.float32)
    hop = max(1, sr // fps)
    win = 1024
    n_frames = int(round(total * fps)) if total else max(1, s.size // hop)
    s = np.concatenate([np.zeros(win, np.float32), s,
                        np.zeros(win + hop, np.float32)])      # pad for windowing
    window = np.hanning(win).astype(np.float32)
    freqs = np.fft.rfftfreq(win, 1.0 / sr)
    edges = np.logspace(np.log10(40.0), np.log10(sr / 2), n_bands + 1)
    bins = np.searchsorted(freqs, edges)
    out = np.zeros((n_frames, n_bands), np.float32)
    CH = 2048                                                  # frames per chunk (bounds memory)
    for a in range(0, n_frames, CH):
        b = min(n_frames, a + CH)
        idx = (np.arange(a, b) * hop)[:, None] + np.arange(win)[None, :] + win
        mag = np.abs(np.fft.rfft(s[idx] * window, axis=1))
        for j in range(n_bands):
            lo, hi = bins[j], max(bins[j] + 1, bins[j + 1])
            out[a:b, j] = mag[:, lo:hi].mean(axis=1)
    out = np.log1p(out)
    out /= (out.max() or 1.0)
    return (out ** 0.8).astype(np.float32)


# --------------------------------------------------------------------------- #
#  Cover art
# --------------------------------------------------------------------------- #
def placeholder_cover(seed, size):
    """Deterministic colored placeholder so missing art still looks intentional."""
    h = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    hue = (h % 360)
    img = Image.new("RGB", (size, size))
    # cheap two-tone diagonal
    top = Image.new("HSV", (1, 1), (int(hue * 255 / 360), 150, 90)).convert("RGB").getpixel((0, 0))
    bot = Image.new("HSV", (1, 1), (int(((hue + 40) % 360) * 255 / 360), 180, 50)).convert("RGB").getpixel((0, 0))
    a = np.linspace(0, 1, size)[:, None]
    grad = (np.array(top) * (1 - a) + np.array(bot) * a).astype(np.uint8)
    img = Image.fromarray(np.repeat(grad[:, None, :], size, axis=1))
    d = ImageDraw.Draw(img)
    d.ellipse([size * 0.2, size * 0.2, size * 0.8, size * 0.8],
              outline=(255, 255, 255, 80), width=max(2, size // 60))
    return img


def load_cover(path, size, seed):
    try:
        if path and os.path.exists(path):
            im = Image.open(path).convert("RGB")
            # center-crop to square then resize
            w, h = im.size
            m = min(w, h)
            im = im.crop(((w - m) // 2, (h - m) // 2, (w + m) // 2, (h + m) // 2))
            return im.resize((size, size), Image.LANCZOS)
    except Exception:
        pass
    return placeholder_cover(seed, size)


# --------------------------------------------------------------------------- #
#  Background (rendered once, reused for every track)
# --------------------------------------------------------------------------- #
def make_background(W, H):
    top = np.array(THEME["bg_top"], np.float32)
    bot = np.array(THEME["bg_bottom"], np.float32)
    a = np.linspace(0, 1, H)[:, None, None]
    bg = (top * (1 - a) + bot * a).astype(np.uint8)
    bg = np.repeat(bg, W, axis=1)
    img = Image.fromarray(bg)
    # faint diagonal dotted texture, like the reference
    d = ImageDraw.Draw(img, "RGBA")
    rng = np.random.default_rng(7)
    for _ in range(700):
        x = rng.integers(0, W); y = rng.integers(0, H)
        if (x + y) % 23 < 3:
            d.point((x, y), fill=(60, 110, 170, 70))
    # soft glow lower-left
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([-W * 0.2, H * 0.4, W * 0.5, H * 1.2], fill=(30, 70, 120, 60))
    glow = glow.filter(ImageFilter.GaussianBlur(120))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
    # bottom panel
    d = ImageDraw.Draw(img, "RGBA")
    return img


def composite_over(bg, rgb, a):
    """Alpha-composite an RGB overlay (HxWx3) with an HxW alpha onto an RGB bg."""
    a = a.astype(np.uint32)[:, :, None]
    out = (bg.astype(np.uint32) * (255 - a) + rgb.astype(np.uint32) * a) // 255
    return out.astype(np.uint8)


def make_bg_base(W, H, blobs=True):
    """Static background: navy gradient + lower-left glow. With blobs=True it also
    bakes a few soft blue 'window-light' blobs (used for the static fallback); when
    animating, blobs are left out and supplied by the moving fog (paint_fog)."""
    top = np.array(THEME["bg_top"], np.float32)
    bot = np.array(THEME["bg_bottom"], np.float32)
    g = np.linspace(0, 1, H)[:, None, None]
    base = np.repeat(top * (1 - g) + bot * g, W, axis=1)            # (H, W, 3) float
    if blobs:
        sc = 4
        small = Image.new("RGB", (W // sc, H // sc), (0, 0, 0))
        bd = ImageDraw.Draw(small)
        for (cx, cy, rr, col) in [(0.12, 0.85, 0.55, (30, 80, 130)),
                                  (0.04, 0.50, 0.45, (20, 60, 110)),
                                  (0.30, 1.00, 0.50, (22, 66, 116))]:
            x, y, r = cx * W / sc, cy * H / sc, rr * H / sc
            bd.ellipse([x - r, y - r, x + r, y + r], fill=col)
        small = small.filter(ImageFilter.GaussianBlur(20))
        base += np.asarray(small.resize((W, H), Image.BILINEAR), np.float32) * 0.9
    return np.clip(base, 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------- #
#  Bokeh background (the "bokeh lounge" theme): warm out-of-focus orbs drifting
#  over a vignette, with film grain and a darkened bottom scrim. Replaces the
#  rain+fog background when --theme bokeh is selected.
# --------------------------------------------------------------------------- #
BOKEH_WARM = [(255, 196, 120), (255, 150, 90), (240, 120, 140),
              (255, 220, 170), (255, 175, 110)]


def _bokeh_sprite(r, tint, bright):
    """A soft out-of-focus disc: bright-ish core + a faint brighter rim."""
    s = 2 * r + 1
    yy, xx = np.mgrid[0:s, 0:s]
    d = np.sqrt((xx - r) ** 2 + (yy - r) ** 2)
    core = np.clip(1 - (d / r) ** 2.2, 0, 1)
    ring = np.exp(-((d - 0.82 * r) ** 2) / (2 * (0.12 * r) ** 2)) * 0.55
    a = np.clip(core * 0.85 + ring, 0, 1)
    a[d > r] = 0
    return np.array(tint, np.float32)[None, None, :] * a[..., None] * (bright / 255.0)


def make_bokeh(W, H):
    """Build the bokeh field once: drifting orb sprites, gradient, vignette,
    bottom scrim. Sizes scale with resolution (tuned at 1080p)."""
    rng = np.random.default_rng(15)
    sc = H / 1080.0
    particles = []

    def spawn(count, rmin, rmax, bmin, bmax, amp, yb=0.0):
        for _ in range(count):
            r = max(2, int(rng.integers(rmin, rmax)))
            tint = BOKEH_WARM[rng.integers(0, len(BOKEH_WARM))]
            particles.append(dict(
                spr=_bokeh_sprite(r, tint, rng.uniform(bmin, bmax)),
                hx=rng.uniform(-40, W + 40), hy=rng.uniform(-40, H * 0.72),
                ax=rng.uniform(amp * 0.4, amp), ay=rng.uniform(amp * 0.4, amp),
                phx=rng.uniform(0, 2 * np.pi), phy=rng.uniform(0, 2 * np.pi),
                harm=int(rng.choice([1, 1, 2])), tw=rng.uniform(0.45, 0.85),
                tp=rng.uniform(0, 2 * np.pi), twh=int(rng.choice([1, 2])),
                drift=rng.uniform(-amp, amp) + yb))
    spawn(46, int(40 * sc), int(92 * sc), 60, 110, 26 * sc, -10 * sc)
    spawn(22, int(16 * sc), int(40 * sc), 120, 190, 36 * sc, -14 * sc)
    spawn(14, int(5 * sc), int(13 * sc), 180, 235, 48 * sc, -18 * sc)

    top = np.array(THEME["bg_top"], np.float32)
    bot = np.array(THEME["bg_bottom"], np.float32)
    gg = np.linspace(0, 1, H)[:, None, None]
    bg = np.repeat(top * (1 - gg) + bot * gg, W, axis=1).astype(np.float32)
    yy, xx = np.mgrid[0:H, 0:W]
    vd = np.sqrt((xx - W / 2) ** 2 + (yy - H / 2) ** 2)
    vd /= vd.max()
    vig = np.clip(1 - 0.6 * vd ** 2.0, 0, 1)[..., None].astype(np.float32)
    sh = int(230 * sc)                                       # bottom scrim height
    ramp = np.clip((np.arange(H) - (H - sh)) / sh, 0, 1) ** 1.4
    scrim = (ramp[:, None, None] * np.ones((H, W, 1), np.float32)) * 0.92
    return dict(particles=particles, bg=bg, vig=vig, scrim=scrim,
                rng=np.random.default_rng(99), W=W, H=H)


def _bokeh_add(buf, spr, cx, cy, f):
    r = spr.shape[0] // 2
    x0, y0 = cx - r, cy - r
    H, W = buf.shape[:2]
    bx0, by0 = max(0, x0), max(0, y0)
    bx1, by1 = min(W, x0 + spr.shape[1]), min(H, y0 + spr.shape[0])
    if bx0 >= bx1 or by0 >= by1:
        return
    buf[by0:by1, bx0:bx1] += spr[by0 - y0:by1 - y0, bx0 - x0:bx1 - x0] * f


def bokeh_frame(bk, g, fps, period=8.0):
    """Render one bokeh background frame (returns an HxWx3 uint8 array)."""
    ph = 2 * np.pi * g / (period * fps)
    buf = bk["bg"].copy()
    for p in bk["particles"]:
        x = p["hx"] + p["ax"] * np.sin(p["harm"] * ph + p["phx"])
        y = p["hy"] + p["ay"] * np.sin(p["harm"] * ph + p["phy"]) + p["drift"] * np.sin(ph)
        fac = p["tw"] * (0.7 + 0.3 * np.sin(p["twh"] * ph + p["tp"])) + 0.15
        _bokeh_add(buf, p["spr"], int(x), int(y), fac)
    buf *= bk["vig"]
    buf += bk["rng"].normal(0, 4, (bk["H"], bk["W"], 1))    # film grain
    buf *= (1 - bk["scrim"])                                # darken bottom for text
    np.clip(buf, 0, 255, out=buf)
    return buf.astype(np.uint8)


# --------------------------------------------------------------------------- #
#  Plexus / constellation background (the "plexus" theme): slowly drifting nodes
#  linked by thin lines when near each other; the whole web brightens/pulses with
#  the audio. Keeps the default layout — only replaces the rain snakes.
# --------------------------------------------------------------------------- #
PLEXUS = dict(n=90, speed=0.45, link=210, node_r=2.6, color=(130, 185, 240))


def make_plexus(W, H):
    """Seed the drifting node field. Count scales with frame area, sizes with H."""
    rng = np.random.default_rng(21)
    s = H / 1080.0
    n = max(8, int(round(PLEXUS["n"] * (W * H) / (1920.0 * 1080.0))))
    pos = rng.uniform(0, 1, (n, 2)).astype(np.float32) * np.array([W, H], np.float32)
    ang = rng.uniform(0, 2 * np.pi, n)
    spd = PLEXUS["speed"] * s * rng.uniform(0.5, 1.5, n)
    vel = np.stack([np.cos(ang) * spd, np.sin(ang) * spd], 1).astype(np.float32)
    return dict(pos=pos, vel=vel, W=W, H=H,
                link=PLEXUS["link"] * s, nr=max(1.5, PLEXUS["node_r"] * s))


def paint_plexus(buf, ctx, pulse):
    """Advance + draw the constellation onto buf, kept behind the UI (suppressed
    where the overlay is opaque). `pulse` (0..1) is the current audio energy."""
    pk = ctx["plexus"]
    pos = pk["pos"]
    pos += pk["vel"]
    np.mod(pos, np.array([pk["W"], pk["H"]], np.float32), out=pos)   # wrap at edges
    link = pk["link"]
    diff = pos[:, None, :] - pos[None, :, :]
    dist = np.sqrt((diff ** 2).sum(2))
    iu, ju = np.triu_indices(len(pos), 1)
    dd = dist[iu, ju]
    sel = dd < link
    iu, ju, dd = iu[sel], ju[sel], dd[sel]
    base = np.array(PLEXUS["color"], np.float32)
    glob = 0.40 + 0.85 * pulse                                       # audio brightness
    lay = Image.new("RGBA", (pk["W"], pk["H"]), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay, "RGBA")
    col = tuple(int(x) for x in base)
    for a, b, dv in zip(iu, ju, dd):
        al = (1.0 - dv / link) * glob
        if al <= 0.02:
            continue
        d.line([(pos[a][0], pos[a][1]), (pos[b][0], pos[b][1])],
               fill=col + (int(min(1.0, al) * 170),), width=1)
    nr = pk["nr"]
    nb = tuple(int(x) for x in np.clip(base * 1.12, 0, 255)) + (int(min(1.0, glob) * 235),)
    for p in pos:
        d.ellipse([p[0] - nr, p[1] - nr, p[0] + nr, p[1] + nr], fill=nb)
    arr = np.asarray(lay)
    la = arr[..., 3:4].astype(np.float32) / 255.0                   # HxWx1 alpha
    la *= (ctx["ov_a"][..., None] < 40)                            # keep behind UI
    v = buf.astype(np.float32) * (1 - la) + arr[..., :3].astype(np.float32) * la
    buf[:] = np.clip(v, 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------- #
#  Aurora background (the "aurora" theme): flowing northern-lights curtains.
#  Bass swells the slow horizontal waves; highs add shimmer. Rendered small and
#  upscaled (it's soft), added as light behind the default layout.
# --------------------------------------------------------------------------- #
AURORA_LAYERS = [
    # cy/amp/sig are fractions of frame height; waves: (freq, amp_frac, speed, phase)
    dict(cy=0.30, amp=0.11, sig=0.085, col=(70, 225, 175),
         waves=[(1.0, 0.55, 0.06, 0.0), (2.3, 0.30, 0.10, 1.7), (0.6, 0.15, 0.04, 3.1)]),
    dict(cy=0.42, amp=0.14, sig=0.110, col=(58, 165, 235),
         waves=[(0.8, 0.60, 0.05, 0.6), (1.7, 0.28, 0.09, 2.2), (3.1, 0.12, 0.14, 0.3)]),
    dict(cy=0.22, amp=0.09, sig=0.140, col=(120, 120, 232),
         waves=[(0.6, 0.70, 0.04, 1.1), (1.3, 0.30, 0.07, 2.8)]),
]


def make_aurora(W, H):
    ds = 3                                          # render at 1/3 res, upscale (soft)
    w, h = max(1, W // ds), max(1, H // ds)
    return dict(ds=ds, w=w, h=h, W=W, H=H,
                xs=np.linspace(0.0, 1.0, w, dtype=np.float32),
                yg=np.arange(h, dtype=np.float32)[:, None])


def paint_aurora(buf, g, ctx, sf):
    """Draw the aurora for this frame (bass -> wave swell, highs -> shimmer,
    overall energy -> brightness), kept behind the UI via the overlay mask."""
    au = ctx["aurora"]
    w, h, xs, yg = au["w"], au["h"], au["xs"], au["yg"]
    t = g / ctx["fps"]
    nb = len(sf)
    third = max(1, nb // 3)
    bass = float(sf[:third].mean())
    high = float(sf[-third:].mean())
    gain = 0.18 + 0.55 * float(sf.mean())
    shimmer = 1.0 + 0.8 * high * np.sin(2 * np.pi * 16.0 * xs + 9.0 * t)
    folds = (0.45 + 0.55 * np.sin(2 * np.pi * 2.4 * xs + 0.5 * t)) * \
            (0.60 + 0.40 * np.sin(2 * np.pi * 5.1 * xs - 0.7 * t + 1.3))
    colint = np.clip(folds * shimmer, 0.0, 1.6)
    acc = np.zeros((h, w, 3), np.float32)
    for L in AURORA_LAYERS:
        wave = np.zeros(w, np.float32)
        for (f, a, sp, ph) in L["waves"]:
            wave += a * np.sin(2 * np.pi * f * xs + sp * t * 2 * np.pi + ph)
        yc = L["cy"] * h + L["amp"] * h * (0.5 + 1.6 * bass) * wave
        sig = max(1.0, L["sig"] * h)
        dy = yg - yc[None, :]
        band = np.exp(-(dy * dy) / (2.0 * sig * sig))
        inten = band * (colint[None, :] * gain)
        acc += inten[..., None] * np.asarray(L["col"], np.float32)
    acc = np.clip(acc, 0, 255).astype(np.uint8)
    up = np.asarray(Image.fromarray(acc).resize((au["W"], au["H"]), Image.BILINEAR),
                    np.float32)
    m = ctx["ov_a"][..., None] < 40                 # keep behind the UI
    v = buf.astype(np.float32) + up * m
    buf[:] = np.clip(v, 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------- #
#  Grid ripples background (the "ripple" theme): the faint dot grid comes alive
#  as expanding sonar rings. A kick (rise in bass) drops a new ring at full,
#  saturated brightness with a dim trailing wake; the ring keeps expanding at
#  that brightness and is only dropped once it (and its wake) leave the screen,
#  so rings read as carrying on forever rather than fading out mid-frame. Built
#  on the same dot grid the rain snaps to.
# --------------------------------------------------------------------------- #
RIPPLE = dict(speed=7.0, thick=6.0, wake_len=70.0, kick_thresh=0.030, gain=4.0,
              wake_amp=0.38, cooldown=5, ambient_every=40, max_rings=80)


def _saturate(rgb):
    """Fully-saturated, full-value version of an RGB color (same hue)."""
    h, _, _ = colorsys.rgb_to_hsv(*(c / 255.0 for c in rgb))
    r, g, b = colorsys.hsv_to_rgb(h, 1.0, 1.0)
    return np.array([r * 255, g * 255, b * 255], np.float32)


def _ring_maxr(cx, cy, W, H):
    """Distance from a ring center to the farthest screen corner (so we know when
    the ring has fully left the frame)."""
    return max(np.hypot(cx, cy), np.hypot(W - cx, cy),
               np.hypot(cx, H - cy), np.hypot(W - cx, H - cy))


def advance_ripples(ctx, g, sf):
    """Spawn rings (one per kick, throttled, + an occasional ambient ring) and
    cull only the ones whose wake has fully crossed off the screen."""
    rng, W, H = ctx["rng"], ctx["W"], ctx["H"]
    nb = len(sf)
    third = max(1, nb // 3)
    bass = float(sf[:third].mean())
    kick = max(0.0, bass - ctx["rip_prev"])
    ctx["rip_prev"] = 0.5 * ctx["rip_prev"] + 0.5 * bass
    speed, wl = RIPPLE["speed"], RIPPLE["wake_len"]
    # keep a ring while even its inner wake edge is still within the far corner
    ctx["ripples"] = [r for r in ctx["ripples"]
                      if (g - r["born"]) * speed - wl < r["maxr"]]

    def spawn(inten):
        cx, cy = rng.uniform(0, W), rng.uniform(0, H)
        ctx["ripples"].append(dict(cx=cx, cy=cy, born=g, inten=inten,
                                   maxr=_ring_maxr(cx, cy, W, H)))
    if kick > RIPPLE["kick_thresh"] and g - ctx["rip_last"] >= RIPPLE["cooldown"]:
        spawn(min(1.5, kick * RIPPLE["gain"]))
        ctx["rip_last"] = g
    if g % RIPPLE["ambient_every"] == 0:
        spawn(0.6)
    if len(ctx["ripples"]) > RIPPLE["max_rings"]:
        ctx["ripples"] = ctx["ripples"][-RIPPLE["max_rings"]:]   # oldest are off-screen


def paint_ripple(buf, g, ctx):
    """Light the (UI-visible) grid dots: a dim baseline, plus each ring's sharp
    saturated front and the dim wake trailing behind it. No distance fade."""
    gxv, gyv, faintv = ctx["gv"]
    if not gxv.size:
        return
    col = ctx["rip_col"]
    thick, wl, wa = RIPPLE["thick"], RIPPLE["wake_len"], RIPPLE["wake_amp"]
    speed = RIPPLE["speed"]
    bright = faintv.astype(np.float32).copy()        # always-on dim grid
    for r in ctx["ripples"]:
        radius = (g - r["born"]) * speed
        d = np.sqrt((gxv - r["cx"]) ** 2 + (gyv - r["cy"]) ** 2)
        front = np.exp(-((d - radius) ** 2) / (2.0 * thick * thick))   # sharp ring
        trail = np.clip(1.0 - (radius - d) / wl, 0.0, 1.0)             # behind the front
        trail[d > radius] = 0.0
        bright += r["inten"] * (front + wa * trail)
    np.clip(bright, 0.0, 1.6, out=bright)
    add = (col[None, :] * bright[:, None]).astype(np.int16)
    _add_dots(buf, gyv, gxv, add)


# --------------------------------------------------------------------------- #
#  Comets background (the "comets" theme): like rain but every streak falls in
#  one fixed direction (down and to the right) with a bright, slightly bigger
#  head and a thin dim dot tail. Spawn rate/length follow the spectrum. Drawn on
#  the same dot grid (snapped + masked) as the rain.
# --------------------------------------------------------------------------- #
COMET = dict(angle=45.0, jitter=0.0, speed=(8.0, 15.0),
             tail_bass=540.0, tail_treble=170.0, sample=14.0,
             gain=0.45, floor=0.05, maxc=140, headbright=1.4, tailbright=0.34)


def _comet_spawn(ctx, power, frac):
    """One comet entering from the top or left edge, heading down-and-right. Tail
    length scales with power (bass bins longer than treble)."""
    rng, W, H = ctx["rng"], ctx["W"], ctx["H"]
    p = min(max(power, 0.0), 1.0)
    a = np.deg2rad(COMET["angle"] + rng.uniform(-COMET["jitter"], COMET["jitter"]))
    L = (COMET["tail_bass"] * (1 - frac) + COMET["tail_treble"] * frac) \
        * (0.5 + 0.9 * p) * rng.uniform(0.85, 1.15)
    sp = rng.uniform(*COMET["speed"])
    if rng.random() < 0.62:                          # enter from the top edge
        x, y = rng.uniform(-0.10 * W, W), rng.uniform(-0.18 * H, 0.0)
    else:                                            # enter from the left edge
        x, y = rng.uniform(-0.15 * W, 0.0), rng.uniform(0.0, 0.80 * H)
    ctx["comets"].append(dict(x=x, y=y, ux=np.cos(a), uy=np.sin(a), sp=sp, L=L))


def advance_comets(ctx, g, sf):
    """Move + cull comets, then spawn new ones from the spectrum frame."""
    W, H = ctx["W"], ctx["H"]
    alive = []
    for c in ctx["comets"]:
        c["x"] += c["ux"] * c["sp"]
        c["y"] += c["uy"] * c["sp"]
        if c["x"] - c["ux"] * c["L"] <= W + 4 and c["y"] - c["uy"] * c["L"] <= H + 4:
            alive.append(c)                          # keep until the tail end exits
    ctx["comets"] = alive
    rng, nb = ctx["rng"], len(sf)
    for bdx in range(nb):
        pw = float(sf[bdx])
        frac = bdx / (nb - 1) if nb > 1 else 0.0     # 0 = bass, 1 = treble
        rate = max(pw, COMET["floor"]) * COMET["gain"]
        nsp = int(rate) + (1 if rng.random() < (rate - int(rate)) else 0)
        for _ in range(nsp):
            if len(ctx["comets"]) >= COMET["maxc"]:
                break
            _comet_spawn(ctx, pw, frac)


def paint_comets(buf, g, ctx):
    """Faint dot grid + every comet (grid-snapped, masked behind the UI): a thin
    dim tail fading back from a bright, enlarged head."""
    gxv, gyv, faintv = ctx["gv"]
    if gxv.size:                                     # faint always-on dot grid
        _add_dots(buf, gyv, gxv, (ctx["dotcol"][None, :] * faintv[:, None]).astype(np.int16))
    sp_g, x0 = ctx["grid_sp"], ctx["grid_x0"]
    ova, W, H = ctx["ov_a"], ctx["W"], ctx["H"]
    col = ctx["dotcol"]
    step = COMET["sample"]
    head_ys, head_xs = [], []
    for c in ctx["comets"]:
        t = np.arange(0.0, c["L"], step)
        gx = (np.round((c["x"] - c["ux"] * t - x0) / sp_g) * sp_g + x0).astype(np.int32)
        gy = (np.round((c["y"] - c["uy"] * t - x0) / sp_g) * sp_g + x0).astype(np.int32)
        on = (gx >= x0) & (gx < W - 2) & (gy >= x0) & (gy < H - 2)
        if not on.any():
            continue
        gx, gy, t = gx[on], gy[on], t[on]
        vis = ova[gy, gx] < 40
        if not vis.any():
            continue
        gx, gy, t = gx[vis], gy[vis], t[vis]
        br = np.where(t < step * 1.5, COMET["headbright"],
                      COMET["tailbright"] * np.clip(1.0 - t / c["L"], 0.0, 1.0))
        _add_dots(buf, gy, gx, (col[None, :] * br[:, None]).astype(np.int16))
        hm = t < step * 0.5                          # the head point
        if hm.any():
            head_ys.append(gy[hm]); head_xs.append(gx[hm])
    if head_ys:                                      # enlarge + brighten the heads
        hy, hx = np.concatenate(head_ys), np.concatenate(head_xs)
        hb = (col * COMET["headbright"]).astype(np.int16)
        for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1)):
            v = buf[hy + oy, hx + ox].astype(np.int16) + hb
            buf[hy + oy, hx + ox] = np.clip(v, 0, 255).astype(np.uint8)


def make_fog_tex(W, H, pad=170, tint=(30, 70, 116)):
    """Half-resolution blurred 'fog' texture (tinted to the palette), larger than
    the half-frame by `pad` on each axis so it can be slowly panned (the aura)."""
    fw, fh = W // 2, H // 2
    rng = np.random.default_rng(99)
    base = np.asarray(tint, np.float32)
    img = Image.new("RGB", (fw + pad, fh + pad), (0, 0, 0))
    d = ImageDraw.Draw(img)
    for _ in range(8):
        cx, cy = rng.uniform(0, fw + pad), rng.uniform(0, fh + pad)
        r = rng.uniform(0.22, 0.55) * fh
        col = tuple(int(min(255, v)) for v in base * rng.uniform(0.65, 1.15))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
    img = img.filter(ImageFilter.GaussianBlur(55))
    return np.asarray(img), pad


def paint_fog(buf, g, ctx):
    """Add the slowly drifting ('blowing') blurred blue fog, masked to the visible
    background (ctx['fog_gain']) so it stays behind the UI."""
    tex, pad, W, H = ctx["fog_tex"], ctx["fog_pad"], ctx["W"], ctx["H"]
    fw, fh = W // 2, H // 2
    ox = int((pad / 2) * (1 + np.sin(g * 0.011)))         # horizontal "wind" sway
    oy = int((pad / 2) * (1 + np.sin(g * 0.006 + 1.3)))   # gentle vertical drift
    win = tex[oy:oy + fh, ox:ox + fw]
    up = np.asarray(Image.fromarray(win).resize((W, H), Image.BILINEAR), np.int32)
    v = buf.astype(np.int32) + ((up * ctx["fog_gain"][:, :, None]) >> 10)   # subtle
    np.clip(v, 0, 255, out=v)
    buf[:] = v.astype(np.uint8)


# Rain tuning. A drop is a thin dotted "snake" that wanders down-and-to-the-right.
RAIN = dict(speed=(11.0, 34.0),          # px/frame (~3/4 of the previous 10x)
            len_bass=1200, len_treble=220, len_floor=120,
            gain=0.32, floor=0.06, max_drops=260, maxage=300,
            angle=(27, 60), amin=16, amax=78,   # spawn band + wander clamp, degrees
            wander=5.0, sample=10.0)


def _spawn(ctx, power, frac, g=0, prefill=0.0):
    """Spawn one snake at a random spot, heading down-and-to-the-right. Length
    scales with `power` (and bass bins are longer). It starts as a single dot and
    grows to its full length as it moves; `prefill` (0..1, used only when seeding)
    pre-grows part of the tail so the very first frame isn't empty."""
    rng, W, H = ctx["rng"], ctx["W"], ctx["H"]
    p = min(max(power, 0.0), 1.0)
    a = np.deg2rad(rng.uniform(*RAIN["angle"]))
    base = RAIN["len_bass"] * (1 - frac) + RAIN["len_treble"] * frac
    slen = max(RAIN["len_floor"], base * (0.35 + 1.3 * p)) * rng.uniform(0.85, 1.15)
    sp = rng.uniform(*RAIN["speed"])
    x, y = rng.uniform(0, W), rng.uniform(0, H)        # start anywhere
    if prefill > 0:
        ux, uy = np.cos(a), np.sin(a)
        t = np.linspace(slen * prefill, 0.0, max(2, int(slen * prefill / RAIN["sample"])))
        px, py = list(x - ux * t), list(y - uy * t)
    else:
        px, py = [x], [y]                              # single dot; grows as it moves
    ctx["active"].append({"px": px, "py": py, "a": a, "sp": sp, "slen": slen, "born": g})


def _trim(d):
    px, py = d["px"], d["py"]
    total, i = 0.0, len(px) - 1
    while i > 0:
        total += ((px[i] - px[i - 1]) ** 2 + (py[i] - py[i - 1]) ** 2) ** 0.5
        if total > d["slen"]:
            break
        i -= 1
    if i > 0:
        del px[:i]
        del py[:i]


def seed_rain(ctx, n):
    rng = ctx["rng"]
    for _ in range(n):
        _spawn(ctx, rng.random(), rng.random(), 0, prefill=rng.uniform(0.2, 1.0))


def advance_rain(ctx, g, sf):
    """Wander + step each snake's head (so the path curves a little), cull ones too
    old or whose tail has left the frame, then spawn new snakes from the spectrum
    frame `sf` (louder bin -> more & longer snakes; bass bins -> longer than treble)."""
    W, H, rng = ctx["W"], ctx["H"], ctx["rng"]
    maxage = RAIN["maxage"]
    lo, hi = np.deg2rad(RAIN["amin"]), np.deg2rad(RAIN["amax"])
    wander = np.deg2rad(RAIN["wander"])
    alive = []
    for d in ctx["active"]:
        px, py = d["px"], d["py"]
        if g - d["born"] > maxage or px[0] > W + 4 or py[0] > H + 4:
            continue
        a = min(max(d["a"] + rng.normal(0, wander), lo), hi)
        d["a"] = a
        px.append(px[-1] + d["sp"] * np.cos(a))
        py.append(py[-1] + d["sp"] * np.sin(a))
        _trim(d)
        alive.append(d)
    ctx["active"] = alive
    nb = len(sf)
    for bdx in range(nb):
        p = float(sf[bdx])
        frac = bdx / (nb - 1) if nb > 1 else 0.0      # 0 = bass, 1 = treble
        rate = max(p, RAIN["floor"]) * RAIN["gain"]
        nsp = int(rate) + (1 if rng.random() < (rate - int(rate)) else 0)
        for _ in range(nsp):
            if len(ctx["active"]) >= RAIN["max_drops"]:
                break
            _spawn(ctx, p, frac, g)


# --------------------------------------------------------------------------- #
#  Layout geometry
# --------------------------------------------------------------------------- #
def layout(W, H, theme="rain"):
    art = int(H * 0.57)
    L = {
        "art_x": 60, "art_y": 60, "art": art,
        "head_x": art + 120, "head_y": 64,
        "list_x": art + 120, "list_y": 175,
        "list_w": W - (art + 120) - 70,
        "row_h": int(H * 0.097),
        "thumb": int(H * 0.058),
        "now_x": 60, "now_y": int(H * 0.64),
        # waveform brought up so its bottom clears YouTube's bottom timeline overlay
        "wave_x": 60, "wave_w": W - 120,
        "wave_y": int(H * 0.81), "wave_h": int(H * 0.095),
        "tc_x": 60, "tc_y": int(H * 0.915),
        "counter_x": W - 70, "counter_y": int(H * 0.64), "counter_size": 76,
        # live spectrogram glyph: left of the counter, as tall as the two numbers
        "spec_x": W - 70 - 290, "spec_w": 165,
        "spec_y": int(H * 0.64), "spec_h": 2 * 76,
    }
    if theme == "bokeh":
        # bokeh lounge (concept look): a slim timeline pinned to the very bottom,
        # timecode bottom-right above it (positioned in ctx).
        S = H / 1080.0
        L["wave_h"] = int(34 * S)
        L["wave_y"] = H - int(48 * S)
        L["tc_y"]   = H - int(128 * S)
    return L


# --------------------------------------------------------------------------- #
#  Static per-track layout
# --------------------------------------------------------------------------- #
def render_base(cfg, tracks, i, W, H, L, covers, dim_band_img):
    # Transparent foreground overlay (composited over the animated bg per frame).
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")

    f_title = font(FONT_BOLD, 34)
    f_sub   = font(FONT_REG, 26)
    f_date  = font(FONT_BOLD, 30)
    f_prev  = font(FONT_REG, 26)
    f_lt    = font(FONT_REG, 30)
    f_la    = font(FONT_BOLD, 27)
    f_li    = font(FONT_REG, 30)
    f_ltime = font(FONT_REG, 25)
    f_na    = font(FONT_BOLD, 40)
    f_nr    = font(FONT_REG, 30)

    # ---- big album art ----
    ax, ay, a = L["art_x"], L["art_y"], L["art"]
    shadow = Image.new("RGBA", (a + 60, a + 60), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle([30, 30, a + 30, a + 30], fill=(0, 0, 0, 160))
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    img.paste(shadow, (ax - 30, ay - 18), shadow)
    img.paste(covers[i].resize((a, a), Image.LANCZOS).convert("RGBA"), (ax, ay))
    d.rectangle([ax, ay, ax + a, ay + a], outline=(255, 255, 255, 30), width=2)

    # ---- header ----
    hx, hy = L["head_x"], L["head_y"]
    text(d, (hx, hy), cfg.get("title", ""), f_title, THEME["white"])
    text(d, (hx, hy + 42), cfg.get("subtitle", ""), f_sub, THEME["gray"])
    rx = W - 70
    text(d, (rx, hy), cfg.get("date", ""), f_date, THEME["white"], anchor="ra")
    text(d, (rx, hy + 42), "Previous Tracks", f_prev, THEME["gray"], anchor="ra")

    # ---- tracklist window: previously-played tracks only ----
    # Shows the last `list_len` tracks that came before the current one, so the
    # list is empty during track 1 and fills as the mix progresses.
    list_len = 5
    hi = i                                   # exclusive: current track is not listed
    lo = max(0, hi - list_len)
    lx, ly, rh, th = L["list_x"], L["list_y"], L["row_h"], L["thumb"]
    list_right = lx + L["list_w"]
    for row, j in enumerate(range(lo, hi)):
        ry = ly + row * rh
        tr = tracks[j]
        # thumbnail
        img.paste(covers[j].resize((th, th), Image.LANCZOS).convert("RGBA"), (lx, ry))
        d.rectangle([lx, ry, lx + th, ry + th], outline=(255, 255, 255, 25), width=1)
        tx = lx + th + 24
        title_w = list_right - tx - 130
        # shrink the title font for longer names so it fits without truncating
        f_lt_i = fit_text(d, tr.get("title", ""), FONT_REG, 31, title_w)
        title_s = ellipsize(d, tr.get("title", ""), f_lt_i, title_w)
        art_s = ellipsize(d, tr.get("artist", ""), f_la, title_w)
        # vertically center the title+artist block against the thumbnail
        block_h = f_lt_i.size + 8 + f_la.size
        ty = ry + (th - block_h) // 2
        ay2 = ty + f_lt_i.size + 8
        text(d, (tx, ty), title_s, f_lt_i, (210, 218, 226))
        text(d, (tx, ay2), art_s, f_la, THEME["accent"])
        # index + timestamp, aligned to the title / artist lines
        text(d, (list_right, ty + 2), f"{j + 1:02d}", f_li, THEME["gray"], anchor="ra")
        text(d, (list_right, ay2 + 2), fmt_time(parse_time(tr["start"])),
             f_ltime, THEME["dim_gray"], anchor="ra")

    # ---- now playing (bottom-left) ----
    nx, ny = L["now_x"], L["now_y"]
    cur = tracks[i]
    now_w = L["spec_x"] - nx - 40                  # extend across to before the spectrogram
    f_nt = font(FONT_BOLD, cfg.get("nt_size", 66)) # one size for all tracks (see uniform_fit)
    text(d, (nx, ny), ellipsize(d, cur.get("title", ""), f_nt, now_w),
         f_nt, THEME["white"])
    ay = ny + f_nt.size + 14
    text(d, (nx, ay), ellipsize(d, cur.get("artist", ""), f_na, now_w),
         f_na, THEME["accent"])
    if cur.get("release"):
        text(d, (nx, ay + 52), ellipsize(d, cur["release"], f_nr, now_w),
             f_nr, THEME["gray"])

    # ---- counter (bottom-right) ----
    cx, cy = L["counter_x"], L["counter_y"]
    cs = L["counter_size"]
    f_cnt = font(FONT_THIN, cs)
    text(d, (cx, cy), f"{i + 1:02d}", f_cnt, THEME["white"], anchor="ra")
    text(d, (cx, cy + cs), f"{len(tracks):02d}", f_cnt, THEME["accent2"], anchor="ra")
    # (the little eq glyph is now a live spectrogram, drawn per-frame in paint_spectrum)

    # ---- waveform (dim bars only; gaps transparent so the rain shows through) ----
    band = Image.fromarray(dim_band_img)                 # RGBA, gaps alpha 0
    img.paste(band, (L["wave_x"], L["wave_y"]), band)

    arr = np.asarray(img)
    return arr[:, :, :3].copy(), arr[:, :, 3].copy()    # (overlay_rgb, overlay_alpha)


def render_base_bokeh(cfg, tracks, i, W, H):
    """Minimal 'bokeh lounge' overlay (the concept look): the current track's
    artist (serif italic) and title bottom-left, the NN / TT count bottom-right.
    No album art, tracklist, or spectrogram — everything else is the bokeh field
    and the slim bottom timeline (painted per frame)."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")
    S = H / 1080.0
    cur = tracks[i]
    artist = (cur.get("artist") or "").strip()
    title  = (cur.get("title") or "").strip()
    lx = int(80 * S)
    f_artist = font(FONT_SERIF_ITALIC, int(58 * S))
    f_title  = font(FONT_SERIF, int(34 * S))
    f_count  = font(FONT_SERIF, int(42 * S))
    maxw = int(W * 0.66)
    artist = ellipsize(d, artist, f_artist, maxw)
    title  = ellipsize(d, title, f_title, maxw)
    # thin accent rule above the artist
    ry = H - int(196 * S)
    d.line([(lx, ry), (lx + int(230 * S), ry)], fill=THEME["accent"], width=max(1, int(2 * S)))
    text(d, (lx, H - int(180 * S)), artist, f_artist, THEME["white"])
    text(d, (lx + int(2 * S), H - int(104 * S)), title, f_title, THEME["gray"])
    # NN / TT count, bottom-right, aligned with the artist line
    text(d, (W - int(70 * S), H - int(176 * S)),
         f"{i + 1:02d} / {len(tracks):02d}", f_count, THEME["accent"], anchor="ra")
    arr = np.asarray(img)
    return arr[:, :, :3].copy(), arr[:, :, 3].copy()


# --------------------------------------------------------------------------- #
#  Waveform band images (dim + bright), built once
# --------------------------------------------------------------------------- #
def build_bands(env, wave_w, wave_h, bar_w, gap, marks=(),
                played_color=None, upcoming_color=None, mark_color=None):
    """Build the waveform band with TRANSPARENT gaps (bars only), so the animated
    background shows between bars instead of a solid blocking rectangle.

    Returns (dim_rgba, bar_mask, bright_col):
      dim_rgba   HxWx4 - the dim (upcoming) band, bars opaque + gaps transparent,
                 pasted into the per-track overlay.
      bar_mask   HxW bool - True where a bar pixel is (gaps False).
      bright_col HxWx3 - the played-region bar colors (used per frame by
                 paint_waveform to light the played bars).
    marks: fractions (0..1) of track-start positions -> a saturated mark_color bar.
    """
    played_color   = played_color   or THEME["wave_on"]
    upcoming_color = upcoming_color or THEME["wave_off"]
    mark_color     = mark_color     or THEME["wave_mark"]
    pitch = bar_w + gap
    n = len(env)
    mark_set = {int(fr * n) for fr in marks if 0 <= int(fr * n) < n}   # one bar each

    def band(base_color):
        im = Image.new("RGBA", (wave_w, wave_h), (0, 0, 0, 0))         # transparent gaps
        d = ImageDraw.Draw(im)
        for k in range(n):
            x = k * pitch
            if x + bar_w > wave_w:
                break
            h = max(2, int(env[k] * (wave_h - 2)))      # same height, marker or not
            color = mark_color if k in mark_set else base_color
            d.rectangle([x, wave_h - h, x + bar_w, wave_h], fill=color + (255,))
        return np.asarray(im).copy()
    dim = band(upcoming_color)
    bright = band(played_color)
    return dim, dim[:, :, 3] > 0, bright[:, :, :3].copy()


def cover_palette(cover):
    """Derive (played_color, mark_color) from a cover's average color.

    played_color = the album's average color, brightened enough to read on the
    dark background. mark_color = a saturated, vivid version of the same hue for
    the track-start ticks ("saturated, rest unsaturated").
    """
    avg = np.asarray(cover.convert("RGB")).reshape(-1, 3).astype(np.float32).mean(0)
    h, s, v = colorsys.rgb_to_hsv(*(avg / 255.0))
    played = colorsys.hsv_to_rgb(h, s, max(v, 0.60))
    mark   = colorsys.hsv_to_rgb(h, min(1.0, s * 2.0 + 0.45), max(v, 0.80))
    to8 = lambda c: tuple(int(round(x * 255)) for x in c)
    return to8(played), to8(mark)


def palette_from_cover(cover):
    """Build a full theme palette (a la a terminal color scheme) from 3-4 colors
    pulled out of a cover: a dark background tinted by the dominant color, and
    bright readable accents from the most vibrant colors. Used by
    --theme-color auto, so the whole palette changes per track."""
    im = cover.convert("RGB").resize((48, 48))
    try:
        q = im.quantize(colors=6, method=Image.FASTOCTREE)
    except Exception:
        q = im.quantize(colors=6)
    pal = q.getpalette() or []
    items = []
    for cnt, idx in (q.getcolors() or []):
        r, g, b = pal[idx * 3:idx * 3 + 3]
        hh, ss, vv = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        items.append(dict(cnt=cnt, h=hh, s=ss, v=vv))
    if not items:
        return PALETTES["blue"]

    def mk(h, s, v):
        r, g, b = colorsys.hsv_to_rgb(h, min(1.0, max(0.0, s)), min(1.0, max(0.0, v)))
        return (int(r * 255), int(g * 255), int(b * 255))

    dom = max(items, key=lambda x: x["cnt"])                    # background hue
    vibe = sorted([x for x in items if x["s"] > 0.20 and x["v"] > 0.22],
                  key=lambda x: x["s"] * x["v"] * (x["cnt"] ** 0.3), reverse=True)
    if not vibe:
        vibe = [dom]
    a1 = vibe[0]
    a2 = vibe[1] if len(vibe) > 1 else dict(h=(a1["h"] + 0.08) % 1.0, s=a1["s"], v=a1["v"])
    bgs = min(0.55, dom["s"] * 0.8 + 0.08)                      # keep bg muted
    accent  = mk(a1["h"], max(0.55, a1["s"]), max(0.80, a1["v"]))
    accent2 = mk(a2["h"], max(0.55, a2["s"]), max(0.90, a2["v"]))
    return {
        "bg_top":     mk(dom["h"], bgs, 0.13),
        "bg_bottom":  mk(dom["h"], bgs, 0.04),
        "panel":      mk(dom["h"], bgs, 0.05),
        "white":      mk(dom["h"], 0.05, 0.96),                 # near-white, faint tint
        "gray":       mk(dom["h"], 0.14, 0.64),
        "dim_gray":   mk(dom["h"], 0.14, 0.44),
        "accent":     accent,
        "accent2":    accent2,
        "wave_off":   mk(dom["h"], 0.16, 0.42),
        "wave_on":    mk(a1["h"], max(0.40, a1["s"] * 0.7), 0.92),
        "wave_mark":  accent2,
        "playhead":   accent2,
        "spectrum":   accent2,
        "dot":        accent,
        "fog":        mk(a1["h"], max(0.40, a1["s"]), 0.30),
    }


# --------------------------------------------------------------------------- #
#  rekordbox .cue support
# --------------------------------------------------------------------------- #
_MOJI = {                       # mis-decoded UTF-8 punctuation (leaves "Café" alone)
    "\u00e2\u0080\u0099": "\u2019", "\u00e2\u0080\u0098": "\u2018",
    "\u00e2\u0080\u009c": "\u201c", "\u00e2\u0080\u009d": "\u201d",
    "\u00e2\u0080\u0093": "\u2013", "\u00e2\u0080\u0094": "\u2014",
    "\u00e2\u0080\u00a6": "\u2026",
}


def fix_mojibake(s):
    if not s:
        return s
    for bad, good in _MOJI.items():
        s = s.replace(bad, good)
    return s


def read_text_any(path):
    data = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def cue_index_to_sec(a, b, c):
    """rekordbox writes INDEX as HH:MM:SS (not the CUE-standard MM:SS:FF)."""
    return a * 3600 + b * 60 + c


def clean_track_title(title, performer, do_clean):
    title = fix_mojibake(title or "")
    artist = fix_mojibake(performer or "")
    if not do_clean:
        return title.strip(), artist.strip()
    # strip a trailing YouTube id  "...-bXLC5a7GgR4"
    title = re.sub(r"-[A-Za-z0-9_-]{11}$", "", title).strip()
    # drop a redundant "(Original Mix)" / "(Extended Mix)" tag wherever it appears
    title = re.sub(r"\s*\(\s*(?:original|extended)\s+mix\s*\)", "", title, flags=re.I).strip()
    # if no performer but "Artist - Title" is embedded in the title, split it
    if not artist and " - " in title:
        artist, title = title.split(" - ", 1)
    return title.strip(), artist.strip()


def parse_cue(path, args):
    txt = read_text_any(path)
    cue_dir = os.path.dirname(os.path.abspath(path))

    header = {"title": "", "performer": "", "date": "", "audio": ""}
    tracks = []
    cur = None
    seen_track = False

    def grab(line, key):
        m = re.match(r'\s*' + key + r'\s+"(.*)"\s*$', line)
        if m:
            return m.group(1)
        m = re.match(r'\s*' + key + r'\s+(.+?)\s*$', line)
        return m.group(1) if m else None

    for line in txt.splitlines():
        if re.match(r"\s*TRACK\s+\d+\s+AUDIO", line):
            if cur:
                tracks.append(cur)
            cur = {"title": "", "performer": "", "src": "", "start": 0.0}
            seen_track = True
            continue
        if not seen_track:                       # ---- header block ----
            m = re.match(r"\s*REM\s+DATE\s+(.+)$", line)
            if m:
                header["date"] = m.group(1).strip().strip('"')
                continue
            v = grab(line, "TITLE")
            if v is not None:
                header["title"] = v; continue
            v = grab(line, "PERFORMER")
            if v is not None:
                header["performer"] = v; continue
            m = re.match(r'\s*FILE\s+"(.*)"\s+\w+\s*$', line)
            if m:
                header["audio"] = m.group(1); continue
        else:                                    # ---- track block ----
            v = grab(line, "TITLE")
            if v is not None:
                cur["title"] = v; continue
            v = grab(line, "PERFORMER")
            if v is not None:
                cur["performer"] = v; continue
            m = re.match(r'\s*FILE\s+"(.*)"\s+\w+\s*$', line)
            if m:
                cur["src"] = m.group(1); continue
            m = re.match(r"\s*INDEX\s+01\s+(\d+):(\d+):(\d+)", line)
            if m:
                a, b, c = (int(x) for x in m.groups())
                cur["start"] = cue_index_to_sec(a, b, c); continue
    if cur:
        tracks.append(cur)

    # clean titles / artists
    out_tracks = []
    for t in tracks:
        title, artist = clean_track_title(t["title"], t["performer"], not args.no_clean_titles)
        out_tracks.append({"title": title, "artist": artist,
                           "start": t["start"], "src": t["src"]})

    # drop repeated tracks (same artist + title); keep the first occurrence, so a
    # track that the cue lists again later is ignored (the prior one plays through)
    seen, deduped = set(), []
    for t in out_tracks:
        key = (t["artist"].strip().lower(), t["title"].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(t)
    if len(deduped) != len(out_tracks):
        print(f"  ({len(out_tracks) - len(deduped)} repeated track(s) ignored)", flush=True)
    out_tracks = deduped

    # mix-level display fields (CLI overrides win)
    date = args.date or _format_cue_date(header["date"])
    audio = args.audio or header["audio"]
    if audio and not os.path.isabs(audio):
        audio = os.path.join(cue_dir, os.path.basename(audio))
    cfg = {
        "title":    args.title    or header["title"] or os.path.splitext(os.path.basename(path))[0],
        "subtitle": args.subtitle or header["performer"],
        "date":     date,
        "audio":    audio,
        "tracks":   out_tracks,
    }
    return cfg


def _format_cue_date(raw):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw or "")
    if not m:
        return raw
    months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    y, mo, _ = m.groups()
    return f"{months[int(mo)]} {y}"


def win_to_wsl(p):
    """C:/Users/... -> /mnt/c/Users/...  (None if not a Windows drive path)."""
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    return f"/mnt/{m.group(1).lower()}/{m.group(2)}" if m else None


def remap_src(src, music_root):
    """Resolve a cue source path: apply --music-root OLD=NEW prefix maps, then
    fall back to Windows->WSL (/mnt/<drive>) translation. Returns an existing
    path when one is found, else the best-guess mapped path."""
    if not src:
        return src
    s = src.replace("\\", "/")
    for pair in music_root or []:
        if "=" in pair:
            old, new = pair.split("=", 1)
            old = old.replace("\\", "/")
            if s.lower().startswith(old.lower()):
                s = new.rstrip("/") + "/" + s[len(old):].lstrip("/")
                break
    candidates = [s]
    wsl = win_to_wsl(s)
    if wsl:
        candidates.append(wsl)
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]


def _ffprobe_has_video(src):
    try:
        probe = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", src],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return b"video" in probe.stdout
    except Exception:
        return False


def _art_bytes_mutagen(src):
    """Read embedded cover bytes via mutagen (reliable for AIFF/MP3/FLAC/M4A).
    Returns None if mutagen isn't installed or the file has no art."""
    try:
        import mutagen
    except ImportError:
        return None
    try:
        f = mutagen.File(src)
        if f is None:
            return None
        tags = getattr(f, "tags", None)
        if tags is not None and hasattr(tags, "getall"):      # ID3 APIC (mp3/aiff/wav)
            apic = tags.getall("APIC")
            if apic:
                return apic[0].data
        if getattr(f, "pictures", None):                      # FLAC / Ogg
            return f.pictures[0].data
        if tags is not None:                                  # MP4 / M4A "covr"
            try:
                if "covr" in tags:
                    return bytes(tags["covr"][0])
            except Exception:
                pass
    except Exception:
        return None
    return None


def src_has_art(src):
    """True if the file carries embedded art, False if not, None if unreachable."""
    if not src or not os.path.exists(src):
        return None
    if _art_bytes_mutagen(src) is not None:
        return True
    return _ffprobe_has_video(src)


def _square(im, size):
    im = im.convert("RGB")
    w, h = im.size
    m = min(w, h)
    im = im.crop(((w - m) // 2, (h - m) // 2, (w + m) // 2, (h + m) // 2))
    return im.resize((size, size), Image.LANCZOS)


def extract_embedded_cover(src, size, tmpdir):
    """Pull embedded album art. Prefers mutagen (handles AIFF/MP3/FLAC/M4A),
    falls back to ffmpeg stream extraction."""
    if not src or not os.path.exists(src):
        return None
    # 1. mutagen (tag-level, reliable)
    data = _art_bytes_mutagen(src)
    if data:
        try:
            from io import BytesIO
            return _square(Image.open(BytesIO(data)), size)
        except Exception:
            pass
    # 2. ffmpeg attached-picture stream
    try:
        if not _ffprobe_has_video(src):
            return None
        out = os.path.join(tmpdir, "c_" + hashlib.md5(src.encode()).hexdigest() + ".png")
        r = subprocess.run(
            [FFMPEG, "-v", "error", "-y", "-i", src,
             "-map", "0:v:0", "-frames:v", "1", out],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return _square(Image.open(out), size)
    except Exception:
        pass
    return None


def resolve_cover(track, idx, size, args, tmpdir, base_dir):
    """Priority: --cover N=PATH > --covers/NN.* > explicit cover field
                 > embedded art (src) > placeholder."""
    # 0. explicit per-track override "--cover N=PATH"
    p = getattr(args, "cover_map", {}).get(idx + 1)
    if p:
        if not os.path.isabs(p):
            p = os.path.join(base_dir, p)
        if os.path.exists(p):
            return load_cover(p, size, "")
        print(f"  WARNING: --cover for track {idx+1} not found: {p}", flush=True)
    # 1. covers dir keyed by track number
    if args.covers:
        for ext in ("jpg", "jpeg", "png", "webp"):
            for name in (f"{idx + 1:02d}.{ext}", f"{idx + 1}.{ext}"):
                p = os.path.join(args.covers, name)
                if os.path.exists(p):
                    return load_cover(p, size, "")
    # 2. explicit cover field (JSON configs)
    cp = track.get("cover")
    if cp:
        if not os.path.isabs(cp):
            cp = os.path.join(base_dir, cp)
        if os.path.exists(cp):
            return load_cover(cp, size, "")
    # 3. embedded art from the original source file
    src = remap_src(track.get("src", ""), args.music_root)
    em = extract_embedded_cover(src, size, tmpdir)
    if em is not None:
        return em
    # 4. placeholder
    seed = f"{track.get('artist','')}|{track.get('title','')}|{idx}"
    return placeholder_cover(seed, size)


def has_real_art(track, idx, args, base_dir):
    """True if track idx resolves to a real cover from any source (override,
    --covers folder, cover field, or embedded art); False if it would fall back
    to a generated placeholder. Mirrors resolve_cover's priority without loading."""
    p = getattr(args, "cover_map", {}).get(idx + 1)
    if p:
        if not os.path.isabs(p):
            p = os.path.join(base_dir, p)
        if os.path.exists(p):
            return True
    if args.covers:
        for ext in ("jpg", "jpeg", "png", "webp"):
            for name in (f"{idx + 1:02d}.{ext}", f"{idx + 1}.{ext}"):
                if os.path.exists(os.path.join(args.covers, name)):
                    return True
    cp = track.get("cover")
    if cp:
        if not os.path.isabs(cp):
            cp = os.path.join(base_dir, cp)
        if os.path.exists(cp):
            return True
    src = remap_src(track.get("src", ""), args.music_root)
    return src_has_art(src) is True


def load_config(path, args):
    if path.lower().endswith(".cue"):
        return parse_cue(path, args)
    cfg = json.load(open(path))
    base = os.path.dirname(os.path.abspath(path))
    if not os.path.isabs(cfg["audio"]):
        cfg["audio"] = os.path.join(base, cfg["audio"])
    # normalize start times to seconds
    for t in cfg["tracks"]:
        t["start"] = parse_time(t["start"])
    # CLI overrides
    for k in ("title", "subtitle", "date"):
        if getattr(args, k):
            cfg[k] = getattr(args, k)
    if args.audio:
        cfg["audio"] = args.audio
    return cfg


def paint_waveform(buf, t_global, ctx):
    """Light the played bars bright and draw the playhead. The unplayed (dim) bars
    are already in the base; gaps are left untouched so the rain shows through."""
    L = ctx["L"]
    wave_w, wave_h = L["wave_w"], L["wave_h"]
    frac = min(1.0, t_global / ctx["total"]) if ctx["total"] else 0.0
    ph = int(wave_w * frac)
    b = buf[ctx["wy0"]:ctx["wy1"], ctx["wx0"]:ctx["wx1"]]
    sel = ctx["bar_mask"].copy()
    sel[:, ph:] = False                         # only played-region bars -> bright
    b[sel] = ctx["bright_col"][sel]
    # playhead: only as tall as the local waveform bar, in the theme color
    px = min(wave_w - 2, max(0, ph))
    env = ctx["env"]
    hh = max(2, int(env[min(len(env) - 1, px // ctx["pitch"])] * (wave_h - 2)))
    b[wave_h - hh:wave_h, px:px + 2] = ctx["ph_col"]


def paint_timecode(buf, t_global, ctx):
    tb = ctx["tc_box"]
    region = buf[tb[1]:tb[1] + tb[3], tb[0]:tb[0] + tb[2]]
    pim = Image.fromarray(region.copy())                 # draw over the live bg
    tot = fmt_time(ctx["total"]) if ctx["total"] else "--:--:--"
    anchor = ctx.get("tc_anchor", "la")
    x = tb[2] if anchor == "ra" else 0
    ImageDraw.Draw(pim).text((x, 0), f"{fmt_time(t_global)}  /  {tot}",
                             font=ctx["f_tc"], fill=ctx.get("tc_fill", THEME["white"]),
                             anchor=anchor)
    region[:] = np.asarray(pim)


def _add_dots(buf, ys, xs, col):
    for ox, oy in ((0, 0), (1, 0), (0, 1)):           # ~2px dots
        v = buf[ys + oy, xs + ox].astype(np.int16) + col
        buf[ys + oy, xs + ox] = np.clip(v, 0, 255).astype(np.uint8)


def paint_rain(buf, g, ctx):
    """Draw the faint dot grid plus every active snake. Each snake is sampled along
    its wandering path at a fixed spacing, snapped to the dot grid, masked to the
    visible background; the lead dot is bright, the rest a uniform dim tone."""
    gxv, gyv, faintv = ctx["gv"]
    if gxv.size:                                       # faint always-on dot grid
        _add_dots(buf, gyv, gxv, (ctx["dotcol"][None, :] * faintv[:, None]).astype(np.int16))
    sp, ox0 = ctx["grid_sp"], ctx["grid_x0"]
    ova, W, H = ctx["ov_a"], ctx["W"], ctx["H"]
    dot = ctx["dotcol"]
    step = RAIN["sample"]
    for d in ctx["active"]:
        px = np.asarray(d["px"])[::-1]                 # head first
        py = np.asarray(d["py"])[::-1]
        if px.size < 2:
            continue
        cum = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(px), np.diff(py)))])
        samp = np.arange(0.0, min(d["slen"], cum[-1]) + step, step)
        sx = np.interp(samp, cum, px)
        sy = np.interp(samp, cum, py)
        bx = ox0 + np.round((sx - ox0) / sp).astype(int) * sp     # snap to grid
        by = ox0 + np.round((sy - ox0) / sp).astype(int) * sp
        ok = (bx >= 0) & (bx < W - 1) & (by >= 0) & (by < H - 1)
        bx, by, samp = bx[ok], by[ok], samp[ok]
        if bx.size == 0:
            continue
        vis = ova[by, bx] < 40                         # only over visible background
        bx, by, samp = bx[vis], by[vis], samp[vis]
        if bx.size == 0:
            continue
        val = np.where(samp < step * 1.5, 1.0, 0.42)   # bright lead dot, dim body
        _add_dots(buf, by, bx, (dot[None, :] * val[:, None]).astype(np.int16))


def paint_spectrum(buf, t_global, ctx):
    """Draw the live spectrogram glyph (with lagging peak-hold dots) for now.
    Assumes buf already holds a fresh composited base (clean bg in this region)."""
    x0, y0, w, h = ctx["spec_box"]
    sp = ctx["spec"]
    if sp is None:
        return
    fi = min(len(sp) - 1, max(0, int(t_global * ctx["fps"])))
    vals = sp[fi]
    nb = len(vals)
    peak = ctx.get("spec_peak")
    if peak is None or len(peak) != nb:
        peak = vals.copy()
        ctx["spec_peak"] = peak
    np.maximum(peak - ctx["spec_decay"], vals, out=peak)  # dots lag, then fall
    pitch = max(1, w // nb)
    bw = max(2, int(pitch * 0.5))
    dh = 3                                                # peak-dot thickness
    span = h - dh - 1
    col, dot = ctx["spec_col"], ctx["spec_dot"]
    for k in range(nb):
        bx = x0 + k * pitch
        bh = max(1, int(vals[k] * span))
        buf[y0 + h - bh:y0 + h, bx:bx + bw] = col
        pk = max(bh, int(peak[k] * span))
        dy = min(max(y0 + h - pk - dh, y0), y0 + h - dh)
        buf[dy:dy + dh, bx:bx + bw] = dot


def safe_filename(s):
    s = re.sub(r'[\\/:*?"<>|]+', "_", s).strip()
    return (s[:80] or "track").rstrip(". ")


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="Render a DJ-mix visualizer video from a JSON config or a rekordbox .cue file.")
    ap.add_argument("config", help="path to a .json config or a rekordbox .cue file")
    ap.add_argument("out", nargs="?", help="output .mp4 (omit with --dry-run)")
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--res", default="1920x1080",
                    help="render resolution (the painters are tuned for 1920x1080; "
                         "use --upscale for 4K output)")
    ap.add_argument("--crf", type=int, default=18,
                    help="quality: x264 CRF, or NVENC CQ (lower = better/bigger)")
    ap.add_argument("--encoder", choices=("auto", "nvenc", "x264"), default="auto",
                    help="video encoder. 'auto' uses h264_nvenc (GPU) if available, "
                         "else libx264. NVENC offloads the encode to the GPU.")
    ap.add_argument("--upscale", metavar="WxH",
                    help="output at this resolution by lanczos-upscaling the rendered "
                         "frames (e.g. 3840x2160 for 4K) — keeps the 1080p-tuned look "
                         "but emits a true-4K file (YouTube allocates more bitrate to it)")
    # cue / display overrides
    ap.add_argument("--title", help="override mix title")
    ap.add_argument("--subtitle", help="override subtitle (defaults to cue PERFORMER)")
    ap.add_argument("--date", help="override date label")
    ap.add_argument("--audio", help="override mix audio path")
    ap.add_argument("--covers", help="folder of cover images named 01.jpg, 02.png, ...")
    ap.add_argument("--cover", action="append", metavar="N=PATH",
                    help="use a specific image for track N (1-based), repeatable, "
                         "e.g. --cover 3=art/choral.jpg")
    ap.add_argument("--music-root", action="append", metavar="OLD=NEW",
                    help="remap source-file path prefixes (repeatable), e.g. "
                         "'C:/Users/Gu5hy/Music=/home/veril0x/Music'")
    ap.add_argument("--no-clean-titles", action="store_true",
                    help="keep cue titles verbatim (don't strip YouTube ids / split artist)")
    ap.add_argument("--preview", nargs="?", const="__AUTO__", default=None, metavar="DIR",
                    help="render one still PNG per track (to DIR, or an auto-named "
                         "folder) for checking art/names, then exit")
    ap.add_argument("--video-preview", nargs="?", const=2.0, type=float, default=None,
                    metavar="SECS",
                    help="render a short MP4: SECS (default 2) of each track from its "
                         "start, concatenated, with matching audio")
    ap.add_argument("--static-bg", action="store_true",
                    help="use a still background (faster) instead of the animated rain effect")
    ap.add_argument("--theme",
                    choices=("rain", "plexus", "aurora", "ripple", "comets", "bokeh"),
                    default="rain",
                    help="visual theme: 'rain' (cool rain-on-window, default), "
                         "'plexus' (drifting constellation), 'aurora' (flowing "
                         "northern lights), 'ripple' (dot-grid sonar rings on the "
                         "beat), 'comets' (down-right streaks with bright heads + "
                         "thin tails) — all keep the default layout; or 'bokeh' "
                         "(warm lounge, minimal layout)")
    ap.add_argument("--theme-color", choices=tuple(PALETTES) + ("auto",), default=None,
                    help="color palette: blue | warm | pink, or 'auto' to derive the "
                         "whole palette from each track's cover art (changes per track). "
                         "Default: blue (warm for bokeh)")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and print the tracklist, then exit (no rendering)")
    ap.add_argument("--missing-art", action="store_true",
                    help="list only the tracks with no real cover art (position + name), "
                         "then exit — use to see which covers to supply via --covers")
    ap.add_argument("--chapters", nargs="?", const="-", metavar="FILE",
                    help="print YouTube-description chapter timestamps and exit; "
                         "writes to FILE if given, else stdout (no rendering)")
    ap.add_argument("--ffmpeg", help="path to ffmpeg.exe or its bin folder (if not on PATH)")
    ap.add_argument("--ffprobe", help="path to ffprobe.exe or its bin folder (if not on PATH)")
    args = ap.parse_args()
    resolve_tools(args)

    # Resolve theme + palette before anything renders: point the active THEME at
    # the chosen palette, and (for bokeh) repoint the font names to the serifs.
    global THEME, FONT_REG, FONT_BOLD, FONT_THIN
    # 'auto' derives the palette per-track from cover art (handled in base_for_track);
    # bokeh has no per-track art, so auto falls back to a fixed palette there.
    auto_palette = (args.theme_color == "auto") and args.theme != "bokeh"
    if args.theme_color in (None, "auto"):
        base_color = "warm" if args.theme == "bokeh" else "blue"
    else:
        base_color = args.theme_color
    THEME = PALETTES[base_color]
    if args.theme == "bokeh":
        FONT_REG, FONT_BOLD, FONT_THIN = FONT_SERIF, FONT_SERIF_BOLD, FONT_SERIF

    for spec in (args.music_root or []):
        if "=" not in spec:
            print(f"NOTE: --music-root '{spec}' has no '=NEW' part and will be ignored "
                  "(the format is OLD=NEW, e.g. "
                  "'C:/Users/Gu5hy/Music=/mnt/c/Users/Gu5hy/Music'). On WSL, C:/ source "
                  "paths are auto-mapped to /mnt/c, so you usually don't need this flag.",
                  flush=True)

    W, H = (int(x) for x in args.res.lower().split("x"))
    fps = args.fps
    cfg = load_config(args.config, args)
    tracks = cfg["tracks"]
    audio = cfg["audio"]

    # parse per-track cover overrides: --cover N=PATH
    args.cover_map = {}
    for spec in (args.cover or []):
        if "=" in spec:
            n, p = spec.split("=", 1)
            try:
                args.cover_map[int(n)] = p.strip()
            except ValueError:
                ap.error(f"--cover expects N=PATH with a track number, got: {spec}")
    preview_mode = args.preview is not None

    # ---- duration + start times, with rekordbox HH:MM:SS sanity check ----
    total = audio_duration(audio) if (audio and os.path.exists(audio)) else None
    starts = [parse_time(t["start"]) for t in tracks]
    if total and starts and max(starts) > total + 1:
        print("WARNING: track starts exceed audio length — check the cue time format "
              "or the --audio file.", flush=True)

    if args.dry_run:
        print(f"\nMix:      {cfg['title']}")
        print(f"Subtitle: {cfg['subtitle']}")
        print(f"Date:     {cfg['date']}")
        print(f"Audio:    {audio}  ({'%.1fs' % total if total else 'NOT FOUND'})")
        print(f"Tracks:   {len(tracks)}\n")
        for i, t in enumerate(tracks):
            src = remap_src(t.get("src", ""), args.music_root)
            has = src_has_art(src)
            art = "embedded" if has else ("no-art" if has is False else "missing")
            print(f"  {i+1:2d}. {fmt_time(starts[i])}  {t['artist'] or '(no artist)'} "
                  f"\u2014 {t['title']}   [art:{art}]")
        return

    if args.missing_art:
        base_dir = os.path.dirname(os.path.abspath(args.config))
        missing = [(i, t) for i, t in enumerate(tracks)
                   if not has_real_art(t, i, args, base_dir)]
        if not missing:
            print("All tracks have cover art.")
        else:
            print(f"Tracks with missing art ({len(missing)} of {len(tracks)}):")
            for i, t in missing:
                name = f"{t['artist']} \u2014 {t['title']}".strip(" \u2014") or "(untitled)"
                print(f"  {i+1:2d}. {name}")
        return

    if args.chapters is not None:
        text_out = youtube_chapters(tracks)
        if args.chapters == "-":
            print(text_out)
        else:
            with open(args.chapters, "w", encoding="utf-8") as fh:
                fh.write(text_out + "\n")
            print(f"Wrote {len(tracks)} chapters -> {args.chapters}", file=sys.stderr)
        return

    if not args.out and not preview_mode:
        ap.error("output file required (or use --preview / --dry-run)")
    if total is None:
        if preview_mode:
            total = (max(starts) if starts else 0) + 30      # estimate last track
            print("NOTE: audio not found — preview uses an estimated timeline and a "
                  "placeholder waveform.", flush=True)
        else:
            ap.error(f"audio not found: {audio}")
    ends = starts[1:] + [total]

    L = layout(W, H, args.theme)
    # one now-playing title size for all tracks, sized so the longest title fits
    _scratch = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    cfg["nt_size"] = uniform_fit(_scratch, [t.get("title", "") for t in tracks],
                                 FONT_BOLD, 66, L["spec_x"] - L["now_x"] - 40, floor=38)
    bar_w, gap = 2, 3                       # thin, clearly-separated bars
    n_bars = L["wave_w"] // (bar_w + gap)

    if audio and os.path.exists(audio):
        print("Extracting waveform envelope ...", flush=True)
        env = extract_envelope(audio, n_bars)
    else:
        env = 0.22 + 0.12 * np.abs(np.sin(np.linspace(0, 60, n_bars)))   # placeholder
    print("Building background ...", flush=True)
    bokeh_mode  = args.theme == "bokeh"
    plexus_mode = args.theme == "plexus"
    aurora_mode = args.theme == "aurora"
    ripple_mode = args.theme == "ripple"
    comets_mode = args.theme == "comets"
    rain_mode   = args.theme == "rain"
    animate = not args.static_bg
    marks = [s / total for s in starts] if total else []
    bokeh = plexus = aurora = None
    grid_x = grid_y = grid_faint = None
    if bokeh_mode:
        # warm bokeh field rebuilt per frame; no static base / rain / fog needed
        bokeh = make_bokeh(W, H)
        static_base = None
        fog_tex, fog_pad = None, 0
    else:
        # rain / plexus / aurora / ripple share the static gradient bg
        static_base = make_bg_base(W, H, blobs=not animate)   # blobs static only when not animating
        # rain + plexus + ripple use the drifting fog aura underneath
        fog_tex, fog_pad = make_fog_tex(W, H, tint=THEME["fog"]) \
            if (animate and not aurora_mode) else (None, 0)
        if plexus_mode:
            plexus = make_plexus(W, H)
        if aurora_mode:
            aurora = make_aurora(W, H)
        if rain_mode or ripple_mode or comets_mode:
            # dot grid: rain/comets snap streaks to it; ripple lights it in rings
            _rng = np.random.default_rng(7)
            _sp = 8
            _gx, _gy = np.meshgrid(np.arange(3, W - 2, _sp), np.arange(3, H - 2, _sp))
            grid_x, grid_y = _gx.ravel(), _gy.ravel()
            grid_faint = 0.022 + 0.03 * _rng.random(grid_x.size)

    if audio and os.path.exists(audio):
        print("Analyzing spectrum ...", flush=True)
        spec = extract_spectrum(audio, total, fps)
    else:
        spec = None

    base_dir = os.path.dirname(os.path.abspath(args.config))
    tmpdir = tempfile.mkdtemp(prefix="mixvid_")
    if bokeh_mode:
        # the minimal bokeh layout has no album art / thumbnails
        covers, palettes = [None] * len(tracks), None
    else:
        print("Loading cover art ...", flush=True)
        try:
            import mutagen  # noqa: F401
        except ImportError:
            if any(t.get("src") for t in tracks) and not args.covers:
                print("  hint: `pip install mutagen` for reliable embedded-art extraction "
                      "(needed for AIFF; ffmpeg alone misses those).", flush=True)
        covers = [resolve_cover(t, n, L["art"], args, tmpdir, base_dir)
                  for n, t in enumerate(tracks)]
        # Per-track waveform colors: played region = the cover's average color,
        # upcoming = grey, track-start ticks = a saturated version of the cover color.
        palettes = [cover_palette(c) for c in covers]

    def make_bands(i):
        if bokeh_mode:                              # uniform warm timeline (no art)
            return build_bands(env, L["wave_w"], L["wave_h"], bar_w, gap, marks,
                               played_color=THEME["wave_on"],
                               upcoming_color=THEME["wave_off"], mark_color=THEME["wave_mark"])
        played, mark = palettes[i]
        return build_bands(env, L["wave_w"], L["wave_h"], bar_w, gap,
                           marks, played_color=played,
                           upcoming_color=THEME["wave_off"], mark_color=mark)

    ctx = {
        "L": L, "total": total, "fps": fps,
        "wx0": L["wave_x"], "wx1": L["wave_x"] + L["wave_w"],
        "wy0": L["wave_y"], "wy1": L["wave_y"] + L["wave_h"],
        "bar_mask": None, "bright_col": None,   # set per-track from make_bands(i)
        "ph_col": np.array(THEME["accent2"], np.uint8),
        "env": env, "pitch": bar_w + gap,
        "tc_box": (L["tc_x"], L["tc_y"] - 6, 560, 42), "f_tc": font(FONT_MONO, 26),
        "spec": spec, "spec_box": (L["spec_x"], L["spec_y"], L["spec_w"], L["spec_h"]),
        "spec_col": np.array(THEME["spectrum"], np.uint8),
        "spec_dot": np.array(THEME["playhead"], np.uint8),
        "spec_peak": None, "spec_decay": max(0.02, 0.7 / fps),
        "dotcol": np.array(THEME["dot"], np.float32),
        "W": W, "H": H, "grid_sp": 8, "grid_x0": 3,
        "rng": np.random.default_rng(1234), "active": [], "ov_a": None,
        "fog_tex": fog_tex, "fog_pad": fog_pad, "fog_gain": None,
        "plexus": plexus, "aurora": aurora,
        "ripples": [], "rip_prev": 0.0, "rip_last": -999,
        "rip_col": _saturate(THEME["dot"]), "comets": [],
    }
    if bokeh_mode:
        # timecode bottom-right (warm), above the slim timeline (concept layout)
        _S = H / 1080.0
        _tcw = int(440 * _S)
        ctx["tc_box"] = (W - int(70 * _S) - _tcw, L["tc_y"], _tcw, int(46 * _S))
        ctx["tc_anchor"] = "ra"
        ctx["tc_fill"] = THEME["accent2"]

    uses_fog = rain_mode or plexus_mode or ripple_mode or comets_mode

    def base_for_track(i):
        """Prepare track i's overlay. For rain, also bake the static bg + overlay
        and set up the rain dot grid. For bokeh, the bg is per-frame, so just keep
        the overlay (rgb + alpha) for compositing later; returns None."""
        global THEME
        bg = static_base
        if auto_palette and covers[i] is not None:
            # rebuild the whole palette from this cover, then refresh everything
            # that caches a color (overlay/bands read THEME live below).
            THEME = palette_from_cover(covers[i])
            ctx["ph_col"] = np.array(THEME["accent2"], np.uint8)
            ctx["spec_col"] = np.array(THEME["spectrum"], np.uint8)
            ctx["spec_dot"] = np.array(THEME["playhead"], np.uint8)
            ctx["dotcol"] = np.array(THEME["dot"], np.float32)
            ctx["rip_col"] = _saturate(THEME["dot"])
            bg = make_bg_base(W, H, blobs=False)
            if uses_fog:
                ctx["fog_tex"], ctx["fog_pad"] = make_fog_tex(W, H, tint=THEME["fog"])
        dim_rgba, ctx["bar_mask"], ctx["bright_col"] = make_bands(i)
        if bokeh_mode:
            ov_rgb, ov_a = render_base_bokeh(cfg, tracks, i, W, H)
        else:
            ov_rgb, ov_a = render_base(cfg, tracks, i, W, H, L, covers, dim_rgba)
        ctx["ov_a"] = ov_a
        ctx["ov_rgb"] = ov_rgb
        if bokeh_mode:
            return None
        base = composite_over(bg, ov_rgb, ov_a)
        ctx["fog_gain"] = (255 - ov_a).astype(np.int16)   # fog only over visible bg
        if rain_mode or ripple_mode or comets_mode:
            vis = ov_a[grid_y, grid_x] < 40      # grid dots over visible bg only
            ctx["gv"] = (grid_x[vis], grid_y[vis], grid_faint[vis])
        return base

    # ----------------------------------------------------------------- #
    #  Preview: one still per track, then exit
    # ----------------------------------------------------------------- #
    if preview_mode:
        pdir = args.preview
        if pdir == "__AUTO__":
            pdir = os.path.splitext(os.path.abspath(args.config))[0] + "_preview"
        os.makedirs(pdir, exist_ok=True)
        print(f"Rendering {len(tracks)} preview stills -> {pdir}", flush=True)
        for i in range(len(tracks)):
            base = base_for_track(i)
            t_at = min(ends[i] - 0.01, starts[i] + 0.5)     # just into the track
            if bokeh_mode:
                buf = composite_over(bokeh_frame(bokeh, i * 13, fps),
                                     ctx["ov_rgb"], ctx["ov_a"])
            else:
                buf = base
                if animate:
                    if aurora_mode:
                        paint_aurora(buf, i * 7, ctx, np.full(16, 0.4, np.float32))
                    elif ripple_mode:
                        paint_fog(buf, i * 40, ctx)
                        rr = np.random.default_rng(i + 1)
                        ctx["ripples"] = []
                        for _ in range(5):
                            cx, cy = rr.uniform(0, W), rr.uniform(0, H)
                            ctx["ripples"].append(dict(cx=cx, cy=cy,
                                                       born=-int(rr.uniform(5, 45)),
                                                       inten=1.0,
                                                       maxr=_ring_maxr(cx, cy, W, H)))
                        paint_ripple(buf, 0, ctx)
                    elif comets_mode:
                        ctx["comets"] = []
                        for _ in range(50):
                            _comet_spawn(ctx, 0.6, ctx["rng"].random())
                        for c in ctx["comets"]:      # scatter heads across the frame
                            adv = ctx["rng"].uniform(0.2, 1.0) * c["L"]
                            c["x"] += c["ux"] * adv; c["y"] += c["uy"] * adv
                        paint_fog(buf, i * 40, ctx)
                        paint_comets(buf, 0, ctx)
                    elif plexus_mode:
                        paint_fog(buf, i * 40, ctx)
                        paint_plexus(buf, ctx, 0.55)
                    else:
                        paint_fog(buf, i * 40, ctx)
                        ctx["active"] = []
                        seed_rain(ctx, 70)                  # populated snapshot
                        paint_rain(buf, 0, ctx)
            paint_waveform(buf, t_at, ctx)
            paint_timecode(buf, t_at, ctx)
            if not bokeh_mode:
                paint_spectrum(buf, t_at, ctx)
            label = safe_filename(f"{tracks[i].get('artist','')} - {tracks[i].get('title','')}".strip(" -"))
            out_png = os.path.join(pdir, f"{i+1:02d}_{label}.png")
            Image.fromarray(buf).save(out_png)
            print(f"  {os.path.basename(out_png)}", flush=True)
        print("Preview done. Fix any art/names, then re-run without --preview.")
        return

    # ----------------------------------------------------------------- #
    #  Encoder + optional upscale (resolve once)
    # ----------------------------------------------------------------- #
    use_nvenc = args.encoder == "nvenc"
    if args.encoder == "auto":
        try:
            encs = subprocess.run([FFMPEG, "-hide_banner", "-encoders"],
                                  capture_output=True, text=True).stdout
            use_nvenc = "h264_nvenc" in encs
        except Exception:
            use_nvenc = False
    if use_nvenc:
        vcodec = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                  "-cq", str(args.crf), "-pix_fmt", "yuv420p"]
    else:
        vcodec = ["-c:v", "libx264", "-crf", str(args.crf),
                  "-preset", "medium", "-pix_fmt", "yuv420p"]
    scale_vf = []
    if args.upscale:
        uw, uh = (int(x) for x in args.upscale.lower().split("x"))
        scale_vf = ["-vf", f"scale={uw}:{uh}:flags=lanczos"]
    print(f"Encoder:  {'h264_nvenc (GPU)' if use_nvenc else 'libx264 (CPU)'}"
          f"{'  upscale -> ' + args.upscale if args.upscale else ''}", flush=True)

    # ----------------------------------------------------------------- #
    #  Streaming renderer (shared by full render + video preview)
    # ----------------------------------------------------------------- #
    def render_segments(out_path, audio_in, segments, label):
        """segments: list of (track_index, seg_start_sec, n_frames)."""
        cmd = [FFMPEG, "-y", "-v", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps),
               "-i", "-", "-i", audio_in,
               *scale_vf, *vcodec, "-c:a", "aac", "-b:a", "256k",
               "-shortest", out_path]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        totf = sum(n for _, _, n in segments)
        if animate and rain_mode:
            ctx["active"] = []
            seed_rain(ctx, 60)                     # pre-warm so frame 0 isn't empty
        if animate and comets_mode:
            ctx["comets"] = []
            for _ in range(70):                    # pre-warm: scatter comets in-frame
                _comet_spawn(ctx, 0.6, ctx["rng"].random())
            for c in ctx["comets"]:
                adv = ctx["rng"].uniform(0.0, 1.0) * (c["L"] + ctx["H"])
                c["x"] += c["ux"] * adv; c["y"] += c["uy"] * adv
        nbins = ctx["spec"].shape[1] if ctx["spec"] is not None else 16
        g = done = 0
        t0 = time.time()
        def _hms(s):
            s = int(s); return f"{s//3600:d}:{(s%3600)//60:02d}:{s%60:02d}"
        def show_progress():
            el = time.time() - t0
            rate = done / el if el > 0 else 0
            eta = (totf - done) / rate if rate > 0 else 0
            frac = done / max(1, totf)
            barw = 28
            fill = int(barw * frac)
            bar = "#" * fill + "-" * (barw - fill)
            sys.stdout.write(
                f"\r  [{bar}] {100*frac:5.1f}%  {done}/{totf}f  "
                f"{rate:4.1f} fps  elapsed {_hms(el)}  ETA {_hms(eta)}   ")
            sys.stdout.flush()
        for (i, seg_start, nfr) in segments:
            base = base_for_track(i)               # bg + overlay (static parts)
            for fnum in range(nfr):
                t = seg_start + fnum / fps
                if bokeh_mode:
                    buf = composite_over(bokeh_frame(bokeh, g, fps),
                                         ctx["ov_rgb"], ctx["ov_a"])
                else:
                    buf = base.copy()
                    if animate:
                        if ctx["spec"] is not None:
                            sf = ctx["spec"][min(len(ctx["spec"]) - 1, max(0, int(t * fps)))]
                        else:
                            sf = np.full(nbins, 0.2, np.float32)
                        if aurora_mode:
                            paint_aurora(buf, g, ctx, sf)
                        elif ripple_mode:
                            advance_ripples(ctx, g, sf)
                            paint_fog(buf, g, ctx)
                            paint_ripple(buf, g, ctx)
                        elif comets_mode:
                            advance_comets(ctx, g, sf)
                            paint_fog(buf, g, ctx)
                            paint_comets(buf, g, ctx)
                        elif plexus_mode:
                            paint_fog(buf, g, ctx)     # drifting blue aura
                            pulse = float(np.clip(0.2 + 1.8 * sf.mean(), 0, 1))
                            paint_plexus(buf, ctx, pulse)
                        else:
                            paint_fog(buf, g, ctx)     # drifting blue aura ("blowing")
                            advance_rain(ctx, g, sf)   # cull + spectrum-driven spawn
                            paint_rain(buf, g, ctx)    # draw the snakes this frame
                paint_waveform(buf, t, ctx)
                if not bokeh_mode:
                    paint_spectrum(buf, t, ctx)
                paint_timecode(buf, t, ctx)
                proc.stdin.write(buf.tobytes())
                g += 1
                done += 1
                if done % 10 == 0:
                    show_progress()
        show_progress()
        sys.stdout.write("\n")
        proc.stdin.close()
        proc.wait()

    # ----------------------------------------------------------------- #
    #  Video preview: a few seconds of each track, concatenated
    # ----------------------------------------------------------------- #
    if args.video_preview is not None:
        psec = args.video_preview
        segs = []
        for i in range(len(tracks)):
            nfr = max(1, int(round(min(psec, ends[i] - starts[i]) * fps)))
            segs.append((i, starts[i], nfr))
        # build the matching concatenated audio
        pa = os.path.join(tmpdir, "preview_audio.wav")
        parts, labs = [], []
        for (i, s, nfr) in segs:
            end = min(s + nfr / fps, total)
            parts.append(f"[0:a]atrim=start={s:.3f}:end={end:.3f},asetpts=N/SR/TB[a{i}]")
            labs.append(f"[a{i}]")
        filt = ";".join(parts) + ";" + "".join(labs) + f"concat=n={len(segs)}:v=0:a=1[out]"
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", audio,
                        "-filter_complex", filt, "-map", "[out]", "-ac", "2", pa],
                       check=True)
        print(f"Rendering video preview ({psec:g}s/track) -> {args.out}", flush=True)
        render_segments(args.out, pa, segs, "preview")
        print("Video preview ->", args.out)
        return

    # ----------------------------------------------------------------- #
    #  Full render
    # ----------------------------------------------------------------- #
    segments = [(i, starts[i], max(1, int(round((ends[i] - starts[i]) * fps))))
                for i in range(len(tracks))]
    render_segments(args.out, audio, segments, "track")
    print("Done ->", args.out)


if __name__ == "__main__":
    main()
