"""Unit tests for autostart.py health summary (fetch + format).

Offline-safe: no server, no browser - format_health_summary is pure and
fetch_health uses a stubbed urlopen. Run: python test_autostart.py
"""
import contextlib
import importlib.util
import io
import urllib.request as ur

spec = importlib.util.spec_from_file_location("as_", "autostart.py")
as_ = importlib.util.module_from_spec(spec)
spec.loader.exec_module(as_)

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


def health(**kw):
    base = {"status": "ok", "gemini_bl": "bl_1", "bl_405_count": 0,
            "cookie": {"exists": True, "age_sec": 3600, "refresh_requested": False},
            "proxy": {"plan": [None, "http://127.0.0.1:7890"]}}
    base.update(kw)
    return base


# ── format_health_summary: fields ────────────────────────────────────────
s = as_.format_health_summary(health())
check("fresh summary shows build label", "bl_1" in s, s)
check("fresh summary shows cookie age", "1.0h" in s and "(3600s)" in s, s)
check("fresh summary shows 405 streak", "405 streak:" in s and "0" in s, s)
check("fresh summary shows refresh state", "not in flight" in s, s)
check("fresh summary shows proxy plan", "7890" in s, s)
check("fresh summary has no warnings", "WARNING" not in s, s)

# ── format_health_summary: edge cases ────────────────────────────────────
stale = health(cookie={"exists": True, "age_sec": 30 * 3600,
                       "refresh_requested": False})
s = as_.format_health_summary(stale)
check("stale cookies -> warning with refresh cmd",
      "WARNING" in s and "manage.bat cookies" in s and "30.0h" in s, s)

s = as_.format_health_summary(
    health(cookie={"exists": False, "age_sec": None, "refresh_requested": False}))
check("no cookie file -> n/a, no warning",
      "n/a (no cookie file)" in s and "WARNING" not in s, s)

s = as_.format_health_summary(
    health(cookie={"exists": True, "age_sec": 1000, "refresh_requested": True}))
check("refresh in flight shown", "IN FLIGHT" in s, s)

s = as_.format_health_summary(health(bl_405_count=5))
check("405 storm -> warning", "405-ing" in s, s)

# ── fetch_health: stubbed urlopen ────────────────────────────────────────
class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b'{"status": "ok", "bl_405_count": 1}'


class _Bad:
    status = 500

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b""


_orig = ur.urlopen
ur.urlopen = lambda req, timeout=None: _Resp()
d = as_.fetch_health(8081)
check("fetch_health parses a 200 payload", d and d.get("bl_405_count") == 1, str(d))
ur.urlopen = lambda req, timeout=None: _Bad()
check("fetch_health non-200 -> None", as_.fetch_health(8081) is None)
ur.urlopen = lambda req, timeout=None: (_ for _ in ()).throw(OSError("refused"))
check("fetch_health unreachable -> None", as_.fetch_health(8081) is None)
ur.urlopen = _orig

# ── health(): exit codes ─────────────────────────────────────────────────
_orig_fetch = as_.fetch_health
as_.fetch_health = lambda port=8081, timeout=5: None
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = as_.health(8081)
check("health() unreachable -> exit 1 + hint", rc == 1 and "Could not reach" in buf.getvalue(),
      buf.getvalue())
as_.fetch_health = lambda port=8081, timeout=5: health()
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    rc = as_.health(8081)
check("health() ok -> exit 0 + summary", rc == 0 and "Health (http://127.0.0.1:8081/)" in buf2.getvalue(),
      buf2.getvalue())
as_.fetch_health = _orig_fetch

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
