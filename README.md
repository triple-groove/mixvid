# mixvid — DJ-mix visualizer renderer

Renders a "now playing" mix video straight to MP4 — no video editor. The look is
modeled on liquid-DnB "now playing" videos (f1rstpers0n style): album art, a
**previous-tracks** list, a full-mix thin-bar waveform with a moving playhead, a
live spectrogram counter, a live timecode, and an animated **"rain on window"**
background (drifting blue fog + dotted diagonal rain snakes driven by the audio).

Takes either a **rekordbox `.cue`** export or a **JSON** config.

## How it works
- Decodes the audio **once** into a peak envelope (the bottom waveform) and a
  short-time spectrum (drives the spectrogram counter and the rain).
- Builds a per-track **RGBA overlay** with Pillow (album art, tracklist, labels,
  counter) and composites it over the background.
- Streams raw frames to **ffmpeg**, redrawing the moving parts each frame: the
  animated background (rain + fog), the played waveform lighting up, the playhead,
  the spectrogram bars, and the ticking timecode.
- The audio is muxed in directly, so output length == audio length.

The animated background is the default. It's heavier than a still frame; pass
`--static-bg` for a fast baked background (no rain/fog) when you just want a
quick render.

## Requirements
`ffmpeg` + `ffprobe` on PATH, and `pip install pillow numpy`.
**`pip install mutagen`** is strongly recommended — it's what reliably reads
embedded album art, especially from **AIFF** (ffmpeg alone doesn't expose AIFF
artwork). On Windows/WSL, if ffmpeg isn't on PATH, point at it with
`--ffmpeg "C:/ffmpeg/bin"` (ffprobe is found alongside it).

---

## rekordbox `.cue` input  (the main path)

```bash
# see what got parsed before committing to a render
python3 mixvid.py 01_REC-2026-06-27.cue --dry-run \
    --music-root "C:/Users/Gu5hy/Music/DJ_Library=/home/veril0x/Music/DJ_Library"

# render it
python3 mixvid.py 01_REC-2026-06-27.cue out.mp4 \
    --music-root "C:/Users/Gu5hy/Music/DJ_Library=/home/veril0x/Music/DJ_Library" \
    --title "Rainsongs Vol.8 | Liquid Trance Mix" --fps 15
```

What it pulls from the cue:
- **Mix audio** <- the top-level `FILE` (resolved next to the .cue; override with `--audio`).
- **Tracklist** <- each `TRACK` block's `TITLE`, `PERFORMER`, and `INDEX 01`.
- **Subtitle** <- the top-level `PERFORMER` (the DJ name). **Date** <- `REM DATE`.
- **Cover art** <- embedded artwork in each track's source `FILE` (see below).

Notable rekordbox quirks this handles automatically:
- **`INDEX` is `HH:MM:SS`, not the CUE-standard `MM:SS:FF`.** rekordbox writes
  wall-clock there; parsing it as frames would make a 55-minute set look 55
  seconds long. There's a sanity check that warns if track starts overrun the
  audio length.
- **Missing `PERFORMER`** (common on YouTube rips): if the title looks like
  `Artist - Title`, it's split; a trailing YouTube id (`...-bXLC5a7GgR4`) is
  stripped. Disable with `--no-clean-titles`.
- **Mojibake** like `Lange<garbled>s` is repaired to `Lange's` (only the broken
  byte-sequences -- correct accents like `Cafe`/`Café` are left alone).

### Cover art for cue input
Per track, in priority order:
1. `--covers DIR` -- a folder with files named `01.jpg`, `02.png`, ... (manual override).
2. **Embedded artwork** extracted from the track's original source file
   (Beatport AIFF/MP3, FLAC, M4A all carry it). The source paths in the cue are
   Windows paths from your DJ library, so map them onto your Fedora mount with
   `--music-root OLD=NEW` (repeatable). **On WSL this is automatic** — Windows
   `C:/...` source paths are mapped to `/mnt/c/...` with no flag needed. Use
   `--music-root` only when your library lives somewhere else (e.g. a NAS mount).
   Note `--music-root` takes a full `OLD=NEW` pair, not just the new root.
3. A deterministic colored placeholder.

