#!/usr/bin/env python3
"""Regenerate all five README gallery screenshots with one command.

Pipeline (the same Chrome-headless capture the docs/*.png images use):

  1. console  -> boot a real `gemini_web2api.py` (probe-free capture config so
                 boot is instant) on an ephemeral port, fire real requests so
                 the request log fills with authentic lines, screenshot the
                 captured terminal text.
  2. health   -> GET / from the SAME booted server, render the JSON payload.
  3. stream   -> curl -N a real SSE chat completion through that server
                 (token-by-token + data: [DONE]).
  4. popup    -> render the real popup.html with a chrome.* stub + canned
                 health payload (no extension runtime needed).
  5. icon     -> composite the REAL icons/icon128.png onto a browser-toolbar
                 mockup (traffic lights + URL pill + pinned slots).

Each page is rendered as dark HTML and screenshotted with headless Chrome
at --force-device-scale-factor=2 so every PNG is the same retina 2x format
(1560 CSS px * 2 = 3120 px wide for the terminal shots) as the gallery.

Usage:
    python docs/capture_screenshots.py            # regenerate all five
    python docs/capture_screenshots.py health popup icon   # selected shots only
    python docs/capture_screenshots.py --port 8099    # all five, custom port
    python docs/capture_screenshots.py --skip stream  # skip flaky upstream

Notes:
  * The booted server uses the REAL config.json minus the boot-time network
    probes (auto_update_bl off, xsrf_token pre-set) so the banner matches
    production but boots instantly. The ephemeral port is normalized to 8081
    in rendered text so the shots match the README's URLs.
  * stream makes one real Gemini call using your cookies (tiny prompt). If it
    fails (405/429/offline), the existing PNG is kept and a warning is shown.
"""
import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))           # docs/
ROOT = os.path.dirname(HERE)                                  # repo root
DOCS = HERE
SERVER = os.path.join(ROOT, "gemini_web2api.py")
REAL_CONFIG = os.path.join(ROOT, "config.json")
POPUP_HTML = os.path.join(ROOT, "gemini-cookie-sync-extension", "popup.html")
POPUP_JS = os.path.join(ROOT, "gemini-cookie-sync-extension", "popup.js")
ICON128 = os.path.join(ROOT, "gemini-cookie-sync-extension", "icons", "icon128.png")

DISPLAY_PORT = 8081  # what banner lines are normalized to

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


# ─── helpers ──────────────────────────────────────────────────────────────

def log(msg):
    print(f"[capture] {msg}", flush=True)


