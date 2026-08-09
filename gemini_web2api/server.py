"""HTTP server: OpenAI-compatible API endpoints."""
import base64
import json
import shutil
import sys
import subprocess
import tempfile
import threading
import time
import uuid
import re
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

import os

from .config import CONFIG
from .models import MODELS, resolve_model
from .gemini import (generate, generate_stream, log,
                     cookie_refresh_requested, request_cookie_refresh,
                     clear_cookie_refresh, refresh_key,
                     _proxy_plan, _proxy_state, _bl_405)
from .tools import messages_to_prompt, parse_tool_calls, google_contents_to_prompt, parse_google_function_calls, normalize_tool_defs, tool_force_escalation
from .multimodal import upload_image, fetch_image_bytes
# NOTE: import by NAME, not `from . import image_bridge` - bundle.py drops
# bare `from . import X` lines (no alias emitted), so the module object would
# be undefined in the bundled single-file edition. These names exist at module
# level once image_bridge.py is concatenated before server.py.
from .image_bridge import (pending_request, claim, submit_result,
                           register, wait_for_result, expire_stale,
                           bridge_health)
from . import __version__


def _extension_manifest_version() -> str | None:
    """The extension's on-disk manifest version (None if not found).

    The popup compares the version carried by the last image-bridge result
    against THIS value: when the result came from an older build than what is
    on disk, the extension was updated but never reloaded - worth a warning
    color in the popup, not just a watchdog log line. Resolves for both
    layouts: package (gemini_web2api/server.py -> ../gemini-cookie-sync-
    extension) and bundle (gemini_web2api.py at the project root ->
    ./gemini-cookie-sync-extension)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (here, os.path.join(here, "..")):
        candidate = os.path.join(base, "gemini-cookie-sync-extension", "manifest.json")
        try:
            with open(candidate, encoding="utf-8") as f:
                ver = json.load(f).get("version")
            if isinstance(ver, str) and ver:
                return ver
        except (OSError, ValueError):
            continue
    return None


def _health_payload() -> dict:
    """/health-style status with BL + cookie + proxy introspection.

    Lets operators (and the watchdog) see at a glance whether the build label
    is current, how old the session cookies are, and which proxy route the
    server is currently preferring - no log archaeology required.
    """
    cookie_file = CONFIG.get("cookie_file")
    cookie_age, cookie_updated_at = None, None
    if cookie_file and os.path.exists(cookie_file):
        try:
            cookie_updated_at = os.path.getmtime(cookie_file)
            cookie_age = int(time.time() - cookie_updated_at)
        except OSError:
            pass
    return {
        "status": "ok",
        "version": __version__,
        "models": list(MODELS.keys()),
        "gemini_bl": CONFIG.get("gemini_bl"),
        "auto_update_bl": bool(CONFIG.get("auto_update_bl", False)),
        # Consecutive live-request 405s since the last success - the watchdog
        # watches this to trigger a cookie refresh when the BL starts failing.
        "bl_405_count": _bl_405["count"],
        "bl_405_last_ts": _bl_405["ts"] or None,
        "cookie": {
            "file": cookie_file,
            "exists": bool(cookie_file and os.path.exists(cookie_file)),
            "age_sec": cookie_age,
            "updated_at": cookie_updated_at,
            "refresh_requested": cookie_refresh_requested(),
        },
        "proxy": {
            "configured": CONFIG.get("proxy"),
            "fallbacks": CONFIG.get("proxy_fallbacks") or [],
            "plan": _proxy_plan(),
            "working": _proxy_state.get("working"),
        },
        # Image-bridge slot state: the watchdog watches claimed_age_sec and
        # expires an abandoned claim (stuck extension) so the next image
        # request is not blocked by a dead claim.
        "image_bridge": bridge_health(),
        # The extension's ON-DISK version, so the popup can compare the last
        # result's ext_version against it and warn when the extension was
        # updated but never reloaded.
        "extension_manifest_version": _extension_manifest_version(),
    }


def _prepare_images(images: list) -> list:
    """Normalize image inputs into [{"name", "mime", "data_b64"}] with bytes.

    Accepts the (data, mime) / (data, mime, name) tuples the prompt builders
    produce, where data may be raw bytes, a base64 data URI, or an http(s)
    URL. The prepared list feeds BOTH the direct upload path and the image
    bridge (which needs the concrete bytes as base64 to re-attach them in the
    browser)."""
    if not images:
        return []
    prepared = []
    for item in images:
        name, mime = "image.png", "image/png"
        data = None
        if isinstance(item, tuple):
            if len(item) >= 3:
                data, mime, name = item
            elif len(item) == 2:
                data, mime = item
        if isinstance(data, str):
            if data.startswith("data:"):
                header, _, b64 = data.partition(",")
                mime = header[5:].split(";")[0] or mime
                data = base64.b64decode(b64)
            else:
                data = fetch_image_bytes(data)
        if not data:
            log("Image input skipped: no usable bytes (empty data or "
                "unfetchable URL)")
            continue
        prepared.append({"name": name, "mime": mime or "image/png",
                         "data_b64": base64.b64encode(data).decode("ascii")})
    return prepared


def _upload_images(images: list) -> list:
    """Upload images and return list of (file_ref, name, mime_type) triples.
    Returns None if no images. The mime type is required by _build_payload,
    which embeds it in the ref array (the live web UI format). Accepts the
    raw tuples the prompt builders produce OR already-prepared dicts."""
    if not images:
        return None
    if images and isinstance(images[0], dict):
        prepared = images
    else:
        prepared = _prepare_images(images)
    if not prepared:
        return None
    file_refs = []
    for p in prepared:
        try:
            ref = upload_image(base64.b64decode(p["data_b64"]), p["name"], p["mime"])
            file_refs.append((ref, p["name"], p["mime"]))
        except Exception as e:
            log(f"Image upload failed: {e}")
            raise
    return file_refs if file_refs else None


def image_bridge_enabled() -> bool:
    """True unless image_mode is 'direct' (never delegate to the browser).

    auto: try the direct upload first, bridge only on image-blocked rejection.
    browser: go straight to the browser bridge (your session 1100s on direct)."""
    return CONFIG.get("image_mode", "auto") != "direct"


def _is_image_blocked(err: Exception) -> bool:
    """True when the upstream rejection means 'this session can't do images'.

    Direct image requests from exported cookies fail with BardErrorInfo 1100
    (or an empty image response) even though the same request works from the
    real browser - those are exactly the failures the bridge is for."""
    msg = str(err)
    return ("BardErrorInfo 1100" in msg
            or "empty response for the image request" in msg)


# Serializes the CDP bridge subprocess (one browser-assisted image at a time,
# mirroring the extension path's single-slot semantics).
_cdp_lock = threading.Lock()


def _find_cdp_script() -> str | None:
    """Locate image_bridge_cdp.py for both layouts:
    - package:  gemini_web2api/server.py -> ../image_bridge_cdp.py
    - bundle:   gemini_web2api.py (project root) -> ./image_bridge_cdp.py
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, "image_bridge_cdp.py"),
                      os.path.join(here, "..", "image_bridge_cdp.py")):
        if os.path.exists(candidate):
            return candidate
    return None


