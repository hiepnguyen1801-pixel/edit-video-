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
 10. Animated progress bar (top or bottom).
 11. Background music from a folder, looped, at 3-5% volume.
 12. Encode H.264 / AAC, 30 fps, 5-8 Mbps, MP4 (+faststart).

Usage:
  python3 tiktok_batch.py -i input/ -o output/ -m music/
  python3 tiktok_batch.py -i input/ -o output/ -m music/ --flip --jobs 2
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
import time
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac", ".opus"}

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
    """Return silent intervals (source timeline) inside [start, start+length]."""
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
    if cur is not None:  # silence runs to the end
        silences.append((start + cur, start + length))
    return silences


def audio_energy(path: Path, start: float, length: float, win: float = 0.1):
    """RMS energy per `win` seconds on the source timeline (numpy array) or None."""
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
    """Complement of silences inside [start, end], with a little padding kept around speech."""
    segs: list[Segment] = []
    cur = start
    for s, e in sorted(silences):
        s2, e2 = s + pad, e - pad  # keep `pad` seconds of silence on each side
        if e2 - s2 <= 0.05:
            continue
        if s2 > cur:
            segs.append((cur, min(s2, end)))
        cur = max(cur, e2)
    if cur < end:
        segs.append((cur, end))
    return [(a, b) for a, b in segs if b - a >= 0.1]


def slice_timeline(segs: list[Segment], t0: float, t1: float) -> list[Segment]:
    """Map interval [t0, t1] of the concatenated timeline back to source segments."""
    out, acc = [], 0.0
    for a, b in segs:
        d = b - a
        lo, hi = max(t0, acc), min(t1, acc + d)
        if hi - lo > 1e-3:
            out.append((a + lo - acc, a + hi - acc))
        acc += d
    return out


