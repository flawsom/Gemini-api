#!/usr/bin/env python3
"""image_bridge_cdp.py - process an image request in the user's REAL browser
session via Chrome DevTools Protocol - no extension required.

Google rejects uploaded-image requests that come from exported cookies
(BardErrorInfo 1100): only a fully-authenticated browser profile session can
process images. This script drives that session directly:

  - browser already running WITH a debug port  -> attach, open a new window,
    process, close ONLY that window
  - browser NOT running                        -> launch the real profile with a
    debug port, process in a new window, then close the whole instance it
    started (nothing else was running)
  - browser running WITHOUT a debug port       -> exit code 3 (the caller
    should use the extension path instead)

The image is attached with DOM.setFileInputFiles (the same mechanism the web
UI's own file picker uses), the prompt is typed with Input.insertText, the
send button is clicked, and the newest model message is polled until its text
stabilizes - then the answer is written to --out as JSON.

Usage:
    python image_bridge_cdp.py --prompt "what is this?" --images a.png,b.png \
        --out result.json [--debug-port 9333] [--timeout 120]

Exit codes:
    0  success (result.json = {"ok": true, "text": "..."})
    2  error       (result.json = {"ok": false, "error": "..."})
    3  browser is running without a debug port - use the extension path
    4  another bridge process is already running (lock file)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Reuse the browser detection + CDP plumbing from the cookie refresher so the
# two tools agree on which browser/profile is the user's default.
import cookie_autorefresh as ca

LOCK_FILE = os.path.join(tempfile.gettempdir(), "gemini-image-bridge-cdp.lock")
DEFAULT_PORTS = list(range(9401, 9411))  # probing range for an existing bridge


# ─── response extraction (mirrors the extension content script) ─────────────
READ_MODEL_JS = r"""
(() => {
  const nodes = document.querySelectorAll('[data-message-author-role="model"]');
  if (!nodes.length) return "";
  const last = nodes[nodes.length - 1];
  const inner = last.querySelector(
    '.markdown, .rich-text, .model-response-text, [data-test-id*="response-text"]');
  const source = inner || last;
  return (source.innerText || "")
    .replace(/^Thinking…\s*\n?/, "")
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l && !(l.length <= 12
      && /^(copy|edit|regenerate|delete|show more|show less|dismiss)\b/i.test(l)))
    .join("\n")
    .trim();
})()
"""


def log(msg: str):
    print(msg, flush=True)


# ─── CDP plumbing (mirrors cookie_autorefresh) ──────────────────────────────

def cdp(ws, mid, method, params=None):
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        raw = ws.recv()
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        if msg.get("id") == mid:
            return msg


def eval_js(ws, mid, expr):
    r = cdp(ws, mid, "Runtime.evaluate",
            {"expression": expr, "returnByValue": True})
    return r.get("result", {}).get("result", {}).get("value")


def probe_port(port: int) -> bool:
    try:
        ca._http_json(f"http://127.0.0.1:{port}/json/version")
        return True
    except Exception:
        return False


def find_existing_debug_port() -> int | None:
    """A port that already answers CDP (user started the browser with one)."""
    for p in list(DEFAULT_PORTS) + [9222, 9223, 9333]:
        if probe_port(p):
            return p
    return None


def wait_for_ws(port: int, url: str, proc=None, max_wait: float = 60) -> str | None:
    """Wait for the debug endpoint, then create/open the target tab."""
    version_url = f"http://127.0.0.1:{port}/json/version"
    for _ in range(int(max_wait / 0.5)):
        if proc is not None and proc.poll() is not None:
            return None  # the instance we launched handed off / died
        try:
            ca._http_json(version_url)
            break
        except Exception:
            time.sleep(0.5)
    else:
        return None
    try:
        import urllib.parse
        target = ca._http_json(
            f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe='')}",
            method="PUT")
        return target.get("webSocketDebuggerUrl")
    except Exception:
        return None


def attach_tab(ws_url: str):
    from websocket import create_connection
    return create_connection(ws_url, timeout=120, enable_multithread=False,
                             suppress_origin=True)


# ─── the actual image processing (CDP against the REAL profile) ─────────────

def process_in_tab(ws, images: list, prompt: str, timeout: int) -> str:
    """Attach images, type prompt, send, and return the model's answer."""
    deadline = time.time() + timeout
    mid = [100]

    def js(expr):
        mid[0] += 1
        return eval_js(ws, mid[0], expr)

    # 1. wait for the composer
    while time.time() < deadline:
        if js("!!(document.querySelector('div[contenteditable=\"true\"], textarea'))"):
            break
        time.sleep(1)
    else:
        raise RuntimeError("Gemini composer never appeared")
    time.sleep(2)

    # 2. click the attach button so the UI materializes its file input
    js(r"""
(() => {
  const b = Array.from(document.querySelectorAll('button')).find(b => {
    const label = (b.getAttribute('aria-label')||'') + ' ' + (b.textContent||'');
    return /upload|attach|add photo/i.test(label) && label.length < 60;
  });
  if (b) { b.click(); return true; }
  return false;
})()
""")
    time.sleep(3)

    # 3. set the files on the input (the web UI's own picker path)
    r = cdp(ws, 200, "DOM.getDocument", {"depth": -1})
    root = r.get("result", {}).get("root", {}).get("nodeId")
    r = cdp(ws, 201, "DOM.querySelectorAll",
            {"nodeId": root, "selector": "input[type=file]"})
    nids = (r.get("result", {}) or {}).get("nodeIds", [])
    if not nids:
        raise RuntimeError("no file input found after clicking attach")
    cdp(ws, 202, "DOM.setFileInputFiles", {"nodeId": nids[0], "files": images})
    time.sleep(4)  # let the upload complete

    # 4. type the prompt (image-only requests skip this)
    if prompt.strip():
        js("document.querySelector('div[contenteditable=\"true\"], textarea').focus()")
        cdp(ws, 203, "Input.insertText", {"text": prompt})
        time.sleep(1)

    # 5. click send
    clicked = js(r"""
(() => {
  const b = Array.from(document.querySelectorAll('button')).find(b =>
    /send/i.test(b.getAttribute('aria-label')||''));
  if (b) { b.click(); return true; }
  return false;
})()
""")
    if not clicked:
        raise RuntimeError("send button not found")

    # 6. poll the newest model message until its text stabilizes
    before = js(READ_MODEL_JS) or ""
    last_text, stable = "", 0
    while time.time() < deadline:
        time.sleep(1.5)
        text = js(READ_MODEL_JS) or ""
        if text and text != before:
            if text == last_text:
                stable += 1
                if stable >= 2:
                    return text
            else:
                stable = 0
                last_text = text
    if last_text:
        return last_text  # timed out mid-stream - return what we have
    raise RuntimeError("no model answer appeared before the timeout")


