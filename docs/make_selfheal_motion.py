"""Generate an animated self-healing loop GIF for the engineering writeup.

Scene: a live status panel where a 405 storm appears, the watchdog probes
candidate build labels, adopts the winner, triggers a cookie refresh, and the
panel returns to green. Same dark glassmorphism language as make_motion.py.

Output: docs/ph-selfheal.gif (960x540 @ 24fps, eased, offline-safe).
Run:  python docs/make_selfheal_motion.py
"""
import os
import math

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
W, H = 960, 540
FPS = 24
FRAME_MS = int(1000 / FPS)

# palette
BG_TOP = (9, 13, 28)
BG_BOT = (14, 22, 44)
ACCENT = (99, 102, 241)
CYAN = (34, 211, 238)
GREEN = (52, 211, 153)
RED = (248, 113, 113)
AMBER = (251, 191, 36)
TEXT = (226, 232, 240)
MUTED = (148, 163, 184)
DIM = (100, 116, 139)

N_FRAMES = int(9.0 * FPS)  # 9-second loop

# timeline (seconds): the five acts
T_STORM = 0.5      # 405 storm begins
T_PROBE = 2.2      # watchdog probes BLs
T_ADOPT = 3.6      # BL adopted, retry 200
T_REFRESH = 5.2    # cookie refresh triggered
T_CLEAR = 6.6      # all green, age 0


def _font(paths, size):
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


F_TITLE = _font([r"C:\Windows\Fonts\segoeuib.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"], 34)
F_LABEL = _font([r"C:\Windows\Fonts\segoeui.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"], 20)
F_MONO = _font([r"C:\Windows\Fonts\consola.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"], 18)
F_SMALL = _font([r"C:\Windows\Fonts\segoeui.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"], 15)
F_TINY = _font([r"C:\Windows\Fonts\segoeui.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"], 13)


def ease(x):
    x = max(0.0, min(1.0, x))
    return 0.5 - 0.5 * math.cos(math.pi * x)


def clamp01(x):
    return max(0.0, min(1.0, x))


def lerp(a, b, t):
    return a + (b - a) * t


def seg_alpha(t, start, dur, hold=0.0):
    """Alpha envelope: fade in, hold, fade out. Returns (alpha, mid_t)."""
    if t < start:
        return 0.0, 0.0
    if t < start + 0.25:
        return ease((t - start) / 0.25), 0.0
    mid_start = start + 0.25
    mid_end = mid_start + hold
    if t < mid_end:
        return 1.0, t - mid_start
    out_end = mid_end + 0.25
    if t < out_end:
        return 1.0 - ease((t - out_end) / 0.25), 1.0
    return 0.0, 1.0


