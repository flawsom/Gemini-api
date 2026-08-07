#!/usr/bin/env python3
"""Regenerate the Gemini Cookie Sync extension icons (stdlib-only, no Pillow).

Design v2 - bolder:
  * High-contrast radial violet gradient disc (bright violet center down to
    deep indigo-950 edge) with transparent corners.
  * Thick teal -> blue -> light-violet rainbow arc sweeping over the top
    (~2x the old arc's width).
  * Bold white 'G' monogram in the center (deep-indigo outline + soft drop
    shadow).  The bowl opens to the right; a crossbar with a short vertical
    tail (Google-G style) closes it so it reads as a G at any size.
  * Small gold cookie dot at the arc's right tip (sizes >= 64px only).

Rendering is pure math: distance-to-circle for the disc/arc/bowl, distance-
to-segment for the crossbar, 4x/8x supersampling, premultiplied-alpha box
filtering, and a hand-rolled RGBA PNG writer (zlib + struct).

Tuning: all key parameters can be overridden by icons/icon_params.json
(the format exported by the live design studio at icons/_preview.html).

Usage:
    python _gen_icons.py                     # regenerate icon128.png + icon32.png
    python _gen_icons.py --dump-defaults     # write a fresh icon_params.json (built-ins)
    python _gen_icons.py --params other.json # use a specific params file
    python _gen_icons.py --store             # Chrome Web Store pack -> ../store-assets/
                                              # icons 16/32/48/128 + promo 1280x800 + small 440x280.
                                              # Tile wordmark needs Pillow (falls back to textless).
"""
import json, math, os, struct, sys, zlib

try:
    from PIL import Image, ImageDraw, ImageFont as _PILF
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------- palette ----------------
BG_STOPS = [                       # (t, (r, g, b)); t = dist/radius, 0 center .. 1 edge
    (0.00, (0x9D, 0x4E, 0xDD)),    # bright violet-fuchsia (center)
    (0.35, (0x7C, 0x3A, 0xED)),    # violet-600
    (0.70, (0x4C, 0x1D, 0x95)),    # violet-900
    (1.00, (0x1E, 0x1B, 0x4B)),    # indigo-950 (edge)
]
ARC_STOPS = [(0x2D, 0xD4, 0xBF),   # teal-400
             (0x3B, 0x82, 0xF6),   # blue-500
             (0xA7, 0x8B, 0xFA)]   # violet-400 (stays bright over the violet bg)
G_STROKE  = (0xFF, 0xFF, 0xFF)
G_OUTLINE = (0x1E, 0x1B, 0x4B)     # deep indigo, matches the bg edge
SHADOW    = (0x0F, 0x08, 0x28)
GOLD      = (0xFB, 0xBF, 0x24)
GOLD_RIM  = (0xD9, 0x77, 0x06)

# ---------------- geometry (normalized 0..1 coordinates) ----------------
BG_C     = (0.5, 0.5)
ARC_C    = (0.5, 0.62)
ARC_R    = 0.40
ARC_W    = 0.115                   # total band width - thick!
G_C      = (0.5, 0.5)
G_R      = 0.24                    # bowl radius
G_OPEN0  = 30.0                    # bowl gap on the right side (degrees, y-down)
G_OPEN1  = 330.0
G_W      = 0.082                   # monogram stroke width - bold
G_OUT    = 0.014                   # outline extra width
G_SHAD   = 0.032                   # shadow blur radius
G_SHAD_Y = 0.024                   # shadow offset downward
G_SHAD_A = 0.40                    # shadow opacity
G_BAR_Y  = 0.42                    # crossbar height (slightly above middle)
G_BAR_X0 = G_C[0] + G_R * math.cos(math.radians(G_OPEN0))  # right end meets the gap edge
G_BAR_X1 = 0.42                    # crossbar left end  (tail junction)
G_TAIL_Y = 0.535                   # bottom of the vertical tail at the bar's left end
DOT_C    = (0.90, 0.62)            # gold cookie at the arc's right tip
DOT_R    = 0.028

