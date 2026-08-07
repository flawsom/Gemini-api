"""Unit test for the proxy-plan iteration in multimodal.upload_image.

Regression guard for the fix that routes image uploads through the SAME
proxy plan as chat (configured proxy -> recently-working fallback -> direct
-> fallbacks) instead of dying when the single configured proxy is down:
chat self-heals via _proxy_plan() and now the Scotty upload does too, for
both the handshake AND the data push.

Stub-based, no network: urllib.request.build_opener / urlopen are mocked,
the page-token cache is pre-seeded, and the SSL context + cookie loader are
stubbed. Mirrors test_proxy_fallback.py.

Run:  python test_multimodal_proxy.py
"""
import os
import sys
import time
import urllib.error
import urllib.request as ur

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gemini_web2api.multimodal as mm
import gemini_web2api.gemini as gem

# Stash the real package state so nothing leaks if tests are ever collected
# in-process; the mutations below are process-lifetime-safe today but restore
# everything in the finally anyway.
_orig_load_cookie = mm.load_cookie
_orig_get_ssl_ctx = mm._get_ssl_ctx
_orig_cache = dict(mm._page_tokens_cache)
_orig_proxy = gem.CONFIG.get("proxy")
_orig_fallbacks = gem.CONFIG.get("proxy_fallbacks")
_orig_proxy_state = dict(gem._proxy_state)

# Pre-seed the real 600s page-token cache so _cached_page_tokens() never
# fetches the network; stub the cookie loader and SSL context.
mm._page_tokens_cache.update({
    "tokens": {"push_id": "feeds/test", "pctx": "CgcTest"},
    "ts": time.time(),
})
mm.load_cookie = lambda: ("", None)
mm._get_ssl_ctx = lambda: None

# ── fake urllib layer ─────────────────────────────────────────────────────
START = "https://content-push.googleapis.com/upload/"
UPLOAD_URL = "https://upload.example.com/ref"
FILE_REF = "/contrib_service/abc123"
P1 = "http://127.0.0.1:7890"   # configured proxy
P2 = "http://p2:8080"          # fallback proxy

calls = []        # (kind, proxy, url) for every network attempt
FAIL = set()      # proxies that refuse the connection
DIRECT_FAIL = False


class FakeResp:
    def __init__(self, headers=None, body=None):
        self.headers = headers if headers is not None else {}
        self._body = (body or FILE_REF).encode()

    def read(self):
        return self._body


class FakeOpener:
    """Opener for ONE proxy route; fails it if the route is in FAIL."""
    def __init__(self, proxy):
        self.proxy = proxy

    def open(self, req, timeout=30):
        calls.append(("opener", self.proxy, req.full_url))
        if self.proxy in FAIL:
            raise OSError(f"[WinError 10061] proxy {self.proxy} refused")
        if req.full_url.startswith(START):
            return FakeResp(headers={"X-Goog-Upload-URL": UPLOAD_URL})
        return FakeResp()  # data push -> file reference


def fake_build_opener(*args, **kwargs):
    for a in args:
        if isinstance(a, ur.ProxyHandler):
            d = getattr(a, "proxies", {})
            return FakeOpener(d.get("https") or d.get("http"))
    return FakeOpener(None)


def fake_urlopen(req, context=None, timeout=30):
    calls.append(("direct", None, req.full_url))
    if DIRECT_FAIL:
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
    if req.full_url.startswith(START):
        return FakeResp(headers={"X-Goog-Upload-URL": UPLOAD_URL})
    return FakeResp()


def reset(proxy, fallbacks, fail=(), direct_fail=False):
    global DIRECT_FAIL
    gem.CONFIG["proxy"] = proxy
    gem.CONFIG["proxy_fallbacks"] = list(fallbacks)
    gem._proxy_state.update({"working": None, "ts": 0})
    FAIL.clear()
    FAIL.update(fail)
    DIRECT_FAIL = direct_fail
    calls.clear()


orig_build, orig_open = ur.build_opener, ur.urlopen
ur.build_opener = fake_build_opener
ur.urlopen = fake_urlopen

