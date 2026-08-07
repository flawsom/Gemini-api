"""CDP image bridge tests (no extension required).

The CDP path processes image requests in the user's REAL browser profile via
image_bridge_cdp.py, so it works without the Gemini Cookie Sync extension. The
actual browser automation can't run on CI, so this test drives the REAL
server wiring with a FAKE bridge script that mimics the exit-code contract:

  rc 0 -> {"ok": true, "text": ...}   (success)
  rc 2 -> {"ok": false, "error": ...} (failure)
  rc 3 -> browser open without a debug port -> extension fallback

Covered: fake-script subprocess round trip (temp image files + result
parsing), image_bridge mode matrix (cdp/auto/extension), the auto fallback to
the extension path, and that image-only + streaming still behave.

Run:  python test_image_bridge_cdp.py
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
import gemini_web2api.image_bridge as ib

_TMP_STATE = os.path.join(tempfile.gettempdir(), "gwa_cdp_bridge_state.json")
ib.STATE_FILE = _TMP_STATE

CONFIG.update({
    "api_keys": ["sk-gemini"],
    "cookie_refresh_key": None,
    "cookie_file": None,
    "xsrf_token": "test-tok",
    "gemini_bl": "test-bl",
    "auth_user": None,
    "auto_update_bl": False,
    "proxy": None,
    "proxy_fallbacks": [],
    "image_mode": "browser",   # skip direct entirely - straight to the bridge
    "image_bridge_timeout": 30,
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


TINY_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
                "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
IMAGE_URL = f"data:image/png;base64,{TINY_PNG_B64}"

_gen_calls = []


def _fake_generate(prompt, model_id, think_mode, file_refs=None, extra_fields=None):
    _gen_calls.append(True)
    return "DIRECT ANSWER"  # must never be reached in browser mode


# ─── fake bridge scripts (mimic image_bridge_cdp.py's contract) ─────────────
FAKE_OK = os.path.join(tempfile.gettempdir(), "gwa_fake_bridge_ok.py")
FAKE_FAIL = os.path.join(tempfile.gettempdir(), "gwa_fake_bridge_fail.py")
FAKE_EXT = os.path.join(tempfile.gettempdir(), "gwa_fake_bridge_ext.py")

with open(FAKE_OK, "w") as f:
    f.write("""import json, os, sys
args = sys.argv[1:]
out = args[args.index("--out") + 1]
imgs = args[args.index("--images") + 1].split(",")
for p in imgs:
    assert os.path.exists(p), p  # the server must have written the image files
n = len([p for p in imgs if os.path.getsize(p) > 0])
json.dump({"ok": True, "text": f"CDP-ANSWER({n})"}, open(out, "w"))
""")
with open(FAKE_FAIL, "w") as f:
    f.write("""import json, sys
args = sys.argv[1:]
out = args[args.index("--out") + 1]
json.dump({"ok": False, "error": "browser open"}, open(out, "w"))
sys.exit(2)
""")
with open(FAKE_EXT, "w") as f:
    f.write("""import json, sys
