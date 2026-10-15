"""Audio/video analysis with ffmpeg, and speech-to-text.

Claude reads text, not sound, so narration is converted to text here first
(faster-whisper locally, or any OpenAI-compatible transcription endpoint).
"""
import json
import re
import subprocess
from pathlib import Path

from . import config

_whisper = None


def run(cmd, timeout=1800):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def probe(path):
    code, out, _ = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], 120)
    if code != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def duration(info):
    try:
        return float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def has_stream(info, kind):
    return any(s.get("codec_type") == kind for s in (info or {}).get("streams", []))


def audio_stats(path, gap=None):
    """Returns dict: mean_db, max_db, silences [(start, end)], leading, trailing."""
    gap = gap or config.SILENCE_GAP_SECONDS
    code, _, err = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
                        "-af", f"silencedetect=noise=-45dB:d={gap},volumedetect", "-f", "null", "-"])
    res = {"mean_db": None, "max_db": None, "silences": []}
    m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", err)
    if m:
        res["mean_db"] = float(m.group(1)) if m.group(1) != "-inf" else -200.0
    m = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", err)
    if m:
        res["max_db"] = float(m.group(1)) if m.group(1) != "-inf" else -200.0
    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", err)]
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        res["silences"].append((max(0.0, s), e))
    return res


def video_picture_checks(path):
    """Black frames and frozen picture (ffmpeg blackdetect / freezedetect)."""
    code, _, err = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-an",
                        "-vf", "blackdetect=d=2:pix_th=0.10,freezedetect=n=0.003:d=6", "-f", "null", "-"])
    blacks = [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+)\s+black_end:([\d.]+)", err)]
    fs = [float(x) for x in re.findall(r"freeze_start:\s*([\d.]+)", err)]
    fe = [float(x) for x in re.findall(r"freeze_end:\s*([\d.]+)", err)]
    freezes = [(s, fe[i] if i < len(fe) else None) for i, s in enumerate(fs)]
    return blacks, freezes


def extract_embedded_subs(path, out_dir: Path):
    info = probe(path) or {}
    subs = [s for s in info.get("streams", []) if s.get("codec_type") == "subtitle"]
    if not subs:
        return None
    out = out_dir / "embedded.srt"
    code, _, _ = run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-map", f"0:{subs[0]['index']}", str(out)], 300)
    return out if code == 0 and out.exists() else None


def to_wav(path, out_path):
    code, _, err = run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", str(out_path)])
    return code == 0


# ------------------------------------------------------------- subtitles
def ts(sec):
    sec = max(0, sec or 0)
    return f"{int(sec // 3600):02d}:{int(sec % 3600 // 60):02d}:{int(sec % 60):02d}"


def _tc(s):
    s = s.strip().replace(",", ".")
    parts = s.split(":")
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return 0.0
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_subtitles(path: Path):
    """Parses .srt or .vtt into [(start, end, text)]."""
    txt = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r", "")
    cues = []
    for block in re.split(r"\n\s*\n", txt):
        lines = [l for l in block.split("\n") if l.strip()]
        for i, l in enumerate(lines):
            m = re.match(r"\s*([\d:.,]+)\s*-->\s*([\d:.,]+)", l)
            if m:
                text = " ".join(lines[i + 1:])
                text = re.sub(r"<[^>]+>|\{[^}]+\}", "", text).strip()
                if text:
                    cues.append((_tc(m.group(1)), _tc(m.group(2)), text))
                break
    return cues


# ------------------------------------------------------------- speech to text
class STTUnavailable(Exception):
    pass


def stt_available():
    eng = config.STT_ENGINE.lower()
    if eng == "none":
        return False
    if eng == "openai":
        return bool(config.STT_API_KEY)
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return bool(config.STT_API_KEY)


def speech_regions(path, dur=None):
    """Non-silent stretches [(start, end)] – a rough map of where someone is speaking."""
    code, _, err = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
                        "-af", "silencedetect=noise=-35dB:d=0.6", "-f", "null", "-"])
    if dur is None:
        dur = duration(probe(path))
    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", err)]
    regions, pos = [], 0.0
    for i, s0 in enumerate(starts):
        s0 = max(0.0, s0)
        if s0 - pos > 0.2:
            regions.append((pos, s0))
        pos = ends[i] if i < len(ends) else dur
    if dur - pos > 0.2:
        regions.append((pos, dur))
    return regions