_DOT_ON = False                    # set per-size in main()
_DOT_ALLOW = True                  # master switch, overridable via params ("dot")


# ---------------- tunable params (icons/icon_params.json) ----------------
PARAMS_PATH = os.path.join(HERE, "icon_params.json")


def _col(hexstr):
    h = hexstr.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _num(p, key, cur):
    v = p.get(key)
    # bool is an int subclass in Python; a stray `true` must not become 1.0
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else cur


def _apply_params(p):
    """Overlay a params dict as exported by the icons/_preview.html studio.

    All color/parse validation happens BEFORE any global mutation, so a
    malformed value can never leave the module half-applied (the caller's
    try/except would otherwise strand a partial mix of old and new params).
    """
    global ARC_C, ARC_R, ARC_W, ARC_STOPS, BG_STOPS
    global G_R, G_W, G_OUT, G_SHAD, G_SHAD_Y, G_SHAD_A, G_BAR_Y, G_BAR_X0
    global DOT_C, DOT_R, GOLD, GOLD_RIM, _DOT_ALLOW
    # -- parse/validate phase (may raise; nothing mutated yet) --
    new_arc_stops = ARC_STOPS
    arc_cols = p.get("arc_colors")
    if isinstance(arc_cols, list) and len(arc_cols) == 3:
        new_arc_stops = tuple(_col(c) for c in arc_cols)
    new_bg_stops = BG_STOPS
    stops = p.get("gradient")
    if isinstance(stops, list) and stops:
        out = [(float(s["pos"]), _col(s["color"]))
               for s in stops if isinstance(s, dict) and "pos" in s and "color" in s]
        if len(out) == len(BG_STOPS):
            new_bg_stops = out
    new_gold     = _col(p["gold"]) if isinstance(p.get("gold"), str) else GOLD
    new_gold_rim = _col(p["gold_rim"]) if isinstance(p.get("gold_rim"), str) else GOLD_RIM
    # -- apply phase (numeric only; cannot raise) --
    ARC_STOPS = new_arc_stops
    BG_STOPS  = new_bg_stops
    ARC_W    = _num(p, "arc_width", ARC_W)
    ARC_R    = _num(p, "arc_radius", ARC_R)
    ARC_C    = (0.5, _num(p, "arc_center_y", ARC_C[1]))
    G_W      = _num(p, "g_width", G_W)
    G_OUT    = _num(p, "g_outline", G_OUT)
    G_R      = _num(p, "g_radius", G_R)
    G_BAR_Y  = _num(p, "g_bar_y", G_BAR_Y)
    G_SHAD_Y = _num(p, "shadow_offset_y", G_SHAD_Y)
    G_SHAD   = _num(p, "shadow_spread", G_SHAD)
    G_SHAD_A = _num(p, "shadow_opacity", G_SHAD_A)
    DOT_R    = _num(p, "dot_size", DOT_R)
    GOLD     = new_gold
    GOLD_RIM = new_gold_rim
    if "dot" in p:
        _DOT_ALLOW = bool(p["dot"])
    # derived geometry (kept in sync with the studio's buildP)
    G_BAR_X0 = G_C[0] + G_R * math.cos(math.radians(G_OPEN0))
    DOT_C    = (ARC_C[0] + ARC_R, ARC_C[1])


def load_params(path=PARAMS_PATH):
    """Apply an optional params file; silently keep built-ins when absent/broken."""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                _apply_params(json.load(fh))
            print("loaded params from %s" % path)
        except Exception as exc:
            print("warning: could not load %s (%s) - using built-in defaults" % (path, exc))


