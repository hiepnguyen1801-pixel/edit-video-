"""
Random effects + color filters layered on top of the edited video.

The original footage is always the bottom layer; everything here is drawn over it.
Each video gets N random effects (default 4) and N random color filters (default 2).
"""

from __future__ import annotations

import math
import random
import re
from pathlib import Path

OUT_W, OUT_H = 1080, 1920
SAFE_W, SAFE_H = 880, 1420
SAFE_X, SAFE_Y = (OUT_W - SAFE_W) // 2, (OUT_H - SAFE_H) // 2   # 100, 250

# Progress bar is always on (base requirement), so it is not part of the random pool.
EFFECTS = {
    "typewriter": "Typewriter (Gõ chữ)",
    "kenburns": "Ken Burns (Thu phóng và trượt chậm)",
    "visualizer": "Audio Visualizer (Sóng âm)",
    "spring": "Spring Pop-up (Nảy bật)",
    "highlight": "Text Highlight (Quét dạ quang)",
    "glitch": "Glitch (Nhiễu RGB)",
    "lightleak": "Light Leaks / Film Burn (Hòa trộn sáng)",
    "sticker": "Lottie Stickers (Ảnh động)",
}

FILTERS = {
    "nga-lanh": ("Ngà lạnh", "brightness(1.1) saturate(0.8) contrast(1.05) hue-rotate(5deg)"),
    "sac-net-4k": ("Sắc nét 4K", "contrast(1.2) saturate(1.15) brightness(1.05)"),
    "dien-anh": ("Phim điện ảnh", "contrast(1.15) saturate(0.9) brightness(0.9) sepia(0.2)"),
    "trang-den": ("Trắng đen", "grayscale(1) contrast(1.3) brightness(0.9)"),
    "sang-am": ("Sáng ấm", "sepia(0.3) saturate(1.2) brightness(1.05) contrast(1.05)"),
}

STICKER_EXTS = {".gif", ".png", ".mov", ".webm", ".apng"}


def choose(rng: random.Random, n_effects: int, n_filters: int, forced_fx: list[str] | None,
           forced_filters: list[str] | None, has_stickers: bool, has_text: bool,
           has_audio: bool = True):
    pool = [k for k in EFFECTS
            if (k != "sticker" or has_stickers)
            and (k != "visualizer" or has_audio)
            and (k not in ("typewriter", "spring", "highlight") or has_text)]
    fx = forced_fx if forced_fx else rng.sample(pool, min(n_effects, len(pool)))
    filters = forced_filters if forced_filters else rng.sample(list(FILTERS), min(n_filters, len(FILTERS)))
    return fx, filters


# --------------------------------------------------------------------------- #
# CSS filter -> ffmpeg (same matrices as the CSS Filter Effects spec)
# --------------------------------------------------------------------------- #

def _mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _css_step(name: str, v: float):
    """Return (matrix, offset) for one CSS filter function."""
    I = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    if name == "brightness":
        return [[v, 0, 0], [0, v, 0], [0, 0, v]], [0, 0, 0]
    if name == "contrast":
        o = 0.5 - 0.5 * v
        return [[v, 0, 0], [0, v, 0], [0, 0, v]], [o, o, o]
    if name == "saturate":
        s = v
        return [[0.213 + 0.787 * s, 0.715 - 0.715 * s, 0.072 - 0.072 * s],
                [0.213 - 0.213 * s, 0.715 + 0.285 * s, 0.072 - 0.072 * s],
                [0.213 - 0.213 * s, 0.715 - 0.715 * s, 0.072 + 0.928 * s]], [0, 0, 0]
    if name == "hue-rotate":
        c, s = math.cos(math.radians(v)), math.sin(math.radians(v))
        return [[0.213 + c * 0.787 - s * 0.213, 0.715 - c * 0.715 - s * 0.715, 0.072 - c * 0.072 + s * 0.928],
                [0.213 - c * 0.213 + s * 0.143, 0.715 + c * 0.285 + s * 0.140, 0.072 - c * 0.072 - s * 0.283],
                [0.213 - c * 0.213 - s * 0.787, 0.715 - c * 0.715 + s * 0.715, 0.072 + c * 0.928 + s * 0.072]], [0, 0, 0]
    if name == "sepia":
        a = 1 - min(1.0, v)
        return [[0.393 + 0.607 * a, 0.769 - 0.769 * a, 0.189 - 0.189 * a],
                [0.349 - 0.349 * a, 0.686 + 0.314 * a, 0.168 - 0.168 * a],
                [0.272 - 0.272 * a, 0.534 - 0.534 * a, 0.131 + 0.869 * a]], [0, 0, 0]
    if name == "grayscale":
        a = 1 - min(1.0, v)
        return [[0.2126 + 0.7874 * a, 0.7152 - 0.7152 * a, 0.0722 - 0.0722 * a],
                [0.2126 - 0.2126 * a, 0.7152 + 0.2848 * a, 0.0722 - 0.0722 * a],
                [0.2126 - 0.2126 * a, 0.7152 - 0.7152 * a, 0.0722 + 0.9278 * a]], [0, 0, 0]
    return I, [0, 0, 0]