try:
    # ── Test 1: configured proxy down -> direct succeeds, fallback untouched ─
    # plan: [P1, direct, P2]
    reset(P1, [P2], fail={P1})
    ref = mm.upload_image(b"img", "a.png", "image/png")
    assert ref == FILE_REF, ref
    routes = [c[1] for c in calls]
    assert routes == [P1, None, None], routes          # P1 failed, direct did both steps
    assert calls[0] == ("opener", P1, START), calls[0]
    assert calls[-1] == ("direct", None, UPLOAD_URL), calls[-1]  # push over the same route
    assert gem._proxy_state["working"] is None, gem._proxy_state  # direct: not remembered
    print("Test 1 OK: configured proxy down -> upload falls through to direct, same route")

    # ── Test 2: proxy AND direct down -> fallback proxy succeeds ────────────
    reset(P1, [P2], fail={P1}, direct_fail=True)
    ref = mm.upload_image(b"img", "a.png", "image/png")
    assert ref == FILE_REF, ref
    assert [c[1] for c in calls] == [P1, None, P2, P2], \
        [(c[0], c[1], c[2][:40]) for c in calls]
    assert calls[-1] == ("opener", P2, UPLOAD_URL), calls[-1]  # push via P2 too
    assert gem._proxy_state["working"] == P2, gem._proxy_state  # fallback remembered
    print("Test 2 OK: proxy + direct down -> fallback proxy carries the whole upload")

    # ── Test 3: working proxy promoted before direct, all down -> clear error ─
    # plan after P2 remembered: [P1, P2, direct]
    reset(P1, [P2], fail={P1, P2}, direct_fail=True)
    gem._mark_proxy_working(P2)   # simulate the remembered state from Test 2
    try:
        mm.upload_image(b"img", "a.png", "image/png")
        assert False, "all routes down should raise"
    except Exception as e:
        assert str(e), "error should carry a message"
    routes = [c[1] for c in calls]
    assert routes == [P1, P2, None], routes   # working proxy tried BEFORE direct
    assert gem._proxy_state["working"] == P2, gem._proxy_state  # not clobbered by failures
    print("Test 3 OK: all routes down -> clear error after full plan, working promoted first")

    # ── Test 4: no proxy configured -> direct only ──────────────────────────
    reset(None, [])
    ref = mm.upload_image(b"img", "a.png", "image/png")
    assert ref == FILE_REF, ref
    assert [c[0] for c in calls] == ["direct", "direct"], calls
    assert gem._proxy_state["working"] is None, gem._proxy_state
    print("Test 4 OK: no proxy config -> direct-only upload")

    # ── Test 5: missing upload URL is retried on the next route, not fatal ──
    # The start succeeds but returns no X-Goog-Upload-URL on P1 (a dead
    # handshake); the upload must move on to direct instead of aborting.
    class NoURLResp:
        headers = {}
    orig_opener_open = FakeOpener.open
    def _no_url_open(self, req, timeout=30):
        calls.append(("opener", self.proxy, req.full_url))
        if self.proxy in FAIL:
            raise OSError(f"[WinError 10061] proxy {self.proxy} refused")
        if req.full_url.startswith(START):
            return NoURLResp()
        return FakeResp()
    FakeOpener.open = _no_url_open
    try:
        reset(P1, [], fail=set())
        ref = mm.upload_image(b"img", "a.png", "image/png")
        assert ref == FILE_REF, ref
        routes = [c[1] for c in calls]
        assert routes == [P1, None, None], routes   # P1 handshake failed -> direct took over
        print("Test 5 OK: dead handshake on proxy retried on direct")
    finally:
        FakeOpener.open = orig_opener_open

    print("ALL TESTS PASSED")
finally:
    ur.build_opener = orig_build
    ur.urlopen = orig_open
    mm.load_cookie = _orig_load_cookie
    mm._get_ssl_ctx = _orig_get_ssl_ctx
    mm._page_tokens_cache.clear()
    mm._page_tokens_cache.update(_orig_cache)
    gem.CONFIG["proxy"] = _orig_proxy
    gem.CONFIG["proxy_fallbacks"] = _orig_fallbacks
    gem._proxy_state.clear()
    gem._proxy_state.update(_orig_proxy_state)