def dump_defaults(path=PARAMS_PATH):
    """Write the built-in defaults in the studio schema (start-fresh file)."""
    payload = {
        "version": 2,
        "comment": "Tuned in icons/_preview.html (Icon Studio). Regenerate with: python icons/_gen_icons.py",
        "arc_width": ARC_W,
        "arc_radius": ARC_R,
        "arc_center_y": ARC_C[1],
        "arc_colors": ["#%02x%02x%02x" % c for c in ARC_STOPS],
        "gradient": [{"pos": t, "color": "#%02x%02x%02x" % c} for t, c in BG_STOPS],
        "g_width": G_W,
        "g_outline": G_OUT,
        "g_radius": G_R,
        "g_bar_y": G_BAR_Y,
        "shadow_offset_y": G_SHAD_Y,
        "shadow_spread": G_SHAD,
        "shadow_opacity": G_SHAD_A,
        "dot": _DOT_ALLOW,
        "dot_size": DOT_R,
        "gold": "#%02x%02x%02x" % GOLD,
        "gold_rim": "#%02x%02x%02x" % GOLD_RIM,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    print("wrote %s" % path)


# ---------------- math helpers ----------------
def _mix(c1, c2, t):
    return (c1[0] + (c2[0] - c1[0]) * t,
            c1[1] + (c2[1] - c1[1]) * t,
            c1[2] + (c2[2] - c1[2]) * t)


def _seg_dist(a, b, p):
    ax, ay = a; bx, by = b; px, py = p
    abx, aby = bx - ax, by - ay
    l2 = abx * abx + aby * aby
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * abx + (py - ay) * aby) / l2))
    return math.hypot(px - (ax + t * abx), py - (ay + t * aby))


def _bg(px, py):
    """Disc gradient color, or None outside the circle."""
    t = math.hypot(px - BG_C[0], py - BG_C[1]) * 2.0
    if t > 1.0:
        return None
    for i in range(len(BG_STOPS) - 1):
        t0, c0 = BG_STOPS[i]
        t1, c1 = BG_STOPS[i + 1]
        if t <= t1:
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return _mix(c0, c1, f)
    return BG_STOPS[-1][1]


def _band(px, py, c, r, w, stops):
    """Thick rainbow band across the upper half, or None. Parametrized so the
    store tiles can echo the icon's arc with their own geometry."""
    ax, ay = px - c[0], py - c[1]
    phi = math.degrees(math.atan2(ay, ax))   # y-down: negative = upper half
    if phi >= 0:
        return None
    d = abs(math.hypot(ax, ay) - r)
    if d > w / 2:
        return None
    t = (phi + 180.0) / 180.0                # 0 = left tip .. 1 = right tip
    if t < 0.5:
        return _mix(stops[0], stops[1], t * 2.0)
    return _mix(stops[1], stops[2], (t - 0.5) * 2.0)


def _arc(px, py):
    return _band(px, py, ARC_C, ARC_R, ARC_W, ARC_STOPS)


def _g_dist(px, py):
    """Distance from a point to the 'G' stroke (bowl arc + crossbar)."""
    gx, gy = px - G_C[0], py - G_C[1]
    r = math.hypot(gx, gy)
    phi = math.degrees(math.atan2(gy, gx)) % 360.0   # y-down: 90 = bottom
    d_bowl = abs(r - G_R) if G_OPEN0 <= phi <= G_OPEN1 else 1e9
    d_bar = _seg_dist((G_BAR_X0, G_BAR_Y), (G_BAR_X1, G_BAR_Y), (px, py))
    d_tail = _seg_dist((G_BAR_X1, G_BAR_Y), (G_BAR_X1, G_TAIL_Y), (px, py))
    return min(d_bowl, d_bar, d_tail)


def _dot(px, py):
    """Gold cookie dot (gold core, darker rim), or None."""
    r = math.hypot(px - DOT_C[0], py - DOT_C[1])
    if r > DOT_R:
        return None
    return _mix(GOLD_RIM, GOLD, min(1.0, r / DOT_R * 2.0))


def _sample(px, py):
    """Composite one sub-sample: (r, g, b, a)."""
    bg = _bg(px, py)
    if bg is None:
        return (0, 0, 0, 0)
    col = bg
    arc = _arc(px, py)
    if arc is not None:
        col = arc
    hw = G_W / 2
    dsh = _g_dist(px, py - G_SHAD_Y)             # true downward drop shadow (y-down coords)
    if dsh < hw + G_SHAD:
        a = G_SHAD_A * (1.0 - dsh / (hw + G_SHAD))
        if a > 0:
            col = _mix(col, SHADOW, a)
    d = _g_dist(px, py)
    if d < hw + G_OUT:
        col = G_STROKE if d < hw else G_OUTLINE
    if _DOT_ON:
        dot = _dot(px, py)
        if dot is not None:
            col = dot
    return (col[0], col[1], col[2], 255)


