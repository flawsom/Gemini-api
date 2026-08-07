"""Tests for cookie_autorefresh.py - unit + live CDP plumbing.

Run: python test_cookie_refresh.py
"""
import importlib.util
import os
import sys
import tempfile
import time

OFFLINE = "--offline" in sys.argv  # skip live registry + CDP (CI/no browser)
PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


spec = importlib.util.spec_from_file_location("car", "cookie_autorefresh.py")
car = importlib.util.module_from_spec(spec)
spec.loader.exec_module(car)

# ─── unit: cookie scoring / picking ─────────────────────────────────────────
print("unit: cookie selection")
CDP_COOKIES = [
    {"name": "SID", "domain": ".google.com", "path": "/", "value": "good",
     "secure": True, "httpOnly": True},
    {"name": "SID", "domain": "accounts.google.com", "path": "/", "value": "bad",
     "secure": True, "httpOnly": True},
    {"name": "SAPISID", "domain": ".google.com", "path": "/", "value": "sap",
     "secure": True, "httpOnly": True},
    {"name": "NID", "domain": ".google.com", "path": "/", "value": "nidv", "secure": False, "httpOnly": True},
    {"name": "non-google", "domain": "example.com", "path": "/", "value": "x", "secure": True, "httpOnly": True},
    {"name": "SID", "domain": "accounts.google.com", "path": "/", "value": "bad2",
     "secure": False, "httpOnly": False},
]
picked = car._pick_google_cookies(CDP_COOKIES)
check("picks best-scoring SID (.google.com over accounts)",
      picked.get("SID") == "good", str(picked))
check("keeps SAPISID", picked.get("SAPISID") == "sap")
check("excludes non-google domains", "non-google" not in picked)
check("includes NID", picked.get("NID") == "nidv")

cookie_str, sapisid = car._build_cookie_payload(picked)
check("cookie string export-order (SID first, NID last)",
      cookie_str.startswith("SID=good;") and "NID=nidv" == cookie_str.rsplit("; ", 1)[1],
      cookie_str)
check("sapisid extracted", sapisid == "sap")

# ─── unit: default-browser detection (live registry) ───────────────────────
if not OFFLINE:
    print("unit: default browser detection")
    info = car.detect_default_browser()
    check("detected a default browser with an exe", bool(info.get("exe")), str(info))
    check("detected browser is chromium", info.get("chromium") is True, str(info))

else:
    print("  SKIP  default-browser detection (offline)")

# ─── unit: refresh endpoint resolution from config.json ─────────────────────
print("unit: refresh endpoint resolution (config.json + CLI overrides)")
base, key = car._resolve_refresh_endpoint({})
check("defaults: 127.0.0.1:8081 + sk-gemini",
      base == "http://127.0.0.1:8081" and key == "sk-gemini", f"{base} {key}")
base, key = car._resolve_refresh_endpoint(
    {"port": 9000, "api_keys": ["k1"], "cookie_refresh_key": "ck"})
check("config port + cookie_refresh_key win",
      base == "http://127.0.0.1:9000" and key == "ck", f"{base} {key}")
base, key = car._resolve_refresh_endpoint({"api_keys": ["k1"]})
check("falls back to api_keys[0]", key == "k1", key)
base, key = car._resolve_refresh_endpoint({"port": 9000},
                                          base_url="http://x:1", key="manual")
check("CLI args override config",
      base == "http://x:1" and key == "manual", f"{base} {key}")

# ─── live: CDP plumbing with a temp profile ─────────────────────────────────
if not OFFLINE:
    print("live: CDP plumbing (temp profile, must not touch cookie.txt)")
    brave = info.get("exe") or r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
    if not os.path.exists(brave):
        print("  SKIP: browser exe not found")
    else:
        tmp_profile = tempfile.mkdtemp(prefix="gwa-cdp-")
        tmp_out = os.path.join(tempfile.gettempdir(), "cookie_test_out.json")
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
        real_cookie = os.path.join(os.getcwd(), "cookie.txt")
        real_mtime = os.path.getmtime(real_cookie) if os.path.exists(real_cookie) else 0
        car.COOKIE_FILE = tmp_out  # never touch the real cookie.txt
        ok = car.refresh_via_cdp(brave, tmp_profile)
        # temp profile has no Google session cookies -> graceful False expected
        check("CDP flow completed (launch/connect/navigate/close)", ok is False, f"ok={ok}")
        time.sleep(1)
        check("real cookie.txt untouched",
              (os.path.getmtime(real_cookie) if os.path.exists(real_cookie) else 0) == real_mtime,
              "cookie.txt was modified!")
else:
    print("  SKIP  CDP plumbing (live) (offline)")

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
