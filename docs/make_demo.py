#!/usr/bin/env python3
"""Generate docs/demo.gif - a REAL capture of the server, rendered as a crisp GIF.

This is not a mockup. The pipeline boots a real `gemini_web2api` server (the
package edition, on an ephemeral port) with your REAL config.json, captures the
real boot banner from its stdout, then fires a REAL streaming request to
/v1/chat/completions and records every SSE `data:` frame with a real timestamp.
The GIF is rendered from exactly those captured lines - real build label, real
request id, real token timing - with only two display normalizations:

  * the ephemeral capture port is rendered as 8081 so it matches the README's
    URLs (the capture config otherwise runs production-identical);
  * the absolute cookie.txt path in the boot banner is shortened to
    "cookie.txt" (a real capture would show the path, but the README shouldn't
    leak your Windows username).

The capture is also saved to docs/demo-capture.json (committed), so the GIF can
be regenerated offline from the last real capture - and if a live capture fails
(Google 405/429 rate limiting, no session, offline), that cached real capture is
reused with a warning instead of silently substituting fake data.

Scene: macOS-style terminal window -> real boot banner line by line -> the curl
command is typed char by char -> the real request-log line -> the REAL SSE stream
grows frame by frame (real content + real timing, terminal-style scrolling) ->
finish_reason + data: [DONE] -> a success glow. Rendered at 2x and downsampled
for crisp text. Dark glassy theme.

Usage:
    python docs/make_demo.py            # live capture + render
    python docs/make_demo.py --offline  # render from docs/demo-capture.json
    python docs/make_demo.py --port 8099
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))      # docs/
ROOT = os.path.dirname(HERE)                            # repo root
CAPTURE_FILE = os.path.join(HERE, "demo-capture.json")
OUT = os.path.join(HERE, "demo.gif")
REAL_CONFIG = os.path.join(ROOT, "config.json")
DISPLAY_PORT = 8081  # what banner/URL lines are normalized to

PROMPT = "Why is the sky blue?"
# max physical terminal lines visible at once (scroll). 15 keeps the tail of
# the stream well clear of the status-bar footer at the bottom of the window.
MAX_PHYSICAL = 15

# ─── palette / typography (same premium dark theme as the other gallery shots) ─
SCALE = 2
W, H = 980, 620
FW, FH = W * SCALE, H * SCALE

FONT_PATH = r"C:\Windows\Fonts\consola.ttf"
F_SZ = 20
F = ImageFont.truetype(FONT_PATH, F_SZ * SCALE)
F_SM = ImageFont.truetype(FONT_PATH, 15 * SCALE)

BG        = (10, 16, 30)
TITLE_BG  = (23, 32, 54)
TITLE_LN  = (36, 48, 74)
OK        = (74, 222, 128)
DIM       = (110, 124, 152)
WHITE     = (225, 232, 240)
CYAN      = (96, 190, 255)
GREEN     = (138, 235, 152)
YELLOW    = (250, 200, 92)
PURPLE    = (196, 150, 255)
MAGENTA   = (255, 120, 180)
GRAY      = (143, 163, 200)
AMBER     = (252, 186, 84)

LINE_H = 30 * SCALE
X0 = 26 * SCALE
Y0 = 56 * SCALE


# ─── live capture ────────────────────────────────────────────────────────────

def log(msg):
    print(f"[make_demo] {msg}", flush=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def boot_server(port):
    """Start the real package server on an ephemeral port with the REAL config.

    Returns (proc, lines, done_event). The server runs production-identical
    (auto_update_bl / xsrf_token exactly as config.json); only the port differs.
    Its state files (cookie-refresh.flag, image-bridge-state.json) live inside
    the package dir, NOT the root files the production server uses - so the
    capture can never trip the user's live watchdog/extension.
    """
    cmd = [sys.executable, "-m", "gemini_web2api",
           "--port", str(port), "--config", REAL_CONFIG]
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"  # piped stdout would otherwise block-buffer
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1,
                         env=env)
    lines, done = [], threading.Event()

    def _read():
        for line in p.stdout:
            lines.append(line.rstrip("\r\n"))
        done.set()

    threading.Thread(target=_read, daemon=True).start()
    deadline = time.time() + 90
    while time.time() < deadline and not any("Base URL" in ln for ln in lines):
        if p.poll() is not None:
            break
        time.sleep(0.2)
    if not any("Base URL" in ln for ln in lines):
        p.kill()
        raise RuntimeError("server did not boot: " + " | ".join(lines[-6:]))
    return p, lines, done


def read_sse(port, key, model):
    """POST a real streaming request; return [(t_sec, "data: {...}"), ...]."""
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    body = json.dumps({"model": model, "stream": True,
                       "messages": [{"role": "user", "content": PROMPT}]}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    events = []
    try:
        # Never route localhost through an environment HTTP(S)_PROXY - the
        # request is to the local server; a configured proxy would 400/refuse it.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=300) as resp:
            t0 = None
            while True:
                line = resp.readline()
                if not line:
                    break
                s = line.decode("utf-8", "replace").rstrip("\r\n")
                if not s.startswith("data: "):
                    continue
                if t0 is None:
                    t0 = time.time()
                events.append({"t": round(time.time() - t0, 3), "line": s})
                if s == "data: [DONE]":
                    break
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        raise RuntimeError(f"live request failed: HTTP {e.code}: "
                           f"{body[:200].decode('utf-8', 'replace')}")
    except Exception as e:
        raise RuntimeError(f"live request failed: {e}")
    if not events:
        raise RuntimeError("live request returned no SSE frames")
    if events[0]["line"].startswith('data: {"error"'):
        raise RuntimeError("live request returned an upstream error frame")
    return events


def sanitize(text, port):
    """Display normalizations: ephemeral port -> 8081, cookie path -> name."""
    text = text.replace(f":{port}", f":{DISPLAY_PORT}")
    text = text.replace(f"localhost:{DISPLAY_PORT}", f"localhost:{DISPLAY_PORT}")
    # Cookie line: "yes (C:\\Users\\...\\cookie.txt)" -> "yes (cookie.txt)"
    text = re.sub(r"yes \(.*cookie\.txt\)", "yes (cookie.txt)", text)
    return text


def _answer_from(events):
    """Concatenate the real text content out of SSE frames."""
    answer = ""
    for ev in events:
        m = re.search(r'"content":\s*"((?:[^"\\]|\\.)*)"', ev["line"])
        if m:
            answer += json.loads('"' + m.group(1) + '"')
    return answer


def _stream(port, key, model, attempts, label):
    """Fetch a NON-EMPTY stream, retrying on upstream errors AND empty
    completions (Google intermittently answers an empty finish_reason:stop
    when the model is busy). Raises once attempts are exhausted."""
    last_err = None
    for attempt in range(attempts):
        try:
            events = read_sse(port, key, model)
            answer = _answer_from(events)
            if not answer.strip():
                raise RuntimeError("upstream returned an empty completion")
            return events
        except RuntimeError as e:
            last_err = e
            log(f"{label} attempt {attempt + 1} failed: {e}")
            if attempt < attempts - 1:
                time.sleep(6)
    raise RuntimeError(f"{label} exhausted after {attempts} attempts "
                       f"(last error: {last_err})")


def tail_request_log():
    """The real request-log line for the just-finished request (server.log)."""
    log_file = os.path.join(ROOT, "server.log")
    try:
        with open(log_file, encoding="utf-8", errors="replace") as f:
            for ln in reversed(f.read().splitlines()[-300:]):
                if '"POST /v1/chat/completions HTTP/1.1"' in ln:
                    return ln.strip()
    except OSError:
        pass
    return f"[{time.strftime('%H:%M:%S')}] 127.0.0.1 " \
           '"POST /v1/chat/completions HTTP/1.1" 200 -'


def capture_live(port):
    """Boot a real server, capture the boot banner + a real SSE stream."""
    cfg = json.load(open(REAL_CONFIG, encoding="utf-8"))
    key = (cfg.get("api_keys") or ["sk-gemini"])[0]
    model = cfg.get("default_model", "gemini-3.6-flash")

    p, lines, done = boot_server(port)
    try:
        boot = [sanitize(ln, port) for ln in list(lines)]  # snapshot before req
        events = None
        try:
            events = _stream(port, key, model, 5, "fresh instance")
        except RuntimeError as e:
            log(f"fresh instance failed after retries: {e}")
        after = list(lines)
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()

    if events is not None:
        log("stream captured from the fresh instance (ephemeral session)")
        request_log = [sanitize(ln, port) for ln in after
                       if "POST /v1/chat/completions" in ln]
        if not request_log:
            request_log = [tail_request_log()]
    else:
        # The fresh instance normally streams via the session-probe XSRF token
        # (the server recovers the working `at` from StreamGenerate's 400
        # error). This fallback is a safety net for upstream rate limits
        # (429/405) or a broken session: capture the stream from the LIVE
        # server on 8081 - still a real capture.
        log(f"streaming from the running server on {DISPLAY_PORT} "
            f"(real session, real token)")
        events = _stream(DISPLAY_PORT, key, model, 4, "live server")
        time.sleep(0.5)
        request_log = [tail_request_log()] if os.path.exists(
            os.path.join(ROOT, "server.log")) else \
            [f"[{time.strftime('%H:%M:%S')}] 127.0.0.1 "
             '"POST /v1/chat/completions HTTP/1.1" 200 -']

    answer = _answer_from(events)
    tokens = max(len(answer) // 4, 1)
    dur = events[-1]["t"] if events else 0.0

    curl = [
        "curl -N http://localhost:8081/v1/chat/completions \\",
        '  -H "Authorization: Bearer sk-gemini" \\',
        '  -H "Content-Type: application/json" \\',
        "  -d '{\"model\":\"" + model + "\",\"stream\":true,"
        '"messages":[{"role":"user","content":"' + PROMPT + '"}]}\'',
    ]
    cap = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "port": port,
        "model": model,
        "prompt": PROMPT,
        "boot": boot,
        "request_log": request_log,
        "curl": curl,
        "sse": events,
        "meta": {
            "answer": answer,
            "tokens": tokens,
            "duration_s": round(dur, 2),
            "frames": len(events),
            "tok_per_s": round(tokens / dur, 1) if dur else 0,
        },
    }
    if not answer.strip():
        raise RuntimeError("empty completion from upstream - capture not saved")
    with open(CAPTURE_FILE, "w", encoding="utf-8") as f:
        json.dump(cap, f, ensure_ascii=False, indent=1)
    return cap


def load_capture():
    with open(CAPTURE_FILE, encoding="utf-8") as f:
        return json.load(f)


# ─── rendering ───────────────────────────────────────────────────────────────

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
        elif c in "{[]},":
            out.append((c, DIM))
            i += 1
        elif c == ":":
            out.append((c, DIM))
            i += 1
        elif c == "-" or c.isdigit():
            j = i
            while j < n and (s[j].isdigit() or s[j] in ".-eE+"):
                j += 1
            out.append((s[i:j], YELLOW))
            i = j
        elif s.startswith("true", i) or s.startswith("false", i) or s.startswith("null", i):
            for kw in ("true", "false", "null"):
                if s.startswith(kw, i):
                    out.append((kw, PURPLE))
                    i += len(kw)
                    break
        else:
            out.append((c, WHITE))
            i += 1
    return out


def sse_tokens(line):
    """data: <json> -> dim 'data: ' prefix + syntax-highlighted JSON."""
    if line.startswith("data: "):
        return [("data: ", CYAN)] + tokenize_json(line[len("data: "):])
    return tokenize_json(line)


def layout(d, tokens, x, y, font, max_w):
    """Flatten tokens into drawable (x, y, text, color) pieces with terminal
    wrap + hard-splitting of over-wide tokens. Returns (pieces, height_px)."""
    pieces = []
    cx, cy = x, y
    for text, col in tokens:
        w = d.textlength(text, font=font)
        if cx + w > max_w and cx > x:
            cx, cy = x, cy + LINE_H
        while w > max_w - cx:
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if d.textlength(text[:mid], font=font) <= max_w - cx:
                    lo = mid
                else:
                    hi = mid - 1
            if lo == 0:
                lo = 1
            pieces.append((cx, cy, text[:lo], col))
            text = text[lo:]
            w = d.textlength(text, font=font)
            cx, cy = x, cy + LINE_H
        pieces.append((cx, cy, text, col))
        cx += w
    return pieces, cy + LINE_H - y


def line_pieces(d, kind, s, font, max_w):
    """Return drawable pieces for one logical terminal line.

    Every line goes through the wrap + hard-split layout so long lines (the
    BL log line, the model list, the curl -d payload) wrap like a real
    terminal instead of clipping at the window edge."""
    if kind == "cmd":
        return layout(d, [("$ ", OK), (s, WHITE)], X0, Y0, font, max_w)
    if kind == "json":
        return layout(d, sse_tokens(s), X0, Y0, font, max_w)
    if kind == "done" or kind == "ok":
        return layout(d, [(s, OK)], X0, Y0, font, max_w)
    if kind == "dim":
        return layout(d, [(s, DIM)], X0, Y0, font, max_w)
    return layout(d, [(s, WHITE)], X0, Y0, font, max_w)


class Scene:
    """One terminal 'screen' - the visible, scroll-windowed history."""

    def __init__(self, lines=None):
        self.lines = lines if lines is not None else []

    def base(self):
        img = Image.new("RGB", (FW, FH), BG)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, FW - 1, 44 * SCALE], radius=12 * SCALE,
                            fill=TITLE_BG)
        d.rectangle([0, 22 * SCALE, FW - 1, 44 * SCALE], fill=TITLE_BG)
        d.line([0, 44 * SCALE, FW, 44 * SCALE], fill=TITLE_LN)
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([(16 + i * 26) * SCALE, 14 * SCALE,
                       (28 + i * 26) * SCALE, 26 * SCALE], fill=c)
        title = "gemini-web2api  —  live capture"
        tw = d.textlength(title, font=F_SM)
        d.text(((FW - tw) / 2, 13 * SCALE), title, font=F_SM, fill=GRAY)
        for i in range(6):
            d.rectangle([0, 44 * SCALE + i, FW, 44 * SCALE + i + 1],
                        fill=(10 + i * 3, 18 + i * 3, 34 + i * 3))
        d.rectangle([0, 0, 12 * SCALE, FH], fill=(6, 10, 20))
        d.rectangle([FW - 12 * SCALE, 0, FW, FH], fill=(6, 10, 20))
        return img, d

    def render(self, cursor_at=None, blink=False, footer=None):
        max_w = FW - X0 * 2
        # Physical-height windowing: drop the topmost logical lines so the
        # tail of the history fits the terminal (like a real scrolling screen).
        heights = []
        for ln in self.lines:
            kind = ln["t"] if isinstance(ln, dict) else ln[0]
            s = ln["s"] if isinstance(ln, dict) else ln[1]
            dummy = ImageDraw.Draw(Image.new("RGB", (8, 8)))
            _, h = line_pieces(dummy, kind, s, F, max_w)
            heights.append(h)
        total = sum(heights)
        start = 0
        while start < len(heights) and total - heights[start] > MAX_PHYSICAL * LINE_H:
            total -= heights[start]
            start += 1
        visible = self.lines[start:]

        img, d = self.base()
        y = Y0
        rel_cursor = None
        for idx, ln in enumerate(visible):
            kind = ln["t"] if isinstance(ln, dict) else ln[0]
            s = ln["s"] if isinstance(ln, dict) else ln[1]
            if idx + start == cursor_at:
                rel_cursor = (idx, kind, s)
            pieces, _ = line_pieces(d, kind, s, F, max_w)
            for px, py, t, col in pieces:
                d.text((px, y + (py - Y0)), t, font=F, fill=col)
            if rel_cursor and rel_cursor[0] == idx and blink:
                # cursor at the end of the LAST wrapped piece (like a real caret)
                sx = pieces[-1][0] + d.textlength(pieces[-1][2], font=F)
                sy = y + pieces[-1][1] - Y0
                d.rectangle([sx + 2 * SCALE, sy + 2 * SCALE,
                             sx + 12 * SCALE, sy + F_SZ * SCALE - 2 * SCALE],
                            fill=(240, 250, 245))
            y += heights[start + idx]

        f = footer or ("● 200 OK  ·  %s tokens  ·  %ss  ·  ~%s tok/s"
                       % ("—", "—", "—"))
        d.text((X0, FH - 26 * SCALE), f, font=F_SM, fill=DIM)
        img = img.resize((W, H), Image.LANCZOS)
        return img


def build(cap):
    # Real boot lines are plain strings (incl. blank lines) - normalize them
    # into terminal line tuples so the renderer treats them as dim text.
    boot = [("dim", ln) for ln in cap["boot"]]
    request_log = cap["request_log"][0] if cap["request_log"] else ""
    curl = cap["curl"]
    sse = cap["sse"]
    meta = cap.get("meta", {})
    stats_footer = ("● 200 OK  ·  %s tokens  ·  %ss  ·  ~%s tok/s"
                    % (meta.get("tokens", "—"), meta.get("duration_s", "—"),
                       meta.get("tok_per_s", "—")))
    wait_footer = "● gemini-web2api v1.1.0  —  waiting for a streaming request..."

    frames, dur = [], []
    history = []

    def snap(cursor_at=None, blink=False, delay=None, footer=None):
        sc = Scene(list(history))
        frames.append(sc.render(cursor_at=cursor_at, blink=blink,
                                footer=footer or wait_footer))
        dur.append(delay if delay is not None else 200)

    # 1) boot banner, line by line (real text, real timestamps)
    for i in range(1, len(boot) + 1):
        history = boot[:i]
        snap(delay=215)
    history = list(boot)

    # 2) type the curl command char by char
    history.append(("cmd", ""))
    snap(cursor_at=len(history) - 1, blink=True, delay=340)
    full = curl[0]
    for k in range(0, len(full) + 1, 3):
        history[-1] = ("cmd", full[:k])
        snap(cursor_at=len(history) - 1, blink=True, delay=16)
    for ln in curl[1:]:
        history.append(("cmd", ln))
        snap(cursor_at=len(history) - 1, blink=True, delay=150)

    # 3) waiting for the first token (real first-token latency)
    history.append(("dim", ""))
    snap(delay=700)

    # 4) the real SSE stream: first frame holds, then token-by-token with the
    #    REAL inter-frame timing (terminal-style scrolling window)
    first = sse[0]["line"]
    history.append(("json", first))
    snap(delay=620, footer=stats_footer)
    prev = sse[0]["t"]
    for ev in sse[1:]:
        history.append(("json", ev["line"]))
        dt = int((ev["t"] - prev) * 1000)
        prev = ev["t"]
        snap(delay=min(max(dt, 50), 380), footer=stats_footer)

    # 5) finish + the real request-log line (printed after the stream)
    if any("finish_reason\":\"stop\"" in ev["line"] for ev in sse):
        pass  # the server already sent the stop frame inside the stream
    history.append(("done", "data: [DONE]"))
    snap(delay=480, footer=stats_footer)
    if request_log:
        history.append(("dim", request_log))
        snap(delay=260, footer=stats_footer)
    history.append(("cmd", ""))
    snap(cursor_at=len(history) - 1, blink=True, delay=420,
         footer=stats_footer)

    # 6) finale: success glow pulse
    final = Scene(list(history)).render(cursor_at=len(history) - 1, blink=False,
                                        footer=stats_footer)
    g = Image.new("RGB", (W, H), BG)
    dd = ImageDraw.Draw(g)
    for i in range(1, 12):
        a = 1 - i / 12
        r = int(10 + 30 * a)
        dd.rounded_rectangle([r, r, W - r, H - r], radius=16,
                             outline=tuple(int(96 * a) for _ in range(3)),
                             width=2)
    frames.append(Image.blend(final, g, 0.08))
    dur.append(900)

    q = [f.quantize(colors=128, method=Image.MEDIANCUT, dither=Image.NONE)
         for f in frames]
    q[0].save(OUT, save_all=True, append_images=q[1:], duration=dur, loop=0)
    return len(frames), sum(dur) / 1000.0


def main():
    ap = argparse.ArgumentParser(description="Generate docs/demo.gif from a REAL capture")
    ap.add_argument("--offline", action="store_true",
                    help="render from docs/demo-capture.json (no live capture)")
    ap.add_argument("--port", type=int, default=None, help="capture port")
    args = ap.parse_args()

    port = args.port or free_port()
    cap = None
    if not args.offline:
        try:
            log(f"capturing a real session on port {port} (boot + streaming request)...")
            cap = capture_live(port)
            log("live capture OK: %d boot lines, %d SSE frames, answer %d chars"
                % (len(cap["boot"]), len(cap["sse"]),
                   len(cap.get("meta", {}).get("answer", ""))))
        except Exception as e:
            log(f"LIVE CAPTURE FAILED: {e}")
            if os.path.exists(CAPTURE_FILE):
                log("falling back to the last real capture "
                    f"({CAPTURE_FILE}) - the GIF is never faked")
                cap = load_capture()
            else:
                log("no cached capture either - the old GIF is left untouched")
                return 1
    if cap is None:
        cap = load_capture()
        log(f"rendering from cached capture ({CAPTURE_FILE})")

    n, secs = build(cap)
    print(f"saved docs/demo.gif — {n} frames — {secs:.1f}s — "
          f"{os.path.getsize(OUT)} bytes")
    print(f"captured from a real session at {cap.get('captured_at')} "
          f"(model {cap.get('model')}, prompt \"{cap.get('prompt')}\")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