# ---------------- PNG writer (RGBA, 8-bit) ----------------
def _flatten(rows):
    """Clamped per-pixel bytes (no scanline filter bytes)."""
    return bytes(min(255, max(0, int(round(v))))
                 for row in rows for px in row for v in px)


def _png_bytes(w, h, rows):
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    flat = _flatten(rows)
    stride = w * 4
    raw = b"".join(b"\x00" + flat[y * stride:(y + 1) * stride] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _png_rgba(size, rows):
    return _png_bytes(size, size, rows)


def _write_png(path, w, h, rows):
    with open(path, "wb") as f:
        f.write(_png_bytes(w, h, rows))


def _rows_to_bytes(rows):
    return _flatten(rows)


def render(size, ss):
    """Render at `size` with `ss` x `ss` supersampling (premultiplied box avg)."""
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            sr = sg = sb = sa = 0.0
            for sy in range(ss):
                for sx in range(ss):
                    px = (x + (sx + 0.5) / ss) / size
                    py = (y + (sy + 0.5) / ss) / size
                    r, g, b, a = _sample(px, py)
                    if a:
                        sr += r; sg += g; sb += b; sa += 1
            n = ss * ss
            if sa:
                row.append((sr / sa, sg / sa, sb / sa, sa / n * 255))
            else:
                row.append((0.0, 0.0, 0.0, 0.0))
        rows.append(row)
    return rows


# ---------------- Chrome Web Store assets (../store-assets/) ----------------
STORE_DIR = os.path.normpath(os.path.join(HERE, "..", "store-assets"))
STORE_ICONS = ((16, 8, False), (32, 8, False), (48, 8, False), (128, 4, True))
NAME_TEXT = "Gemini Cookie Sync"
TAGLINE_TEXT = "Keeps your Gemini session fresh \u00b7 the free Web \u2192 API bridge"
_BOLD_FONTS = ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
               "C:/Windows/Fonts/calibrib.ttf", "C:/Windows/Fonts/DejaVuSans-Bold.ttf")
_REG_FONTS = ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
              "C:/Windows/Fonts/calibri.ttf", "C:/Windows/Fonts/DejaVuSans.ttf")


def _font(families, size):
    if not _HAS_PIL:
        return None
    for fam in families:
        if os.path.exists(fam):
            try:
                return _PILF.truetype(fam, size)
            except Exception:
                continue
    return _PILF.load_default()


def _tile(w, h, icon_size, icon_ss, icon_cy, glow_r, name_size, name_y, tagline_size, tagline_y):
    """Compose one store tile: indigo gradient + glow + arc echo + icon + floor shadow."""
    global _DOT_ON
    _DOT_ON = _DOT_ALLOW                  # tile icons are large - keep the cookie dot
    icon = render(icon_size, icon_ss)     # rows of (r, g, b, a)
    cx = w / 2.0
    cy = icon_cy
    half = icon_size / 2.0
    shad_cx, shad_cy = cx, cy + icon_size * 0.52
    shad_r = icon_size * 0.55
    top = (42, 36, 94)
    bot = (11, 10, 26)
    glow = (124, 58, 237)
    shad = (4, 3, 12)
    arc_c = (0.5, 0.55)   # arc echo that visibly sweeps the upper area
    arc_r = 0.42
    arc_w = 0.09
    rows = []
    for y in range(h):
        ny = y / h
        bgcol = _mix(bot, top, 1.0 - ny)
        row = []
        for x in range(w):
            col = bgcol
            # large faint arc echo (same rainbow motif as the icon)
            acol = _band(x / w, y / h, arc_c, arc_r, arc_w, ARC_STOPS)
            if acol is not None:
                col = _mix(col, acol, 0.16)
            # radial glow behind the icon
            dx, dy = x - cx, y - cy
            d2 = math.hypot(dx, dy)
            if d2 < glow_r:
                col = _mix(col, glow, 0.5 * (1.0 - d2 / glow_r) ** 2)
            # soft floor shadow (only below the icon's midline)
            if y > cy:
                sx, sy = x - shad_cx, y - shad_cy
                ds = math.hypot(sx, sy)
                if ds < shad_r:
                    col = _mix(col, shad, 0.30 * (1.0 - ds / shad_r) ** 2)
            # the icon itself (over-composite)
            ix = int(x - (cx - half))
            iy = int(y - (cy - half))
            if 0 <= ix < icon_size and 0 <= iy < icon_size:
                r, g, b, a = icon[iy][ix]
                if a:
                    fa = a / 255.0
                    col = (col[0] + (r - col[0]) * fa,
                           col[1] + (g - col[1]) * fa,
                           col[2] + (b - col[2]) * fa)
            row.append((col[0], col[1], col[2], 255))
        rows.append(row)
    return rows


