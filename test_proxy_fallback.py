"""Unit test for the 429 -> proxy fallback logic in gemini_web2api.py (stub-based, no network)."""
import importlib.util
import json
import time
import urllib.error
import urllib.request as ur

spec = importlib.util.spec_from_file_location("gw2a", "gemini_web2api.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# ── Test 1: proxy plan ordering ──────────────────────────────────────────────
mod.CONFIG["proxy"] = "http://127.0.0.1:7890"
mod.CONFIG["proxy_fallbacks"] = ["http://p2:8080", "http://p3:8080"]
mod._proxy_state.update({"working": None, "ts": 0})
plan = mod._proxy_plan()
assert plan == ["http://127.0.0.1:7890", None, "http://p2:8080", "http://p3:8080"], plan
print("Test 1 OK: plan ordering (config proxy, direct, fallbacks) ->", plan)

# ── Test 2: working proxy promoted before direct, expiry demotes it ─────────
mod._mark_proxy_working("http://p2:8080")
plan = mod._proxy_plan()
assert plan == ["http://127.0.0.1:7890", "http://p2:8080", None, "http://p3:8080"], plan
mod._proxy_state["ts"] = time.time() - 2000  # expire (>30 min)
plan = mod._proxy_plan()
assert plan == ["http://127.0.0.1:7890", None, "http://p2:8080", "http://p3:8080"], plan
mod._proxy_state["ts"] = time.time()
print("Test 2 OK: working proxy promoted before direct + expiry demotes it")

# ── Test 3: no proxies configured -> direct only ────────────────────────────
mod.CONFIG["proxy"] = None
mod.CONFIG["proxy_fallbacks"] = []
mod._proxy_state.update({"working": None, "ts": 0})
assert mod._proxy_plan() == [None]
assert mod._proxy_for_attempt(0) is None
assert mod._proxy_for_attempt(5) is None
print("Test 3 OK: no proxy config -> all attempts direct")

# ── Test 4: 429 triggers proxy switch inside the retry loop ─────────────────
mod.CONFIG.update({
    "proxy": None,
    "proxy_fallbacks": ["http://127.0.0.1:7890"],
    "gemini_bl": "bl_test",
    "auth_user": "1",
    "xsrf_token": "tok",
    "cookie_file": None,
    "retry_attempts": 3,
    "retry_delay_sec": 0,
    "request_timeout_sec": 30,
})
mod._proxy_state.update({"working": None, "ts": 0})

calls = []

# A realistic wrb.fr response so generate() can parse real text out of it.
_inner = [None, "pad" + "x" * 300, None, None,
          [["rc_1", ["HELLO WORLD"], None, None, None]]]
_line = json.dumps([["wrb.fr", None, json.dumps(_inner)]])


class FakeResp:
    def read(self):
        return (str(len(_line)) + "\n" + _line).encode()

class FakeOpener:
    """429 on direct, success on proxy (simulates Google gating the direct IP)."""
    def __init__(self, proxy):
        self.proxy = proxy
    def open(self, req, timeout=30):
        calls.append(self.proxy)
        if self.proxy is None:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
        return FakeResp()

def fake_build_opener(*args, **kwargs):
    for a in args:
        if isinstance(a, ur.ProxyHandler):
            d = getattr(a, "proxies", {})
            return FakeOpener(d.get("https") or d.get("http"))
    return FakeOpener(None)

def fake_urlopen(req, context=None, timeout=30):
    calls.append(None)
    raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

orig_build, orig_open = mod.urllib.request.build_opener, mod.urllib.request.urlopen
try:
    mod.urllib.request.build_opener = fake_build_opener
    mod.urllib.request.urlopen = fake_urlopen
    out = mod.generate("hello", 1, 0)
    assert out == "HELLO WORLD", out
    assert calls[0] is None, calls          # direct tried first
    assert calls[1] == "http://127.0.0.1:7890", calls  # then proxy fallback
    assert mod._proxy_state["working"] == "http://127.0.0.1:7890", mod._proxy_state
    print("Test 4 OK: 429 on direct -> retried via fallback proxy, proxy remembered")

    # ── Test 5: after remembering, next request prefers the working proxy ────
    calls.clear()
    out = mod.generate("hello again", 1, 0)
    assert out == "HELLO WORLD", out
    assert calls[0] == "http://127.0.0.1:7890", calls  # working proxy first, no wasted 429
    print("Test 5 OK: subsequent requests use the known-good proxy first")
finally:
    mod.urllib.request.build_opener = orig_build
    mod.urllib.request.urlopen = orig_open

# ── Test 6: 429 + unreachable proxy -> clear RuntimeError, not raw 10061 ─────
def fake_build_opener_dead(*args, **kwargs):
    for a in args:
        if isinstance(a, ur.ProxyHandler):
            d = getattr(a, "proxies", {})
            if d.get("https") or d.get("http"):
                class DeadOpener:
                    def open(self, req, timeout=30):
                        raise OSError("[WinError 10061] No connection could be made because the target machine actively refused it")
                return DeadOpener()
    return FakeOpener(None)

mod.CONFIG.update({"proxy": None, "proxy_fallbacks": ["http://127.0.0.1:7890"]})
mod._proxy_state.update({"working": None, "ts": 0})
mod.urllib.request.build_opener = fake_build_opener_dead
mod.urllib.request.urlopen = fake_urlopen
try:
    try:
        mod.generate("boom", 1, 0)
        assert False, "should have raised"
    except RuntimeError as e:
        msg = str(e)
        assert "429" in msg and "proxy" in msg and "proxy_fallbacks" in msg, msg
        print("Test 6 OK: 429 + dead proxy -> clear RuntimeError hint:", msg[:80], "...")
finally:
    mod.urllib.request.build_opener = orig_build
    mod.urllib.request.urlopen = orig_open

print("ALL TESTS PASSED")
