#!/usr/bin/env python3
"""Generate docs/demo.gif - a premium animated demo of Gemini Web2API.

Scene: macOS-style terminal window -> server boot banner -> user types the
curl command char-by-char -> SSE stream grows token-by-token with syntax
highlighting -> data: [DONE] + a timing summary + success glow.

Rendered at 2x and downsampled for crisp text. Dark glassy theme.
"""
import os, math
from PIL import Image, ImageDraw, ImageFont

SCALE = 2
W, H = 980, 620
FW, FH = W * SCALE, H * SCALE

FONT_PATH = r"C:\Windows\Fonts\consola.ttf"
F_SZ = 20
F = ImageFont.truetype(FONT_PATH, F_SZ * SCALE)
F_SM = ImageFont.truetype(FONT_PATH, 15 * SCALE)

# palette
BG        = (10, 16, 30)
TITLE_BG  = (23, 32, 54)
TITLE_LN  = (36, 48, 74)
PROMPT    = (74, 222, 128)   # green
OK        = (74, 222, 128)
DIM       = (110, 124, 152)
WHITE     = (225, 232, 240)
CYAN      = (96, 190, 255)   # json keys
GREEN     = (138, 235, 152)  # json strings
YELLOW    = (250, 200, 92)   # numbers / warnings
PURPLE    = (196, 150, 255)  # json booleans
MAGENTA   = (255, 120, 180)  # accents
GRAY      = (143, 163, 200)
RED       = (255, 112, 112)
AMBER     = (252, 186, 84)

LINE_H = 30 * SCALE
X0 = 26 * SCALE
Y0 = 56 * SCALE


def rgb(hexs):
    return tuple(int(hexs[i:i+2], 16) for i in (0, 2, 4))


def draw_text(d, xy, text, font, fill):
    d.text(xy, text, font=font, fill=fill)


def tokenize_json(s):
    """Split an SSE json line into (text, color) tokens."""
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == '"':
            j = i + 1
            while j < n and s[j] != '"':
                j += 1
            j = min(j + 1, n)
            out.append((s[i:j], GREEN))
            i = j
        elif c == '{' or c == '}' or c == '[' or c == ']' or c == ',' or c == ':':
            out.append((c, DIM))
            i += 1
        elif c == '-' or c.isdigit():
            j = i
            while j < n and (s[j].isdigit() or s[j] in '.-eE+'):
                j += 1
            out.append((s[i:j], YELLOW))
            i = j
        elif s.startswith('true', i) or s.startswith('false', i) or s.startswith('null', i):
            for kw in ('true', 'false', 'null'):
                if s.startswith(kw, i):
                    out.append((kw, PURPLE))
                    i += len(kw)
                    break
        else:
            out.append((c, WHITE))
            i += 1
    return out


def draw_json_line(d, x, y, text, font):
    cx = x
    for t, col in tokenize_json(text):
        d.text((cx, y), t, font=font, fill=col)
        cx += d.textlength(t, font=font)


def wrap_text(d, text, font, max_w):
    """Greedy word-wrap preserving token colors; returns list of pieces."""
    words = tokenize_json(text)
    lines, cur, cur_w = [], [], 0
    for t, col in words:
        w = d.textlength(t, font=font)
        if cur and cur_w + w > max_w:
            lines.append(("".join(c for c, _ in cur), list(cur)))
            cur, cur_w = [], 0
        cur.append((t, col))
        cur_w += w
    if cur:
        lines.append(("".join(c for c, _ in cur), list(cur)))
    # return just the strings; caller re-tokenizes for color (simpler)
    return [l for l, _ in lines]


class Scene:
    """Builds frames of the recording."""

    def __init__(self):
        self.lines = []   # list of dicts: {t: kind, s: text}

    def base(self):
        img = Image.new("RGB", (FW, FH), BG)
        d = ImageDraw.Draw(img)
        # window chrome
        d.rounded_rectangle([0, 0, FW - 1, 44 * SCALE], radius=12 * SCALE,
                            fill=TITLE_BG)
        d.rectangle([0, 22 * SCALE, FW - 1, 44 * SCALE], fill=TITLE_BG)
        d.line([0, 44 * SCALE, FW, 44 * SCALE], fill=TITLE_LN)
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([(16 + i * 26) * SCALE, 14 * SCALE,
                       (28 + i * 26) * SCALE, 26 * SCALE], fill=c)
        # title centered
        title = "gemini-web2api  —  live demo"
        tw = d.textlength(title, font=F_SM)
        draw_text(d, ((FW - tw) / 2, 13 * SCALE), title, F_SM, GRAY)
        # subtle top glow
        for i in range(6):
            d.rectangle([0, 44 * SCALE + i, FW, 44 * SCALE + i + 1],
                        fill=(10 + i * 3, 18 + i * 3, 34 + i * 3))
        # side vignette
        d.rectangle([0, 0, 12 * SCALE, FH], fill=(6, 10, 20))
        d.rectangle([FW - 12 * SCALE, 0, FW, FH], fill=(6, 10, 20))
        return img, d

    def render(self, cursor_at=None, blink=False):
        img, d = self.base()
        y = Y0
        for idx, ln in enumerate(self.lines):
            if isinstance(ln, dict):
                kind, s = ln["t"], ln["s"]
            else:
                kind, s = ln[0], ln[1]
            if kind == "dim":
                draw_text(d, (X0, y), s, F, DIM)
            elif kind == "cmd":
                draw_text(d, (X0, y), "$ ", F, PROMPT)
                draw_text(d, (X0 + d.textlength("$ ", font=F), y), s, F, WHITE)
            elif kind == "ok":
                draw_text(d, (X0, y), s, F, OK)
            elif kind == "warn":
                draw_text(d, (X0, y), s, F, AMBER)
            elif kind == "json":
                # prefix dim, rest syntax-highlighted, wrapped like a terminal
                pre = s.split('{', 1)
                head = pre[0]
                draw_text(d, (X0, y), head, F, DIM)
                x = X0 + d.textlength(head, font=F)
                if len(pre) > 1:
                    body = '{' + pre[1]
                    for piece in wrap_text(d, body, F, FW - X0 * 2):
                        draw_json_line(d, x, y, piece, F)
                        x = X0
                        y += LINE_H
                y -= LINE_H  # we already advanced for continuation lines
            elif kind == "data":
                draw_text(d, (X0, y), s, F, CYAN)
            elif kind == "done":
                draw_text(d, (X0, y), s, F, OK)
            elif kind == "accent":
                draw_text(d, (X0, y), s, F, MAGENTA)
            # cursor
            if cursor_at == idx and blink:
                sx = X0
                if kind == "cmd":
                    sx = X0 + d.textlength("$ " + s, font=F)
                else:
                    sx = X0 + d.textlength(s, font=F)
                d.rectangle([sx + 2 * SCALE, y + 2 * SCALE,
                             sx + 12 * SCALE, y + F_SZ * SCALE - 2 * SCALE],
                            fill=(240, 250, 245))
            y += LINE_H
        # footer status bar
        draw_text(d, (X0, FH - 26 * SCALE), "● 200 OK   ·   421 tokens   ·   5.2s   ·   ~81 tok/s",
                  F_SM, DIM)
        img = img.resize((W, H), Image.LANCZOS)
        return img


