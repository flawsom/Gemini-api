"""Ephemeral-port integration test for the package server.

Replaces the ad-hoc test_server.py / test_server2.py smoke scripts with a real
test: it starts ThreadedServer on 127.0.0.1:0 (an ephemeral port), mocks the
Gemini network layer (generate / generate_stream), and exercises the actual
HTTP surface - health, auth, model routing, validation, CORS, SSE framing,
tool calling, the native Google endpoints and the internal cookie-refresh
endpoints - then tears the server down in a finally block.

Pure stdlib. No external services, no browser, no live cookie, and no
config.json required, so it runs on CI in offline mode too.

Run:  python test_integration.py
"""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from gemini_web2api.config import CONFIG
from gemini_web2api.server import GeminiHandler, ThreadedServer
import gemini_web2api.server as srv

# Deterministic config: the test must never depend on the user's cookie, keys,
# or config.json - it runs entirely on DEFAULT_CONFIG + these overrides.
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
TOOL_TEXT = ("```tool_call\n"
             '{"name": "calculator", "arguments": {"expression": "8*125"}}\n'
             "```\nThe answer is 1000.")


_last_prompt = [""]


def _fake_generate(prompt, model_id, think_mode, file_refs=None, extra_fields=None):
    _last_prompt[0] = prompt
    if "calculator" in prompt:
        return TOOL_TEXT
    return "MOCK ANSWER"


def _fake_generate_stream(prompt, model_id, think_mode, file_refs=None, extra_fields=None):
    yield "Hello "
    yield "world"


_orig_gen = srv.generate
_orig_gen_stream = srv.generate_stream
srv.generate = _fake_generate
srv.generate_stream = _fake_generate_stream

server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
port = server.server_address[1]
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
BASE = f"http://127.0.0.1:{port}"
print(f"integration: server on {BASE} (mocked Gemini, no external services)")


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
    # A refused/timeout connection must fail a CHECK with a readable reason,
    # not crash the whole suite with an opaque traceback (flaky under load).
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return 0, f"request error: {e}", {}