def find_chrome():
    for c in CHROME_CANDIDATES:
        if os.path.exists(c):
            return c
    return shutil.which("chrome")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def capture_config():
    """Real config, minus the boot-time network probes (instant boot)."""
    with open(REAL_CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["auto_update_bl"] = False
    cfg["xsrf_token"] = "capture-mode"  # non-empty -> boot skips the fetch
    fd, path = tempfile.mkstemp(suffix=".json", prefix="_cap_cfg_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return path


def boot_server(port, cfg_path):
    """Start the server, returning (proc, lines, done_event)."""
    cmd = [sys.executable, SERVER, "--port", str(port), "--config", cfg_path]
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
    deadline = time.time() + 30
    while time.time() < deadline and not any(
            "Base URL" in ln for ln in lines):
        if p.poll() is not None:
            break
        time.sleep(0.2)
    if not any("Base URL" in ln for ln in lines):
        p.kill()
        raise RuntimeError("server did not boot: " + " | ".join(lines[-6:]))
    return p, lines, done


def api_key():
    """The configured API key (used to hit /v1 endpoints for the log)."""
    with open(REAL_CONFIG, encoding="utf-8") as f:
        return (json.load(f).get("api_keys") or ["sk-gemini"])[0]


def http(port, path, method="GET", body=None, key=None):
    """Fire a real request and return the status code (0 on failure)."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     method=method)
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body).encode()
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def screenshot(chrome, html, out, width=1560, height=None, scale=2):
    """Write html to a temp file and screenshot it with headless Chrome."""
    fd, path = tempfile.mkstemp(suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(html)
    if height is None:
        height = 900
    cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--force-device-scale-factor=%d" % scale,
           "--window-size=%d,%d" % (width, height),
           "--screenshot=%s" % out, "file:///%s" % path.replace(os.sep, "/")]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    os.unlink(path)
    if r.returncode != 0:
        raise RuntimeError("Chrome failed: " + r.stderr[-1500:])
    if not os.path.exists(out):
        raise RuntimeError("Chrome produced no screenshot")


def page_template(title, badge, body, font_size=19):
    return """<!doctype html>
<html><head><meta charset="utf-8">
<style>
  html, body { margin:0; padding:0; background:#0b1220; }
  body { font-family:'Cascadia Code','Consolas','Menlo',monospace; color:#e2e8f0;
         font-size:%(fs)dpx; line-height:1.6; padding:28px 32px 34px; width:1500px; }
  .bar { display:flex; align-items:center; gap:8px; margin-bottom:20px;
         background:#151d30; border:1px solid #1e293b; border-radius:10px;
         padding:12px 16px; }
  .dot { width:13px; height:13px; border-radius:50%%; }
  .r { background:#f87171; } .y { background:#fbbf24; } .g { background:#34d399; }
  .title { color:#94a3b8; font-size:15px; letter-spacing:.5px; margin-left:8px; }
  .badge { margin-left:auto; background:#0f172a; border:1px solid #334155;
           color:#34d399; font-size:14px; padding:4px 10px; border-radius:20px; }
  .banner { color:#38bdf8; font-weight:700; margin-bottom:6px; }
  .ip { color:#64748b; }
  .c4 { color:#34d399; } .c5 { color:#fbbf24; } .c6 { color:#f87171; }
  .k { color:#7dd3fc; } .c { color:#fbbf24; } .n { color:#a5b4d0; }
  .t { color:#34d399; } .f { color:#f472b6; }
  pre { margin:0; }
  .cmd { color:#94a3b8; }
  .sse { color:#7dd3fc; }
  .done { color:#34d399; font-weight:700; }
  .wrap { white-space:pre-wrap; overflow-wrap:anywhere; }
</style></head>
<body>
  <div class="bar">
    <span class="dot r"></span><span class="dot y"></span><span class="dot g"></span>
    <span class="title">%(title)s</span>
    <span class="badge">%(badge)s</span>
  </div>
  %(body)s
</body></html>""" % {"title": title, "badge": badge, "body": body, "fs": font_size}


def esc(s):
    import html as _h
    return _h.escape(s)


# ─── shot 1: console ──────────────────────────────────────────────────────

def capture_console(chrome, port, lines, out, height_estimate):
    """Screenshot the console of the ALREADY-booted server (lines from boot_server).

    Fires a few real requests so the request log fills with authentic lines:
    GET / (health payload), GET /v1/models, and two fast-validation 400s that
    never touch upstream Gemini.
    """
    log("capturing console ...")
    key = api_key()
    time.sleep(1.0)
    hits = [
        ("/", "GET", None, None),
        ("/v1/models", "GET", None, key),
        ("/v1beta/models/gemini-3.6-flash:generateContent", "POST",
         {"contents": []}, key),
        ("/v1/chat/completions", "POST",
         {"model": "gemini-3.6-flash", "messages": []}, key),
    ]
    for path, method, body, k in hits:
        http(port, path, method, body, key=k)
        time.sleep(0.5)
    time.sleep(0.8)

    rows = []
    for line in lines:
        e = esc(line.replace(f":{port}", f":{DISPLAY_PORT}"))
        if e.startswith("gemini-web2api v"):
            rows.append(f'<div class="banner">{e}</div>')
        elif e.strip().startswith(("127.0.0.1", "::1", "- ")):
            ip, _, rest = e.partition(" ")
            code = rest.rsplit(" ", 1)[-1]
            rest_e = esc(rest[:rest.rfind(code)])
            cls = "c4" if code.startswith(("2", "3")) else "c5" if code.startswith("4") else "c6"
            rows.append(f'<div><span class="ip">{esc(ip)}</span> {rest_e}'
                        f'<span class="{cls}">{esc(code)}</span></div>')
        else:
            rows.append(f"<div>{e}</div>")
    html = page_template("server console · gemini_web2api.py",
                         "● live session · python", "\n".join(rows))
    screenshot(chrome, html, out, height=max(400, height_estimate))
    log(f"  -> {os.path.basename(out)} ({len(lines)} lines)")


# ─── shot 2: health ───────────────────────────────────────────────────────

def capture_health(chrome, port, out, height_estimate):
    log("capturing health ...")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
        payload = json.loads(r.read().decode("utf-8", errors="replace"))
    pretty = json.dumps(payload, indent=4, ensure_ascii=False)
    rows = []
    for line in esc(pretty).split("\n"):
        if line.strip().startswith('"') and ":" in line:
            k, _, rest = line.partition(":")
            rows.append(f'<span class="k">{esc(k)}</span><span class="c">:</span>{rest}')
        else:
            rows.append(line)
    html = page_template(f"GET http://127.0.0.1:{DISPLAY_PORT}/ · health check",
                         "● live · HTTP 200", "<pre>" + "\n".join(rows) + "</pre>")
    screenshot(chrome, html, out, height=max(400, height_estimate))
    log(f"  -> {os.path.basename(out)}")


# ─── shot 3: stream ───────────────────────────────────────────────────────

def capture_stream(chrome, port, out, height_estimate):
    log("capturing stream (real curl -N, one tiny Gemini call) ...")
    key = api_key()
    body = json.dumps({
        "model": "gemini-3.6-flash",
        "messages": [{"role": "user", "content": "Say hello in three words."}],
        "stream": True,
    })
    curl = shutil.which("curl") or "curl"
    cmd = [curl, "-sN", f"http://127.0.0.1:{port}/v1/chat/completions",
           "-H", "Content-Type: application/json",
           "-H", f"Authorization: Bearer {key}",
           "-d", body]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        log("  !! curl timed out - keeping existing PNG")
        return False
    raw = r.stdout
    # Match the JSON error KEY, not the bare word: a legit first chunk whose
    # model text says "error" would otherwise false-positive and skip the shot.
    if r.returncode != 0 or '"error"' in raw[:400]:
        log("  !! stream failed upstream - keeping existing PNG")
        log("  " + raw[:300].replace("\n", " "))
        return False

    cmd_line = (f"$ curl -N http://localhost:{DISPLAY_PORT}/v1/chat/completions "
                f"-H \"Authorization: Bearer {key}\" "
                f"-d '{{\"model\":\"gemini-3.6-flash\",\"stream\":true,"
                f"\"messages\":[{{\"role\":\"user\",\"content\":\"Say hello in three words.\"}}]}}'")
    rows = [f'<div class="cmd wrap">{esc(cmd_line)}</div>']
    n = 0
    for ln in raw.splitlines():
        if not ln.strip():
            continue
        if ln.strip() == "data: [DONE]":
            rows.append('<div class="done">data: [DONE]</div>')
            break
        if ln.startswith("data: "):
            n += 1
            try:
                chunk = json.loads(ln[6:])
                delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if delta:
                    rows.append(f'<div class="wrap"><span class="sse">data: </span>{esc(delta)}</div>')
            except (ValueError, IndexError, AttributeError):
                rows.append(f'<div class="wrap"><span class="sse">data: </span>{esc(ln[6:])}</div>')
    if n == 0:
        log("  !! no stream tokens - keeping existing PNG")
        return False
    html = page_template("real SSE streaming · curl -N",
                         f"● {n} tokens · SSE", "\n".join(rows))
    screenshot(chrome, html, out, height=max(300, height_estimate))
    log(f"  -> {os.path.basename(out)} ({n} tokens)")
    return True


# ─── shot 4: popup ────────────────────────────────────────────────────────

POPUP_STUB = """
// chrome.* stub - popup.js runs unchanged, with canned-but-realistic data.
const __storage = {
  serverBase: "http://127.0.0.1:%s",
  apiKey: "sk-gemini",
  lastSuccess: null,
  lastFailure: null,
  refresh: null,          // no refresh in flight -> Activity shows "idle"
  serverReachable: true,
};
const chrome = {
  __onChanged: null,
  storage: {
    local: {
      get: (defaults) => {
        const out = {};
        const keys = Array.isArray(defaults) ? defaults : Object.keys(defaults);
        for (const k of keys) out[k] = (k in __storage) ? __storage[k] : (defaults[k] ?? null);
        return Promise.resolve(out);
      },
      set: (obj) => { Object.assign(__storage, obj); return Promise.resolve(); },
    },
    onChanged: { addListener: (fn) => { chrome.__onChanged = fn; } },
  },
  runtime: { sendMessage: () => Promise.resolve({ ok: true }) },
  cookies: { getAll: () => Promise.resolve([]), getAllCookieStores: () => Promise.resolve([]) },
  tabs: { query: () => Promise.resolve([]), create: () => Promise.resolve({}) },
  windows: { create: () => Promise.resolve({ id: 1 }), remove: () => Promise.resolve() },
  scripting: { executeScript: () => Promise.resolve() },
  downloads: { download: () => Promise.resolve() },
};
window.chrome = chrome;
window.__HEALTH__ = %s;
const __realFetch = window.fetch.bind(window);
window.fetch = async (url, opts) => {
  if (String(url).endsWith("/")) {
    return { ok: true, status: 200, json: async () => window.__HEALTH__ };
  }
  return __realFetch(url, opts);
};
"""


def capture_popup(chrome, out, live_health=None):
    """Render the real popup.html with a chrome.* stub + health payload.

    Uses the LIVE GET / payload from the booted server when available (so the
    shot never drifts from server.py's _health_payload shape); falls back to a
    canned-but-realistic payload when only `popup` is requested. NOTE: if
    popup.js ever adds a chrome.* call at load time, extend POPUP_STUB.
    """
    log("capturing popup ...")
    with open(POPUP_HTML, encoding="utf-8") as f:
        html_src = f.read()
    with open(POPUP_JS, encoding="utf-8") as f:
        js_src = f.read()
    # extract the <style> block from the real popup so the shot is pixel-true
    style = html_src.split("<style>", 1)[1].split("</style>", 1)[0]
    body = html_src.split("<body>", 1)[1].split("</body>", 1)[0]
    body = body.replace('<script src="popup.js"></script>', "")
    if live_health is None:
        live_health = {
            "status": "ok",
            "version": "1.1.0",
            "models": ["gemini-3.6-flash"],
            "gemini_bl": "boq_assistant-bard-web-server_20260803.06_p0",
            "auto_update_bl": True,
            "bl_405_count": 0,
            "bl_405_last_ts": None,
            "cookie": {"file": "cookie.txt", "exists": True, "age_sec": 30600,
                       "updated_at": time.time() - 30600,
                       "refresh_requested": False},
            "proxy": {"configured": None,
                      "fallbacks": ["http://127.0.0.1:7890"],
                      "plan": [None, "http://127.0.0.1:7890"],
                      "working": None},
        }
    stub = POPUP_STUB % (DISPLAY_PORT, json.dumps(live_health))
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>{style}</style></head>
<body>{body}
<script>{stub}</script>
<script>{js_src}</script>
</body></html>"""
    fd, path = tempfile.mkstemp(suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(html)
    # 480x1200 leaves headroom below the content so the footer note never
    # crops if popup.html grows (it already grew once, with the health panel).
    cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--force-device-scale-factor=2", "--window-size=480,1200",
           "--screenshot=%s" % out, "file:///%s" % path.replace(os.sep, "/")]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    os.unlink(path)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError("popup capture failed: " + r.stderr[-1500:])
    log(f"  -> {os.path.basename(out)}")


# ─── shot 5: extension icon on a browser-toolbar mockup ──────────────────

def capture_icon(chrome, out):
    """Composite the REAL 128px extension icon onto a browser-toolbar mockup
    (traffic lights + URL pill + pinned slots), screenshotted with the same
    retina-2x headless-Chrome pipeline as the other shots.
    """
    log("capturing extension icon ...")
    with open(ICON128, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    html = """<!doctype html>
<html><head><meta charset="utf-8">
<style>
  html, body { margin:0; padding:0; background:#0b1220; }
  body { padding:36px 44px 40px; }
  .chrome {
    background:#151d30; border:1px solid #1e293b; border-radius:12px;
    padding:10px 14px; display:flex; align-items:center; gap:12px;
  }
  .dots { display:flex; gap:7px; }
  .dot { width:12px; height:12px; border-radius:50%; }
  .r { background:#f87171; } .y { background:#fbbf24; } .g { background:#34d399; }
  .url {
    flex:1; background:#0f172a; border:1px solid #1e293b; border-radius:8px;
    color:#94a3b8; font-family:'Cascadia Code','Consolas',monospace; font-size:14px;
    padding:7px 14px; text-align:center; white-space:nowrap; overflow:hidden;
  }
  .url b { color:#7dd3fc; font-weight:600; }
  .pin { width:28px; height:28px; border-radius:7px; }
  .pin img { width:28px; height:28px; border-radius:7px; display:block; }
  .pin.on { background:#2e1065; box-shadow:0 0 0 1px #a78bfa66; }
  .stage {
    margin-top:26px; background:linear-gradient(180deg,#121a3c 0%,#0b1220 100%);
    border:1px solid #1e293b; border-radius:16px; padding:36px 30px 32px;
    display:flex; flex-direction:column; align-items:center; gap:6px;
  }
  .icon {
    width:128px; height:128px; border-radius:26px;
    box-shadow:0 18px 50px rgba(0,0,0,.55), 0 0 0 1px rgba(167,139,250,.35);
    margin-bottom:14px;
  }
  .name { color:#e2e8f0; font-family:system-ui,sans-serif; font-weight:700; font-size:19px; }
  .sub { color:#64748b; font-family:system-ui,sans-serif; font-size:13px; }
</style></head>
<body>
  <div class="chrome">
    <div class="dots"><span class="dot r"></span><span class="dot y"></span><span class="dot g"></span></div>
    <div class="url"><b>gemini.google.com</b></div>
    <div class="pin on"><img src="data:image/png;base64,__ICON__" alt=""></div>
    <div class="pin"><img src="data:image/png;base64,__ICON__" alt="" style="opacity:.55"></div>
    <div class="pin"><img src="data:image/png;base64,__ICON__" alt="" style="opacity:.3"></div>
  </div>
  <div class="stage">
    <img class="icon" src="data:image/png;base64,__ICON__" alt="Gemini Cookie Sync icon">
    <div class="name">Gemini Cookie Sync</div>
    <div class="sub">extension toolbar icon &middot; keeps your session fresh</div>
  </div>
</body></html>""".replace("__ICON__", b64)
    screenshot(chrome, html, out, width=820, height=560)
    log(f"  -> {os.path.basename(out)}")


# ─── CLI ──────────────────────────────────────────────────────────────────

def estimate_height(n_items, per_item=34, base=140):
    return base + n_items * per_item


def main():
    ap = argparse.ArgumentParser(description="Regenerate README gallery screenshots")
    ap.add_argument("shots", nargs="*",
                    help="which shots to regenerate (default: all of "
                         "console health stream popup icon)")
    ap.add_argument("--port", type=int, default=None,
                    help="boot the capture server on this port (default: ephemeral)")
    ap.add_argument("--skip", action="append", default=[],
                    help="skip a shot even if requested (e.g. --skip stream)")
    args = ap.parse_args()

    chrome = find_chrome()
    if not chrome:
        log("ERROR: Chrome not found")
        sys.exit(1)

    KNOWN = {"console", "health", "stream", "popup", "icon"}
    bad = set(args.shots) - KNOWN
    if bad:
        ap.error("unknown shot(s): %s (known: %s)" % (", ".join(sorted(bad)),
                                                       ", ".join(sorted(KNOWN))))
    requested = args.shots or sorted(KNOWN)
    requested = [s for s in requested if s not in args.skip]

    port = args.port or free_port()
    cfg = None  # only the server-backed shots need config.json
    server = None
    try:
        if any(s in requested for s in ("console", "health", "stream")):
            cfg = capture_config()
            server, lines, done = boot_server(port, cfg)
            log(f"booted capture server on port {port}")

        if "console" in requested:
            capture_console(chrome, port, lines,
                            os.path.join(DOCS, "shot-console.png"),
                            estimate_height(16))
        if "health" in requested:
            capture_health(chrome, port, os.path.join(DOCS, "shot-health.png"),
                           estimate_height(34))
        if "stream" in requested:
            capture_stream(chrome, port, os.path.join(DOCS, "shot-stream.png"),
                           estimate_height(12))
        if "popup" in requested:
            live = None
            if server is not None:  # reuse the booted server's live payload
                try:
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/", timeout=5) as r:
                        live = json.loads(r.read().decode("utf-8", errors="replace"))
                except Exception:
                    live = None
            capture_popup(chrome, os.path.join(DOCS, "shot-popup.png"), live)
        if "icon" in requested:  # no server needed - pure composition
            capture_icon(chrome, os.path.join(DOCS, "shot-icon.png"))
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
            done.wait(timeout=3)
        if cfg is not None:
            try:
                os.unlink(cfg)
            except OSError:
                pass
    log("done.")


if __name__ == "__main__":
    main()
