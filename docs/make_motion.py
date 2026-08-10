"""Generate ultra-premium motion-graphics GIFs for the Product Hunt launch. (v3)

Three animated assets (dark glassmorphism, glowing accents, eased motion):

  docs/ph-architecture.gif  - animated flow diagram: Your AI app -> Gemini Web2API -> Gemini web
  docs/ph-health.gif        - live self-healing health dashboard with animated metrics
  docs/ph-hero.gif          - brand hero: logo aura + gradient wordmark + status pills

960x600 @ 24 fps. Pure PIL, offline-safe. Run:  python docs/make_motion.py
"""
import os
import math
import time

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
W, H = 960, 600
FPS = 24
MAX_BYTES = 1_880_000
FRAME_MS = int(1000 / FPS)
STILL_MODE = False  # set True to export keyframe stills (suppresses transient FX)

# ── palette ──────────────────────────────────────────────────────────────
BG_TOP = (9, 13, 28)
BG_BOT = (14, 22, 44)
ACCENT = (99, 102, 241)      # indigo
ACCENT2 = (34, 211, 238)     # cyan
ACCENT3 = (52, 211, 153)     # emerald
TEXT = (226, 232, 240)
MUTED = (148, 163, 184)
DIM = (100, 116, 139)

# ── fonts ────────────────────────────────────────────────────────────────
def _font(paths, size):
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()

F_TITLE = _font([r"C:\Windows\Fonts\segoeuib.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"], 46)
F_SUB = _font([r"C:\Windows\Fonts\segoeui.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"], 24)
F_SMALL = _font([r"C:\Windows\Fonts\segoeui.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"], 19)
F_MICRO = _font([r"C:\Windows\Fonts\segoeui.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"], 15)
F_MONO = _font([r"C:\Windows\Fonts\consola.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"], 22)
F_LOGO = _font([r"C:\Windows\Fonts\segoeuib.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"], 74)


def ease(x):
    return 0.5 - 0.5 * math.cos(math.pi * x)


def clamp01(x):
    return max(0.0, min(1.0, x))


def lerp(a, b, t):
    return a + (b - a) * t


def bg():
    im = Image.new("RGB", (W, H))
    dr = ImageDraw.Draw(im)
    for y in range(H):
        t = y / H
        dr.line([(0, y), (W, y)], fill=tuple(int(lerp(a, b, t)) for a, b in zip(BG_TOP, BG_BOT)))
    vig = Image.new("L", (W, H), 0)
    vd = ImageDraw.Draw(vig)
    vd.ellipse([-W * 0.35, -H * 0.5, W * 1.35, H * 1.5], fill=70)
    vig = vig.filter(ImageFilter.GaussianBlur(120))
    black = Image.new("RGB", (W, H), (0, 0, 0))
    im = Image.composite(im, black, vig.point(lambda p: 255 - p))
    return im


def dot_grid(alpha=14, step=38):
    g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(g)
    for x in range(step // 2, W, step):
        for y in range(step // 2, H, step):
            d.ellipse([x - 1, y - 1, x + 1, y + 1], fill=(120, 140, 190, alpha))
    return g


def glow_layer(draw_fn, radius=18):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(layer))
    return layer.filter(ImageFilter.GaussianBlur(radius))


def glass_card(x, y, w, h, r=22, fill=22, alpha=150, stroke=70, lift=0):
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sdr = ImageDraw.Draw(sh)
    sdr.rounded_rectangle([x + 4, y + 10 + lift, x + w + 4, y + h + 10 + lift], radius=r, fill=(0, 0, 0, 110))
    base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bdr = ImageDraw.Draw(base)
    bdr.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=(fill, fill, fill, alpha),
                          outline=(stroke, stroke, stroke, 90), width=1)
    bdr.rounded_rectangle([x + 14, y + 1, x + w - 14, y + 5], radius=2, fill=(255, 255, 255, 26))
    return Image.alpha_composite(sh, base)


def gradient_text(dr, xy, text, font, c1, c2, anchor="mm"):
    tmp = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    td = ImageDraw.Draw(tmp)
    td.text(xy, text, font=font, fill=(255, 255, 255, 255), anchor=anchor)
    bbox = tmp.getbbox()
    if not bbox:
        return
    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for x in range(bbox[0], bbox[2]):
        t = (x - bbox[0]) / max(1, bbox[2] - bbox[0])
        gd.line([(x, bbox[1]), (x, bbox[3])], fill=tuple(int(lerp(a, b, t)) for a, b in zip(c1, c2)) + (255,))
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.paste(grad, (0, 0), tmp.split()[3])
    dr._image.alpha_composite(out)


