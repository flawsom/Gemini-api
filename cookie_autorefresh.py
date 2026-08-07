#!/usr/bin/env python3
"""cookie_autorefresh.py - autonomously refresh the Gemini cookies in cookie.txt.

Opens the user's DEFAULT browser, lets Gemini set fresh session cookies, saves
them to cookie.txt, and closes ONLY the window it opened - never the windows
the user is using.

Two paths, chosen automatically:

1. Browser NOT running  -> CDP (Chrome DevTools Protocol). Launches the browser
   with the user's real profile + a debug port, opens gemini.google.com in a
   new window, extracts the Google cookies over CDP, writes cookie.txt, then
   closes the whole instance it started (nothing else is running anyway).

2. Browser IS running   -> extension path. Tells the local server to set a
   "cookie refresh requested" flag. The Gemini Cookie Sync extension (v1.1+)
   polls the server, opens a NEW WINDOW of its own, reads the cookies, uploads
   them to the server, and closes ONLY that window. Your open tabs are never
   touched.

Usage:
    python cookie_autorefresh.py            # full refresh via default browser
    python cookie_autorefresh.py --browser  # print the detected browser only
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(HERE, "cookie.txt")
# Defaults; overridden at startup from config.json (port + cookie_refresh_key).
SERVER_BASE = "http://127.0.0.1:8081"
API_KEY = "sk-gemini"
PORT_TRIES = range(9333, 9341)

# Cookies we export, in this priority order (mirrors the extension).
EXPORT_ORDER = [
    "SID", "HSID", "SSID", "APISID", "SAPISID", "LSID", "OSID", "SIDCC",
    "AEC", "NID", "COMPASS",
    "__Secure-1PAPISID", "__Secure-1PSID", "__Secure-1PSIDTS",
    "__Secure-1PSIDCC", "__Secure-1PSIDRTS",
    "__Secure-3PAPISID", "__Secure-3PSID", "__Secure-3PSIDTS",
    "__Secure-3PSIDCC", "__Secure-3PSIDRTS",
    "__Secure-OSID", "__Host-1PLSID", "__Host-3PLSID",
]

# ProgId (registry) -> (browser name, is_chromium)
PROGID_MAP = {
    "BraveHTML": ("brave", True),
    "ChromeHTML": ("chrome", True),
    "MSEdgeHTM": ("msedge", True),
    "OperaStable": ("opera", True),
    "VivaldiHTM": ("vivaldi", True),
}

EXE_CANDIDATES = {
    "brave": [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    ],
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "msedge": [
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ],
    "opera": [r"C:\Program Files\Opera\launcher.exe"],
    "vivaldi": [r"C:\Program Files\Vivaldi\Application\vivaldi.exe"],
}

PROFILE_DIRS = {
    "brave": os.path.join(os.environ.get("LOCALAPPDATA", ""),
                          "BraveSoftware", "Brave-Browser", "User Data"),
    "chrome": os.path.join(os.environ.get("LOCALAPPDATA", ""),
                           "Google", "Chrome", "User Data"),
    "msedge": os.path.join(os.environ.get("LOCALAPPDATA", ""),
                           "Microsoft", "Edge", "User Data"),
    "opera": os.path.join(os.environ.get("LOCALAPPDATA", ""),
                          "Opera Software", "Opera Stable"),
    "vivaldi": os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "Vivaldi", "User Data"),
}


def log(msg: str):
    print(msg, flush=True)


# ─── default browser detection ───────────────────────────────────────────────

def _reg_default_progid() -> str | None:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-ItemProperty "
             "'HKCU:\\Software\\Microsoft\\Windows\\Shell\\Associations\\"
             "UrlAssociations\\http\\UserChoice' -ErrorAction Stop).ProgId"],
            capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.strip():
                    return line.strip()
    except Exception:
        pass
    return None


def _find_exe(candidates: list) -> str | None:
    for p in candidates:
        if os.path.exists(p):
            return p
    # per-user install fallback (LOCALAPPDATA mirror of the Program Files paths)
    la = os.environ.get("LOCALAPPDATA", "")
    for p in candidates:
        rel = p.split("Program Files")[-1].lstrip("\\ (x86)")
        alt = os.path.join(la, rel)
        if os.path.exists(alt):
            return alt
    return None


def detect_default_browser() -> dict:
    """Return {name, exe, chromium, progid} for the default browser."""
    progid = _reg_default_progid() or ""
    base = progid.split(".")[0] if progid else ""
    name, chromium = PROGID_MAP.get(base, ("", False))
    exe = _find_exe(EXE_CANDIDATES.get(name, [])) if name else None
    if not exe and progid:
        # last resort: parse the ProgId's open command for the exe path
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-ItemProperty "
                 f"'Registry::HKEY_CLASSES_ROOT\\{progid}\\shell\\open\\command' "
                 "-ErrorAction Stop).'(default)'"],
                capture_output=True, text=True, timeout=20)
            cmdline = (r.stdout or "").strip()
            m = re.search(r'"([^"]+\.exe)"', cmdline)
            if m:
                exe = m.group(1)
        except Exception:
            pass
    return {"name": name, "exe": exe, "chromium": chromium, "progid": progid}


def is_browser_running(exe: str) -> bool:
    stem = os.path.splitext(os.path.basename(exe))[0].lower()
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"(Get-Process {stem} -ErrorAction SilentlyContinue).Id"],
                       capture_output=True, text=True, timeout=20)
    return bool((r.stdout or "").strip())


# ─── CDP path (browser not running) ─────────────────────────────────────────

def _http_json(url: str, method: str = "GET", data=None) -> dict:
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(data).encode() if data else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _find_free_port() -> int:
    import socket
    for port in PORT_TRIES:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return PORT_TRIES[0]


def _cdp_command(ws, msg_id, method, params=None) -> dict:
    ws.send(json.dumps({"id": msg_id, "method": method,
                        "params": params or {}}))
    while True:
        raw = ws.recv()
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        if msg.get("id") == msg_id:
            return msg


def _score_cookie(c) -> int:
    domain = (c.get("domain") or "").lstrip(".").lower()
    score = 0
    if domain == "google.com": score += 110
    elif domain == ".google.com": score += 120
    elif domain == "gemini.google.com": score += 95
    elif domain == ".gemini.google.com": score += 100
    elif domain == "accounts.google.com": score += 75
    elif domain == ".accounts.google.com": score += 80
    elif domain.endswith(".google.com"): score += 40
    if c.get("path") == "/": score += 10
    if c.get("secure"): score += 3
    if c.get("httpOnly"): score += 2
    return score


def _pick_google_cookies(all_cookies) -> dict:
    """Return {name: value} choosing the best-scoring cookie per name."""
    best = {}
    for c in all_cookies:
        domain = (c.get("domain") or "").lstrip(".").lower()
        name = c.get("name")
        if not name or not c.get("value"):
            continue
        if domain != "google.com" and not domain.endswith(".google.com"):
            continue
        key = name
        cur = best.get(key)
        if cur is None or _score_cookie(c) > _score_cookie(cur):
            best[key] = c
    return {n: c["value"] for n, c in best.items()}


def _build_cookie_payload(cookies_by_name: dict) -> tuple:
    ordered = [f"{n}={cookies_by_name[n]}" for n in EXPORT_ORDER
               if n in cookies_by_name]
    cookie_str = "; ".join(ordered)
    sapisid = cookies_by_name.get("SAPISID", "")
    return cookie_str, sapisid


def refresh_via_cdp(exe: str, profile: str) -> bool:
    """Launch the browser (real profile) with CDP, extract cookies, close."""
    port = _find_free_port()
    url = "https://gemini.google.com/app"
    cmd = [exe, f"--user-data-dir={profile}",
           f"--remote-debugging-port={port}",
           "--remote-allow-origins=*",  # modern Chromium rejects CDP WS otherwise
           "--no-first-run", "--no-default-browser-check",
           "--start-minimized", url]
    log(f"[1/5] Launching {os.path.basename(exe)} with a fresh window...")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError as e:
        log(f"      Could not launch the browser: {e}")
        return False

    # wait for the debug endpoint
    version_url = f"http://127.0.0.1:{port}/json/version"
    ok = False
    for _ in range(40):
        if proc.poll() is not None:
            log("      The browser is already running - new window handed off.")
            log("      (Close it or use the extension path.)")
            return False
        try:
            _http_json(version_url)
            ok = True
            break
        except Exception:
            time.sleep(0.5)
    if not ok:
        log("      Browser never opened its debug port. Aborting.")
        return False

    try:
        # create/open the gemini tab
        log("[2/5] Opening https://gemini.google.com/app ...")
        target = _http_json(
            f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe='')}",
            method="PUT")
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("no webSocketDebuggerUrl")

        from websocket import create_connection
        ws = create_connection(ws_url, timeout=90, enable_multithread=False,
                               suppress_origin=True)

        _cdp_command(ws, 1, "Network.enable")
        _cdp_command(ws, 2, "Page.enable")
        _cdp_command(ws, 3, "Page.navigate", {"url": url})

        # wait for the page to finish loading
        log("[3/5] Waiting for the Gemini page to load...")
        for _ in range(60):
            try:
                r = _cdp_command(ws, 4, "Runtime.evaluate",
                                 {"expression": "document.readyState",
                                  "returnByValue": True})
                state = (r.get("result", {}).get("result", {})
                         .get("value", ""))
                if state == "complete":
                    break
            except Exception:
                pass
            time.sleep(1)
        time.sleep(3)  # let the page JS settle

        # grab cookies + account
        log("[4/5] Reading session cookies...")
        r = _cdp_command(ws, 5, "Network.getAllCookies")
        all_cookies = (r.get("result", {}) or {}).get("cookies", [])
        picked = _pick_google_cookies(all_cookies)
        cookie_str, sapisid = _build_cookie_payload(picked)

        auth_user = None
        try:
            r = _cdp_command(ws, 6, "Runtime.evaluate",
                             {"expression": "location.href",
                              "returnByValue": True})
            href = (r.get("result", {}).get("result", {}).get("value", ""))
            m = re.search(r"/u/(\d+)", href or "")
            auth_user = m.group(1) if m else None
        except Exception:
            pass

        ws.close()

        # Never clobber a working cookie.txt with a partial session: require
        # SAPISID plus at least one real session cookie, and back up the old
        # file before overwriting.
        session_names = {"SID", "__Secure-1PSID", "__Secure-3PSID"}
        if (not cookie_str or not sapisid
                or not (set(picked) & session_names)):
            log("      No complete Google session found (need SAPISID + "
                "SID/__Secure-1PSID) - cookie.txt left untouched. "
                "Are you signed in to Gemini in this browser?")
            return False

        if os.path.exists(COOKIE_FILE):
            try:
                os.replace(COOKIE_FILE, COOKIE_FILE + ".bak")
                log(f"      Backed up the previous cookies to cookie.txt.bak")
            except OSError:
                pass
        payload = {"cookie": cookie_str, "sapisid": sapisid}
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        log(f"      Wrote {len(picked)} cookies to cookie.txt"
            + (f" (account /u/{auth_user})" if auth_user else ""))
        return True
    except Exception as e:
        log(f"      CDP error: {e}")
        return False
    finally:
        log("[5/5] Closing the browser window...")
        # Browser.close over CDP usually handled the exit; this is the backstop
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass


# ─── extension path (browser running) ────────────────────────────────────────

def _post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "X-API-Key": API_KEY},
        method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def refresh_via_extension(timeout: int = 150) -> bool:
    """Ask the server to flag a refresh; the extension does the browser work."""
    log("[1/4] Requesting cookie refresh from the local server...")
    try:
        _post(f"{SERVER_BASE}/internal/cookie-refresh/request",
              {"reason": "manual"})
    except Exception as e:
        log(f"      Could not reach the server at {SERVER_BASE}: {e}")
        log("      Is the server running? Start it with start_server.bat")
        return False

    log("[2/4] Waiting for the Gemini Cookie Sync extension "
        "(it opens a new window, exports, and closes only that window)...")
    mtime0 = os.path.getmtime(COOKIE_FILE) if os.path.exists(COOKIE_FILE) else 0
    waited = 0
    while waited < timeout:
        time.sleep(5)
        waited += 5
        try:
            r = _http_json(f"{SERVER_BASE}/internal/cookie-refresh/request")
            if not r.get("requested"):
                log(f"[3/4] Refresh finished after ~{waited}s.")
                return True
        except Exception:
            pass
        if os.path.exists(COOKIE_FILE) and os.path.getmtime(COOKIE_FILE) > mtime0:
            log(f"[3/4] cookie.txt updated after ~{waited}s.")
            return True
    log(f"      Timed out after {timeout}s - is the extension installed "
        "and reloaded?")
    log("      chrome://extensions -> Gemini Cookie Sync -> reload")
    return False


def _load_config() -> dict:
    """Read config.json next to this script (best effort)."""
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _resolve_refresh_endpoint(cfg: dict, base_url: str = None, key: str = None) -> tuple:
    """(base_url, api_key) from config.json; explicit CLI args override.

    base_url defaults to http://127.0.0.1:{port}; the key resolves
    cookie_refresh_key -> api_keys[0] -> "sk-gemini" (matches the server's
    own _refresh_key resolution, so a non-default port or custom key works
    without code changes).
    """
    if not base_url:
        base_url = f"http://127.0.0.1:{cfg.get('port', 8081)}"
    if not key:
        keys = cfg.get("api_keys") or []
        key = cfg.get("cookie_refresh_key") or (keys[0] if keys else "sk-gemini")
    return base_url, key


def main():
    global SERVER_BASE, API_KEY
    ap = argparse.ArgumentParser(description="Refresh Gemini cookies automatically")
    ap.add_argument("--browser", action="store_true",
                    help="only print the detected default browser")
    ap.add_argument("--base-url", default=None,
                    help="server base URL (default: from config.json port)")
    ap.add_argument("--key", default=None,
                    help="API key for the refresh endpoints (default: config.json)")
    args = ap.parse_args()

    SERVER_BASE, API_KEY = _resolve_refresh_endpoint(_load_config(),
                                                     args.base_url, args.key)
    log(f"Cookie refresh endpoint: {SERVER_BASE}")

    info = detect_default_browser()
    if args.browser:
        print(json.dumps(info, indent=2))
        return
    if not info.get("exe"):
        log("Could not detect your default browser.")
        log("Using the extension path instead (works with any browser).")
        refresh_via_extension()
        return

    log(f"Default browser: {info['name']} ({info['progid']})")
    if not info.get("chromium"):
        log("Non-Chromium browser - using the extension path.")
        refresh_via_extension()
        return

    if is_browser_running(info["exe"]):
        log(f"{info['name']} is currently open - using the extension path "
            "so your windows stay untouched.")
        refresh_via_extension()
    else:
        profile = PROFILE_DIRS.get(info["name"], "")
        if profile and os.path.isdir(profile):
            refresh_via_cdp(info["exe"], profile)
        else:
            log("No browser profile found - using the extension path.")
            refresh_via_extension()


if __name__ == "__main__":
    main()
