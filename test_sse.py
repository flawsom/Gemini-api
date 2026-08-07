"""SSE protocol edge-case tests for the streaming endpoints.

Verifies the failure modes agentic clients (AionUI, OpenAI SDKs, Codex CLI...)
can actually hit, against a real ThreadedServer with a mocked Gemini stream
layer:

1. MID-STREAM UPSTREAM FAILURE - when the upstream raises after a few deltas,
   the chat stream must emit a VALID SSE error frame followed by
   data: [DONE] - never a raw JSON 500 body inside the event stream after the
   200 was already sent. The native :streamGenerateContent stream must emit a
   finishReason=ERROR frame (its protocol has no [DONE]).

2. stream_options.include_usage ORDERING - the usage chunk must appear
   exactly once, after the content deltas and the finish chunk, immediately
   before data: [DONE] - on BOTH the generate_stream path and the
   single-chunk tool/stream path. With include_usage absent, no usage frame
   is emitted.

3. CLIENT DISCONNECT - an abrupt RST mid-stream must not crash the handler
   or kill the server: the next request succeeds and no traceback or
   "POST error" is logged (the outer BrokenPipeError/ConnectionResetError
   catch in the chat and v1beta stream paths absorbs it).

Pure stdlib, ephemeral port, mocked Gemini - no cookie, config.json or
external services, so it is CI/offline safe.

Run:  python test_sse.py
"""
import json
import os
import socket
import struct
import sys
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

# Deterministic config: never depend on the user's cookie, keys or config.json.
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

STREAM_SCENARIO = ["ok"]  # "ok" | "fail" | "disconnect"


def _fake_generate(prompt, model_id, think_mode, file_refs=None, extra_fields=None):
    if "calculator" in prompt:
        return TOOL_TEXT
    return "MOCK ANSWER"


def _fake_generate_stream(prompt, model_id, think_mode, file_refs=None, extra_fields=None):
    sc = STREAM_SCENARIO[0]
    if sc == "fail":
        yield "Hello "
        raise RuntimeError("boom")
    if sc == "disconnect":
        # Sleep between yields so the client can RST the socket while the
        # server is still mid-loop - the next write then fails deterministically.
        yield "Hello "
        time.sleep(0.15)
        yield "world"
        time.sleep(0.15)
        yield "again"
    yield "Hello "
    yield "world"


_orig_gen = srv.generate
_orig_gen_stream = srv.generate_stream
_orig_log = srv.log
srv.generate = _fake_generate
srv.generate_stream = _fake_generate_stream

LOGS = []


def _cap_log(msg, *args):
    LOGS.append(str(msg))


srv.log = _cap_log

server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
port = server.server_address[1]
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
BASE = f"http://127.0.0.1:{port}"
print(f"sse: server on {BASE} (mocked Gemini stream, no external services)")


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


H = {"Authorization": "Bearer sk-gemini"}


def sse_frames(body):
    """Parse an SSE body into (kind, value) frames.

    kind is "json" (parsed payload), "done" (data: [DONE]) or "raw" - any
    payload that is neither, i.e. content that would prove a protocol break.
    """
    out = []
    for line in body.split("\n"):
        if line.startswith("data: "):
            payload = line[6:]
            if payload == "[DONE]":
                out.append(("done", "[DONE]"))
            else:
                try:
                    out.append(("json", json.loads(payload)))
                except (json.JSONDecodeError, ValueError):
                    out.append(("raw", payload))
    return out


def is_sse(body):
    """True iff every non-empty line is a framed data: event (no raw leak)."""
    return all(not ln or ln.startswith("data: ") for ln in body.splitlines())