def rounded(dr, cx, cy, text, font, fill, anchor="mm"):
    dr.text((cx, cy), text, font=font, fill=fill, anchor=anchor)


def build_palette(frames, colors=168):
    sample = [frames[k] for k in range(0, len(frames), 5)][:10]
    canvas = Image.new("RGB", (W * len(sample), H))
    for k, f in enumerate(sample):
        canvas.paste(f.convert("RGB"), (k * W, 0))
    pal_img = canvas.quantize(colors=colors, method=Image.FASTOCTREE, dither=Image.NONE)
    pal_img.putpalette(pal_img.getpalette()[: colors * 3])
    return pal_img


def save_gif(frames, path, palette, duration_ms=FRAME_MS):
    # disposal=1 (leave in place) + optimize=True: frames are full-canvas opaque,
    # so minimal deltas compress enormously (disposal=2 forces full re-encode
    # and balloons the file 4-5x).
    q = [f.convert("RGB").quantize(palette=palette, dither=Image.NONE) for f in frames]
    q[0].save(path, save_all=True, append_images=q[1:], duration=duration_ms,
              loop=0, optimize=True, disposal=1)
    return os.path.getsize(path)


def shrink_to_fit(frames, path, palette, budget=MAX_BYTES):
    """Re-encode smaller/fewer colors, ALWAYS with a shared palette (per-frame
    palettes destroy temporal compression and can even increase size)."""
    sizes = [(168, 1.0), (150, 0.94), (132, 0.88), (114, 0.82), (96, 0.76)]
    for colors, scale in sizes:
        sf = frames
        if scale < 1.0:
            sf = [f.resize((int(W * scale), int(H * scale)), Image.LANCZOS) for f in frames]
        pal2 = build_palette(sf, colors=colors)
        q = [f.convert("RGB").quantize(palette=pal2, dither=Image.NONE) for f in sf]
        q[0].save(path, save_all=True, append_images=q[1:], duration=FRAME_MS,
                  loop=0, optimize=True, disposal=1)
        if os.path.getsize(path) <= budget:
            return os.path.getsize(path)
    return os.path.getsize(path)