def rounded(draw, box, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def glow(img, xy, radius, color, strength=40):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse(xy, fill=color + (strength,))
    layer = layer.filter(ImageFilter.GaussianBlur(radius))
    img.alpha_composite(layer)


def chip(d, x, y, w, h, label, sub, color, alpha, pulse=0.0):
    if alpha <= 0:
        return
    base = (18, 26, 50)
    fill = (base[0] + int(pulse * 6), base[1] + int(pulse * 6), base[2] + int(pulse * 10))
    rounded(d, [x, y, x + w, y + h], 14, fill=fill, outline=color + (int(90 * alpha),), width=2)
    # status dot
    dot_r = 5
    d.ellipse([x + 16, y + h / 2 - dot_r, x + 16 + dot_r * 2, y + h / 2 + dot_r], fill=color + (255,))
    d.text((x + 30, y + 11), label, font=F_LABEL, fill=TEXT + (255,))
    d.text((x + 30, y + 38), sub, font=F_SMALL, fill=MUTED + (255,))


def draw_frame(t, act_progress):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    # background
    bg = Image.new("RGBA", (W, H), BG_TOP)
    d = ImageDraw.Draw(bg)
    for i in range(H):
        k = i / H
        c = tuple(int(lerp(BG_TOP[j], BG_BOT[j], k)) for j in range(3))
        d.line([(0, i), (W, i)], fill=c + (255,))
    img.alpha_composite(bg)
    d = ImageDraw.Draw(img)

    # ambient glows
    glow(img, (W - 260, -120, W + 120, 260), 90, ACCENT, 26)
    glow(img, (-140, H - 200, 220, H + 60), 90, CYAN, 18)

    # title bar
    d.text((32, 26), "Gemini Web2API  —  self-healing loop", font=F_TITLE, fill=TEXT + (255,))
    d.text((32, 70), "a 405 storm is detected, the build label is probed, and cookies refresh themselves",
           font=F_SMALL, fill=MUTED + (255,))

    # ── the request line ─────────────────────────────────────────────
    y_req = 112
    x0, x1 = 60, W - 60
    d.line([(x0, y_req), (x1, y_req)], fill=DIM + (255,), width=2)
    # moving request dot
    phase = (t % 3.2) / 3.2
    px = x0 + (x1 - x0) * phase
    d.ellipse([px - 6, y_req - 6, px + 6, y_req + 6], fill=CYAN + (255,))
    d.text((x0, y_req - 26), "request", font=F_TINY, fill=MUTED + (255,))
    d.text((x1 - 40, y_req - 26), "Gemini web", font=F_TINY, fill=MUTED + (255,))

    # ── status rows ──────────────────────────────────────────────────
    rows = [
        ("HTTP status", "", 0),
        ("build label (BL)", "boq_assistant-bard-web-server_20260803.06_p0", 1),
        ("cookie age", "3h 12m", 2),
        ("405 streak", "0", 3),
    ]

    def status_color(act, t):
        if act == "storm" and t > T_STORM:
            return RED
        if act == "probe":
            return AMBER
        if act == "adopt" and t > T_ADOPT:
            return GREEN
        if act == "refresh" and t > T_REFRESH:
            return AMBER
        if act == "clear" and t > T_CLEAR:
            return GREEN
        return GREEN

    panel_x, panel_y = 60, 160
    panel_w, panel_h = 430, 118
    rounded(d, [panel_x, panel_y, panel_x + panel_w, panel_y + panel_h], 18,
            fill=(15, 21, 42), outline=(45, 55, 90) + (255,), width=1)

    # HTTP status row
    http_color = status_color("storm", t)
    if t > T_STORM and t < T_ADOPT:
        http_txt = "405 Method Not Allowed"
    elif t > T_ADOPT:
        http_txt = "200 OK  (after probe)"
    else:
        http_txt = "200 OK"
    d.text((panel_x + 18, panel_y + 14), "HTTP status", font=F_SMALL, fill=MUTED + (255,))
    d.text((panel_x + 18, panel_y + 36), http_txt, font=F_MONO, fill=http_color + (255,))

    # BL row
    bl_alpha, _ = seg_alpha(t, T_PROBE, 0.4, hold=1.0)
    if bl_alpha > 0:
        rounded(d, [panel_x + 18, panel_y + 64, panel_x + 168, panel_y + 104], 10,
                fill=(24, 32, 60), outline=AMBER + (int(160 * bl_alpha),), width=1)
        d.text((panel_x + 26, panel_y + 72), "probing BL candidates…", font=F_TINY, fill=AMBER + (255,))
        # spinner
        sp = (t * 6.0) % (2 * math.pi)
        cx, cy = panel_x + 140, panel_y + 84
        for i in range(8):
            a = sp + i * math.pi / 4
            sx, sy = cx + 9 * math.cos(a), cy + 9 * math.sin(a)
            d.ellipse([sx - 2.5, sy - 2.5, sx + 2.5, sy + 2.5], fill=AMBER + (255,))

    # right side: metrics
    mx = panel_x + 200
    # 405 streak
    streak = 0
    if t > T_STORM and t < T_ADOPT:
        streak = 3
    elif t >= T_ADOPT:
        streak = 0
    d.text((mx, panel_y + 14), "405 streak", font=F_SMALL, fill=MUTED + (255,))
    d.text((mx + 160, panel_y + 14), str(streak), font=F_MONO,
           fill=(RED if streak else GREEN) + (255,))

    # cookie age: depletes then refreshes
    age_h = 3.0
    if t > T_REFRESH and t < T_CLEAR:
        k = ease(clamp01((t - T_REFRESH) / 0.8))
        age_h = lerp(3.0, 0.0, k)
    elif t >= T_CLEAR:
        age_h = 0.0
    d.text((mx, panel_y + 44), "cookie age", font=F_SMALL, fill=MUTED + (255,))
    age_txt = f"{age_h:.1f}h"
    d.text((mx + 160, panel_y + 44), age_txt, font=F_MONO,
           fill=(AMBER if age_h > 0 else GREEN) + (255,))

    d.text((mx, panel_y + 74), "auto_update_bl", font=F_SMALL, fill=MUTED + (255,))
    d.text((mx + 160, panel_y + 74), "on", font=F_MONO, fill=GREEN + (255,))

    # ── the probe panel (right side) ─────────────────────────────────
    if t > T_PROBE:
        probe_x, probe_y = W - 420, 160
        probe_w, probe_h = 360, 150
        rounded(d, [probe_x, probe_y, probe_x + probe_w, probe_y + probe_h], 18,
                fill=(15, 21, 42), outline=(45, 55, 90) + (255,), width=1)
        d.text((probe_x + 18, probe_y + 12), "BL probe  (probe-before-apply)", font=F_SMALL,
               fill=AMBER + (255,))
        bls = [
            ("BL_20260802.16_p0", "405 — reject", RED, 0.0),
            ("BL_20260803.06_p0", "200 — adopt ✓", GREEN, 0.45),
        ]
        for i, (name, verdict, color, delay) in enumerate(bls):
            a, _ = seg_alpha(t, T_PROBE + 0.15 + delay, 0.3, hold=1.6)
            if a <= 0:
                continue
            yy = probe_y + 44 + i * 46
            rounded(d, [probe_x + 18, yy, probe_x + probe_w - 18, yy + 36], 9,
                    fill=(22, 30, 56), outline=color + (int(120 * a),), width=1)
            d.text((probe_x + 28, yy + 9), name, font=F_MONO, fill=TEXT + (255,))
            d.text((probe_x + 200, yy + 9), verdict, font=F_MONO, fill=color + (255,))
        if t > T_ADOPT:
            a, _ = seg_alpha(t, T_ADOPT, 0.3, hold=2.0)
            if a > 0:
                rounded(d, [probe_x + 18, probe_y + 138, probe_x + probe_w - 18, probe_y + 158], 8,
                        fill=(20, 40, 32), outline=GREEN + (int(150 * a),), width=1)
                d.text((probe_x + 28, probe_y + 141), "adopted → retry succeeded", font=F_TINY,
                       fill=GREEN + (255,))

    # ── the refresh banner (bottom) ──────────────────────────────────
    if t > T_REFRESH:
        bx, by = 60, H - 96
        bw, bh = W - 120, 52
        a, _ = seg_alpha(t, T_REFRESH, 0.35, hold=1.8)
        if a > 0:
            rounded(d, [bx, by, bx + bw, by + bh], 14,
                    fill=(20, 28, 52), outline=AMBER + (int(140 * a),), width=1)
            d.text((bx + 20, by + 15), "watchdog: cookie age > 24h · 405 storm → triggering refresh",
                   font=F_SMALL, fill=TEXT + (255,))
            # progress bar
            pk = ease(clamp01((t - T_REFRESH) / 1.4))
            d.rounded_rectangle([bx + 20, by + 36, bx + 20 + int((bw - 40) * pk), by + 42],
                                radius=3, fill=CYAN + (255,))
    if t > T_CLEAR:
        a, _ = seg_alpha(t, T_CLEAR, 0.3, hold=2.0)
        if a > 0:
            rounded(d, [60, H - 96, W - 60, H - 52], 14,
                    fill=(18, 40, 32), outline=GREEN + (int(160 * a),), width=1)
            d.text((80, H - 88), "✓ healed — 200 OK · BL current · cookie age 0.0h · streak 0",
                   font=F_SMALL, fill=GREEN + (255,))

    # footer
    d.text((32, H - 30), "github.com/flawsom/Gemini-api — unofficial bridge, self-healing, 515 tests",
           font=F_TINY, fill=DIM + (255,))
    return img


def main():
    frames = []
    for fi in range(N_FRAMES):
        t = fi / FPS
        img = draw_frame(t, None)
        frames.append(img.convert("RGB"))
        if fi % 24 == 0:
            print(f"  frame {fi}/{N_FRAMES}")
    out = os.path.join(HERE, "ph-selfheal.gif")
    frames[0].save(out, save_all=True, append_images=frames[1:],
                   duration=FRAME_MS, loop=0, optimize=False)
    size = os.path.getsize(out)
    print(f"wrote {out}  ({size/1024:.0f} KB, {N_FRAMES} frames)")
    if size > 1_880_000:
        print("WARN: over 1.8MB — recompress before upload")
    else:
        print("OK: under 1.8MB")


if __name__ == "__main__":
    main()