def _run_cdp_bridge(prompt: str, prepared: list, timeout: int, script: str = None):
    """Process the image via image_bridge_cdp.py (real browser profile).

    Returns (exit_code, text_or_None, error_or_None). Exit codes from the
    script: 0 success, 2 error, 3 browser running without a debug port (the
    caller should use the extension path), 4 already busy. `script` is
    injectable for tests (default: auto-located next to the server)."""
    if not script:
        script = _find_cdp_script()
    if not script:
        return 2, None, "image_bridge_cdp.py not found next to the server"
    tmp = tempfile.mkdtemp(prefix="gwa_bridge_")
    out = os.path.join(tmp, "result.json")
    try:
        image_paths = []
        for i, p in enumerate(prepared or []):
            ext = {"image/png": ".png", "image/jpeg": ".jpg",
                   "image/webp": ".webp", "image/gif": ".gif"}.get(p["mime"], ".img")
            path = os.path.join(tmp, f"img_{i}{ext}")
            with open(path, "wb") as f:
                f.write(base64.b64decode(p["data_b64"]))
            image_paths.append(path)
        cmd = [sys.executable, script, "--prompt", prompt,
               "--images", ",".join(image_paths), "--out", out,
               "--timeout", str(timeout)]
        try:
            with _cdp_lock:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=timeout + 30)
        except subprocess.TimeoutExpired:
            return 2, None, "image bridge script timed out"
        rc = proc.returncode
        try:
            with open(out, encoding="utf-8") as f:
                result = json.load(f)
        except (OSError, ValueError):
            result = {}
        if rc == 0 and result.get("ok"):
            return 0, result.get("text", ""), None
        if rc == 3:
            return 3, None, None
        if rc == 4:
            return 4, None, result.get("error") or "another image bridge is already running"
        return 2, None, result.get("error") or (proc.stderr or "").strip() or \
            "image bridge script failed"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _bridge_chat(prompt: str, prepared: list, model_name: str = None) -> str:
    """Process an image request in the user's real browser session.

    image_bridge mode:
      cdp        - run image_bridge_cdp.py against the real browser profile
                   (no extension needed; browser may be closed)
      extension  - park the request for the Gemini Cookie Sync extension
      auto       - try CDP first (fast, no extension when the browser is
                   closed); fall back to the extension when the browser is
                   open without a debug port
    """
    timeout = CONFIG.get("image_bridge_timeout")
    mode = CONFIG.get("image_bridge", "auto")
    if mode in ("cdp", "auto"):
        rc, text, err = _run_cdp_bridge(prompt, prepared, timeout)
        if rc == 0:
            log(f"Image bridge (cdp): answered ({len(prepared or [])} image(s))")
            return text
        if rc == 3 and mode == "auto":
            log("Image bridge: browser open without a debug port - "
                "falling back to the extension")
        else:
            raise RuntimeError(err or f"image bridge (cdp) failed (exit {rc}) - "
                               "install the Gemini Cookie Sync extension, close "
                               "the browser, or start it with --remote-debugging-port")
    rid = register(prompt, prepared or [], model_name, timeout_ms=timeout * 1000)
    log(f"Image bridge: request {rid} parked for the extension "
        f"(images={len(prepared or [])})")
    # The extension's real cycle is longer than it looks: poll (≤30s) + cold
    # window load (~20-60s) + attach/send (~10s) + Gemini answer (up to the
    # content script's own answer budget). The server must wait the FULL
    # image_bridge_timeout - an early cap cancels a legitimately-slow answer
    # (observed: a working extension finished ~70s AFTER a 180s cap had
    # already cancelled its claim). The watchdog's expire is the backstop for
    # genuinely dead claims; this budget is for live ones.
    return wait_for_result(rid, timeout)