Use `--dry-run` to see, per track, whether art resolved as `embedded`,
`no-art` (file found, no image), or `missing` (path didn't resolve).

---

## JSON input

```bash
python3 mixvid.py config.json out.mp4 --fps 15
```
```json
{
  "title": "Rainsongs Vol.8 | Liquid Drum & Bass Mix",
  "subtitle": "f1rstpers0n #236",
  "date": "Mar 2026",
  "audio": "mix.flac",
  "tracks": [
    {"start": "0:00", "title": "The Place", "artist": "Jack Boston",
     "release": "The Place / Always There", "cover": "covers/01.jpg"}
  ]
}
```
`start` accepts `H:MM:SS`, `MM:SS`, or seconds. Each track runs to the next
one's start; the last runs to end of audio. `cover`/`release` optional.

---

## Previewing & fixing art / names

### Still preview — one PNG per track
Before committing to a full render, dump one still per track:

```bash
python3 mixvid.py 01_REC-2026-06-27.cue --preview \
    --music-root "C:/Users/Gu5hy/Music/DJ_Library=/home/veril0x/Music/DJ_Library"
```

This writes `01_Artist - Title.png`, `02_...png`, ... into an auto-named
`<cue>_preview/` folder (or pass your own `--preview DIR`). Each still is the
exact layout for that track -- its art, title, the previous-tracks list, the
counter, and the playhead parked at that track's start -- so you can flip
through and catch wrong covers or messy titles. Preview works even before you
have the final audio (it falls back to an estimated timeline + flat waveform).

### Video preview — a few seconds of each track
To check the **motion** (rain, spectrogram, playhead) without rendering the whole
mix, render a short MP4 of a few seconds from each track, concatenated with
matching audio:

```bash
# NOTE: put the output path BEFORE --video-preview (its optional value would
# otherwise swallow the positional out arg).
python3 mixvid.py 01_REC-2026-06-27.cue vpreview.mp4 \
    --covers Covers/ --video-preview 4      # 4 seconds per track (default 2)
```

### Fixing covers / titles
Fix a wrong/missing cover by pointing a track at an image (1-based track number,
beats embedded art and everything else):

```bash
python3 mixvid.py 01_REC-2026-06-27.cue out.mp4 \
    --cover 3=art/choral_reef.jpg \
    --cover 12=art/field_of_joy.png
```

Wrong title/artist? Edit the `.cue` directly, or use `--no-clean-titles` if the
auto-cleaning split something it shouldn't have. Re-run `--preview` to confirm,
then render without `--preview`.

## Progress
The full render and the video preview show a live single-line progress bar with
percent, frames done/total, encode speed (fps), elapsed time, and **ETA**:

```
  [############----------------]  43.2%  47200/109200f  18.3 fps  elapsed 0:42:58  ETA 0:56:27
```

The ETA settles after ~10–20 s of frames (it averages over all frames so far).

## Options
```
--fps 15            frames/sec (raise to 30 for a smoother playhead)
--res 1920x1080     output resolution
--crf 18            x264 quality (lower = better/bigger)
--title / --subtitle / --date / --audio    override cue/JSON display fields
--covers DIR        manual cover folder (01.jpg, 02.png, ...)
--cover N=PATH      use a specific image for track N (1-based), repeatable
--music-root OLD=NEW   remap source-file path prefixes (repeatable; auto on WSL)
--ffmpeg PATH / --ffprobe PATH   point at ffmpeg/ffprobe if not on PATH
--no-clean-titles   keep cue titles verbatim
--preview [DIR]     render one still PNG per track for checking art/names, then exit
--video-preview [SECS]   render SECS (default 2) of each track, concatenated, then exit
--static-bg         still background (faster) instead of the animated rain/fog
--dry-run           parse + print the tracklist, then exit
```

## Customizing the look
Top of `mixvid.py`:
- **`THEME`** -- every color (background gradient, accent blue, cyan counter/playhead,
  played/un-played waveform, track-start tick `wave_mark`, `spectrum` bars).
- **`FONT_REG / FONT_BOLD / FONT_MONO / FONT_THIN`** -- point at your own `.ttf`.
  Timecode uses the mono font so digits don't jitter; the big lower-right track
  counter uses the thin/geometric face.
- **`layout(W, H)`** -- all geometry (waveform/timecode/counter/spectrogram positions).
- **`list_len`** in `render_base()` -- how many previous tracks the list shows (default 5).
- **`RAIN`** dict + `paint_rain` / `paint_fog` / `make_fog_tex` -- the animated
  background: rain snake speed, length (bass→long, treble→short), spawn density,
  wander, and fog drift.
- Waveform: `bar_w, gap` in `main()`; the envelope uses peak + 0.7 gamma.

## Notes
- Software x264 by default. On a 5090, swap the encoder in the `cmd` list to
  `-c:v h264_nvenc -preset p5 -cq 19` for a big speedup.
- The animated background is the slow part; `--static-bg` is the fast path.
- 4K: `--res 3840x2160` (bump the font sizes in `render_base`/`layout`).