def speech_seconds(path):
    return sum(b - a for a, b in speech_regions(path))


def transcribe(path):
    """Returns [(start, end, text)] segments."""
    eng = config.STT_ENGINE.lower()
    if eng == "none":
        raise STTUnavailable("Speech-to-text is turned off (STT_ENGINE=none)")
    if eng == "openai":
        return _transcribe_api(path)
    global _whisper
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        if config.STT_API_KEY:
            return _transcribe_api(path)
        raise STTUnavailable("faster-whisper is not installed and no STT_API_KEY is set")
    if _whisper is None:
        _whisper = WhisperModel(config.WHISPER_MODEL, device=config.WHISPER_DEVICE,
                                compute_type="float16" if config.WHISPER_DEVICE == "cuda" else "int8")
    segs, _ = _whisper.transcribe(str(path), beam_size=5, vad_filter=False, language="en")
    return [(s.start, s.end, s.text.strip()) for s in segs]


def _transcribe_api(path):
    import requests
    if not config.STT_API_KEY:
        raise STTUnavailable("STT_ENGINE=openai needs STT_API_KEY")
    with open(path, "rb") as fh:
        r = requests.post(config.STT_API_URL, headers={"Authorization": f"Bearer {config.STT_API_KEY}"},
                          files={"file": (Path(path).name, fh)},
                          data={"model": config.STT_API_MODEL, "response_format": "verbose_json"}, timeout=900)
    r.raise_for_status()
    j = r.json()
    if j.get("segments"):
        return [(s.get("start", 0), s.get("end", 0), s.get("text", "").strip()) for s in j["segments"]]
    return [(0, 0, j.get("text", ""))]


def caption_frame(video, t, text, out, width=1366):
    """A frame at `t` with the caption drawn on it like a player would, plus each word's position.

    Returns {"nodes": [...], "words": [...]} in the frame's pixel space (width = `width`)."""
    from PIL import Image, ImageDraw, ImageFont
    tmp = Path(str(out) + ".src.png")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0, t):.2f}", "-i", str(video), "-frames:v", "1",
         "-vf", f"scale={width}:-2", str(tmp)], 60)
    if tmp.exists():
        im = Image.open(tmp).convert("RGB")
    else:
        im = Image.new("RGB", (width, round(width * 9 / 16)), (20, 20, 20))
    W, H = im.size
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", max(18, W // 45))
    except OSError:
        font = ImageFont.load_default()
    d = ImageDraw.Draw(im, "RGBA")
    space = d.textlength(" ", font=font)
    maxw = W * 0.86
    lines, cur, curw = [], [], 0.0
    for w in text.split():
        ww = d.textlength(w, font=font)
        if cur and curw + space + ww > maxw:
            lines.append(cur); cur, curw = [], 0.0
        cur.append((w, ww)); curw += (space if len(cur) > 1 else 0) + ww
    if cur:
        lines.append(cur)
    lh = int(font.size * 1.35)
    block_h = lh * len(lines) + 16
    top = H - block_h - int(H * 0.05)
    widest = max((sum(x[1] for x in ln) + space * (len(ln) - 1)) for ln in lines) if lines else 0
    d.rectangle([(W - widest) / 2 - 14, top, (W + widest) / 2 + 14, top + block_h], fill=(0, 0, 0, 185))
    words = []
    for i, ln in enumerate(lines):
        lw = sum(x[1] for x in ln) + space * (len(ln) - 1)
        x = (W - lw) / 2
        y = top + 8 + i * lh
        for w, ww in ln:
            d.text((x, y), w, font=font, fill=(255, 255, 255))
            words.append({"w": w, "n": 0, "b": [x, y, ww, font.size * 1.15]})
            x += ww + space
    im.save(out, "PNG")
    tmp.unlink(missing_ok=True)
    return {"nodes": [{"t": text, "b": [(W - widest) / 2, top, widest, block_h]}], "words": words}