def css_to_ffmpeg(css: str) -> str:
    """Compose a CSS `filter:` string into colorchannelmixer (matrix) + lutrgb (offset)."""
    M = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    off = [0.0, 0.0, 0.0]
    for name, val, unit in re.findall(r"([a-z-]+)\(\s*([-\d.]+)\s*(%|deg)?\s*\)", css):
        v = float(val) / (100 if unit == "%" else 1)
        S, o = _css_step(name, v)
        # x' = S (M x + off) + o
        M = _mul(S, M)
        off = [sum(S[i][k] * off[k] for k in range(3)) + o[i] for i in range(3)]
    ch = "rgb"
    mix = ":".join(f"{ch[i]}{ch[j]}={M[i][j]:.4f}" for i in range(3) for j in range(3))
    out = f"format=rgb24,colorchannelmixer={mix}"
    if any(abs(x) > 1e-4 for x in off):
        out += ",lutrgb=" + ":".join(f"{ch[i]}='clip(val+{off[i] * 255:.2f},0,255)'"
                                    for i in range(3))
    return out


def color_filters(names: list[str]) -> str:
    return ",".join(css_to_ffmpeg(FILTERS[n][1]) for n in names)


# --------------------------------------------------------------------------- #
# Ken Burns: slow zoom + pan across the whole clip
# --------------------------------------------------------------------------- #

def ken_burns(rng: random.Random, dur: float) -> list[str]:
    z0, z1 = 1.04, rng.uniform(1.10, 1.13)
    if rng.random() < 0.5:
        z0, z1 = z1, z0
    px0, px1 = rng.uniform(0.25, 0.75), rng.uniform(0.25, 0.75)
    py0, py1 = rng.uniform(0.3, 0.7), rng.uniform(0.3, 0.7)
    p = f"min(1,t/{dur:.3f})"
    z = f"({z0:.4f}+{z1 - z0:.4f}*{p})"
    return [f"scale=w='trunc({OUT_W}*{z}/2)*2':h='trunc({OUT_H}*{z}/2)*2':eval=frame",
            f"crop={OUT_W}:{OUT_H}:x='(in_w-out_w)*({px0:.3f}+{px1 - px0:.3f}*{p})'"
            f":y='(in_h-out_h)*({py0:.3f}+{py1 - py0:.3f}*{p})'"]


# --------------------------------------------------------------------------- #
# Text effects (ASS): typewriter, spring pop-up, highlighter sweep
# --------------------------------------------------------------------------- #