def _run_generation(prompt: str, model_id: int, think_mode: int,
                    images: list, extra_fields: dict,
                    model_name: str = None) -> str:
    """Generate, falling back to the browser image bridge when needed.

    Text-only requests are unchanged. Image requests honour image_mode:
    'browser' skips the doomed direct attempt entirely; 'auto' tries direct
    and bridges on BardErrorInfo 1100 / empty image response; 'direct' never
    bridges (the old behaviour)."""
    if not images:
        return generate(prompt, model_id, think_mode, None, extra_fields)
    prepared = _prepare_images(images)
    if image_bridge_enabled() and CONFIG.get("image_mode") == "browser":
        return _bridge_chat(prompt, prepared, model_name)
    try:
        return generate(prompt, model_id, think_mode,
                        _upload_images(prepared), extra_fields)
    except RuntimeError as e:
        if image_bridge_enabled() and _is_image_blocked(e):
            log(f"Direct image request blocked ({e}); delegating to the browser")
            return _bridge_chat(prompt, prepared, model_name)
        raise


def _force_tool_call(prompt, model_id, think_mode, images, extra_fields, model_name,
                     tool_choice, tool_defs, parse):
    """Hard-enforce a required tool call with escalating prompts.

    Returns (text, tool_calls). Raises RuntimeError if the model still refuses
    after the escalation steps, so ``tool_choice: "required"`` (or native
    functionCallingConfig mode=ANY) never silently degrades into plain text.
    """
    last_text = ""
    for attempt in range(3):
        retry_prompt = prompt + tool_force_escalation(tool_choice, tool_defs, attempt)
        try:
            text = _run_generation(retry_prompt, model_id, think_mode,
                                   images, extra_fields, model_name)
        except Exception as e:
            if attempt == 2:
                raise RuntimeError(f"upstream failed while forcing tool call: {e}")
            continue
        if text:
            last_text = text
            cleaned, calls = parse(text)
            if calls:
                return cleaned, calls
    raise RuntimeError(
        "model refused to produce the required tool call "
        f"(tool_choice required / mode=ANY); last response: {last_text[:200]}"
    )


def _google_tool_names(req: dict) -> list:
    """Extract function names from native Gemini tools (functionDeclarations)."""
    names = []
    for group in req.get("tools") or []:
        for fn in group.get("functionDeclarations", []):
            names.append(fn.get("name"))
    return names


class GeminiHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        client_ip = self.client_address[0] if self.client_address else "-"
        log(f"{client_ip} {fmt % args}")

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _parse_body(self, body: bytes) -> dict:
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return None

    def _authorized(self):
        keys = CONFIG.get("api_keys") or []
        if not keys:
            return True
        # Authorization: Bearer <key>
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] in keys:
            return True
        # header keys (OpenAI x-api-key / Google x-goog-api-key)
        for h in ("x-api-key", "x-goog-api-key"):
            if self.headers.get(h, "") in keys:
                return True
        # query param ?key= (Gemini CLI native style)
        if "?" in self.path:
            for pair in self.path.split("?", 1)[1].split("&"):
                if pair.startswith("key=") and pair[4:] in keys:
                    return True
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        try:
            if self.path.startswith("/v1") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            path = self.path.split("?", 1)[0]
            if path == "/internal/cookie-refresh/request":
                self.send_json({"requested": cookie_refresh_requested()})
            elif path == "/internal/image-bridge/request":
                self._handle_bridge_request()
            elif path == "/internal/cookie-refresh/config":
                # Advertise the base URL + refresh key so the extension can
                # self-configure for a non-default port or custom api key.
                # The key is only revealed to loopback clients.
                if not self._is_loopback():
                    self.send_json({"error": "loopback only"}, 403)
                else:
                    self.send_json({
                        "base_url": f"http://127.0.0.1:{CONFIG.get('port', 8081)}",
                        "api_key": refresh_key(),
                        "version": __version__,
                    })
            elif path == "/v1/models":
                self.send_json({"object": "list", "data": [
                    {"id": n, "object": "model", "created": 1700000000,
                     "owned_by": "google", "description": c["desc"]}
                    for n, c in MODELS.items()
                ]})
            elif path.startswith("/v1beta/models"):
                self.send_json({"models": [
                    {"name": f"models/{n}", "displayName": n, "description": c["desc"],
                     "supportedGenerationMethods": ["generateContent", "streamGenerateContent"]}
                    for n, c in MODELS.items()
                ]})
            elif path == "/":
                self.send_json(_health_payload())
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        try:
            if self.path.startswith("/v1") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            path = self.path.split("?", 1)[0]
            if path == "/internal/cookie-refresh/request":
                self._handle_refresh_request(body)
            elif path == "/internal/cookie-refresh/upload":
                self._handle_refresh_upload(body)
            elif path == "/internal/cookie-refresh/verify":
                self._handle_refresh_verify(body)
            elif path == "/internal/image-bridge/request":
                self._handle_bridge_request()
            elif path == "/internal/image-bridge/claim":
                self._handle_bridge_claim(body)
            elif path == "/internal/image-bridge/result":
                self._handle_bridge_result(body)
            elif path == "/internal/image-bridge/expire":
                self._handle_bridge_expire(body)
            elif path == "/v1/chat/completions":
                self._handle_chat(body)
            elif path == "/v1/responses":
                self._handle_responses(body)
            elif ":generateContent" in self.path:
                self._handle_google_generate(body, stream=False)
            elif ":streamGenerateContent" in self.path:
                self._handle_google_generate(body, stream=True)
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"POST error: {e}\n{traceback.format_exc()}")
            try:
                self.send_json({"error": {"message": str(e)}}, 500)
            except:
                pass

    # ─── Internal: cookie auto-refresh (extension contract) ─────────────────

    def _is_loopback(self) -> bool:
        ip = self.client_address[0] if self.client_address else ""
        return ip in ("127.0.0.1", "::1", "localhost")

    def _internal_key_ok(self, req: dict) -> bool:
        """Cookie-refresh endpoints accept exactly the refresh key.

        Resolution mirrors refresh_key(): cookie_refresh_key, else api_keys[0],
        else the "sk-gemini" default - so a custom key in config.json works for
        the extension and cookie_autorefresh.py without code changes.
        With NO keys configured the endpoints stay open, matching the /v1
        convention (empty api_keys = no auth).
        """
        if not CONFIG.get("api_keys") and not CONFIG.get("cookie_refresh_key"):
            return True
        k = req.get("key") or self.headers.get("X-API-Key", "")
        return k == refresh_key()

    def _handle_refresh_request(self, body: bytes):
        req = self._parse_body(body) or {}
        if not self._internal_key_ok(req):
            self.send_json({"error": "invalid api key"}, 401)
            return
        request_cookie_refresh(req.get("reason", "manual"))
        self.send_json({"requested": True})

    def _handle_refresh_verify(self, body: bytes):
        """Key check with NO side effects (used by the popup's Test connection)."""
        req = self._parse_body(body) or {}
        if not self._internal_key_ok(req):
            self.send_json({"error": "invalid api key"}, 401)
            return
        self.send_json({"ok": True})

    def _handle_refresh_upload(self, body: bytes):
        req = self._parse_body(body) or {}
        if not self._internal_key_ok(req):
            self.send_json({"error": "invalid api key"}, 401)
            return
        cookie = req.get("cookie", "")
        if not cookie:
            self.send_json({"error": "empty cookie"}, 400)
            return
        # Defensive: never clobber a working cookie.txt with a session that
        # lacks any real session cookie (bad upload, wrong account, buggy
        # extension, or an api_keys mismatch). Log a clear warning, reject.
        # Match on cookie NAMES, not substrings, so a value containing
        # "SID=" can never false-positive.
        names = {p.split("=", 1)[0] for p in cookie.split("; ") if "=" in p}
        if not (names & {"SID", "__Secure-1PSID", "__Secure-3PSID"}):
            log("Cookie refresh: upload rejected - missing a session cookie "
                "(SID/__Secure-1PSID) in the upload")
            self.send_json(
                {"error": "upload missing a session cookie "
                          "(SID/__Secure-1PSID/__Secure-3PSID)"}, 400)
            return
        payload = {"cookie": cookie}
        if req.get("sapisid"):
            payload["sapisid"] = req["sapisid"]
        cookie_file = CONFIG.get("cookie_file")
        if cookie_file:
            try:
                os.makedirs(os.path.dirname(cookie_file) or ".", exist_ok=True)
                with open(cookie_file, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                log(f"Cookie refresh: wrote {len(cookie.split('; '))} cookies to {cookie_file}")
            except OSError as e:
                self.send_json({"error": f"write failed: {e}"}, 500)
                return
        else:
            log("Cookie refresh: received cookies but no cookie_file configured - not saved")
        if req.get("xsrf_token"):
            CONFIG["xsrf_token"] = req["xsrf_token"]
            log("XSRF token updated from cookie refresh")
        if req.get("gemini_bl") and req["gemini_bl"] != CONFIG.get("gemini_bl"):
            CONFIG["gemini_bl"] = req["gemini_bl"]
            log(f"gemini_bl updated to {CONFIG['gemini_bl']}")
        if req.get("auth_user") is not None and req["auth_user"] != "":
            CONFIG["auth_user"] = req["auth_user"]
            log(f"auth_user updated to {CONFIG['auth_user']}")
        clear_cookie_refresh()
        self.send_json({"ok": True})

    # ─── Internal: image bridge (extension contract) ─────────────────────────
    # GET  /internal/image-bridge/request -> parked image request, if any
    # POST /internal/image-bridge/claim   -> one processor takes the request
    # POST /internal/image-bridge/result  -> the answer comes back
    # The extension polls /request every ~30s and processes the request in a
    # real gemini.google.com window (its fully-authenticated session is the
    # only context Google lets process uploaded images).

    def _handle_bridge_request(self):
        if not self._internal_key_ok({}):
            self.send_json({"error": "invalid api key"}, 401)
            return
        self.send_json(pending_request())

    def _handle_bridge_claim(self, body: bytes):
        req = self._parse_body(body) or {}
        if not self._internal_key_ok(req):
            self.send_json({"error": "invalid api key"}, 401)
            return
        rid = req.get("id", "")
        if claim(rid):
            self.send_json({"ok": True})
        else:
            self.send_json({"ok": False, "error": "no unclaimed request with that id"}, 409)

    def _handle_bridge_result(self, body: bytes):
        req = self._parse_body(body) or {}
        if not self._internal_key_ok(req):
            self.send_json({"error": "invalid api key"}, 401)
            return
        rid = req.get("id", "")
        ext_version = req.get("ext_version") or req.get("version")
        ok = submit_result(rid, bool(req.get("ok")),
                           req.get("text", ""), req.get("error", ""),
                           ext_version=ext_version)
        # Log the extension build that produced this result - a stale-looking
        # version here is the reload warning, visible at a glance.
        log(f"Image bridge: result ok={bool(req.get('ok'))} "
            f"ext={ext_version or 'unknown'} for {rid}")
        if ok:
            self.send_json({"ok": True})
        else:
            self.send_json({"ok": False, "error": "unknown/unclaimed request id"}, 409)

    def _handle_bridge_expire(self, body: bytes):
        """Watchdog recovery: expire an abandoned claim (> min_age_sec old).

        Loopback-only (like /internal/cookie-refresh/config): the watchdog is
        localhost by definition and carries no API key, and a remote caller
        must not be able to cancel image processing."""
        if not self._is_loopback():
            self.send_json({"error": "loopback only"}, 403)
            return
        req = self._parse_body(body) or {}
        try:
            min_age = float(req.get("min_age_sec")) if req.get("min_age_sec") is not None else None
        except (TypeError, ValueError):
            min_age = None
        self.send_json(expire_stale(min_age))

    # ─── /v1/chat/completions ─────────────────────────────────────────────────

    def _handle_chat(self, body: bytes):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        model_name, model_id, think_mode, err, extra_fields = resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        tools = req.get("tools")
        tool_choice = req.get("tool_choice", "auto")
        prompt, images = messages_to_prompt(req.get("messages", []), tools, tool_choice)
        # Agentic clients (OpenAI SDKs, AionUI, Claude Code...) send
        # response_format to request JSON. Gemini-web cannot enforce a schema
        # server-side, so we add a soft non-coercive note - the same pattern
        # as tool_choice - so the model actually emits valid JSON.
        rf = req.get("response_format") or {}
        if isinstance(rf, dict) and rf.get("type") == "json_object":
            prompt += ("\n\nNote: respond with a single valid JSON object only - "
                       "no markdown fences, no text outside the JSON.")
        elif isinstance(rf, dict) and rf.get("type") == "json_schema" and isinstance(rf.get("json_schema"), dict):
            schema = rf["json_schema"].get("schema") or rf["json_schema"]
            prompt += ("\n\nNote: respond with a single valid JSON object matching this "
                       "schema (no markdown fences):\n" + json.dumps(schema, ensure_ascii=False))
        # Image-only messages are legal (the web UI allows them): an empty
        # text prompt is fine as long as an image is attached.
        if not prompt.strip() and not images:
            self.send_json({"error": {"message": "empty prompt"}}, 400)
            return

        stream = req.get("stream", False)
        include_usage = bool((req.get("stream_options") or {}).get("include_usage", False))
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        # Image requests that may go through the browser bridge cannot stream
        # token-by-token (the bridge returns the finished answer) - route them
        # through the non-stream path, which still emits valid SSE when
        # stream=true (single chunk + [DONE]).
        need_bridge = bool(images) and image_bridge_enabled()

        if stream and (not tools or tool_choice == "none") and not need_bridge:
            try:
                self._start_sse()
                full_text = ""
                try:
                    for delta in generate_stream(prompt, model_id, think_mode, _upload_images(images), extra_fields):
                        full_text += delta
                        chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                                 "model": model_name, "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}]}
                        self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                        self.wfile.flush()
                except Exception as e:
                    # Client hung up (incl. Windows ConnectionAbortedError 10053):
                    # let the outer handler swallow it silently - no error frame.
                    if isinstance(e, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
                        raise
                    # Upstream failed mid-stream: emit a valid SSE error frame
                    # instead of letting a raw JSON 500 body leak into the
                    # already-started event stream.
                    try:
                        err = {"error": {"message": f"upstream error: {e}"}}
                        self.wfile.write(f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode())
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        pass
                    return
                end = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                       "model": model_name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                self.wfile.write(f"data: {json.dumps(end)}\n\n".encode())
                if include_usage:
                    usage = {"prompt_tokens": len(prompt)//4, "completion_tokens": len(full_text)//4,
                             "total_tokens": (len(prompt)+len(full_text))//4}
                    usage_chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                                   "model": model_name, "choices": [], "usage": usage}
                    self.wfile.write(f"data: {json.dumps(usage_chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return

        try:
            text = _run_generation(prompt, model_id, think_mode, images,
                                   extra_fields, model_name)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        tool_calls = None
        if tools and text and tool_choice != "none":
            text, tool_calls = parse_tool_calls(text)
        # tool_choice requires a tool: hard-enforce it with escalating retries;
        # never silently degrade a required tool call into plain text.
        requires_tool = tool_choice == "required" or isinstance(tool_choice, dict)
        if tools and requires_tool and not tool_calls:
            try:
                text, tool_calls = _force_tool_call(
                    prompt, model_id, think_mode, images, extra_fields,
                    model_name, tool_choice, normalize_tool_defs(tools),
                    parse_tool_calls)
            except RuntimeError as e:
                self.send_json({"error": {"message": f"tool call required but not produced: {e}"}}, 502)
                return
        msg = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else "stop"

        if stream:
            self._start_sse()
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": model_name, "choices": [{"index": 0, "delta": msg, "finish_reason": finish}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            if include_usage:
                usage = {"prompt_tokens": len(prompt)//4, "completion_tokens": len(text or "")//4,
                         "total_tokens": (len(prompt)+len(text or ""))//4}
                usage_chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                               "model": model_name, "choices": [], "usage": usage}
                self.wfile.write(f"data: {json.dumps(usage_chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self.send_json({
                "id": cid, "object": "chat.completion", "created": int(time.time()),
                "model": model_name,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": {"prompt_tokens": len(prompt)//4, "completion_tokens": len(text or "")//4,
                          "total_tokens": (len(prompt)+len(text or ""))//4},
            })

    # ─── /v1/responses (Codex CLI) ───────────────────────────────────────────

    def _handle_responses(self, body: bytes):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        model_name, model_id, think_mode, err, extra_fields = resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        input_items = req.get("input", [])
        tools = req.get("tools")
        messages = []
        if req.get("instructions"):
            messages.append({"role": "system", "content": req["instructions"]})
        if isinstance(input_items, str):
            messages.append({"role": "user", "content": input_items})
        elif isinstance(input_items, list):
            for item in input_items:
                if isinstance(item, str):
                    messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    if item.get("type") == "function_call_output":
                        messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""),
                                         "name": item.get("name", ""), "content": item.get("output", "")})
                    elif item.get("role") == "assistant" or (item.get("type") == "message" and item.get("role") == "assistant"):
                        cp = item.get("content", [])
                        text_acc, tc_list = "", []
                        if isinstance(cp, list):
                            for c in cp:
                                if isinstance(c, dict):
                                    if c.get("type") == "output_text": text_acc += c.get("text", "")
                                    elif c.get("type") == "function_call": tc_list.append(c)
                        elif isinstance(cp, str):
                            text_acc = cp
                        m = {"role": "assistant", "content": text_acc or None}
                        if tc_list:
                            m["tool_calls"] = [{"id": tc.get("call_id", f"call_{i}"), "type": "function",
                                                "function": {"name": tc.get("name",""), "arguments": tc.get("arguments","{}")}}
                                               for i, tc in enumerate(tc_list)]
                        messages.append(m)
                    else:
                        role = item.get("role", "user")
                        content = item.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(c.get("text", "") for c in content if c.get("type") in ("text", "input_text"))
                        messages.append({"role": role, "content": content})

        if tools:
            tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}}
                     if t.get("type") == "function" and "function" not in t else t for t in tools]

        tool_choice = req.get("tool_choice", "auto")
        prompt, images = messages_to_prompt(messages, tools, tool_choice)
        # Image-only input is legal (mirrors the web UI / chat completions).
        if not prompt.strip() and not images:
            self.send_json({"error": {"message": "empty input"}}, 400)
            return

        try:
            text = _run_generation(prompt, model_id, think_mode, images,
                                   extra_fields, model_name)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        tool_calls = None
        if tools and text and tool_choice != "none":
            text, tool_calls = parse_tool_calls(text)
        # tool_choice requires a tool: hard-enforce it with escalating retries;
        # never silently degrade a required tool call into plain text.
        requires_tool = tool_choice == "required" or isinstance(tool_choice, dict)
        if tools and requires_tool and not tool_calls:
            try:
                text, tool_calls = _force_tool_call(
                    prompt, model_id, think_mode, images, extra_fields,
                    model_name, tool_choice, normalize_tool_defs(tools),
                    parse_tool_calls)
            except RuntimeError as e:
                self.send_json({"error": {"message": f"tool call required but not produced: {e}"}}, 502)
                return

        rid = f"resp_{uuid.uuid4().hex[:16]}"
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        output = []
        if tool_calls:
            for tc in tool_calls:
                output.append({"type": "function_call", "id": tc["id"], "call_id": tc["id"],
                               "name": tc["function"]["name"], "arguments": tc["function"]["arguments"], "status": "completed"})
        if text or not tool_calls:
            output.append({"type": "message", "id": mid, "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": text or "", "annotations": []}]})

        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            seq = [0]

            def emit(ev_type, **fields):
                seq[0] += 1
                ev = {"type": ev_type, "sequence_number": seq[0], **fields}
                self.wfile.write(f"event: {ev_type}\ndata: {json.dumps(ev)}\n\n".encode())

            usage = {"input_tokens": len(prompt)//4, "output_tokens": len(text or "")//4,
                     "total_tokens": (len(prompt)+len(text or ""))//4}
            base_resp = {"id": rid, "object": "response", "created_at": int(time.time()), "model": model_name}
            emit("response.created", response={**base_resp, "status": "in_progress", "output": [], "usage": None})
            emit("response.in_progress", response={**base_resp, "status": "in_progress", "output": [], "usage": None})
            for oi, item in enumerate(output):
                if item["type"] == "function_call":
                    pending = {"type": "function_call", "id": item["id"], "call_id": item["call_id"],
                               "name": item["name"], "arguments": "", "status": "in_progress"}
                    emit("response.output_item.added", output_index=oi, item=pending)
                    emit("response.function_call_arguments.delta", item_id=item["id"], output_index=oi, delta=item["arguments"])
                    emit("response.function_call_arguments.done", item_id=item["id"], output_index=oi, arguments=item["arguments"])
                    emit("response.output_item.done", output_index=oi, item=item)
                elif item["type"] == "message":
                    pending = {"type": "message", "id": item["id"], "role": "assistant", "status": "in_progress", "content": []}
                    emit("response.output_item.added", output_index=oi, item=pending)
                    for ci, cp in enumerate(item["content"]):
                        emit("response.content_part.added", item_id=item["id"], output_index=oi, content_index=ci,
                             part={"type": "output_text", "text": "", "annotations": []})
                        emit("response.output_text.delta", item_id=item["id"], output_index=oi, content_index=ci, delta=cp["text"])
                        emit("response.output_text.done", item_id=item["id"], output_index=oi, content_index=ci, text=cp["text"])
                        emit("response.content_part.done", item_id=item["id"], output_index=oi, content_index=ci, part=cp)
                    emit("response.output_item.done", output_index=oi, item=item)
            emit("response.completed", response={**base_resp, "status": "completed", "output": output, "usage": usage})
            self.wfile.flush()
        else:
            self.send_json({"id": rid, "object": "response", "created_at": int(time.time()), "status": "completed",
                            "model": model_name, "output": output,
                            "usage": {"input_tokens": len(prompt)//4, "output_tokens": len(text or "")//4, "total_tokens": (len(prompt)+len(text or ""))//4}})

    # ─── /v1beta/models (Google Gemini CLI) ──────────────────────────────────

    def _handle_google_generate(self, body: bytes, stream: bool):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        m = re.match(r'/v1beta/models/([^:?]+)', self.path)
        model_name = m.group(1) if m else CONFIG["default_model"]
        model_name, model_id, think_mode, err, extra_fields = resolve_model(model_name)
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        tool_config = req.get("toolConfig", {})
        fc_mode = tool_config.get("functionCallingConfig", {}).get("mode", "AUTO")
        has_tools = bool(req.get("tools")) and fc_mode != "NONE"
        prompt, images = google_contents_to_prompt(req)
        # Image-only content is legal (mirrors the web UI / chat completions).
        if not prompt.strip() and not images:
            self.send_json({"error": {"message": "empty content"}}, 400)
            return

        need_bridge = bool(images) and image_bridge_enabled()
        if stream and not has_tools and not need_bridge:
            try:
                self._start_sse()
                full_text = ""
                try:
                    for delta in generate_stream(prompt, model_id, think_mode, _upload_images(images), extra_fields):
                        if not delta:
                            continue
                        full_text += delta
                        chunk_obj = {
                            "candidates": [{"content": {"parts": [{"text": delta}], "role": "model"}, "index": 0}],
                            "modelVersion": model_name,
                        }
                        self.wfile.write(f"data: {json.dumps(chunk_obj, ensure_ascii=False)}\n\n".encode())
                        self.wfile.flush()
                except Exception as e:
                    if isinstance(e, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
                        raise
                    # Valid SSE error frame for the native protocol - never a
                    # raw JSON body inside the event stream.
                    err_chunk = {"candidates": [{"finishReason": "ERROR", "index": 0}],
                                 "error": {"message": f"upstream error: {e}"},
                                 "modelVersion": model_name}
                    try:
                        self.wfile.write(f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n".encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        pass
                    return
                final_chunk = {
                    "candidates": [{"finishReason": "STOP", "index": 0}],
                    "usageMetadata": {
                        "promptTokenCount": len(prompt) // 4,
                        "candidatesTokenCount": len(full_text) // 4,
                        "totalTokenCount": (len(prompt) + len(full_text)) // 4,
                    },
                    "modelVersion": model_name,
                }
                self.wfile.write(f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return

        log(f"Google API: model={model_name} stream={stream} tools={has_tools} prompt_len={len(prompt)}")

        try:
            text = _run_generation(prompt, model_id, think_mode, images,
                                   extra_fields, model_name)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        if not text:
            log("Warning: empty response from Gemini")

        response_parts = []
        if has_tools and text:
            clean_text, function_calls = parse_google_function_calls(text)
            if not function_calls and fc_mode == "ANY":
                # Native mode=ANY is a hard requirement: escalate until a
                # function call is produced instead of downgrading to text.
                tool_defs = [{"name": n, "description": "", "parameters": {}}
                             for n in _google_tool_names(req)]
                try:
                    clean_text, function_calls = _force_tool_call(
                        prompt, model_id, think_mode, images, extra_fields,
                        model_name, "required", tool_defs,
                        parse_google_function_calls)
                    text = clean_text
                except RuntimeError as e:
                    self.send_json({"error": {"message": f"function call required but not produced: {e}"}}, 502)
                    return
            if function_calls:
                if clean_text:
                    response_parts.append({"text": clean_text})
                for fc in function_calls:
                    response_parts.append({"functionCall": {"name": fc["name"], "args": fc["args"]}})
            else:
                response_parts.append({"text": text})
        else:
            response_parts.append({"text": text or "I apologize, but I was unable to generate a response. Please try again."})

        candidate = {
            "content": {"parts": response_parts, "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
        usage = {
            "promptTokenCount": len(prompt) // 4,
            "candidatesTokenCount": len(text or "") // 4,
            "totalTokenCount": (len(prompt) + len(text or "")) // 4,
        }
        response_obj = {
            "candidates": [candidate],
            "usageMetadata": usage,
            "modelVersion": model_name,
        }

        if stream:
            self._start_sse()
            self.wfile.write(f"data: {json.dumps(response_obj, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
        else:
            self.send_json(response_obj)


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