try:
    # ── 1a. mid-stream upstream failure: /v1/chat/completions stream ──────
    STREAM_SCENARIO[0] = "fail"
    s, b, hdr = req("POST", "/v1/chat/completions",
                    {"model": "gemini-3.6-flash", "stream": True,
                     "messages": [{"role": "user", "content": "hi"}]}, H)
    frames = sse_frames(b)
    check("chat stream failure -> 200 + text/event-stream",
          s == 200 and (hdr.get("Content-Type") or "").startswith("text/event-stream"),
          f"status={s} ct={hdr.get('Content-Type')}")
    check("chat stream failure -> no raw JSON leaked into the stream", is_sse(b), b[:200])
    check("chat stream failure -> exact frame sequence [delta, error, done]",
          [k for k, _ in frames] == ["json", "json", "done"]
          and frames[0][1]["choices"][0]["delta"].get("content") == "Hello "
          and frames[1][1].get("error") is not None,
          str([k for k, _ in frames]))
    err_frame = frames[1][1] if len(frames) > 1 and frames[1][0] == "json" else {}
    err_msg = (err_frame.get("error") or {}).get("message", "")
    check("chat stream failure -> error frame surfaces the cause",
          "boom" in err_msg, err_msg)
    check("chat stream failure -> ends with data: [DONE]",
          frames[-1] == ("done", "[DONE]"), b[-120:])

    # ── 1b. mid-stream upstream failure: :streamGenerateContent stream ─────
    s, b, hdr = req("POST", "/v1beta/models/gemini-3.6-flash:streamGenerateContent",
                    {"contents": [{"parts": [{"text": "hi"}]}]}, H)
    frames = sse_frames(b)
    check("v1beta stream failure -> 200 + text/event-stream",
          s == 200 and (hdr.get("Content-Type") or "").startswith("text/event-stream"),
          f"status={s} ct={hdr.get('Content-Type')}")
    check("v1beta stream failure -> no raw JSON leaked into the stream", is_sse(b), b[:200])
    check("v1beta stream failure -> ERROR finish frame",
          any(k == "json" and v.get("candidates", [{}])[0].get("finishReason") == "ERROR"
              for k, v in frames),
          b[-200:])
    check("v1beta stream failure -> error message surfaces",
          any(k == "json" and "boom" in json.dumps(v) for k, v in frames),
          b[-200:])
    check("v1beta stream failure -> no [DONE] (native protocol)", "[DONE]" not in b, b[-80:])

    # ── 1c. v1beta stream success: final STOP chunk carries usageMetadata ──
    STREAM_SCENARIO[0] = "ok"
    s, b, hdr = req("POST", "/v1beta/models/gemini-3.6-flash:streamGenerateContent",
                    {"contents": [{"parts": [{"text": "hi"}]}]}, H)
    frames = sse_frames(b)
    last = frames[-1] if frames else ("", {})
    check("v1beta stream ok -> final STOP chunk with usageMetadata",
          last[0] == "json"
          and last[1].get("candidates", [{}])[0].get("finishReason") == "STOP"
          and last[1].get("usageMetadata", {}).get("totalTokenCount", 0) > 0,
          str(last[1])[:160])

    # ── 2a. include_usage ordering (generate_stream path) ──────────────────
    s, b, hdr = req("POST", "/v1/chat/completions",
                    {"model": "gemini-3.6-flash", "stream": True,
                     "stream_options": {"include_usage": True},
                     "messages": [{"role": "user", "content": "hi"}]}, H)
    frames = sse_frames(b)
    js = [v for k, v in frames if k == "json"]
    usage_frames = [v for v in js if "usage" in v and v["choices"] == []]
    check("include_usage -> exactly one usage chunk", len(usage_frames) == 1,
          f"count={len(usage_frames)}")
    check("include_usage -> usage chunk immediately before [DONE]",
          bool(usage_frames) and frames[-1] == ("done", "[DONE]")
          and js[-1] == usage_frames[0],
          str([k for k, _ in frames]))
    check("include_usage -> ordering: deltas, finish, usage, [DONE]",
          len(js) >= 3
          and js[0]["choices"][0]["delta"].get("content") == "Hello "
          and js[-2]["choices"][0]["finish_reason"] == "stop"
          and js[-2]["choices"][0]["delta"] == {},
          str([k for k, _ in frames]))
    check("include_usage -> usage numbers > 0",
          bool(usage_frames) and usage_frames[0]["usage"]["total_tokens"] > 0,
          str(usage_frames[0].get("usage"))[:120] if usage_frames else "no usage")

    # ── 2b. without include_usage -> no usage chunk ────────────────────────
    s, b, hdr = req("POST", "/v1/chat/completions",
                    {"model": "gemini-3.6-flash", "stream": True,
                     "messages": [{"role": "user", "content": "hi"}]}, H)
    check("no include_usage -> no usage chunk",
          not any(k == "json" and "usage" in v for k, v in sse_frames(b)), b[-200:])

    # ── 2c. include_usage on the single-chunk tool/stream path ─────────────
    TOOLS = [{"type": "function", "function": {
        "name": "calculator", "description": "math",
        "parameters": {"type": "object", "properties": {}}}}]
    s, b, hdr = req("POST", "/v1/chat/completions",
                    {"model": "gemini-3.6-flash", "stream": True,
                     "stream_options": {"include_usage": True},
                     "tools": TOOLS,
                     "messages": [{"role": "user", "content": "8*125?"}]}, H)
    frames = sse_frames(b)
    js = [v for k, v in frames if k == "json"]
    u = [v for v in js if v.get("usage")]
    check("tool/stream include_usage -> [chunk, usage, done]",
          len(js) == 2 and bool(u) and frames[-1] == ("done", "[DONE]")
          and js[-1] == u[0] and js[-1]["choices"] == [],
          str([k for k, _ in frames]))

    # ── 3. client disconnect mid-stream (RST) ──────────────────────────────
    # The server's next write after the RST raises a connection error; it must
    # be absorbed silently (no traceback, no "POST error", no raw JSON leak)
    # and the server must keep serving.
    STREAM_SCENARIO[0] = "disconnect"
    LOGS.clear()
    body = json.dumps({"model": "gemini-3.6-flash", "stream": True,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        req_raw = (f"POST /v1/chat/completions HTTP/1.1\r\n"
                   f"Host: 127.0.0.1:{port}\r\n"
                   f"Content-Type: application/json\r\n"
                   f"Authorization: Bearer sk-gemini\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode() + body
        sock.sendall(req_raw)
        buf = b""
        while b"data: " not in buf:
            try:
                chunk = sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        check("disconnect -> SSE actually started before the RST", b"data: " in buf,
              f"got {len(buf)} bytes")
    finally:
        # SO_LINGER(1, 0) forces an RST on close -> guaranteed connection error
        # on the server's next write (ConnectionResetError on both Windows and
        # POSIX), instead of a graceful FIN that could pass unnoticed.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.close()
    # Poll (not a fixed sleep) so the assertion can never run BEFORE the server
    # thread has hit the dead socket - the failing write lands within ~0.15s,
    # and a broken disconnect path would log "POST error" promptly; 2s of head
    # room covers any CI load.
    deadline = time.time() + 2.0
    while (time.time() < deadline
           and not any("Traceback" in ln or "POST error" in ln for ln in LOGS)):
        time.sleep(0.05)
    s, b, hdr = req("GET", "/v1/models", headers=H)
    check("disconnect -> server survives (next request 200)", s == 200, f"status={s}")
    bad_logs = [ln for ln in LOGS if "Traceback" in ln or "POST error" in ln]
    check("disconnect -> no traceback / POST error logged", not bad_logs, bad_logs[:3])

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
finally:
    srv.generate = _orig_gen
    srv.generate_stream = _orig_gen_stream
    srv.log = _orig_log
    srv.clear_cookie_refresh()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)

raise SystemExit(1 if FAIL else 0)