def raw_post(path, raw, headers=None):
    hdr = {"Content-Type": "application/json"}
    if headers:
        hdr.update(headers)
    r = urllib.request.Request(BASE + path, data=raw.encode(), headers=hdr, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return 0, f"request error: {e}", {}


H = {"Authorization": "Bearer sk-gemini"}
TOOLS = [{"type": "function", "function": {
    "name": "calculator", "description": "math",
    "parameters": {"type": "object", "properties": {}}}}]

try:
    # ── health ──────────────────────────────────────────────────────────────
    s, b, hdr = req("GET", "/")
    ok = s == 200 and '"status": "ok"' in b
    check("GET / -> 200 ok", ok, b[:120])
    if s == 200:
        d = json.loads(b)
        check("health carries bl/405/cookie/proxy/models",
              d.get("gemini_bl") == "test-bl"
              and "bl_405_count" in d and "bl_405_last_ts" in d
              and isinstance(d.get("cookie"), dict)
              and isinstance(d.get("proxy", {}).get("plan"), list)
              and isinstance(d.get("models"), list) and d["models"])

    # ── models + auth ──────────────────────────────────────────────────────
    s, b, hdr = req("GET", "/v1/models")
    check("models no key -> 401", s == 401)
    s, b, hdr = req("GET", "/v1/models", headers={"Authorization": "Bearer WRONG"})
    check("models wrong key -> 401", s == 401)
    s, b, hdr = req("GET", "/v1/models", headers=H)
    check("models valid key -> 200 + list", s == 200 and "gemini-3.6-flash" in b, b[:120])
    s, b, hdr = req("GET", "/v1/models?key=sk-gemini")
    check("models ?key= auth -> 200", s == 200)
    s, b, hdr = req("GET", "/v1/models?key=WRONG")
    check("models ?key= wrong -> 401", s == 401)
    s, b, hdr = req("GET", "/v1/models", headers={"x-api-key": "sk-gemini"})
    check("models x-api-key -> 200", s == 200)

    # ── chat: auth + validation + mocked generation ────────────────────────
    body = {"model": "gemini-3.6-flash", "messages": [{"role": "user", "content": "hi"}]}
    s, b, hdr = req("POST", "/v1/chat/completions", body)
    check("chat no key -> 401", s == 401)
    s, b, hdr = req("POST", "/v1/chat/completions", body, {"Authorization": "Bearer WRONG"})
    check("chat wrong key -> 401", s == 401)
    s, b, hdr = req("POST", "/v1/chat/completions", body, H)
    ok = s == 200 and json.loads(b)["choices"][0]["message"].get("content") == "MOCK ANSWER"
    check("chat -> 200 + mock content", ok, b[:150])
    check("chat response has usage block", '"usage"' in b)
    s, b, hdr = req("POST", "/v1/chat/completions",
                    {"model": "nope-model", "messages": [{"role": "user", "content": "hi"}]}, H)
    check("chat unknown model -> 400", s == 400 and "Unknown model" in b, b[:100])
    s, b, hdr = req("POST", "/v1/chat/completions",
                    {"model": "gemini-3.6-flash", "messages": [{"role": "user", "content": "   "}]}, H)
    check("chat empty prompt -> 400", s == 400, b[:100])
    s, b, hdr = raw_post("/v1/chat/completions", "{not json", H)
    check("chat malformed JSON -> 400", s == 400 and "invalid JSON" in b, b[:100])

    # ── tool calling over HTTP (mocked generate returns a tool_call block) ─
    s, b, hdr = req("POST", "/v1/chat/completions",
                    {"model": "gemini-3.6-flash", "tools": TOOLS, "tool_choice": "required",
                     "messages": [{"role": "user", "content": "What is 8*125?"}]}, H)
    ok, tcs = False, None
    if s == 200:
        d = json.loads(b)
        msg = d["choices"][0]["message"]
        tcs = msg.get("tool_calls")
        ok = (d["choices"][0].get("finish_reason") == "tool_calls"
              and tcs and tcs[0]["function"]["name"] == "calculator")
    check("chat tools required -> tool_calls + finish_reason", ok, b[:200])
    if tcs:
        s, b, hdr = req("POST", "/v1/chat/completions",
                        {"model": "gemini-3.6-flash", "tools": TOOLS, "tool_choice": "none",
                         "messages": [{"role": "user", "content": "hi"}]}, H)
        check("chat tool_choice none -> text only (no tool_calls)",
              s == 200 and json.loads(b)["choices"][0]["message"].get("tool_calls") is None,
              b[:150])

    # ── streaming chat: SSE framing + content deltas + [DONE] ──────────────
    s, b, hdr = req("POST", "/v1/chat/completions",
                    {"model": "gemini-3.6-flash", "stream": True,
                     "messages": [{"role": "user", "content": "hi"}]}, H)
    check("chat stream -> SSE ends with [DONE]", s == 200 and b.rstrip().endswith("data: [DONE]"),
          f"status={s} len={len(b)}")
    check("chat stream -> content deltas", "Hello" in b and "world" in b, b[:200])

    # ── response_format + stream_options (agentic-platform inputs) ─────────
    s, b, hdr = req("POST", "/v1/chat/completions",
                    {"model": "gemini-3.6-flash",
                     "response_format": {"type": "json_object"},
                     "messages": [{"role": "user", "content": "hi"}]}, H)
    check("chat response_format json_object -> 200", s == 200, b[:120])
    check("json_object appends a JSON instruction to the prompt",
          "JSON" in _last_prompt[0], _last_prompt[0][:160])
    s, b, hdr = req("POST", "/v1/chat/completions",
                    {"model": "gemini-3.6-flash", "stream": True,
                     "stream_options": {"include_usage": True},
                     "messages": [{"role": "user", "content": "hi"}]}, H)
    check("chat stream include_usage -> usage chunk before [DONE]",
          s == 200 and '"usage"' in b and b.rstrip().endswith("data: [DONE]"),
          b[-300:])

    # ── /v1/responses (Codex CLI): non-stream + stream SSE events ──────────
    s, b, hdr = req("POST", "/v1/responses", {"model": "gemini-3.6-flash", "input": "hi"}, H)
    ok = s == 200
    if ok:
        out = json.loads(b).get("output") or []
        ok = bool(out) and out[0].get("type") == "message" \
            and out[0]["content"][0]["type"] == "output_text"
    check("responses non-stream -> output_text", ok, b[:200])
    s, b, hdr = req("POST", "/v1/responses",
                    {"model": "gemini-3.6-flash", "input": "hi", "stream": True}, H)
    check("responses stream -> SSE events",
          s == 200 and "event: response.created" in b and "response.completed" in b,
          f"status={s} len={len(b)}")

    # ── native Google endpoints ────────────────────────────────────────────
    s, b, hdr = req("POST", "/v1beta/models/gemini-3.6-flash:generateContent",
                    {"contents": [{"parts": [{"text": "hi"}]}]}, H)
    check("generateContent native -> 200 + text", s == 200 and "MOCK ANSWER" in b, b[:150])
    s, b, hdr = req("POST", "/v1beta/models/gemini-3.6-flash:streamGenerateContent",
                    {"contents": [{"parts": [{"text": "hi"}]}]}, H)
    check("streamGenerateContent -> SSE + STOP",
          s == 200 and "Hello" in b and "STOP" in b, f"status={s} len={len(b)}")
    s, b, hdr = req("GET", "/v1beta/models", headers=H)
    check("GET /v1beta/models -> 200", s == 200)

    # ── CORS + routing ─────────────────────────────────────────────────────
    s, b, hdr = req("OPTIONS", "/v1/chat/completions")
    check("OPTIONS -> 204 + CORS", s == 204 and hdr.get("Access-Control-Allow-Origin") == "*")
    s, b, hdr = req("GET", "/nonexistent")
    check("unknown path -> 404", s == 404)

    # ── internal cookie-refresh endpoints ──────────────────────────────────
    s, b, hdr = req("GET", "/internal/cookie-refresh/config")
    check("internal config exposes refresh key",
          s == 200 and json.loads(b).get("api_key") == "sk-gemini", b[:120])
    s, b, hdr = req("POST", "/internal/cookie-refresh/verify", {},
                    {"X-API-Key": "sk-gemini"})
    check("verify correct key -> 200 ok", s == 200 and '"ok": true' in b, b[:80])
    s, b, hdr = req("POST", "/internal/cookie-refresh/verify", {},
                    {"X-API-Key": "WRONG"})
    check("verify wrong key -> 401", s == 401)

    tmp_cf = os.path.join(tempfile.gettempdir(), "gwa_integration_cookie.json")
    if os.path.exists(tmp_cf):
        os.remove(tmp_cf)
    CONFIG["cookie_file"] = tmp_cf
    try:
        s, b, hdr = req("POST", "/internal/cookie-refresh/request",
                        {"reason": "test"}, {"X-API-Key": "sk-gemini"})
        check("refresh request -> requested true",
              s == 200 and json.loads(b).get("requested"), b[:80])
        s, b, hdr = req("GET", "/internal/cookie-refresh/request")
        check("request flag visible via GET",
              s == 200 and json.loads(b).get("requested") is True, b[:80])
        s, b, hdr = req("POST", "/internal/cookie-refresh/upload",
                        {"cookie": "SID=abc; SAPISID=def", "sapisid": "def"},
                        {"X-API-Key": "sk-gemini"})
        ok = (s == 200 and os.path.exists(tmp_cf)
              and json.load(open(tmp_cf)).get("sapisid") == "def")
        check("upload session cookie -> 200 + written", ok, f"{s} {b[:80]}")
        s, b, hdr = req("GET", "/internal/cookie-refresh/request")
        check("upload clears the refresh flag",
              s == 200 and json.loads(b).get("requested") is False, b[:80])
        s, b, hdr = req("POST", "/internal/cookie-refresh/upload",
                        {"cookie": "NID=nid"}, {"X-API-Key": "sk-gemini"})
        check("upload without session cookie -> 400", s == 400, f"{s} {b[:80]}")
    finally:
        CONFIG["cookie_file"] = None
        if os.path.exists(tmp_cf):
            os.remove(tmp_cf)

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
finally:
    srv.generate = _orig_gen
    srv.generate_stream = _orig_gen_stream
    srv.clear_cookie_refresh()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)

raise SystemExit(1 if FAIL else 0)