def gen_store_assets():
    """Write the Chrome Web Store pack into ../store-assets/."""
    global _DOT_ON
    os.makedirs(STORE_DIR, exist_ok=True)
    for size, ss, dot in STORE_ICONS:
        _DOT_ON = dot and _DOT_ALLOW
        rows = render(size, ss)
        _write_png(os.path.join(STORE_DIR, "icon%d.png" % size), size, size, rows)
        print("wrote %s/icon%d.png (%dpx, %dx supersampling)"
              % (os.path.basename(STORE_DIR), size, size, ss))
    for w, h, cfg in ((1280, 800, (360, 4, 300, 470, 62, 612, 30, 692)),
                      (440, 280, (132, 8, 104, 175, 26, 210, 14, 240))):
        icon_size, icon_ss, icon_cy, glow_r, name_size, name_y, tagline_size, tagline_y = cfg
        rows = _tile(w, h, icon_size, icon_ss, icon_cy, glow_r,
                     name_size, name_y, tagline_size, tagline_y)
        if w == 1280:
            path = os.path.join(STORE_DIR, "promo-%dx%d.png" % (w, h))
        else:
            path = os.path.join(STORE_DIR, "small-%dx%d.png" % (w, h))
        if _HAS_PIL:
            # Pillow is used ONLY for font rendering; pixels go back through the
            # same stdlib writer so every asset shares one verifiable pipeline.
            img = Image.frombytes("RGBA", (w, h), _rows_to_bytes(rows))
            d = ImageDraw.Draw(img)
            d.text((w / 2, name_y), NAME_TEXT, font=_font(_BOLD_FONTS, name_size),
                   anchor="mm", fill=(236, 240, 253))
            d.text((w / 2, tagline_y), TAGLINE_TEXT, font=_font(_REG_FONTS, tagline_size),
                   anchor="mm", fill=(148, 156, 178))
            data = img.tobytes()
            rows = [[(data[(y * w + x) * 4], data[(y * w + x) * 4 + 1],
                      data[(y * w + x) * 4 + 2], 255) for x in range(w)] for y in range(h)]
        _write_png(path, w, h, rows)
        print("wrote %s (tile %dx%d, Pillow %s)"
              % (os.path.basename(path), w, h, "on" if _HAS_PIL else "off (textless)"))


def main():
    global _DOT_ON
    if "--dump-defaults" in sys.argv:
        dump_defaults()
        return
    params = PARAMS_PATH
    if "--params" in sys.argv:
        i = sys.argv.index("--params")
        if i + 1 < len(sys.argv):
            params = sys.argv[i + 1]
    load_params(params)
    if "--store" in sys.argv:
        gen_store_assets()
        return
    for size, ss, dot in ((128, 4, True), (32, 8, False)):
        _DOT_ON = dot and _DOT_ALLOW
        rows = render(size, ss)
        path = os.path.join(HERE, "icon%d.png" % size)
        with open(path, "wb") as f:
            f.write(_png_rgba(size, rows))
        print("wrote %s (%dpx, %dx supersampling)" % (path, size, ss))


if __name__ == "__main__":
    main()
