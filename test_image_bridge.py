"""Image bridge integration tests.

The image bridge delegates image processing to the user's real browser session
(the Gemini Cookie Sync extension) because direct image requests from exported
cookies are rejected by Google with BardErrorInfo 1100. This test runs the
REAL server on an ephemeral port with the Gemini network layer mocked and
drives a simulated extension through the actual HTTP endpoints:

  - /internal/image-bridge request/claim/result contract (incl. atomic claim)
  - /v1/chat/completions with an image: direct 1100 -> bridge -> answer
  - image_mode 'browser': bridge WITHOUT a doomed direct attempt
  - image_mode 'direct': no bridge, the 1100 error surfaces as a 502
  - image_mode 'auto' with a working direct path: no bridge involved
  - image-only requests (no text) are accepted
  - streaming image requests emit valid SSE via the non-stream path

Pure stdlib, no external services, no browser, no live cookie. Offline-safe.
Run:  python test_image_bridge.py
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

# Isolate the bridge state file - never touch the real one.
_TMP_STATE = os.path.join(tempfile.gettempdir(), "gwa_image_bridge_state.json")
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
    "image_mode": "auto",
    # This suite exercises the EXTENSION contract (park/claim/result) - the
    # CDP script path has its own suite (test_image_bridge_cdp.py).
    "image_bridge": "extension",
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


# ── mocked Gemini layer ───────────────────────────────────────────────────
# A tiny real PNG (1x1) as a data URI so _prepare_images needs no network.
TINY_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
                "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
IMAGE_URL = f"data:image/png;base64,{TINY_PNG_B64}"

_gen_calls = []      # (file_refs is not None) per generate() call
_direct_1100 = True  # when True, image requests raise the image-blocked error


def _fake_generate(prompt, model_id, think_mode, file_refs=None, extra_fields=None):
    _gen_calls.append(file_refs is not None)
    if file_refs and _direct_1100:
        raise RuntimeError(
            "Google rejected image input (BardErrorInfo 1100) - this account/session "
            "cannot process uploaded images. Try the same chat without an image."
        )
    return "DIRECT ANSWER"


def _fake_upload_image(image_bytes, filename="image.png", mime_type="image/png"):
    return "/contrib_service/ttl_1d/test-ref"


def _fake_generate_stream(prompt, model_id, think_mode, file_refs=None, extra_fields=None):
    yield "HELLO "
    yield "STREAM"


_orig_gen = srv.generate
_orig_gen_stream = srv.generate_stream
_orig_upload = srv.upload_image
srv.generate = _fake_generate
srv.generate_stream = _fake_generate_stream
srv.upload_image = _fake_upload_image


server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
port = server.server_address[1]
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
BASE = f"http://127.0.0.1:{port}"
print(f"image bridge: server on {BASE} (mocked Gemini, simulated extension)")


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


def chat_body(stream=False, with_image=True, text="what is in this image?"):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if with_image:
        content.append({"type": "image_url", "image_url": {"url": IMAGE_URL}})
    body = {"model": "gemini-3.6-flash", "messages": [{"role": "user", "content": content}]}
    if stream:
        body["stream"] = True
    return body


# ── simulated extension: poll -> claim -> (work) -> result via HTTP ─────────
def simulated_extension(answer="BRIDGE ANSWER", delay=0.4):
    """A thread acting exactly like the extension's poll/claim/result flow."""

    def run():
        time.sleep(delay)
        s, b, _ = req("GET", "/internal/image-bridge/request", headers=IK)
        if s != 200:
            return
        data = json.loads(b)
        if not data.get("requested"):
            return
        rid = data["request"]["id"]
        s, b, _ = req("POST", "/internal/image-bridge/claim",
                      {"id": rid}, IK)
        if s != 200:
            return
        time.sleep(0.2)  # pretend to process
        s, b, _ = req("POST", "/internal/image-bridge/result",
                      {"id": rid, "ok": True, "text": answer}, IK)

    threading.Thread(target=run, daemon=True).start()