def pick_hook(segs: list[Segment], energy, e_start: float, hook_len: float, win: float = 0.1) -> float:
    """Return start time (on concatenated timeline) of the most energetic `hook_len` window."""
    total = sum(b - a for a, b in segs)
    if total <= hook_len * 2:
        return 0.0
    if energy is None:
        return total * 0.35  # no audio analysis possible -> a point in the first half

    import numpy as np
    # Build the energy curve on the concatenated timeline.
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
    # Skip the very beginning: it is already at the front.
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
        return segs

    total = sum(b - a for a, b in segs)
    h0 = pick_hook(segs, energy, start, cfg.hook_len)
    h1 = min(total, h0 + cfg.hook_len)
    if h0 <= 0.01:
        return segs
    hook = slice_timeline(segs, h0, h1)
    if cfg.hook_mode == "copy":
        body = segs
    else:  # move
        body = slice_timeline(segs, 0, h0) + slice_timeline(segs, h1, total)
    return hook + body


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
    """[v] -> [lay] 1080x1920."""
    if mode == "fill":  # crop to cover the whole 9:16 frame
        return (f"[v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                f"crop={OUT_W}:{OUT_H},setsar=1[lay]")
    # blur / safe: content fitted on a blurred background
    box_w, box_h = (SAFE_W, SAFE_H) if mode == "safe" else (OUT_W, OUT_H)
    return (f"[v]split=2[bg0][fg0];"
            f"[bg0]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
            f"boxblur=luma_radius=30:luma_power=2,eq=brightness=-0.08[bg];"
            f"[fg0]scale={box_w}:{box_h}:force_original_aspect_ratio=decrease,"
            f"scale=trunc(iw/2)*2:trunc(ih/2)*2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[lay]")


def build_filter(segs: list[Segment], has_audio: bool, out_dur: float, cfg: Settings,
                 rng: random.Random, bgm_input: int | None) -> str:
    f: list[str] = []
    n = len(segs)

    # 1) Cut + reorder segments
    for i, (a, b) in enumerate(segs):
        f.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        if has_audio:
            f.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
                     f"aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo[a{i}]")
        else:
            f.append(f"anullsrc=r={SAMPLE_RATE}:cl=stereo,atrim=duration={b - a:.3f}[a{i}]")
    f.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")

    # 2) Speed 1.04x. Audio: asetrate resamples -> pitch rises (no pitch preservation).
    f.append(f"[vc]setpts=PTS/{SPEED}[v]")
    f.append(f"[ac]asetrate={SAMPLE_RATE * SPEED:.0f},aresample={SAMPLE_RATE}[voice]")

    # 3) Layout into 9:16 / safe box
    f.append(layout_filter(cfg.layout))

    # 4) Flip, dynamic zoom, color, grain
    chain = []
    if cfg.flip:
        chain.append("hflip")
    zmid = (cfg.zoom_min + cfg.zoom_max) / 2
    zamp = (cfg.zoom_max - cfg.zoom_min) / 2
    period = rng.uniform(6, 10)          # seconds per zoom in/out cycle
    phase = rng.uniform(0, 2 * math.pi)
    z = f"({zmid:.4f}+{zamp:.4f}*sin(2*PI*t/{period:.2f}+{phase:.3f}))"
    chain.append(f"scale=w='trunc({OUT_W}*{z}/2)*2':h='trunc({OUT_H}*{z}/2)*2':eval=frame")
    chain.append(f"crop={OUT_W}:{OUT_H}")
    chain.append(f"eq=contrast={1 + cfg.contrast:.3f}:brightness={cfg.brightness:.3f}")
    if cfg.grain > 0:
        # noise strength is in 0..255 units: 2.5% opacity ~= +/-6
        chain.append(f"noise=alls={round(cfg.grain * 255)}:allf=t+u")
    chain.append(f"fps={FPS},format=yuv420p")
    f.append("[lay]" + ",".join(chain) + "[graded]")

    # 5) Progress bar
    bh = cfg.bar_height
    y = 0 if cfg.bar_pos == "top" else OUT_H - bh
    f.append(f"color=c=black@0.35:s={OUT_W}x{bh}:r={FPS}[track]")
    f.append(f"color=c={cfg.bar_color}:s={OUT_W}x{bh}:r={FPS}[bar]")
    f.append(f"[graded][track]overlay=0:{y}:shortest=1[t1]")
    f.append(f"[t1][bar]overlay=x='-w+w*t/{out_dur:.3f}':y={y}:shortest=1[vout]")

    # 6) Background music
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

    graph = build_filter(segs, info["has_audio"], out_dur, cfg, rng, bgm_input)
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
        tmp.unlink(missing_ok=True)

    return (f"{src.name}: {info['duration']:.1f}s -> {out_dur:.1f}s "
            f"({len(segs)} segments) in {time.time() - t0:.0f}s")


def parse_range(s: str) -> tuple[float, float]:
    parts = [float(x) for x in s.split("-")] if "-" in s.strip("-") else [float(s)] * 2
    return (min(parts), max(parts))


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
    a = p.parse_args()

    cfg = Settings(
        music_dir=a.music, trim_head=parse_range(a.trim_head), trim_tail=parse_range(a.trim_tail),
        silence_db=a.silence_db, silence_min=a.silence_min, silence_pad=a.silence_pad,
        hook_len=a.hook, hook_mode=a.hook_mode, layout=a.layout, flip=a.flip,
        zoom_min=parse_range(a.zoom)[0], zoom_max=parse_range(a.zoom)[1],
        contrast=a.contrast, brightness=a.brightness, grain=a.grain,
        bar_pos=a.bar, bar_color=a.bar_color, bar_height=a.bar_height,
        bgm_volume=parse_range(a.bgm_volume), bitrate=a.bitrate, maxrate=a.maxrate,
        preset=a.preset, overwrite=a.overwrite, keep_temp=a.keep_temp,
    )
    if cfg.music_dir and not cfg.music_dir.is_dir():
        p.error(f"music folder not found: {cfg.music_dir}")

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
            except Exception as e:  # keep going with the rest of the batch
                print(f"FAIL {src.name}: {e}", file=sys.stderr, flush=True)
                fail += 1
    print(f"Done: {ok} ok, {fail} failed.")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