def _t(t: float) -> str:
    cs = max(0, round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _esc(x: str) -> str:
    return x.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def write_text_fx(path: Path, fx: list[str], title: str, cta: str, dur: float, font: str,
                  rng: random.Random) -> bool:
    if not any(k in fx for k in ("typewriter", "spring", "highlight")):
        return False
    title, cta = _esc(title.strip()), _esc(cta.strip())
    size = 72 if len(title) <= 22 else 60
    top = SAFE_Y + 70
    ml = SAFE_X + 20
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {OUT_W}", f"PlayResY: {OUT_H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        # plain white title with outline
        f"Style: Title,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,5,2,8,{ml},{ml},{top},1",
        # opaque box style (box colour = OutlineColour)
        f"Style: Box,{font},{size},&H00FFFFFF,&H00FFFFFF,&H006A2BFF,&H00000000,"
        f"-1,0,0,0,100,100,0,0,3,18,0,8,{ml},{ml},{top},1",
        f"Style: Mark,{font},{size},&H00000000,&H00000000,&H0000F2FF,&H00000000,"
        f"-1,0,0,0,100,100,0,0,3,14,0,8,{ml},{ml},{top},1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    if "typewriter" in fx and title:
        start, step = 0.25, min(0.07, 2.0 / max(1, len(title)))
        hold = min(dur, 4.5)
        for i in range(1, len(title) + 1):
            s = start + (i - 1) * step
            e = start + i * step if i < len(title) else hold
            cursor = "|" if i < len(title) else ""
            lines.append(f"Dialogue: 1,{_t(s)},{_t(e)},Title,,0,0,0,,{title[:i]}{cursor}")

    if "highlight" in fx and title:
        s = max(5.0, dur * rng.uniform(0.35, 0.55))
        e = min(dur - 0.5, s + 3.0)
        if e - s > 1:
            x0, x1 = SAFE_X, SAFE_X + SAFE_W
            y0, y1 = top - 30, top + size * 2 + 60
            # yellow marker box revealed left -> right, black text on top
            lines.append(f"Dialogue: 0,{_t(s)},{_t(e)},Mark,,0,0,0,,"
                         f"{{\\1a&HFF&\\clip({x0},{y0},{x0},{y1})\\t(0,450,\\clip({x0},{y0},{x1},{y1}))}}"
                         f"{title}")
            lines.append(f"Dialogue: 1,{_t(s)},{_t(e)},Mark,,0,0,0,,{{\\3a&HFF&}}{title}")

    if "spring" in fx and cta:
        s = max(0.5, dur - 3.2)
        mv = top + 260
        spring = (r"{\fscx0\fscy0\t(0,170,\fscx122\fscy122)\t(170,300,\fscx90\fscy90)"
                  r"\t(300,400,\fscx106\fscy106)\t(400,480,\fscx100\fscy100)}")
        lines.append(f"Dialogue: 2,{_t(s)},{_t(dur)},Box,,0,0,{mv},,{spring}{cta}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


# --------------------------------------------------------------------------- #
# Overlay layers (filtergraph snippets)
# --------------------------------------------------------------------------- #

def visualizer(audio_in: str, audio_out: str, rng: random.Random) -> tuple[list[str], str]:
    """Split the voice and draw its waveform at the bottom of the safe box. Returns (filters, label)."""
    color = rng.choice(["white", "0x00E5FF", "0xFFD000", "0xFF4FA3"])
    h = 130
    f = [f"[{audio_in}]asplit=2[{audio_out}][vizsrc]",
         f"[vizsrc]showwaves=s={SAFE_W}x{h}:mode=cline:rate=30:scale=sqrt:colors={color},"
         f"format=rgba,colorchannelmixer=aa=0.75[wave]"]
    return f, "wave"


def visualizer_overlay(base: str, out: str) -> str:
    y = SAFE_Y + SAFE_H - 150
    return f"[{base}][wave]overlay={SAFE_X}:{y}:shortest=1[{out}]"


def light_leaks(base: str, out: str, times: list[float], fps: int,
                rng: random.Random) -> list[str]:
    f, cur = [], base
    for i, t in enumerate(times):
        d = rng.uniform(1.0, 1.6)
        c = rng.choice([("0xFF6A00", "0xFF1E56", "0xFFC300"), ("0xFF8C42", "0xFFD166", "0xEF476F"),
                        ("0xFFB703", "0xFB8500", "0xE63946")])
        f.append(f"gradients=s=540x960:r={fps}:d={d:.2f}:type=radial:speed=0.04:"
                 f"c0={c[0]}:c1={c[1]}:c2={c[2]}:nb_colors=3:seed={rng.randrange(1 << 16)},"
                 f"scale={OUT_W}:{OUT_H},format=rgba,colorchannelmixer=aa=0.55,"
                 f"fade=t=in:st=0:d={d * 0.35:.2f}:alpha=1,fade=t=out:st={d * 0.6:.2f}:d={d * 0.4:.2f}:alpha=1,"
                 f"setpts=PTS-STARTPTS+{t:.3f}/TB[leak{i}]")
        f.append(f"[{cur}][leak{i}]overlay=0:0:eof_action=pass[lk{i}]")
        cur = f"lk{i}"
    f.append(f"[{cur}]null[{out}]")
    return f


def sticker_overlays(base: str, out: str, inputs: list[tuple[int, float, float]],
                     rng: random.Random) -> list[str]:
    """inputs: (input_index, start_time, duration)."""
    f, cur = [], base
    size = 260
    spots = [(SAFE_X + 30, SAFE_Y + 220), (SAFE_X + SAFE_W - size - 30, SAFE_Y + 220),
             (SAFE_X + 30, SAFE_Y + SAFE_H - size - 330),
             (SAFE_X + SAFE_W - size - 30, SAFE_Y + SAFE_H - size - 330)]
    for i, (idx, t, d) in enumerate(inputs):
        x, y = rng.choice(spots)
        f.append(f"[{idx}:v]scale={size}:{size}:force_original_aspect_ratio=decrease,format=rgba,"
                 f"fade=t=in:st=0:d=0.25:alpha=1,fade=t=out:st={d - 0.3:.2f}:d=0.3:alpha=1,"
                 f"setpts=PTS-STARTPTS+{t:.3f}/TB[stk{i}]")
        f.append(f"[{cur}][stk{i}]overlay={x}:{y}:eof_action=pass:"
                 f"enable='between(t,{t:.3f},{t + d:.3f})'[st{i}]")
        cur = f"st{i}"
    f.append(f"[{cur}]null[{out}]")
    return f
