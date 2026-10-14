#!/usr/bin/env python3
"""
Batch video editor for TikTok (9:16, 1080x1920, 30fps, H.264 MP4).

Pipeline per video:
  1. Trim 1-2 s at the start and end.
  2. Auto-cut silent parts (dead air) with ffmpeg silencedetect.
  3. Move the loudest/most energetic 3 s window to the front as a hook.
  4. Speed 1.04x for video + audio, WITHOUT pitch preservation (~+0.68 semitone).
  5. Layout into 1080x1920 keeping content inside the 880x1420 central safe box.
  6. Optional horizontal flip.
  7. Dynamic zoom oscillating between 102% and 105%.
  8. Color grade: contrast +3%, brightness -2%.
  9. Film grain overlay (~2-3% opacity).
 10. Frame Drop/Duplicate (Ngẫu nhiên xô lệch khung hình).
 11. Animated progress bar (top or bottom).
 12. Background music from a folder, looped, at 3-5% volume.
 13. Blur the original subtitle/logo areas and burn in new word-by-word subtitles.
 14. Short flash / glitch / B-roll (0.5 s) inserts at random points.
 15. Ghost Overlay (Cấy bóng ma thị giác 1% chạy quanh màn hình).
 16. Voice EQ (cut low bass + high treble) and a light reverb (< 5%).

Usage:
  python3 tiktok_batch.py -i input/ -o output/ -m music/
  python3 tiktok_batch.py -i input/ -o output/ -m music/ --flip --jobs 2
  python3 tiktok_batch.py -i input/ -o output/ -m music/ -b broll/ \
      --blur-box 0,0.78,1,0.17 --subs auto
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import math
import random
import re
import subprocess
import sys
import threading
import time

import effects as efx
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac", ".opus"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

OUT_W, OUT_H = 1080, 1920
SAFE_W, SAFE_H = 880, 1420
FPS = 30
SPEED = 1.04
SAMPLE_RATE = 44100

Segment = tuple[float, float]  # (start, end) in seconds on the source timeline


@dataclass
class Settings:
    music_dir: Path | None
    trim_head: tuple[float, float]
    trim_tail: tuple[float, float]
    silence_db: float
    silence_min: float
    silence_pad: float
    hook_len: float
    hook_mode: str
    chop_keep: float
    chop_drop: float
    layout: str
    flip: bool
    zoom_min: float
    zoom_max: float
    contrast: float
    brightness: float
    grain: float
    bar_pos: str
    bar_color: str
    bar_height: int
    bgm_volume: tuple[float, float]
    bitrate: float
    maxrate: float
    preset: str
    overwrite: bool
    keep_temp: bool
    blur_boxes: list[tuple[float, float, float, float]]
    subs: str
    whisper_model: str
    sub_lang: str | None
    font: str
    font_size: int
    sub_upper: bool
    broll_dir: Path | None
    fx_every: tuple[float, float]
    broll_len: float
    eq_low: float
    eq_high: float
    reverb: float
    n_effects: int
    n_filters: int
    effects: list[str]
    filters: list[str]
    stickers_dir: Path | None
    title: str | None
    cta: str


# --------------------------------------------------------------------------- #
# ffmpeg helpers
# --------------------------------------------------------------------------- #

def run(cmd: list[str], capture: bool = True) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, capture_output=capture, text=True)
    if res.returncode != 0:
        tail = (res.stderr or "")[-2000:]
        raise RuntimeError(f"Command failed ({res.returncode}): {' '.join(cmd[:6])} ...\n{tail}")
    return res


def probe(path: Path) -> dict:
    res = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)])
    info = json.loads(res.stdout)
    duration = float(info["format"].get("duration", 0) or 0)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if v is None:
        raise RuntimeError("no video stream")
    return {"duration": duration, "has_audio": a is not None,
            "width": int(v["width"]), "height": int(v["height"])}


def detect_silences(path: Path, start: float, length: float, db: float, min_dur: float) -> list[Segment]:
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
         "-i", str(path), "-vn", "-af", f"silencedetect=n={db}dB:d={min_dur}", "-f", "null", "-"],
        capture_output=True, text=True)
    silences: list[Segment] = []
    cur = None
    for line in res.stderr.splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            cur = max(0.0, float(m.group(1)))
            continue
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m and cur is not None:
            silences.append((start + cur, start + float(m.group(1))))
            cur = None
    if cur is not None:  
        silences.append((start + cur, start + length))
    return silences


def audio_energy(path: Path, start: float, length: float, win: float = 0.1):
    try:
        import numpy as np
    except ImportError:
        return None
    sr = 8000
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
         "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
        capture_output=True)
    if res.returncode != 0 or not res.stdout:
        return None
    pcm = np.frombuffer(res.stdout, dtype=np.int16).astype(np.float32)
    n = int(sr * win)
    frames = len(pcm) // n
    if frames == 0:
        return None
    pcm = pcm[: frames * n].reshape(frames, n)
    return np.sqrt((pcm ** 2).mean(axis=1))


# --------------------------------------------------------------------------- #
# Timeline logic: trim, auto-cut, hook
# --------------------------------------------------------------------------- #

def keep_segments(start: float, end: float, silences: list[Segment], pad: float) -> list[Segment]:
    segs: list[Segment] = []
    cur = start
    for s, e in sorted(silences):
        s2, e2 = s + pad, e - pad
        if e2 - s2 <= 0.05:
            continue
        if s2 > cur:
            segs.append((cur, min(s2, end)))
        cur = max(cur, e2)
    if cur < end:
        segs.append((cur, end))
    return [(a, b) for a, b in segs if b - a >= 0.1]


def slice_timeline(segs: list[Segment], t0: float, t1: float) -> list[Segment]:
    out, acc = [], 0.0
    for a, b in segs:
        d = b - a
        lo, hi = max(t0, acc), min(t1, acc + d)
        if hi - lo > 1e-3:
            out.append((a + lo - acc, a + hi - acc))
        acc += d
    return out


def pick_hook(segs: list[Segment], energy, e_start: float, hook_len: float, win: float = 0.1) -> float:
    total = sum(b - a for a, b in segs)
    if total <= hook_len * 2:
        return 0.0
    if energy is None:
        return total * 0.35

    import numpy as np
    parts = []
    for a, b in segs:
        i0 = int((a - e_start) / win)
        i1 = int((b - e_start) / win)
        parts.append(energy[max(0, i0):max(0, i1)])
    curve = np.concatenate(parts) if parts else np.array([])
    k = int(hook_len / win)
    if len(curve) <= k:
        return 0.0
    sums = np.convolve(curve, np.ones(k), mode="valid")
    skip = min(len(sums) - 1, k)
    best = skip + int(np.argmax(sums[skip:]))
    return best * win


def build_timeline(path: Path, info: dict, cfg: Settings, rng: random.Random) -> list[Segment]:
    dur = info["duration"]
    head = rng.uniform(*cfg.trim_head)
    tail = rng.uniform(*cfg.trim_tail)
    start, end = head, dur - tail
    if end - start < 3:
        raise RuntimeError(f"video too short after trimming ({dur:.1f}s)")

    if info["has_audio"]:
        sil = detect_silences(path, start, end - start, cfg.silence_db, cfg.silence_min)
        segs = keep_segments(start, end, sil, cfg.silence_pad)
        if not segs:
            segs = [(start, end)]
        energy = audio_energy(path, start, end - start)
    else:
        segs, energy = [(start, end)], None

    if cfg.hook_len <= 0:
        return chop(segs, cfg.chop_keep, cfg.chop_drop)

    total = sum(b - a for a, b in segs)
    h0 = pick_hook(segs, energy, start, cfg.hook_len)
    h1 = min(total, h0 + cfg.hook_len)
    if h0 <= 0.01:
        # hook is already at the start: keep it whole, chop what follows
        return slice_timeline(segs, 0, h1) + chop(slice_timeline(segs, h1, total),
                                                  cfg.chop_keep, cfg.chop_drop)
    hook = slice_timeline(segs, h0, h1)
    if cfg.hook_mode == "copy":
        body = segs
    else:
        body = slice_timeline(segs, 0, h0) + slice_timeline(segs, h1, total)
    return hook + chop(body, cfg.chop_keep, cfg.chop_drop)


def chop(segs: list[Segment], keep: float, drop: float) -> list[Segment]:
    """Every `keep` seconds of content, cut out the next `drop` seconds."""
    if keep <= 0 or drop <= 0:
        return segs
    total = sum(b - a for a, b in segs)
    out: list[Segment] = []
    t = 0.0
    while t < total:
        out += slice_timeline(segs, t, min(total, t + keep))
        t += keep + drop
    return out


# --------------------------------------------------------------------------- #
# Subtitles
# --------------------------------------------------------------------------- #

Word = tuple[float, float, str]

_whisper_lock = threading.Lock()
_whisper_models: dict[str, object] = {}

def transcribe_whisper(path: Path, model_name: str, lang: str | None) -> list[Word]:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError("--subs auto needs: pip install faster-whisper")
    with _whisper_lock:
        if model_name not in _whisper_models:
            _whisper_models[model_name] = WhisperModel(model_name, device="auto", compute_type="int8")
        model = _whisper_models[model_name]
        segments, _ = model.transcribe(str(path), language=lang, word_timestamps=True, vad_filter=True)
        return [(w.start, w.end, w.word.strip())
                for seg in segments for w in (seg.words or []) if w.word.strip()]

def parse_srt(path: Path) -> list[Word]:
    def ts(x: str) -> float:
        h, m, rest = x.strip().replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)

    words: list[Word] = []
    blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig").strip())
    for block in blocks:
        lines = block.strip().splitlines()
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        a, b = lines[idx].split("-->")
        t0, t1 = ts(a), ts(b.split()[0])
        toks = re.sub(r"<[^>]+>", "", " ".join(lines[idx + 1:])).split()
        if not toks:
            continue
        step = (t1 - t0) / len(toks)
        words += [(t0 + i * step, t0 + (i + 1) * step, w) for i, w in enumerate(toks)]
    return words

def map_words(words: list[Word], segs: list[Segment]) -> list[Word]:
    out: list[Word] = []
    cum = 0.0
    for a, b in segs:
        for s, e, w in words:
            if a <= s < b:
                out.append(((cum + s - a) / SPEED, (cum + min(e, b) - a) / SPEED, w))
        cum += b - a
    return sorted(out)

def ass_time(t: float) -> str:
    cs = max(0, round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"

def write_ass(words: list[Word], dst: Path, cfg: Settings, max_words: int = 3) -> None:
    def esc(x: str) -> str:
        return x.replace("\\", "").replace("{", "(").replace("}", ")")

    chunks: list[list[Word]] = []
    for w in words:
        if chunks and len(chunks[-1]) < max_words and w[0] - chunks[-1][-1][1] < 0.35:
            chunks[-1].append(w)
        else:
            chunks.append([w])

    margin_lr = (OUT_W - SAFE_W) // 2 + 20
    margin_v = (OUT_H - SAFE_H) // 2 + 170
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {OUT_W}", f"PlayResY: {OUT_H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Sub,{cfg.font},{cfg.font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        f"-1,0,0,0,100,100,0,0,1,6,2,2,{margin_lr},{margin_lr},{margin_v},1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for chunk in chunks:
        texts = [esc(w[2].upper() if cfg.sub_upper else w[2]) for w in chunk]
        for i, (s, e, _) in enumerate(chunk):
            end = chunk[i + 1][0] if i + 1 < len(chunk) else e + 0.15
            parts = []
            for j, t in enumerate(texts):
                if j == i:
                    parts.append(r"{\c&H00E5FF&\fscx118\fscy118\t(0,90,\fscx108\fscy108)}" + t + r"{\r}")
                else:
                    parts.append(t)
            pop = r"{\fscx80\fscy80\t(0,80,\fscx100\fscy100)}" if i == 0 else ""
            lines.append(f"Dialogue: 0,{ass_time(s)},{ass_time(max(end, s + 0.05))},Sub,,0,0,0,,"
                         + pop + " ".join(parts))
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")

def get_subtitles(src: Path, segs: list[Segment], dst_ass: Path, cfg: Settings) -> bool:
    if cfg.subs == "off":
        return False
    srt = src.with_suffix(".srt")
    if srt.exists():
        words = parse_srt(srt)
    elif cfg.subs == "auto":
        words = transcribe_whisper(src, cfg.whisper_model, cfg.sub_lang)
    else: 
        return False
    words = map_words(words, segs)
    if not words:
        return False
    write_ass(words, dst_ass, cfg)
    return True

def filter_path(p: Path) -> str:
    s = str(p.resolve()).replace("\\", "/")
    return "'" + s.replace("'", r"'\''").replace(":", r"\:") + "'"

# --------------------------------------------------------------------------- #
# Transition FX: flash / glitch / B-roll
# --------------------------------------------------------------------------- #

def plan_fx(segs: list[Segment], out_dur: float, cfg: Settings, rng: random.Random,
            n_broll: int, glitch: bool = True) -> list[tuple[str, float]]:
    if cfg.fx_every[1] <= 0:
        return []
    cuts, cum = [], 0.0
    for a, b in segs[:-1]:
        cum += b - a
        cuts.append(cum / SPEED)
    kinds = ["flash"] + (["glitch"] if glitch else []) + (["broll"] if n_broll else [])
    events, t = [], cfg.hook_len / SPEED
    if glitch and cfg.hook_len > 0 and t < out_dur - 1.5:
        events.append(("glitch", max(0.0, t - 0.1)))   # hit the hook -> body transition
    while True:
        t += rng.uniform(*cfg.fx_every)
        last = events[-1][1] if events else 0.0
        near = [c for c in cuts if abs(c - t) < 1.5 and c > last + 1.0]
        if near:
            t = near[0]
        if t > out_dur - 1.5:
            break
        events.append((rng.choice(kinds), t))
    return events

def flash_expr(times: list[float], width: float = 0.12, amount: float = 0.45) -> str:
    return "+".join(f"{amount}*max(0,1-abs(t-{t:.3f})/{width})" for t in times)

def between(times: list[float], length: float) -> str:
    return "+".join(f"between(t,{t:.3f},{t + length:.3f})" for t in times)

# --------------------------------------------------------------------------- #
# Filter graph
# --------------------------------------------------------------------------- #

def merge_adjacent(segs: list[Segment]) -> list[Segment]:
    out: list[Segment] = []
    for a, b in segs:
        if out and abs(out[-1][1] - a) < 1e-3:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out

def layout_filter(mode: str) -> str:
    if mode == "fill":  
        return (f"[v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                f"crop={OUT_W}:{OUT_H},setsar=1[lay]")
    box_w, box_h = (SAFE_W, SAFE_H) if mode == "safe" else (OUT_W, OUT_H)
    return (f"[v]split=2[bg0][fg0];"
            f"[bg0]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
            f"boxblur=luma_radius=30:luma_power=2,eq=brightness=-0.08[bg];"
            f"[fg0]scale={box_w}:{box_h}:force_original_aspect_ratio=decrease,"
            f"scale=trunc(iw/2)*2:trunc(ih/2)*2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[lay]")

def blur_regions(boxes: list[tuple[float, float, float, float]]) -> list[str]:
    if not boxes:
        return ["[vs]null[v]"]
    f = [f"[vs]split={len(boxes) + 1}[base]" + "".join(f"[r{i}]" for i in range(len(boxes)))]
    cur = "base"
    for i, (x, y, w, h) in enumerate(boxes):
        f.append(f"[r{i}]crop=w='max(8,trunc(iw*{w}/2)*2)':h='max(8,trunc(ih*{h}/2)*2)'"
                 f":x='iw*{x}':y='ih*{y}',boxblur=luma_radius='min(w,h)/4':luma_power=3[rb{i}]")
        f.append(f"[{cur}][rb{i}]overlay=x='main_w*{x}':y='main_h*{y}'[bl{i}]")
        cur = f"bl{i}"
    f.append(f"[{cur}]null[v]")
    return f

def build_filter(segs: list[Segment], has_audio: bool, out_dur: float, cfg: Settings,
                 rng: random.Random, bgm_input: int | None, broll_inputs: list[int],
                 ass_file: Path | None, plan: dict) -> str:
    f: list[str] = []
    n = len(segs)

    for i, (a, b) in enumerate(segs):
        f.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        if has_audio:
            f.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
                     f"aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo[a{i}]")
        else:
            f.append(f"anullsrc=r={SAMPLE_RATE}:cl=stereo,atrim=duration={b - a:.3f}[a{i}]")
    f.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")

    f.append(f"[vc]setpts=PTS/{SPEED}[vs]")
    voice = [f"asetrate={SAMPLE_RATE * SPEED:.0f}", f"aresample={SAMPLE_RATE}"]
    if cfg.eq_low > 0:
        voice.append(f"highpass=f={cfg.eq_low:.0f}")
    if cfg.eq_high > 0:
        voice.append(f"lowpass=f={cfg.eq_high:.0f}")
    if cfg.reverb > 0:
        r = cfg.reverb
        voice.append(f"aecho=in_gain=1:out_gain=1:delays=43|67|89:"
                     f"decays={r:.3f}|{r * 0.7:.3f}|{r * 0.45:.3f}")
    fx_on = plan["effects"]
    if "visualizer" in fx_on:
        f.append("[ac]" + ",".join(voice) + "[voice0]")
        viz, _ = efx.visualizer("voice0", "voice", rng)
        f += viz
    else:
        f.append("[ac]" + ",".join(voice) + "[voice]")

    f += blur_regions(cfg.blur_boxes)
    f.append(layout_filter(cfg.layout))

    chain = []
    if cfg.flip:
        chain.append("hflip")
    if "kenburns" in fx_on:
        chain += efx.ken_burns(rng, out_dur)
    else:
        zmid = (cfg.zoom_min + cfg.zoom_max) / 2
        zamp = (cfg.zoom_max - cfg.zoom_min) / 2
        period = rng.uniform(6, 10)
        phase = rng.uniform(0, 2 * math.pi)
        z = f"({zmid:.4f}+{zamp:.4f}*sin(2*PI*t/{period:.2f}+{phase:.3f}))"
        chain.append(f"scale=w='trunc({OUT_W}*{z}/2)*2':h='trunc({OUT_H}*{z}/2)*2':eval=frame")
        chain.append(f"crop={OUT_W}:{OUT_H}")
    fx = plan_fx(segs, out_dur, cfg, rng, len(broll_inputs), "glitch" in fx_on)
    flashes = [t for k, t in fx if k == "flash"]
    glitches = [t for k, t in fx if k == "glitch"]
    brolls = [t for k, t in fx if k == "broll"]
    bright = f"{cfg.brightness:.3f}" + (f"+{flash_expr(flashes)}" if flashes else "")
    chain.append(f"eq=contrast={1 + cfg.contrast:.3f}:brightness='{bright}':eval=frame")
    if glitches:
        on = between(glitches, 0.2)
        chain.append(f"rgbashift=rh=-14:bh=14:gv=6:enable='{on}'")
        chain.append(f"noise=alls=45:allf=t:enable='{on}'")
    if plan["filters"]:
        chain.append(efx.color_filters(plan["filters"]))
    if cfg.grain > 0:
        chain.append(f"noise=alls={round(cfg.grain * 255)}:allf=t+u")


    drop_cycle = int(rng.uniform(120, 180))
    chain.append(f"select='not(eq(mod(n\\,{drop_cycle})\\,0))'")
    # --------------------------------------------------------

    chain.append(f"fps={FPS},format=yuv420p")
    f.append("[lay]" + ",".join(chain) + "[g0]")

    cur = "g0"
    for i, t in enumerate(brolls):
        src = broll_inputs[i % len(broll_inputs)]
        f.append(f"[{src}:v]trim=duration={cfg.broll_len:.2f},"
                 f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                 f"crop={OUT_W}:{OUT_H},setsar=1,fps={FPS},format=yuv420p,"
                 f"setpts=PTS-STARTPTS+{t:.3f}/TB[br{i}]")
        f.append(f"[{cur}][br{i}]overlay=0:0:eof_action=pass:"
                 f"enable='between(t,{t:.3f},{t + cfg.broll_len:.3f})'[gb{i}]")
        cur = f"gb{i}"

    if "lightleak" in fx_on:
        n = max(1, min(4, int(out_dur // 8)))
        times = sorted(rng.uniform(1.0, max(1.5, out_dur - 2.0)) for _ in range(n))
        f += efx.light_leaks(cur, "leaked", times, FPS, rng)
        cur = "leaked"
    if plan["stickers"]:
        f += efx.sticker_overlays(cur, "stickered", plan["stickers"], rng)
        cur = "stickered"
    if ass_file is not None:
        f.append(f"[{cur}]ass={filter_path(ass_file)}[subbed]")
        cur = "subbed"
    if plan["text_ass"] is not None:
        f.append(f"[{cur}]ass={filter_path(plan['text_ass'])}[texted]")
        cur = "texted"
    f.append(f"[{cur}]null[graded]")

    # --- [THÊM KỸ THUẬT: BÓNG MA THỊ GIÁC (Ghost Overlay)] ---
    # Tự động tạo một lớp hình vuông trắng có Opacity = 1% ngay trên RAM, 
    # không cần load thêm file ảnh bên ngoài. Dùng hàm Toán học bắt nó chạy vòng vòng.
    gx = rng.uniform(0.5, 2.5)
    gy = rng.uniform(0.5, 2.5)
    
    # Tạo source bóng ma
    f.append(f"color=c=white@0.01:s=100x100:r={FPS}[ghost]")
    # Tính toán quỹ đạo
    expr_x = f"({OUT_W}-w)/2+({OUT_W}-w)/2*sin(t*{gx:.3f})"
    expr_y = f"({OUT_H}-h)/2+({OUT_H}-h)/2*cos(t*{gy:.3f})"
    # Ghép lên video chính
    f.append(f"[graded][ghost]overlay=x='{expr_x}':y='{expr_y}':shortest=1[with_ghost]")
    # ---------------------------------------------------------

    # 5) Progress bar
    bh = cfg.bar_height
    y = 0 if cfg.bar_pos == "top" else OUT_H - bh
    f.append(f"color=c=black@0.35:s={OUT_W}x{bh}:r={FPS}[track]")
    f.append(f"color=c={cfg.bar_color}:s={OUT_W}x{bh}:r={FPS}[bar]")
    
    # Ở đây thay vì lấy [graded] để ghép, ta lấy [with_ghost] là output của bước trên
    if "visualizer" in fx_on:
        f.append(efx.visualizer_overlay("with_ghost", "with_wave"))
        f.append(f"[with_wave][track]overlay=0:{y}:shortest=1[t1]")
    else:
        f.append(f"[with_ghost][track]overlay=0:{y}:shortest=1[t1]")
    f.append(f"[t1][bar]overlay=x='-w+w*t/{out_dur:.3f}':y={y}:shortest=1[vout]")

    if bgm_input is not None:
        vol = rng.uniform(*cfg.bgm_volume)
        f.append(f"[{bgm_input}:a]aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo,"
                 f"volume={vol:.3f},afade=t=out:st={max(0, out_dur - 1.5):.3f}:d=1.5[bgm]")
        f.append("[voice][bgm]amix=inputs=2:duration=first:normalize=0,"
                 f"atrim=duration={out_dur:.3f}[aout]")
    else:
        f.append("[voice]anull[aout]")

    return ";\n".join(f)


# --------------------------------------------------------------------------- #
# Processing
# --------------------------------------------------------------------------- #

def process_one(src: Path, dst: Path, cfg: Settings, seed: int) -> str:
    rng = random.Random(seed)
    t0 = time.time()
    info = probe(src)
    segs = merge_adjacent(build_timeline(src, info, cfg, rng))
    content = sum(b - a for a, b in segs)
    out_dur = content / SPEED

    music = []
    if cfg.music_dir:
        music = sorted(p for p in cfg.music_dir.iterdir() if p.suffix.lower() in AUDIO_EXTS)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    bgm_input = None
    if music:
        track = rng.choice(music)
        cmd += ["-stream_loop", "-1", "-i", str(track)]
        bgm_input = 1

    broll_inputs: list[int] = []
    if cfg.broll_dir:
        pool = sorted(p for p in cfg.broll_dir.iterdir()
                      if p.suffix.lower() in VIDEO_EXTS | IMAGE_EXTS)
        rng.shuffle(pool)
        n_fx = len(plan_fx(segs, out_dur, cfg, random.Random(seed), 1))
        for clip in pool[:max(0, min(len(pool), n_fx))]:
            idx = cmd.count("-i")
            if clip.suffix.lower() in IMAGE_EXTS:
                cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{cfg.broll_len:.2f}",
                        "-i", str(clip)]
            else:
                cmd += ["-t", f"{cfg.broll_len:.2f}", "-i", str(clip)]
            broll_inputs.append(idx)

    # Random effects + color filters (original video stays the bottom layer)
    title = video_title(src, cfg)
    stickers = []
    if cfg.stickers_dir:
        stickers = sorted(p for p in cfg.stickers_dir.iterdir()
                          if p.suffix.lower() in efx.STICKER_EXTS)
    fx_on, filters = efx.choose(rng, cfg.n_effects, cfg.n_filters, cfg.effects, cfg.filters,
                                bool(stickers), bool(title), info["has_audio"])
    sticker_inputs = []
    if "sticker" in fx_on and stickers:
        for k in range(1 if out_dur < 15 else 2):
            clip, d = rng.choice(stickers), 2.5
            t = rng.uniform(cfg.hook_len / SPEED + 0.5, max(cfg.hook_len / SPEED + 1, out_dur - d - 0.5))
            idx = cmd.count("-i")
            ext = clip.suffix.lower()
            if ext == ".gif":
                cmd += ["-ignore_loop", "0", "-t", f"{d}", "-i", str(clip)]
            elif ext == ".png":
                cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{d}", "-i", str(clip)]
            else:
                if ext == ".webm":
                    cmd += ["-c:v", "libvpx-vp9"]  # keeps the alpha channel
                cmd += ["-stream_loop", "-1", "-t", f"{d}", "-i", str(clip)]
            sticker_inputs.append((idx, t, d))

    text_ass = dst.with_suffix(".fx.ass")
    has_text = efx.write_text_fx(text_ass, fx_on, title, cfg.cta, out_dur, cfg.font, rng)
    plan = {"effects": fx_on, "filters": filters, "stickers": sticker_inputs,
            "text_ass": text_ass if has_text else None}

    ass_file = dst.with_suffix(".subs.ass")
    has_subs = get_subtitles(src, segs, ass_file, cfg)

    graph = build_filter(segs, info["has_audio"], out_dur, cfg, rng, bgm_input, broll_inputs,
                         ass_file if has_subs else None, plan)
    graph_file = dst.with_suffix(".filter.txt")
    graph_file.write_text(graph)

    b, mx = cfg.bitrate, cfg.maxrate
    tmp = dst.with_suffix(".part.mp4")
    cmd += [
        "-filter_complex_script", str(graph_file),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", cfg.preset, "-profile:v", "high", "-level", "4.1",
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
        "-b:v", f"{b}M", "-maxrate", f"{mx}M", "-bufsize", f"{mx * 2}M",
        "-c:a", "aac", "-b:a", "192k", "-ar", str(SAMPLE_RATE),
        "-t", f"{out_dur:.3f}", "-movflags", "+faststart", str(tmp),
    ]
    try:
        run(cmd)
        tmp.replace(dst)
    finally:
        if not cfg.keep_temp:
            graph_file.unlink(missing_ok=True)
            ass_file.unlink(missing_ok=True)
            text_ass.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)

    return (f"{src.name}: {info['duration']:.1f}s -> {out_dur:.1f}s "
            f"({len(segs)} segments) in {time.time() - t0:.0f}s\n"
            f"     effects: {', '.join(efx.EFFECTS[k] for k in fx_on)}\n"
            f"     filters: {', '.join(efx.FILTERS[k][0] for k in filters)}")


def video_title(src: Path, cfg: Settings) -> str:
    """Title text for typewriter/highlight: video.txt (first line) > --title > file name."""
    side = src.with_suffix(".txt")
    if side.exists():
        first = side.read_text(encoding="utf-8-sig").strip().splitlines()
        if first and first[0].strip():
            return first[0].strip()
    if cfg.title:
        return cfg.title
    name = re.sub(r"[_\-.]+", " ", src.stem).strip()
    return name if re.search(r"[^\W\d]{3,}", name) else ""


def parse_range(s: str) -> tuple[float, float]:
    parts = [float(x) for x in s.split("-")] if "-" in s.strip("-") else [float(s)] * 2
    return (min(parts), max(parts))


def parse_chop(s: str) -> tuple[float, float]:
    parts = [float(v) for v in s.split(",")]
    return (parts[0], parts[1]) if len(parts) == 2 else (0.0, 0.0)


def parse_box(s: str) -> tuple[float, float, float, float]:
    x, y, w, h = (float(v) for v in s.split(","))
    return (x, y, w, h)


def main() -> int:
    p = argparse.ArgumentParser(description="Batch TikTok 9:16 video editor (ffmpeg).")
    p.add_argument("-i", "--input", type=Path, required=True, help="input folder (or single file)")
    p.add_argument("-o", "--output", type=Path, required=True, help="output folder")
    p.add_argument("-m", "--music", type=Path, help="folder of background music tracks")
    p.add_argument("-j", "--jobs", type=int, default=1, help="videos processed in parallel")
    p.add_argument("--trim-head", default="1-2", help="seconds cut at start, e.g. 1-2")
    p.add_argument("--trim-tail", default="1-2", help="seconds cut at end, e.g. 1-2")
    p.add_argument("--silence-db", type=float, default=-35, help="silence threshold in dB")
    p.add_argument("--silence-min", type=float, default=0.6, help="min silence length to cut (s)")
    p.add_argument("--silence-pad", type=float, default=0.12, help="silence kept around speech (s)")
    p.add_argument("--hook", type=float, default=3.0, help="hook length in seconds (0 = off)")
    p.add_argument("--chop", default="3,1", metavar="KEEP,DROP",
                   help="every KEEP seconds cut DROP seconds (default 3,1; 0 = off)")
    p.add_argument("--hook-mode", choices=["move", "copy"], default="move",
                   help="move: cut hook out of its place; copy: also keep it in place")
    p.add_argument("--layout", choices=["blur", "safe", "fill"], default="blur",
                   help="blur: fit + blurred bg; safe: fit into 880x1420 box; fill: crop to fill")
    p.add_argument("--flip", action="store_true", help="mirror horizontally (no text/logo videos!)")
    p.add_argument("--zoom", default="1.02-1.05", help="dynamic zoom range")
    p.add_argument("--contrast", type=float, default=0.03)
    p.add_argument("--brightness", type=float, default=-0.02)
    p.add_argument("--grain", type=float, default=0.025, help="grain opacity 0..1 (0 = off)")
    p.add_argument("--bar", choices=["top", "bottom"], default="top", help="progress bar position")
    p.add_argument("--bar-color", default="white")
    p.add_argument("--bar-height", type=int, default=10)
    p.add_argument("--bgm-volume", default="0.03-0.05", help="music volume range (0.03 = 3%%)")
    p.add_argument("--bitrate", type=float, default=6, help="target video bitrate, Mbps")
    p.add_argument("--maxrate", type=float, default=8, help="max video bitrate, Mbps")
    p.add_argument("--preset", default="medium", help="x264 preset (faster = quicker, bigger)")
    p.add_argument("--seed", type=int, help="random seed for reproducible results")
    p.add_argument("--overwrite", action="store_true", help="re-render existing outputs")
    p.add_argument("--keep-temp", action="store_true", help="keep generated filter graphs")
    p.add_argument("--blur-box", action="append", default=[], metavar="X,Y,W,H",
                   help="blur a region of the source (fractions 0..1), repeatable; "
                        "e.g. 0,0.78,1,0.17 = bottom subtitle band")
    p.add_argument("--subs", choices=["auto", "srt", "off"], default="off",
                   help="auto: video.srt if present, else Whisper; srt: only video.srt")
    p.add_argument("--whisper-model", default="small", help="faster-whisper model name")
    p.add_argument("--lang", default=None, help="speech language for Whisper, e.g. vi, en")
    p.add_argument("--font", default="Arial", help="subtitle font (must support your language)")
    p.add_argument("--font-size", type=int, default=80)
    p.add_argument("--no-upper", action="store_true", help="keep subtitle case as-is")
    p.add_argument("-b", "--broll", type=Path, help="folder of B-roll/meme images or clips")
    p.add_argument("--fx-every", default="4-8",
                   help="seconds between flash/glitch/B-roll inserts (0 = off)")
    p.add_argument("--broll-len", type=float, default=0.5, help="B-roll insert length (s)")
    p.add_argument("--eq-low", type=float, default=90, help="high-pass Hz (cut bass, 0 = off)")
    p.add_argument("--eq-high", type=float, default=13000, help="low-pass Hz (cut treble, 0 = off)")
    p.add_argument("--reverb", type=float, default=0.04, help="reverb amount 0..0.05 (0 = off)")
    p.add_argument("--n-effects", type=int, default=4, help="random effects per video")
    p.add_argument("--n-filters", type=int, default=2, help="random color filters per video")
    p.add_argument("--effects", default="", help="force effects, comma list: "
                   + ",".join(efx.EFFECTS))
    p.add_argument("--filters", default="", help="force color filters, comma list: "
                   + ",".join(efx.FILTERS))
    p.add_argument("-s", "--stickers", type=Path, help="folder of animated stickers (gif/png/mov/webm)")
    p.add_argument("--title", help="title text for typewriter/highlight (default: video.txt or file name)")
    p.add_argument("--cta", default="FOLLOW ĐỂ XEM THÊM", help="spring pop-up text at the end")
    a = p.parse_args()

    cfg = Settings(
        music_dir=a.music, trim_head=parse_range(a.trim_head), trim_tail=parse_range(a.trim_tail),
        silence_db=a.silence_db, silence_min=a.silence_min, silence_pad=a.silence_pad,
        hook_len=a.hook, hook_mode=a.hook_mode,
        chop_keep=parse_chop(a.chop)[0], chop_drop=parse_chop(a.chop)[1], layout=a.layout, flip=a.flip,
        zoom_min=parse_range(a.zoom)[0], zoom_max=parse_range(a.zoom)[1],
        contrast=a.contrast, brightness=a.brightness, grain=a.grain,
        bar_pos=a.bar, bar_color=a.bar_color, bar_height=a.bar_height,
        bgm_volume=parse_range(a.bgm_volume), bitrate=a.bitrate, maxrate=a.maxrate,
        preset=a.preset, overwrite=a.overwrite, keep_temp=a.keep_temp,
        blur_boxes=[parse_box(b) for b in a.blur_box], subs=a.subs,
        whisper_model=a.whisper_model, sub_lang=a.lang, font=a.font, font_size=a.font_size,
        sub_upper=not a.no_upper, broll_dir=a.broll, fx_every=parse_range(a.fx_every),
        broll_len=a.broll_len, eq_low=a.eq_low, eq_high=a.eq_high,
        reverb=min(max(a.reverb, 0.0), 0.05),
        n_effects=a.n_effects, n_filters=a.n_filters,
        effects=[x.strip() for x in a.effects.split(",") if x.strip()],
        filters=[x.strip() for x in a.filters.split(",") if x.strip()],
        stickers_dir=a.stickers, title=a.title, cta=a.cta,
    )
    bad = [x for x in cfg.effects if x not in efx.EFFECTS] + \
          [x for x in cfg.filters if x not in efx.FILTERS]
    if bad:
        p.error(f"unknown effect/filter: {', '.join(bad)}")
    if cfg.music_dir and not cfg.music_dir.is_dir():
        p.error(f"music folder not found: {cfg.music_dir}")
    if cfg.broll_dir and not cfg.broll_dir.is_dir():
        p.error(f"B-roll folder not found: {cfg.broll_dir}")

    if a.input.is_file():
        sources = [a.input]
    else:
        sources = sorted(x for x in a.input.rglob("*")
                         if x.suffix.lower() in VIDEO_EXTS and x.is_file())
    a.output.mkdir(parents=True, exist_ok=True)

    jobs = []
    base_seed = a.seed if a.seed is not None else random.randrange(1 << 30)
    for idx, src in enumerate(sources):
        dst = a.output / f"{src.stem}_tiktok.mp4"
        if dst.exists() and not cfg.overwrite:
            print(f"skip (exists): {dst.name}")
            continue
        jobs.append((src, dst, base_seed + idx))

    if not jobs:
        print("Nothing to do.")
        return 0
    print(f"Processing {len(jobs)} video(s) with {a.jobs} worker(s)...")

    ok = fail = 0
    with cf.ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        futs = {ex.submit(process_one, s, d, cfg, seed): s for s, d, seed in jobs}
        for fut in cf.as_completed(futs):
            src = futs[fut]
            try:
                print("OK   " + fut.result(), flush=True)
                ok += 1
            except Exception as e:
                print(f"FAIL {src.name}: {e}", file=sys.stderr, flush=True)
                fail += 1
    print(f"Done: {ok} ok, {fail} failed.")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