# ─── orchestration ──────────────────────────────────────────────────────────

def write_result(path: str, ok: bool, text: str = "", error: str = "") -> int:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ok": ok, "text": text, "error": error}, f)
    return 0 if ok else 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Process an image in the real browser session")
    ap.add_argument("--prompt", default="", help="text prompt alongside the image(s)")
    ap.add_argument("--images", required=True, help="comma-separated image file paths")
    ap.add_argument("--out", required=True, help="result JSON path")
    ap.add_argument("--debug-port", type=int, default=None,
                    help="existing CDP port (default: auto-detect, else launch)")
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args(argv)

    images = [p.strip() for p in args.images.split(",") if p.strip()]
    if not images or not all(os.path.exists(p) for p in images):
        return write_result(args.out, False, error="image file(s) not found")

    # Serialize: only one browser-assisted image at a time. A leftover lock
    # from a crashed run is reclaimed once it is older than the timeout, so a
    # kill mid-processing can never block future requests forever.
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except OSError:
        try:
            if time.time() - os.path.getmtime(LOCK_FILE) > args.timeout + 60:
                os.remove(LOCK_FILE)
            else:
                return write_result(args.out, False,
                                    error="another image bridge is already running")
        except OSError:
            return write_result(args.out, False,
                                error="another image bridge is already running")
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except OSError:
            return write_result(args.out, False,
                                error="another image bridge is already running")
    try:
        return _run(args, images)
    finally:
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass


def _run(args, images: list) -> int:
    info = ca.detect_default_browser()
    if not info.get("exe") or not info.get("chromium"):
        return write_result(args.out, False,
                            error="could not detect a Chromium default browser")
    url = "https://gemini.google.com/app"

    # 1) Attach to an existing debug port if one is reachable.
    port = args.debug_port or find_existing_debug_port()
    launched = None
    if port is None:
        # 2) Browser not running? Launch the REAL profile with a debug port.
        if ca.is_browser_running(info["exe"]):
            log("Browser is open without a debug port - the extension path "
                "is required (or close the browser and retry).")
            return 3
        port = ca._find_free_port()
        log(f"Launching {os.path.basename(info['exe'])} (real profile, "
            f"debug port {port})...")
        launched = subprocess.Popen(
            [info["exe"], f"--user-data-dir={ca.PROFILE_DIRS.get(info['name'], '')}",
             f"--remote-debugging-port={port}", "--remote-allow-origins=*",
             "--no-first-run", "--no-default-browser-check",
             "--start-minimized", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ws_url = wait_for_ws(port, url, proc=launched)
    if not ws_url:
        if launched is not None:
            try:
                launched.terminate()
            except Exception:
                pass
        return write_result(args.out, False,
                            error="could not connect to the browser's debug port")
    ws = attach_tab(ws_url)
    try:
        log("Waiting for the Gemini page to load...")
        deadline = time.time() + 45
        while time.time() < deadline:
            if eval_js(ws, 900, "document.readyState") == "complete":
                break
            time.sleep(1)
        time.sleep(4)
        log("Attaching image(s) and sending...")
        text = process_in_tab(ws, images, args.prompt, args.timeout)
        log("Answer captured.")
        return write_result(args.out, True, text=text)
    except Exception as e:
        log(f"Processing failed: {e}")
        return write_result(args.out, False, error=str(e))
    finally:
        try:
            ws.close()
        except Exception:
            pass
        # Close ONLY what we opened: a launched instance (nothing else was
        # running) or the single new window in an attached browser.
        try:
            if launched is not None:
                launched.terminate()
            else:
                _close_current_window(ws_url)
        except Exception:
            pass


def _close_current_window(ws_url: str):
    """Close the tab/window this script created in an attached browser."""
    try:
        import urllib.parse
        import urllib.request
        port = re.search(r"127\.0\.0\.1:(\d+)", ws_url).group(1)
        # /json/close closes the given target - the one we created.
        target_id = re.search(r"/([a-f0-9]+)$", ws_url)
        if target_id:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/close/{target_id.group(1)}",
                timeout=10).read()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
