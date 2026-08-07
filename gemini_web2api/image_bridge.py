"""Image bridge: delegate image processing to the user's real browser session.

Google rejects uploaded-image requests that originate from exported cookies
(BardErrorInfo 1100) - image processing only works in a fully-authenticated
browser session. The Gemini Cookie Sync extension runs inside that session, so
when a direct image request fails with 1100 the server parks the request here
and the extension picks it up, processes it in a real gemini.google.com window
(attaching the image + sending the prompt), and uploads the answer back.

State is a JSON file next to this package (like cookie-refresh.flag) with an
age cap so a crashed server / unloaded extension can never leave a stale
pending request around forever. The extension talks to this module only via
the /internal/image-bridge endpoints in server.py.
"""
import json
import os
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "image-bridge-state.json")

# A pending request expires after this long even if nobody claims it (the
# extension polls every ~30s, so this is very generous).
PENDING_TTL_SEC = 420
# Server-side wait budget for the result (config override: image_bridge_timeout).
DEFAULT_WAIT_SEC = 240

# The server is threaded: all state transitions (register/claim/submit/cancel)
# are serialized behind one lock so two parallel image requests can never
# clobber each other's pending entry, and two pollers can never both claim.
_lock = threading.Lock()


class BridgeBusy(RuntimeError):
    """Raised by register() when another image request is still in flight.

    Only one image request can be processed by the browser at a time (the
    extension opens a single window); a second concurrent request is told to
    try again instead of silently replacing the first and hanging it."""


def _read() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def _expire(state: dict) -> dict:
    """Drop stale pending/claimed requests (age > PENDING_TTL_SEC).

    Pending is stamped with requested_at; claimed with claimed_at - a claimed
    entry that outlived its request (extension died mid-flight) must still
    expire so a later poll can offer a fresh request."""
    now = time.time()
    for key, stamp in (("pending", "requested_at"), ("claimed", "claimed_at")):
        req = state.get(key)
        if req and now - float(req.get(stamp, now)) > PENDING_TTL_SEC:
            state[key] = None
    return state


def _state() -> dict:
    state = _read()
    return _expire(state) if state else {}


# ─── server side ─────────────────────────────────────────────────────────────

def pending_request() -> dict:
    """The extension's poll contract: {"requested": bool, "request": {...}}.

    Only an UNCLAIMED, non-expired request is offered - once one extension
    claims it, later polls (and second instances) see requested=false."""
    st = _state()
    p = st.get("pending")
    if p and not st.get("claimed"):
        return {"requested": True, "request": p}
    return {"requested": False}


def register(prompt: str, images: list, model: str = None,
             timeout_ms: int = None) -> str:
    """Park an image request for the extension. Returns the bridge id.

    timeout_ms is the server's wait budget, forwarded to the content script so
    its in-browser answer wait matches how long the server will hold the
    request open (the extension cycle - poll + cold window load + answer -
    can legitimately take a few minutes).

    Raises BridgeBusy if another request is still pending/claimed - the
    browser processes one image at a time and a second registration would
    silently replace the first (hanging its client for the full timeout)."""
    with _lock:
        st = _state()
        if st.get("pending") or st.get("claimed"):
            raise BridgeBusy(
                "another image request is already being processed in the "
                "browser - try again in a moment")
        rid = uuid.uuid4().hex[:12]
        payload = {
            "id": rid,
            "prompt": prompt,
            "images": images,      # [{name, mime, data_b64}]
            "model": model,
            "timeout_ms": int(timeout_ms) if timeout_ms else None,
            "requested_at": time.time(),
        }
        st["pending"] = payload
        st["claimed"] = None
        st["result"] = None
        # last_result intentionally survives register(): the / health payload
        # reports the most recent extension outcome (incl. its version) so the
        # watchdog can detect a stale-extension result without a live request.
        _write(st)
        return rid


def wait_for_result(rid: str, timeout: int = None) -> str:
    """Block until the extension posts the result for `rid`.

    Returns the assistant text. Raises RuntimeError on extension error or
    timeout - the caller turns that into a clean API error.
    """
    timeout = timeout or DEFAULT_WAIT_SEC
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = _state()
        res = st.get("result") or {}
        if res.get("id") == rid:
            if res.get("ok"):
                return res.get("text", "")
            raise RuntimeError(res.get("error") or "image bridge reported a failure")
        time.sleep(1.5)
    cancel(rid)  # do not leave a dead request parked for the extension
    raise RuntimeError(
        "image bridge timed out - is the Gemini Cookie Sync extension "
        "installed and running? (image processing needs your real browser "
        "session; config: image_mode, image_bridge_timeout)"
    )


def cancel(rid: str):
    """Drop the parked request after a server-side wait gives up."""
    with _lock:
        st = _state()
        if (st.get("pending") or {}).get("id") != rid:
            return
        st["pending"] = None
        st["claimed"] = None
        st["result"] = None
        _write(st)