# ---------------------------------------------------------------- course videos the test browser can play
# The test browser is open-source Chromium: it plays MP3, WebM, VP9, AV1, Opus and Vorbis, but not H.264 video
# or AAC audio (the usual Storyline MP4 / M4A). Before a course is tested, every such file in the unpacked copy is
# re-encoded in place (same name, same length) so the screen plays as it does for a learner. The upload itself is
# never changed. Fixed settings, so the same file always gives the same result.
PLAYABLE_VIDEO = {"vp8", "vp9", "av1", "theora"}
PLAYABLE_AUDIO = {"mp3", "opus", "vorbis", "flac", "pcm_s16le", "pcm_s24le", "pcm_f32le", "pcm_u8"}
MEDIA_EXT = (".mp4", ".m4v", ".mov", ".m4a", ".aac", ".webm", ".ogv")


def _codecs(path):
    info = probe(path) or {}
    v = [s.get("codec_name") for s in info.get("streams", []) if s.get("codec_type") == "video"
         and not (s.get("disposition") or {}).get("attached_pic")]
    a = [s.get("codec_name") for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    return v, a


def media_to_prepare(root: Path):
    """[(path, video codecs, audio codecs, seconds)] of course media the test browser cannot play."""
    todo = []
    for p in sorted(p for p in Path(root).rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_EXT):
        v, a = _codecs(p)
        if (v and any(c not in PLAYABLE_VIDEO for c in v)) or (a and any(c not in PLAYABLE_AUDIO for c in a)):
            todo.append((p, v, a, duration(probe(p) or {})))
    return todo


def prepare_estimate(todo):
    """Planned seconds to prepare the media (measured: about 0.6 s per second of 4K video, less for smaller)."""
    return sum(max(2.0, d * (0.6 if v else 0.05)) for _, v, _, d in todo)


def make_playable(root: Path, log=print, todo=None, tick=None):
    """Re-encodes course media the test browser cannot play. Returns [{file, from, seconds}] for the report.
    Video is made at most 1280 pixels wide (the size the course is seen at), which keeps large 4K videos quick."""
    import time
    done = []
    todo = media_to_prepare(root) if todo is None else todo
    total = sum(max(1.0, d) for *_, d in todo) or 1.0
    so_far = 0.0
    for k, (p, v, a, dur) in enumerate(todo, 1):
        mb = p.stat().st_size / 1e6
        log(f"Preparing video {k} of {len(todo)} for playback ({p.name}, {mb:.0f} MB"
            + (f", {media_ts(dur)}" if dur else "") + ")")
        if tick:
            tick("media", so_far, total)
        t0 = time.time()
        tmp = p.with_name(p.stem + ".__play__" + p.suffix)
        cmd = ["ffmpeg", "-y", "-v", "error", "-threads", "0", "-i", str(p), "-map", "0:v?", "-map", "0:a?"]
        if v:
            cmd += ["-vf", "scale='min(1280,iw)':-2", "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8",
                    "-row-mt", "1", "-b:v", "0", "-crf", "34", "-pix_fmt", "yuv420p"]
        if a:
            cmd += ["-c:a", "libopus", "-b:a", "96k", "-ar", "48000"]
        cmd += ["-movflags", "+faststart", "-f", "mp4", str(tmp)]
        try:
            code, _, err = run(cmd, 1800)
            if code == 0 and tmp.exists() and tmp.stat().st_size > 0:
                tmp.replace(p)
                done.append({"file": p.relative_to(root).as_posix(), "from": "/".join(v + a),
                             "seconds": round(time.time() - t0, 1)})
                so_far += max(1.0, dur)
            else:
                log(f"Could not prepare {p.name} for playback")
        except Exception as e:
            log(f"Could not prepare {p.name} for playback: {e!r}"[:200])
        finally:
            if tmp.exists():
                tmp.unlink()
    if tick and todo:
        tick("media", total, total)
    return done


def media_ts(sec):
    sec = int(round(sec or 0))
    return f"{sec // 60}:{sec % 60:02d} min"