# ═══════════════════════════════════════════════════════════════════════
#  1. ARCHITECTURE — animated flow diagram
# ═══════════════════════════════════════════════════════════════════════
def make_architecture():
    frames = []
    n = int(4.0 * FPS)
    cards = [
        ("Your AI app", "any OpenAI-compatible client", ACCENT, "OpenAI SDK · AionUI · Cursor"),
        ("Gemini Web2API", "localhost:8081/v1", ACCENT2, "self-healing · SSE · tools"),
        ("Gemini web", "free account · no API keys", ACCENT3, "Flash · Pro · thinking"),
    ]
    cw, ch = 268, 170
    gap = 44
    total = 3 * cw + 2 * gap
    x0 = (W - total) // 2
    y0 = 282
    cy = y0 + ch // 2
    edges = [(x0 + cw + 8, cy, x0 + cw + gap - 8, cy),
             (x0 + 2 * cw + gap + 8, cy, x0 + 2 * cw + 2 * gap - 8, cy)]
    dots = dot_grid()
    for i in range(n):
        t = i / n
        frame = bg().convert("RGBA")
        frame.alpha_composite(dots)
        amb = glow_layer(lambda d: (
            d.ellipse([W // 2 - 300, H // 2 - 220, W // 2 + 300, H // 2 + 220],
                      fill=(70, 80, 190, 20))), radius=60)
        frame.alpha_composite(amb)

        dr = ImageDraw.Draw(frame)
        gradient_text(dr, (W // 2, 64), "HOW IT WORKS", F_TITLE, ACCENT2, ACCENT3)
        rounded(dr, W // 2, 122, "One bridge from any OpenAI-compatible client to Gemini", F_SUB, MUTED)

        for e_i, (x1, y1, x2, y2) in enumerate(edges):
            dr.line([(x1, y1), (x2, y2)], fill=(255, 255, 255, 34), width=3)
            dx, dy = (x2 - x1), (y2 - y1)
            ln = math.hypot(dx, dy) or 1
            ux, uy = dx / ln, dy / ln
            ah = 12
            dr.polygon([(x2, y2), (x2 - ux * ah - uy * ah * 0.5, y2 - uy * ah + ux * ah * 0.5),
                        (x2 - ux * ah + uy * ah * 0.5, y2 - uy * ah - ux * ah * 0.5)],
                       fill=(200, 210, 235, 120))
            col = cards[e_i + 1][2]
            for k in range(2):
                ph = (t + e_i * 0.5 + k * 0.5) % 1.0
                px = lerp(x1, x2, ph)
                py = lerp(y1, y2, ph)
                gl = glow_layer(lambda d, px=px, py=py, col=col: (
                    d.ellipse([px - 12, py - 12, px + 12, py + 12], fill=col + (200,))), radius=12)
                frame.alpha_composite(gl)
                dr = ImageDraw.Draw(frame)
                dr.ellipse([px - 4, py - 4, px + 4, py + 4], fill=(255, 255, 255, 235))

        for c_i, (title, big, col, sub) in enumerate(cards):
            st = ease(clamp01((t - c_i * 0.14) / 0.45))
            cx = x0 + c_i * (cw + gap)
            cyy = y0 + (1 - st) * 30
            lift = -6 if c_i == 1 else 0
            card = glass_card(cx, cyy, cw, ch, lift=lift)
            frame.alpha_composite(card)
            dr = ImageDraw.Draw(frame)
            pulse = 0.5 + 0.5 * math.sin(t * math.tau + c_i)
            ng = glow_layer(lambda d, cx=cx, cyy=cyy, col=col, pulse=pulse: (
                d.rounded_rectangle([cx, cyy, cx + cw, cyy + ch], radius=22,
                                    fill=col + (int(22 + 16 * pulse),))), radius=18)
            frame.alpha_composite(ng)
            dr = ImageDraw.Draw(frame)
            acw = int(cw * (0.3 + 0.7 * st))
            dr.rounded_rectangle([cx + 18, cyy + 18, cx + 18 + acw, cyy + 25], radius=4, fill=col)
            pill_w = int(80 + 40 * (0.5 + 0.5 * math.sin(t * math.tau + c_i)))
            dr.rounded_rectangle([cx + cw - pill_w - 18, cyy + 18, cx + cw - 18, cyy + 38],
                                 radius=10, fill=col + (36,), outline=col + (120,))
            dr.text((cx + cw - pill_w // 2 - 18, cyy + 20), "LIVE" if c_i == 1 else ("v1" if c_i == 0 else "free"),
                    font=F_MICRO, fill=col, anchor="ma")
            rounded(dr, cx + cw // 2, cyy + 76, title, F_SUB, TEXT)
            rounded(dr, cx + cw // 2, cyy + 112, big, F_SMALL, col)
            rounded(dr, cx + cw // 2, cyy + 140, sub, F_MICRO, DIM)

        rounded(dr, W // 2, H - 48, "SSE streaming  ·  tool calling  ·  vision  ·  self-healing",
                F_SMALL, MUTED)
        frames.append(frame.convert("RGB"))
    return frames


# ═══════════════════════════════════════════════════════════════════════
#  2. HEALTH — animated self-healing dashboard
# ═══════════════════════════════════════════════════════════════════════
def make_health():
    frames = []
    n = int(4.0 * FPS)
    px, py, pw, phh = 128, 132, W - 256, 392

    for i in range(n):
        t = i / n
        frame = bg().convert("RGBA")
        amb = glow_layer(lambda d: (
            d.ellipse([px - 70, py - 50, px + pw + 70, py + phh + 50], fill=(40, 50, 110, 22))), radius=70)
        frame.alpha_composite(amb)
        dr = ImageDraw.Draw(frame)
        gradient_text(dr, (W // 2, 62), "SELF-HEALING HEALTH", F_TITLE, ACCENT2, ACCENT3)
        rounded(dr, W // 2, 112, "GET / — cookie age · 405 streak · build label · proxy plan", F_SUB, MUTED)

        panel = glass_card(px, py, pw, phh)
        frame.alpha_composite(panel)
        dr = ImageDraw.Draw(frame)
        pulse = 0.5 + 0.5 * math.sin(t * math.tau * 2)
        r_ = 6 + 2 * pulse
        dr.ellipse([px + 28 - r_, py + 32 - r_, px + 28 + r_, py + 32 + r_], fill=(52, 211, 153, 255))
        dr.text((px + 46, py + 20), "online · all systems operational", font=F_SMALL, fill=TEXT)
        dr.rounded_rectangle([px + pw - 140, py + 18, px + pw - 24, py + 40], radius=10,
                             fill=(34, 211, 238, 34), outline=(34, 211, 238, 130))
        dr.text((px + pw - 82, py + 21), "AUTO-HEAL", font=F_MICRO, fill=ACCENT2, anchor="ma")

        age = int(lerp(23, 0, ease(clamp01(t / 0.9))))
        rows = [
            ("cookie age", f"{age}h", ACCENT3, 1.0 - age / 23.0),
            ("405 streak", "0", ACCENT3, 0.96),
            ("build label", "auto-updated", ACCENT2, 0.5 + 0.5 * math.sin(t * math.tau * 0.6)),
            ("proxy plan", "direct → fallbacks", ACCENT2, 0.42 + 0.5 * ease(clamp01((t + 0.5) % 1.0))),
        ]
        for r_i, (label, val, col, frac) in enumerate(rows):
            ry = py + 68 + r_i * 70
            blink = (r_i in (2, 3)) and int(t * 6 + r_i) % 2 == 0
            dr.text((px + 30, ry), label + ("▌" if blink else ""), font=F_MONO, fill=TEXT)
            dr.text((px + pw - 30, ry), val, font=F_MONO, fill=col, anchor="rm")
            bx, by, bw, bh = px + 30, ry + 36, pw - 60, 7
            dr.rounded_rectangle([bx, by, bx + bw, by + bh], radius=4, fill=(255, 255, 255, 20))
            fw = int(bw * clamp01(frac))
            dr.rounded_rectangle([bx, by, bx + fw, by + bh], radius=4, fill=col)

        rcx, rcy, rr = px + 56, py + phh - 42, 21
        arc = 0.5 + 0.5 * math.sin(t * math.tau * 0.5)
        dr.arc([rcx - rr, rcy - rr, rcx + rr, rcy + rr], 90, 90 + 360 * arc, fill=ACCENT2, width=5)
        dr.text((rcx, rcy + 2), f"{int(arc * 100)}%", font=F_MICRO, fill=TEXT, anchor="mm")
        dr.text((rcx + 36, rcy - 12), "uptime", font=F_SMALL, fill=MUTED)
        dr.text((rcx + 36, rcy + 14), "watchdog · 30s poll", font=F_SMALL, fill=DIM)
        dr.text((px + pw - 30, rcy - 12), "last refresh", font=F_SMALL, fill=MUTED, anchor="rm")
        dr.text((px + pw - 30, rcy + 14), "0s ago · extension", font=F_SMALL, fill=DIM, anchor="rm")

        frames.append(frame.convert("RGB"))
    return frames


# ═══════════════════════════════════════════════════════════════════════
#  3. HERO — brand aura + gradient wordmark + status pills
# ═══════════════════════════════════════════════════════════════════════
def make_hero():
    frames = []
    n = int(4.0 * FPS)
    logo_path = os.path.join(HERE, "..", "logo.png")
    logo = Image.open(logo_path).convert("RGBA") if os.path.exists(logo_path) else None
    lsize = 172
    pills = ["SSE streaming", "Tool calling", "Vision", "Open source", "Free forever"]

    for i in range(n):
        t = i / n
        frame = bg().convert("RGBA")
        neb = glow_layer(lambda d, t=t: (
            d.ellipse([W // 2 - 300 + 40 * math.sin(t * math.tau),
                       H // 2 - 330, W // 2 + 300 + 40 * math.sin(t * math.tau),
                       H // 2 + 90], fill=(99, 102, 241, 28)),
            d.ellipse([W // 2 - 220, H // 2 - 250, W // 2 + 220, H // 2 + 130],
                      fill=(34, 211, 238, 18))), radius=75)
        frame.alpha_composite(neb)

        dr = ImageDraw.Draw(frame)
        for p_i in range(18):
            ph = (t + p_i / 18) % 1.0
            px_ = int(lerp(24, W - 24, ph)) + int(22 * math.sin(ph * math.tau * 3 + p_i))
            py_ = int(lerp(40, H - 40, (p_i * 0.37) % 1.0)) + int(16 * math.sin(ph * math.tau * 2))
            a = int(50 + 130 * (0.5 + 0.5 * math.sin(ph * math.tau * 2 + p_i)))
            dr.ellipse([px_ - 2, py_ - 2, px_ + 2, py_ + 2], fill=(255, 255, 255, a))

        ang = t * math.tau
        cx, cyy = W // 2, 150
        breathe = 0.5 + 0.5 * math.sin(t * math.tau * 0.8)
        aur = glow_layer(lambda d, ang=ang: (
            d.arc([cx - 142, cyy - 142, cx + 142, cyy + 142], math.degrees(ang) % 360,
                  math.degrees(ang) % 360 + 200, fill=(34, 211, 238, 160), width=4),
            d.arc([cx - 160, cyy - 160, cx + 160, cyy + 160], math.degrees(-ang) % 360,
                  math.degrees(-ang) % 360 + 120, fill=(99, 102, 241, 130), width=3)), radius=16)
        frame.alpha_composite(aur)
        if logo:
            l = logo.resize((lsize, lsize), Image.LANCZOS)
            frame.paste(l, (cx - lsize // 2, cyy - lsize // 2), l)
        lg = glow_layer(lambda d: (
            d.ellipse([cx - lsize - 12, cyy - 14, cx + lsize + 12, cyy + lsize + 8],
                      fill=(99, 102, 241, int(44 + 34 * breathe)))), radius=36)
        frame.alpha_composite(lg)

        dr = ImageDraw.Draw(frame)
        gradient_text(dr, (cx, 372), "Gemini Web2API", F_LOGO, (140, 150, 255), (34, 211, 238))
        bbox = dr.textbbox((0, 0), "Gemini Web2API", font=F_LOGO)
        tw = bbox[2] - bbox[0]
        tx = (W - tw) // 2
        if not STILL_MODE:
            # transient shine sweep — looks like a glitch frozen in a still frame
            sw = int(lerp(-200, W + 200, (t * 1.3) % 1.0))
            band = glow_layer(lambda d, sw=sw: (
                d.rectangle([sw - 120, 336, sw + 26, 424], fill=(255, 255, 255, 60))), radius=18)
            frame.alpha_composite(band)
            dr = ImageDraw.Draw(frame)

        tf = ease(clamp01((t - 0.2) / 0.5))
        rounded(dr, cx, 458, "Free OpenAI-compatible API for Gemini — no keys, no limits.", F_SUB, TEXT)
        uw = int(tw * (0.25 + 0.75 * tf))
        dr.rounded_rectangle([tx + (tw - uw) // 2, 494, tx + (tw + uw) // 2, 498],
                             radius=3, fill=(34, 211, 238, 210))

        for p_i, pill in enumerate(pills):
            st = ease(clamp01((t - 0.45 - p_i * 0.09) / 0.3))
            if st <= 0:
                continue
            bw_ = int(len(pill) * 11 + 38)
            bx = (W - (len(pills) * (bw_ + 12) - 12)) // 2 + p_i * (bw_ + 12)
            by = 530 - int(8 * (1 - st))
            dr.rounded_rectangle([bx, by, bx + bw_, by + 34], radius=17,
                                 fill=(255, 255, 255, int(14 + 18 * st)),
                                 outline=(160, 180, 220, int(60 + 60 * st)))
            dr.text((bx + bw_ // 2, by + 17), pill, font=F_MICRO, fill=(200, 212, 230, int(160 + 95 * st)),
                    anchor="mm")
        rounded(dr, W // 2, H - 26, "localhost:8081/v1 · one Python file · MIT", F_SMALL, DIM)
        frames.append(frame.convert("RGB"))
    return frames


if __name__ == "__main__":
    t0 = time.time()
    for name, fn in [("ph-architecture", make_architecture),
                     ("ph-health", make_health),
                     ("ph-hero", make_hero)]:
        fr = fn()
        path = os.path.join(HERE, f"{name}.gif")
        palette = build_palette(fr)
        size = save_gif(fr, path, palette)
        if size > MAX_BYTES:
            size = shrink_to_fit(fr, path, palette)
        print(f"{name}.gif: {len(fr)} frames, {size // 1024} KB, {size / MAX_BYTES * 100:.0f}% of budget")
    print(f"done in {time.time() - t0:.1f}s")