def build():
    sc = Scene()
    frames, dur = [], []

    boot = [
        ("dim", "[20:00:00] gemini-web2api v1.1.0  ·  MIT  ·  FREE  ·  SELF-HEALING"),
        ("ok",  "[20:00:00] listening on 0.0.0.0:8081  →  http://localhost:8081/v1"),
        ("dim", "[20:00:00] cookie.txt   session OK   age 3h   (auto-refresh armed)"),
        ("dim", "[20:00:00] proxy plan  direct → 127.0.0.1:7890  (fallback pool)"),
        ("dim", "[20:00:00] gemini_bl   boq_assistant-bard-web-server_20260802.16_p0"),
        ("dim", "[20:00:00] 8 models · 15 endpoints · self-healing watchdog on"),
    ]
    for i in range(1, len(boot) + 1):
        sc.lines = boot[:i]
        frames.append(sc.render(blink=True))
        dur.append(230)

    # type the curl command char by char
    cmd_lines = [
        "curl -N http://localhost:8081/v1/chat/completions \\",
        '  -H "Authorization: Bearer sk-gemini" \\',
        '  -d \'{"model":"gemini-3.6-flash","stream":true,',
        '       "messages":[{"role":"user","content":"Why is the sky blue?"}]}\'',
    ]
    sc.lines = boot + [("cmd", "")]
    full = "curl -N http://localhost:8081/v1/chat/completions \\"
    for k in range(0, len(full) + 1, 3):
        sc.lines[-1] = ("cmd", full[:k])
        frames.append(sc.render(cursor_at=len(sc.lines) - 1, blink=True))
        dur.append(18)
    for ln in cmd_lines[1:]:
        sc.lines.append(("cmd", ln))
        frames.append(sc.render(cursor_at=len(sc.lines) - 1, blink=True))
        dur.append(140)

    # stream opens
    sc.lines.append(("dim", ""))
    frames.append(sc.render())
    dur.append(200)
    sc.lines.append(("json", 'data: {"id":"chatcmpl-a1b2c3d4e5f6","object":"chat.completion.chunk","model":"gemini-3.6-flash","choices":[{"index":0,"delta":{"content":""},"finish_reason":null}]}'))
    frames.append(sc.render())
    dur.append(320)

    # grow the answer token-by-token with a scrolling window (like a real
    # terminal): each frame shows the last N stream lines, so new tokens
    # are always on screen and every frame is visibly different.
    base = list(sc.lines)
    answer = ("The sky appears blue because sunlight interacts with air "
              "molecules: short blue wavelengths scatter far more strongly "
              "than longer ones (Rayleigh scattering), so blue light "
              "reaches your eyes from every direction at once.")
    acc = ""
    stream = []
    for k in range(6, len(answer) + 6, 6):
        acc = answer[:k]
        escaped = acc.replace('"', '\\"')
        stream.append(("json", 'data: {"choices":[{"index":0,"delta":{"content":"%s"}}]}' % escaped))
        sc.lines = base + stream[-4:]  # keep the tail, like scrolling output
        frames.append(sc.render())
        dur.append(120)

    # finish
    sc.lines = base + stream[-4:] + [
        ("json", 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}'),
        ("done", "data: [DONE]"),
    ]
    frames.append(sc.render())
    dur.append(500)

    # finale: glow pulse frame
    final = sc.render()
    g = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(g)
    for i in range(1, 12):
        a = 1 - i / 12
        r = int(10 + 30 * a)
        d.rounded_rectangle([r, r, W - r, H - r], radius=16,
                            outline=(96, 190, 255, 0) if False else
                            tuple(int(96 * a) for _ in range(3)),
                            width=2)
    final = Image.blend(final, g, 0.08)
    frames.append(final)
    dur.append(900)

    q = [f.quantize(colors=128, method=Image.MEDIANCUT, dither=Image.NONE)
         for f in frames]
    q[0].save("docs/demo.gif", save_all=True, append_images=q[1:],
              duration=dur, loop=0)
    print("saved docs/demo.gif —", len(frames), "frames —",
          os.path.getsize("docs/demo.gif"), "bytes")


if __name__ == "__main__":
    build()
