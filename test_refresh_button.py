"""Full Refresh-now button lifecycle test against a live ephemeral server.

Regression-guards the exact HTTP flow the popup's "Refresh now" button fires:

  1. POST /internal/cookie-refresh/request   -> the button's POST
  2. the server-side flag file appears
  3. GET / health payload flips refresh_requested to true
  4. GET /internal/cookie-refresh/request    -> the extension's poll sees it
  5. POST /internal/cookie-refresh/upload    -> the extension completing
  6. flag file gone, health flips back, cookie.txt rewritten

Also covers the failure guards that protect the real cookie.txt: a wrong-key
upload is rejected (401), a session-less upload is rejected (400, file
untouched), and a stale flag self-expires instead of triggering windows
forever.

Like test_integration.py this starts the real ThreadedServer on an ephemeral
port (127.0.0.1:0) with a temp cookie file - no external services, no live
cookie.txt, no config.json - so it is CI-safe in offline mode too.

Run:  python test_refresh_button.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from gemini_web2api.config import CONFIG
from gemini_web2api.server import GeminiHandler, ThreadedServer
import gemini_web2api.server as srv
# The flag path is module state in gemini.py (server.py only imports the
# helpers) - read it from the source of truth so the test watches the exact
# file the server writes.
import gemini_web2api.gemini as gem

# Deterministic config: never touch the user's cookie, keys, or config.json.
CONFIG.update({
    "api_keys": ["sk-gemini"],
    "cookie_refresh_key": None,
    "cookie_file": None,
    "xsrf_token": None,
    "gemini_bl": "test-bl",
    "auth_user": None,
    "auto_update_bl": False,
    "proxy": None,
    "proxy_fallbacks": [],
})

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
port = server.server_address[1]
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
BASE = f"http://127.0.0.1:{port}"
print(f"refresh-button: server on {BASE} (ephemeral, no external services)")

KEY = "sk-gemini"
IK = {"X-API-Key": KEY}


def req(method, path, body=None, headers=None, timeout=30):
    hdr = {"Content-Type": "application/json"}
    if headers:
        hdr.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers=hdr, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return 0, f"request error: {e}", {}


# Isolated cookie file - the lifecycle must never touch the real cookie.txt.
tmp_cf = os.path.join(tempfile.gettempdir(), "gwa_refresh_button_cookie.json")
FLAG = gem.REFRESH_FLAG


def flag_exists():
    return os.path.exists(FLAG)


def health_refresh_requested():
    s, b, _ = req("GET", "/")
    if s != 200:
        return None
    return bool((json.loads(b).get("cookie") or {}).get("refresh_requested"))


try:
    # ── clean slate ────────────────────────────────────────────────────────
    gem.clear_cookie_refresh()
    if os.path.exists(tmp_cf):
        os.remove(tmp_cf)
    check("clean slate: no flag file", not flag_exists())
    check("clean slate: health says not requested", health_refresh_requested() is False)

    # Point the server at the isolated cookie file BEFORE any upload lands.
    CONFIG["cookie_file"] = tmp_cf

    # ── 1. the button's POST ───────────────────────────────────────────────
    s, b, _ = req("POST", "/internal/cookie-refresh/request",
                  {"reason": "popup"}, IK)
    check("button POST /request -> 200 requested=true",
          s == 200 and json.loads(b).get("requested") is True, b[:80])

    # ── 2. the flag file appears with the reason ───────────────────────────
    check("flag file created", flag_exists())
    if flag_exists():
        with open(FLAG) as f:
            flag = json.load(f)
        check("flag records reason 'popup'", flag.get("reason") == "popup", str(flag))
        check("flag records requested_at timestamp",
              isinstance(flag.get("requested_at"), (int, float)), str(flag))

    # ── 3. health flips ────────────────────────────────────────────────────
    check("health refresh_requested -> true", health_refresh_requested() is True)

    # ── 4. the extension's poll sees it ────────────────────────────────────
    s, b, _ = req("GET", "/internal/cookie-refresh/request", headers=IK)
    check("extension poll GET /request -> requested=true",
          s == 200 and json.loads(b).get("requested") is True, b[:80])

    # ── 5. wrong-key upload rejected, flag + file untouched ────────────────
    s, b, _ = req("POST", "/internal/cookie-refresh/upload",
                  {"cookie": "SID=bad; SAPISID=bad"}, {"X-API-Key": "WRONG"})
    check("wrong-key upload -> 401", s == 401, f"{s} {b[:60]}")
    check("flag survives rejected auth", flag_exists())
    check("cookie file untouched by wrong key", not os.path.exists(tmp_cf))

    # ── 6. session-less upload rejected, flag + file untouched ─────────────
    s, b, _ = req("POST", "/internal/cookie-refresh/upload",
                  {"cookie": "NID=nid; AEC=aec"}, IK)
    check("session-less upload -> 400", s == 400, f"{s} {b[:80]}")
    check("flag survives rejected upload", flag_exists())
    check("cookie file untouched by session-less upload", not os.path.exists(tmp_cf))

    # ── 7. the extension completes: upload session cookies ─────────────────
    s, b, _ = req("POST", "/internal/cookie-refresh/upload",
                  {"cookie": "SID=abc; HSID=def; SAPISID=sap; NID=nid",
                   "sapisid": "sap", "auth_user": "1",
                   "xsrf_token": "tok-new", "gemini_bl": "bl-new"}, IK)
    check("extension upload -> 200 ok", s == 200 and json.loads(b).get("ok"), b[:80])

    # ── 8. flag cleared ────────────────────────────────────────────────────
    check("flag file removed after upload", not flag_exists())
    s, b, _ = req("GET", "/internal/cookie-refresh/request", headers=IK)
    check("extension poll GET /request -> requested=false",
          s == 200 and json.loads(b).get("requested") is False, b[:80])
    check("health refresh_requested -> false", health_refresh_requested() is False)

    # ── 9. cookie.txt rewritten + metadata adopted ─────────────────────────
    ok = os.path.exists(tmp_cf)
    written = json.load(open(tmp_cf)) if ok else {}
    check("cookie.txt rewritten", ok and written.get("sapisid") == "sap"
          and "SID=abc" in written.get("cookie", ""), str(written)[:120])
    check("uploaded gemini_bl adopted",
          CONFIG.get("gemini_bl") == "bl-new", CONFIG.get("gemini_bl"))
    check("uploaded xsrf adopted",
          CONFIG.get("xsrf_token") == "tok-new", str(CONFIG.get("xsrf_token")))
    check("uploaded auth_user adopted",
          CONFIG.get("auth_user") == "1", str(CONFIG.get("auth_user")))

    # ── 10. stale flag self-expires (no refresh windows forever) ───────────
    gem.request_cookie_refresh("test")
    check("fresh flag -> requested", flag_exists() and gem.cookie_refresh_requested())
    if flag_exists():
        with open(FLAG) as f:
            flag = json.load(f)
        flag["requested_at"] = time.time() - 3600  # backdate 1h
        with open(FLAG, "w") as f:
            json.dump(flag, f)
    check("stale flag (1h) self-expires",
          not gem.cookie_refresh_requested() and not flag_exists())

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
finally:
    # Restore every CONFIG key the upload handler may have mutated so the test
    # stays self-contained if ever imported/run in-process.
    CONFIG.update({"cookie_file": None, "gemini_bl": "test-bl",
                   "xsrf_token": None, "auth_user": None})
    gem.clear_cookie_refresh()
    if os.path.exists(tmp_cf):
        os.remove(tmp_cf)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)

raise SystemExit(1 if FAIL else 0)