args = sys.argv[1:]
out = args[args.index("--out") + 1]
json.dump({"ok": False, "error": "browser open"}, open(out, "w"))
sys.exit(3)
""")

def _fake_generate_stream(prompt, model_id, think_mode, file_refs=None, extra_fields=None):
    yield "HELLO "
    yield "STREAM"


_orig_gen = srv.generate
_orig_gen_stream = srv.generate_stream
srv.generate = _fake_generate
srv.generate_stream = _fake_generate_stream


server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
port = server.server_address[1]
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
BASE = f"http://127.0.0.1:{port}"
print(f"image bridge cdp: server on {BASE} (fake bridge script, mocked Gemini)")


def req(method, path, body=None, headers=None, timeout=40):
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


H = {"Authorization": "Bearer sk-gemini"}
IK = {"X-API-Key": "sk-gemini"}


def chat_body(stream=False, text="what is in this image?"):
    content = [{"type": "text", "text": text}] if text else []
    content.append({"type": "image_url", "image_url": {"url": IMAGE_URL}})
    body = {"model": "gemini-3.6-flash", "messages": [{"role": "user", "content": content}]}
    if stream:
        body["stream"] = True
    return body


def simulated_extension(answer="EXT-ANSWER", delay=0.3):
    def run():
        time.sleep(delay)
        s, b, _ = req("GET", "/internal/image-bridge/request", headers=IK)
        if s != 200:
            return
        data = json.loads(b)
        if not data.get("requested"):
            return
        rid = data["request"]["id"]
        req("POST", "/internal/image-bridge/claim", {"id": rid}, IK)
        req("POST", "/internal/image-bridge/result",
            {"id": rid, "ok": True, "text": answer}, IK)

    threading.Thread(target=run, daemon=True).start()


try:
    # ── cdp mode: fake script success (subprocess wiring end to end) ───────
    CONFIG["image_bridge"] = "cdp"
    srv._run_cdp_bridge.__defaults__ = (FAKE_OK,)  # inject the fake script
    s, b, _ = req("POST", "/v1/chat/completions", chat_body(), H)
    ok = s == 200 and json.loads(b)["choices"][0]["message"].get("content") == "CDP-ANSWER(1)"
    check("cdp mode -> fake script answer + 1 image written", ok, f"{s} {b[:140]}")

    # ── cdp mode: failure surfaces cleanly ─────────────────────────────────
    srv._run_cdp_bridge.__defaults__ = (FAKE_FAIL,)
    s, b, _ = req("POST", "/v1/chat/completions", chat_body(), H)
    check("cdp mode -> rc2 surfaces as 502 with the script error",
          s == 502 and "browser open" in b, f"{s} {b[:140]}")
    # ── cdp mode: rc3 (browser open) -> no fallback, clear install hint ───
    srv._run_cdp_bridge.__defaults__ = (FAKE_EXT,)
    s, b, _ = req("POST", "/v1/chat/completions", chat_body(), H)
    check("cdp mode -> rc3 explains how to enable image input",
          s == 502 and "install the Gemini Cookie Sync extension" in b, f"{s} {b[:140]}")

    # ── auto mode: rc0 -> cdp answer, no extension involved ────────────────
    CONFIG["image_bridge"] = "auto"
    srv._run_cdp_bridge.__defaults__ = (FAKE_OK,)
    s, b, _ = req("POST", "/v1/chat/completions", chat_body(), H)
    check("auto mode -> cdp answer when the script succeeds",
          s == 200 and "CDP-ANSWER" in b, f"{s} {b[:120]}")

    # ── auto mode: rc3 -> extension fallback ───────────────────────────────
    srv._run_cdp_bridge.__defaults__ = (FAKE_EXT,)
    simulated_extension("EXT-ANSWER")
    s, b, _ = req("POST", "/v1/chat/completions", chat_body(), H)
    check("auto mode -> extension fallback when browser is open",
          s == 200 and json.loads(b)["choices"][0]["message"].get("content") == "EXT-ANSWER",
          f"{s} {b[:140]}")

    # ── extension mode: never calls the cdp script ─────────────────────────
    CONFIG["image_bridge"] = "extension"
    calls_before = list(srv._run_cdp_bridge.__defaults__)
    srv._run_cdp_bridge.__defaults__ = (FAKE_OK,)
    simulated_extension("EXT-ONLY")
    s, b, _ = req("POST", "/v1/chat/completions", chat_body(), H)
    check("extension mode -> extension answer",
          s == 200 and json.loads(b)["choices"][0]["message"].get("content") == "EXT-ONLY",
          f"{s} {b[:140]}")

    # ── stream image request -> valid SSE via the non-stream path ──────────
    srv._run_cdp_bridge.__defaults__ = (FAKE_OK,)
    CONFIG["image_bridge"] = "cdp"
    s, b, _ = req("POST", "/v1/chat/completions", chat_body(stream=True), H)
    ok = (s == 200 and b.rstrip().endswith("data: [DONE]")
          and "CDP-ANSWER" in b)
    check("stream image -> single-chunk SSE ending [DONE]", ok, f"{s} len={len(b)}")

    # ── image-only (no text) -> still bridged ──────────────────────────────
    s, b, _ = req("POST", "/v1/chat/completions", chat_body(text=""), H)
    check("image-only request -> bridged (not 400)",
          s == 200 and "CDP-ANSWER" in b, f"{s} {b[:120]}")

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
finally:
    srv.generate = _orig_gen
    srv.generate_stream = _orig_gen_stream
    for p in (FAKE_OK, FAKE_FAIL, FAKE_EXT, _TMP_STATE):
        if os.path.exists(p):
            os.remove(p)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)

raise SystemExit(1 if FAIL else 0)