try:
    # ── endpoint contract ──────────────────────────────────────────────────
    s, b, _ = req("GET", "/internal/image-bridge/request")
    check("bridge request endpoint requires key", s == 401, b[:80])
    s, b, _ = req("POST", "/internal/image-bridge/claim", {"id": "x"}, {"X-API-Key": "WRONG"})
    check("bridge claim wrong key -> 401", s == 401, f"{s}")
    s, b, _ = req("POST", "/internal/image-bridge/result", {"id": "x"}, {"X-API-Key": "WRONG"})
    check("bridge result wrong key -> 401", s == 401, f"{s}")

    rid = ib.register("ping", [{"name": "a.png", "mime": "image/png", "data_b64": "aGk="}])
    s, b, _ = req("GET", "/internal/image-bridge/request", headers=IK)
    ok = s == 200 and json.loads(b).get("requested") and json.loads(b)["request"]["id"] == rid
    check("pending request is offered to the extension", ok, b[:120])
    s, b, _ = req("POST", "/internal/image-bridge/claim", {"id": rid}, IK)
    check("first claim wins", s == 200 and '"ok": true' in b, f"{s} {b[:80]}")
    s, b, _ = req("GET", "/internal/image-bridge/request", headers=IK)
    check("claimed request is not offered again", not json.loads(b).get("requested"), b[:80])
    s, b, _ = req("POST", "/internal/image-bridge/claim", {"id": rid}, IK)
    check("second claim -> 409", s == 409, f"{s}")
    s, b, _ = req("POST", "/internal/image-bridge/result",
                  {"id": "not-claimed", "ok": True, "text": "x"}, IK)
    check("result for unknown id -> 409", s == 409, f"{s}")
    s, b, _ = req("POST", "/internal/image-bridge/result",
                  {"id": rid, "ok": False, "error": "boom"}, IK)
    check("result for claimed id accepted", s == 200, f"{s} {b[:80]}")

    # ── single-slot semantics: a second register while busy is rejected ────
    ib.cancel(rid)  # clear the endpoint-contract request first
    rid_a = ib.register("first", [{"name": "a.png", "mime": "image/png", "data_b64": "aGk="}])
    busy = False
    try:
        ib.register("second", [{"name": "b.png", "mime": "image/png", "data_b64": "aGk="}])
    except ib.BridgeBusy as e:
        busy = "already being processed" in str(e)
    check("second register while busy -> BridgeBusy", busy)
    s, b, _ = req("POST", "/v1/chat/completions", chat_body(), H)
    check("concurrent chat while bridge busy -> clean 502",
          s == 502 and "already being processed" in b, f"{s} {b[:140]}")
    ib.claim(rid_a)  # simulate the extension claiming + finishing the first
    ib.submit_result(rid_a, True, "first done")

    # ── wait_for_result timeout -> cancel clears the parked request ─────────
    rid_t = ib.register("timeout", [{"name": "t.png", "mime": "image/png", "data_b64": "aGk="}])
    timed_out = False
    try:
        ib.wait_for_result(rid_t, timeout=3)  # no extension ever claims it
    except RuntimeError as e:
        timed_out = "timed out" in str(e)
    check("wait_for_result times out cleanly", timed_out)
    s, b, _ = req("GET", "/internal/image-bridge/request", headers=IK)
    check("timeout cancels the parked request", not json.loads(b).get("requested"), b[:80])

    # ── health payload exposes the bridge slot state (watchdog input) ───────
    s, b, _ = req("GET", "/")
    hp = json.loads(b).get("image_bridge") or {}
    check("health payload has an image_bridge section", bool(hp), str(hp)[:120])
    ev = json.loads(b).get("extension_manifest_version")
    check("health exposes the on-disk extension manifest version",
          isinstance(ev, str) and len(ev) > 0, str(ev))
    check("health reports idle bridge when empty",
          hp.get("claimed") is False and hp.get("pending") is False
          and hp.get("claimed_age_sec") is None, str(hp))
    rid_h = ib.register("health", [{"name": "h.png", "mime": "image/png", "data_b64": "aGk="}])
    ib.claim(rid_h)
    s, b, _ = req("GET", "/")
    hp = json.loads(b).get("image_bridge") or {}
    check("health reports a claimed bridge with its age",
          hp.get("claimed") is True and isinstance(hp.get("claimed_age_sec"), int),
          str(hp))
    ib.cancel(rid_h)

    # ── result carries the extension version; /health surfaces it ────────────
    # The extension reports its own manifest version with every result POST so
    # the watchdog can flag a stale (unreloaded) build without a live request.
    rid_v = ib.register("versioned", [{"name": "v.png", "mime": "image/png", "data_b64": "aGk="}])
    ib.claim(rid_v)
    s, b, _ = req("POST", "/internal/image-bridge/result",
                  {"id": rid_v, "ok": True, "text": "versioned answer",
                   "ext_version": "1.15"}, IK)
    check("result POST with ext_version accepted", s == 200, f"{s} {b[:80]}")
    s, b, _ = req("GET", "/")
    lr = (json.loads(b).get("image_bridge") or {}).get("last_result") or {}
    check("health last_result carries ok + text + ext_version",
          lr.get("ok") is True and lr.get("text") == "versioned answer"
          and lr.get("ext_version") == "1.15", str(lr))
    # last_result survives the NEXT register (a new request must not wipe the
    # history the watchdog compares against)
    rid_v2 = ib.register("after", [{"name": "a.png", "mime": "image/png", "data_b64": "aGk="}])
    s, b, _ = req("GET", "/")
    lr2 = (json.loads(b).get("image_bridge") or {}).get("last_result") or {}
    check("last_result survives a subsequent register",
          lr2.get("ext_version") == "1.15" and lr2.get("text") == "versioned answer",
          str(lr2))
    ib.cancel(rid_v2)
    # a result WITHOUT a version is tolerated (older extension / CDP path)
    rid_v3 = ib.register("nover", [{"name": "n.png", "mime": "image/png", "data_b64": "aGk="}])
    ib.claim(rid_v3)
    s, b, _ = req("POST", "/internal/image-bridge/result",
                  {"id": rid_v3, "ok": False, "error": "boom"}, IK)
    s, b, _ = req("GET", "/")
    lr3 = (json.loads(b).get("image_bridge") or {}).get("last_result") or {}
    check("result without ext_version tolerated (ext_version None)",
          lr3.get("ok") is False and lr3.get("ext_version") is None, str(lr3))

    # ── watchdog expire endpoint: fresh claim is left alone ─────────────────
    rid_e = ib.register("expire", [{"name": "e.png", "mime": "image/png", "data_b64": "aGk="}])
    ib.claim(rid_e)  # just claimed - NOT stale
    s, b, _ = req("POST", "/internal/image-bridge/expire", {"min_age_sec": 300}, {})
    ok = s == 200 and json.loads(b).get("expired") is False
    check("expire leaves a fresh claim alone", ok, f"{s} {b[:120]}")
    s, b, _ = req("GET", "/internal/image-bridge/request", headers=IK)
    check("fresh claim still claimed after no-op expire", not json.loads(b).get("requested"), b[:80])
    ib.cancel(rid_e)  # free the slot for the abandoned-claim test

    # ── watchdog expire: an abandoned claim fails the waiter FAST ───────────
    # A server thread is waiting for the answer; the watchdog sees the claim
    # has exceeded its budget and expires it. The waiter must wake with the
    # watchdog's error almost immediately - not after the full timeout.
    rid_a = ib.register("abandoned", [{"name": "a.png", "mime": "image/png", "data_b64": "aGk="}])
    ib.claim(rid_a)
    result = {}

    def _waiter():
        try:
            ib.wait_for_result(rid_a, timeout=30)
            result["ok"] = True
        except RuntimeError as e:
            result["error"] = str(e)

    wt = threading.Thread(target=_waiter, daemon=True)
    wt.start()
    time.sleep(0.5)  # let the waiter enter its poll loop
    s, b, _ = req("POST", "/internal/image-bridge/expire", {"min_age_sec": 0}, {})
    ok = s == 200 and json.loads(b).get("expired") is True and json.loads(b).get("id") == rid_a
    check("expire (min_age 0) claims the abandoned request", ok, f"{s} {b[:120]}")
    wt.join(timeout=5)
    err = result.get("error", "")
    check("abandoned-claim waiter wakes FAST with the watchdog error",
          "expired" in err and not result.get("ok"), err[:160])
    s, b, _ = req("GET", "/internal/image-bridge/request", headers=IK)
    check("expired claim is no longer offered", not json.loads(b).get("requested"), b[:80])
    rid_n = ib.register("next", [{"name": "n.png", "mime": "image/png", "data_b64": "aGk="}])
    check("slot is free: next request registers immediately", bool(rid_n))
    ib.cancel(rid_n)

    # ── watchdog expire mirrors the failure into last_result ─────────────────
    # After an expiry the health payload must NOT keep reporting an ancient
    # extension result (the stale-extension warning would fire on old data).
    rid_w = ib.register("expw", [{"name": "w.png", "mime": "image/png", "data_b64": "aGk="}])
    ib.claim(rid_w)
    ib.submit_result(rid_w, False, "", "transient", ext_version="1.13")  # old build
    rid_w2 = ib.register("expw2", [{"name": "w2.png", "mime": "image/png", "data_b64": "aGk="}])
    ib.claim(rid_w2)
    s, b, _ = req("POST", "/internal/image-bridge/expire", {"min_age_sec": 0}, {})
    check("expire after a stale-version result succeeds",
          s == 200 and json.loads(b).get("expired") is True, f"{s} {b[:120]}")
    s, b, _ = req("GET", "/")
    lr_w = (json.loads(b).get("image_bridge") or {}).get("last_result") or {}
    check("expire overwrites last_result (no stale ext_version left)",
          lr_w.get("ext_version") is None and "watchdog" in (lr_w.get("error") or ""),
          str(lr_w))

    # ── health payload never leaks the full answer text (CORS-open /) ───────
    rid_tx = ib.register("text", [{"name": "t.png", "mime": "image/png", "data_b64": "aGk="}])
    ib.claim(rid_tx)
    long_answer = "word " * 500  # far past the 200-char health cap
    ib.submit_result(rid_tx, True, long_answer, "", ext_version="1.15")
    s, b, _ = req("GET", "/")
    lr_tx = (json.loads(b).get("image_bridge") or {}).get("last_result") or {}
    check("health truncates long answer text",
          len(lr_tx.get("text") or "") <= 203 and (lr_tx.get("text") or "").endswith("..."),
          str(lr_tx)[:140])
    # the FULL answer is still returned to the waiting client via wait_for_result
    full = ib.wait_for_result(rid_tx, timeout=5)
    check("full answer preserved for the client (not truncated)",
          full == long_answer, f"len={len(full)}")

    # ── auto mode: direct 1100 -> bridge -> answer ─────────────────────────
    CONFIG["image_mode"] = "auto"
    _direct_1100 = True
    simulated_extension("BRIDGE ANSWER")
    s, b, _ = req("POST", "/v1/chat/completions", chat_body(), H)
    ok = s == 200
    if ok:
        ok = json.loads(b)["choices"][0]["message"].get("content") == "BRIDGE ANSWER"
    check("auto: direct 1100 falls back to the bridge", ok, f"{s} {b[:160]}")
    check("auto: direct attempt was made first", _gen_calls[-1] is True, str(_gen_calls[-1]))

    # ── browser mode: bridge WITHOUT the doomed direct attempt ─────────────
    CONFIG["image_mode"] = "browser"
    n_before = len(_gen_calls)
    simulated_extension("BROWSER ANSWER")
    s, b, _ = req("POST", "/v1/chat/completions", chat_body(), H)
    ok = s == 200 and json.loads(b)["choices"][0]["message"].get("content") == "BROWSER ANSWER"
    check("browser: bridge used", ok, f"{s} {b[:160]}")
    check("browser: no direct attempt made", len(_gen_calls) == n_before,
          f"{len(_gen_calls)} vs {n_before}")

    # ── direct mode: no bridge, clean 502 ──────────────────────────────────
    CONFIG["image_mode"] = "direct"
    _direct_1100 = True
    s, b, _ = req("POST", "/v1/chat/completions", chat_body(), H)
    check("direct: 1100 surfaces as 502", s == 502 and "BardErrorInfo 1100" in b, f"{s} {b[:140]}")
    s, b, _ = req("GET", "/internal/image-bridge/request", headers=IK)
    check("direct: nothing parked for the bridge", not json.loads(b).get("requested"), b[:80])

    # ── auto mode, working direct: no bridge involved ──────────────────────
    CONFIG["image_mode"] = "auto"
    _direct_1100 = False
    s, b, _ = req("POST", "/v1/chat/completions", chat_body(), H)
    ok = s == 200 and json.loads(b)["choices"][0]["message"].get("content") == "DIRECT ANSWER"
    check("auto: working direct path stays direct", ok, f"{s} {b[:120]}")
    s, b, _ = req("GET", "/internal/image-bridge/request", headers=IK)
    check("auto: successful direct parks nothing", not json.loads(b).get("requested"), b[:80])

    # ── image-only request (no text) is legal ──────────────────────────────
    _direct_1100 = False
    s, b, _ = req("POST", "/v1/chat/completions", chat_body(text=""), H)
    check("image-only request -> 200 (not 400)", s == 200, f"{s} {b[:120]}")

    # ── streaming image request -> valid SSE via the non-stream path ───────
    _direct_1100 = True
    simulated_extension("STREAM BRIDGE")
    s, b, _ = req("POST", "/v1/chat/completions", chat_body(stream=True), H)
    ok = (s == 200 and b.rstrip().endswith("data: [DONE]")
          and "STREAM BRIDGE" in b and "chat.completion.chunk" in b)
    check("stream image -> single-chunk SSE ending [DONE]", ok, f"{s} len={len(b)}")

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
finally:
    srv.generate = _orig_gen
    srv.generate_stream = _orig_gen_stream
    srv.upload_image = _orig_upload
    if os.path.exists(_TMP_STATE):
        os.remove(_TMP_STATE)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)

raise SystemExit(1 if FAIL else 0)