def bridge_health() -> dict:
    """Bridge state for the / health payload (watchdog introspection).

    Reports whether a request is parked and/or claimed, plus how long each has
    been sitting there, and the LAST extension outcome (ok/error/ext_version/
    ts) so the watchdog can detect a stale-extension result without running a
    live image request: a result posted by an extension build older than the
    on-disk manifest is a reload warning, not a mystery.

    The watchdog uses the claimed age to detect an abandoned claim (extension
    stuck / service worker suspended) and calls expire_stale() so the NEXT
    image request is not blocked by a dead claim. Reading state applies
    _expire, so a TTL-expired entry already self-heals."""
    st = _state()
    now = time.time()
    p, c = st.get("pending"), st.get("claimed")

    def _age(entry, stamp_key):
        # A hand-edited/corrupt state file must never 500 the health endpoint.
        try:
            return int(now - float(entry.get(stamp_key, now)))
        except (TypeError, ValueError):
            return None

    last = st.get("last_result")
    if last and isinstance(last, dict):
        # / is served without auth and with CORS open to the world - never
        # expose the full model answer. Truncate so the payload only carries
        # the outcome + version the watchdog needs.
        text = last.get("text") or ""
        last = {
            "ok": last.get("ok"),
            "error": last.get("error"),
            "text": text[:200] + ("..." if len(text) > 200 else ""),
            "ext_version": last.get("ext_version"),
            "ts": last.get("ts"),
        }
    else:
        last = None
    return {
        "pending": bool(p),
        "pending_age_sec": _age(p, "requested_at") if p else None,
        "claimed": bool(c),
        "claimed_age_sec": _age(c, "claimed_at") if c else None,
        "last_result": last,
    }


def expire_stale(min_age_sec: float = None) -> dict:
    """Force-expire a claimed request that is older than min_age_sec.

    The watchdog's recovery action: when a claim sits unanswered past its
    budget, the extension is gone (stuck window, suspended worker) and the
    server-side wait_for_result would otherwise run the full timeout while
    blocking the single bridge slot. This records a FAILURE result for the
    abandoned claim - which wakes any waiting server thread immediately with a
    clean error instead of after the full budget - and clears the slot so the
    next image request can register right away.

    Only CLAIMED requests are eligible: an unclaimed pending request simply
    hasn't been polled yet (the extension polls every ~30s) and must not be
    cancelled. Returns {"expired": bool, "id": str|None}.
    """
    min_age_sec = DEFAULT_WAIT_SEC if min_age_sec is None else min_age_sec
    with _lock:
        st = _state()
        c = st.get("claimed")
        if not c:
            return {"expired": False, "id": None}
        age = time.time() - float(c.get("claimed_at", time.time()))
        if age < min_age_sec:
            return {"expired": False, "id": c["id"], "age_sec": int(age)}
        rid = c["id"]
        now = time.time()
        st["result"] = {"id": rid, "ok": False,
                        "error": "image bridge claim expired - the extension "
                                  "did not finish in time (watchdog)",
                        "ts": now}
        # Mirror the failure into last_result (no ext_version: this came from
        # the watchdog, not the extension) so /health always reflects the most
        # recent bridge outcome - otherwise the stale-extension warning could
        # fire on an ANCIENT extension version long after the claim died.
        st["last_result"] = {"ok": False, "text": "",
                             "error": "image bridge claim expired - the "
                                       "extension did not finish in time "
                                       "(watchdog)",
                             "ext_version": None, "ts": now}
        st["pending"] = None
        st["claimed"] = None
        _write(st)
        return {"expired": True, "id": rid, "age_sec": int(age)}


# ─── extension contract (served via /internal/image-bridge/*) ───────────────

def claim(rid: str) -> bool:
    """The extension claims a pending request (only one processor wins)."""
    with _lock:
        st = _state()
        if not st.get("pending") or st.get("claimed"):
            return False
        if st["pending"].get("id") != rid:
            return False
        st["claimed"] = {"id": rid, "claimed_at": time.time()}
        _write(st)
        return True


def submit_result(rid: str, ok: bool, text: str = "", error: str = "",
                  ext_version: str = None) -> bool:
    """The extension posts the outcome for a claimed request.

    ext_version is the extension's own manifest version - recorded so the
    server log and the / health payload can tell at a glance whether a result
    came from stale extension code."""
    with _lock:
        st = _state()
        if not st.get("claimed") or st["claimed"].get("id") != rid:
            return False  # never accepted a result for an unclaimed/expired request
        result = {"id": rid, "ok": bool(ok), "text": text or "",
                  "error": error or "", "ts": time.time()}
        if ext_version:
            result["ext_version"] = str(ext_version)
        st["result"] = result
        # Keep the last outcome (incl. its version) past the next register so
        # /health can always report which extension build produced it.
        st["last_result"] = {k: result.get(k) for k in
                              ("ok", "text", "error", "ext_version", "ts")}
        # The request is finished: clear it so the next register() is free.
        st["pending"] = None
        st["claimed"] = None
        _write(st)
        return True
