"""Unit tests for watchdog.py health analysis + cookie-refresh triggers.

Offline-safe: exercises _analyze / _maybe_warn_and_refresh / _health_summary
with stubbed payloads and a stubbed _spawn_refresh - never launches a browser
or manage.bat. Run: python test_watchdog.py
"""
import importlib.util

spec = importlib.util.spec_from_file_location("wd", "watchdog.py")
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


# ── fixture payloads ──────────────────────────────────────────────────────
def health(**kw):
    base = {"status": "ok", "gemini_bl": "bl_1", "bl_405_count": 0,
            "cookie": {"exists": True, "age_sec": 3600, "refresh_requested": False},
            "proxy": {"plan": [None, "http://127.0.0.1:7890"]}}
    base.update(kw)
    return base


fresh = health()
stale = health(cookie={"exists": True, "age_sec": 30 * 3600,
                       "refresh_requested": False})
no_cookie = health(cookie={"exists": False, "age_sec": None,
                           "refresh_requested": False})
inflight = health(cookie={"exists": True, "age_sec": 30 * 3600,
                          "refresh_requested": True})
storm3 = health(bl_405_count=3)
storm2 = health(bl_405_count=2)
storm_recovered = health(bl_405_count=0)

# ── _analyze ──────────────────────────────────────────────────────────────
s = wd._analyze(fresh, 24, 3)
check("fresh cookies -> not stale", not s["stale_cookies"] and s["server_ok"])
check("fresh cookies -> no refresh in flight", not s["refresh_inflight"])
check("fresh cookies -> no BL storm", not s["bl_405_storm"] and s["bl_405_count"] == 0)
check("no image-bridge payload -> bridge idle",
      not s["bridge_claimed"] and not s["bridge_stale"] and s["bridge_claimed_age_sec"] is None)

# ── image-bridge claim analysis ──────────────────────────────────────────
bridge_idle = health(image_bridge={"pending": False, "pending_age_sec": None,
                                   "claimed": False, "claimed_age_sec": None})
s = wd._analyze(bridge_idle, 24, 3)
check("bridge idle -> not stale", not s["bridge_claimed"] and not s["bridge_stale"])

bridge_fresh = health(image_bridge={"pending": False, "pending_age_sec": None,
                                    "claimed": True, "claimed_age_sec": 60})
s = wd._analyze(bridge_fresh, 24, 3)
check("fresh claim (60s) -> claimed, not stale",
      s["bridge_claimed"] and not s["bridge_stale"] and s["bridge_claimed_age_sec"] == 60)

bridge_old = health(image_bridge={"pending": False, "pending_age_sec": None,
                                  "claimed": True, "claimed_age_sec": 500})
s = wd._analyze(bridge_old, 24, 3)
check("old claim (500s) -> stale", s["bridge_stale"])
s = wd._analyze(bridge_old, 24, 3, bridge_stale_sec=900)
check("custom stale threshold (900s) -> not stale yet", not s["bridge_stale"])

bridge_malformed = health(image_bridge={"claimed": True, "claimed_age_sec": "n/a"})
s = wd._analyze(bridge_malformed, 24, 3)
check("unparseable claim age -> not stale", not s["bridge_stale"])

s = wd._analyze(stale, 24, 3)
check("30h cookies -> stale at 24h threshold", s["stale_cookies"])
check("stale keeps age in hours usable",
      isinstance(s["cookie_age_sec"], (int, float)) and s["cookie_age_sec"] > 24 * 3600)

s = wd._analyze(no_cookie, 24, 3)
check("no cookie file -> not stale (nothing to refresh)", not s["stale_cookies"])

s = wd._analyze(inflight, 24, 3)
check("refresh_requested -> in flight", s["refresh_inflight"])

s = wd._analyze(storm3, 24, 3)
check("3 consecutive 405s -> storm at trigger 3", s["bl_405_storm"])
s = wd._analyze(storm2, 24, 3)
check("2 consecutive 405s -> below trigger, no storm", not s["bl_405_storm"])
check("missing bl_405 field -> counts as 0",
      not wd._analyze(health().get("status") and {"status": "ok", "cookie": {}}, 24, 3)
      ["bl_405_storm"])

import time as _time
old_storm = health(bl_405_count=3, bl_405_last_ts=_time.time() - 2 * 3600)
check("405 storm older than freshness window -> not a storm",
      not wd._analyze(old_storm, 24, 3)["bl_405_storm"])
recent_storm = health(bl_405_count=3, bl_405_last_ts=_time.time() - 60)
check("recent 405 storm -> storm", wd._analyze(recent_storm, 24, 3)["bl_405_storm"])

# small debounce values so the fake clock (1000-6000s) behaves deterministically
W_E, T_C = 60, 30

# ── _maybe_warn_and_refresh: stale cookies ────────────────────────────────
spawned = []
wd._spawn_refresh = lambda cmd: (spawned.append(cmd), True)[1]
state = {"last_cookie_warn": 0.0, "last_bl405_warn": 0.0, "last_trigger": 0.0}
ev = wd._maybe_warn_and_refresh(stale, state, 24, 3, ["manage.bat", "cookies"],
                                warn_every=W_E, trigger_cooldown=T_C, now=1000.0)
check("stale cookies -> warns", any("Cookie age" in e for e in ev), str(ev))
check("stale cookies -> triggers refresh", len(spawned) == 1, str(spawned))
check("trigger command is manage.bat cookies", spawned[0] == ["manage.bat", "cookies"])
check("trigger debounced (cooldown)",
      not wd._maybe_warn_and_refresh(stale, state, 24, 3, ["x"],
                                     warn_every=W_E, trigger_cooldown=T_C,
                                     now=1010.0)
      and len(spawned) == 1)

# refresh already in flight -> never a second trigger
state2 = {"last_cookie_warn": 0.0, "last_bl405_warn": 0.0, "last_trigger": 0.0}
ev2 = wd._maybe_warn_and_refresh(inflight, state2, 24, 3, ["x"],
                                 warn_every=W_E, trigger_cooldown=T_C, now=2000.0)
check("in-flight refresh -> waits, no trigger", len(spawned) == 1 and any(
    "in flight" in e for e in ev2), str(ev2))

# ── _maybe_warn_and_refresh: repeated 405s ────────────────────────────────
state3 = {"last_cookie_warn": 0.0, "last_bl405_warn": 0.0, "last_trigger": 0.0}
ev3 = wd._maybe_warn_and_refresh(storm3, state3, 24, 3, ["manage.bat", "cookies"],
                                 warn_every=W_E, trigger_cooldown=T_C, now=3000.0)
check("405 storm -> warns", any("405-ing" in e for e in ev3), str(ev3))
check("405 storm -> triggers refresh", len(spawned) == 2, str(spawned))
state3b = {"last_cookie_warn": 0.0, "last_bl405_warn": 0.0, "last_trigger": 0.0}
wd._maybe_warn_and_refresh(storm2, state3b, 24, 3, ["x"],
                           warn_every=W_E, trigger_cooldown=T_C, now=4000.0)
check("below-trigger 405 count -> no trigger", len(spawned) == 2)

# ── _maybe_warn_and_refresh: recovery resets timers ───────────────────────
state4 = {"last_cookie_warn": 100.0, "last_bl405_warn": 100.0, "last_trigger": 100.0}
ev4 = wd._maybe_warn_and_refresh(fresh, state4, 24, 3, ["x"],
                                 warn_every=W_E, trigger_cooldown=T_C, now=5000.0)
check("healthy poll -> no events", ev4 == [], str(ev4))
check("healthy poll -> cookie timer reset", state4["last_cookie_warn"] == 0.0)
check("healthy poll -> 405 timer reset", state4["last_bl405_warn"] == 0.0)
check("healthy poll -> trigger timer reset", state4["last_trigger"] == 0.0)
state5 = {"last_cookie_warn": 0.0, "last_bl405_warn": 777.0, "last_trigger": 0.0}
wd._maybe_warn_and_refresh(storm_recovered, state5, 24, 3, ["x"],
                           warn_every=W_E, trigger_cooldown=T_C, now=6000.0)
check("recovered 405s -> 405 timer reset", state5["last_bl405_warn"] == 0.0)

# ── spawn failure: backoff, no retry storm ───────────────────────────────
state6 = {"last_cookie_warn": 0.0, "last_bl405_warn": 0.0, "last_trigger": 0.0}
wd._spawn_refresh = lambda cmd: False  # refresh command persistently broken
spawn_tries = []
wd._spawn_refresh = lambda cmd: (spawn_tries.append(1), False)[1]
ev6 = wd._maybe_warn_and_refresh(stale, state6, 24, 3, ["x"],
                                 warn_every=W_E, trigger_cooldown=T_C, now=7000.0)
check("spawn failure -> no trigger event", not any("Triggered" in e for e in ev6), str(ev6))
check("spawn failure -> attempted once", len(spawn_tries) == 1, str(spawn_tries))
wd._maybe_warn_and_refresh(stale, state6, 24, 3, ["x"],
                           warn_every=W_E, trigger_cooldown=T_C, now=7010.0)
check("spawn failure -> backoff set (no retry next poll)", len(spawn_tries) == 1)

# ── state persistence (load_state / save_state) ──────────────────────────
import tempfile as _tf
import os as _os
import json as _json

_state_path = _os.path.join(_tf.gettempdir(), "gwa_wd_state_test.json")
if _os.path.exists(_state_path):
    _os.remove(_state_path)

s0 = wd.load_state(_state_path)
check("load_state missing file -> zeroed defaults",
      s0 == {"last_cookie_warn": 0.0, "last_bl405_warn": 0.0, "last_trigger": 0.0,
             "last_bridge_warn": 0.0, "last_ext_warn": 0.0},
      str(s0))

wd.save_state({"last_cookie_warn": 1000.0, "last_bl405_warn": 2000.0,
               "last_trigger": 3000.0, "last_bridge_warn": 4000.0,
               "last_ext_warn": 5000.0, "last_summary": 999.0}, _state_path)
s1 = wd.load_state(_state_path)
check("save/load round-trips the debounce timers",
      s1["last_cookie_warn"] == 1000.0 and s1["last_bl405_warn"] == 2000.0
      and s1["last_trigger"] == 3000.0 and s1["last_bridge_warn"] == 4000.0
      and s1["last_ext_warn"] == 5000.0, str(s1))
check("last_summary is NOT persisted (in-memory only)",
      "last_summary" not in s1, str(s1))

with open(_state_path, "w", encoding="utf-8") as _f:
    _f.write("{not json!!")
s2 = wd.load_state(_state_path)
check("load_state corrupt file -> defaults", s2["last_cookie_warn"] == 0.0, str(s2))

with open(_state_path, "w", encoding="utf-8") as _f:
    _json.dump({"last_cookie_warn": "bogus", "last_bl405_warn": -5,
                "last_trigger": 42, "last_bridge_warn": "x",
                "last_ext_warn": "y"}, _f)
s3 = wd.load_state(_state_path)
check("load_state rejects non-numeric + negative -> defaults",
      s3["last_cookie_warn"] == 0.0 and s3["last_bl405_warn"] == 0.0
      and s3["last_trigger"] == 42.0 and s3["last_bridge_warn"] == 0.0
      and s3["last_ext_warn"] == 0.0, str(s3))

# bool is an int subclass: a tampered file with "last_trigger": true must NOT
# load as epoch 1.0 (that would make the cooldown always expired -> a rebooted
# watchdog would immediately re-trigger a refresh + browser window).
with open(_state_path, "w", encoding="utf-8") as _f:
    _json.dump({"last_cookie_warn": True, "last_bl405_warn": False,
                "last_trigger": 42, "last_bridge_warn": True,
                "last_ext_warn": True}, _f)
s3b = wd.load_state(_state_path)
check("load_state rejects booleans (int subclass) -> defaults",
      s3b["last_cookie_warn"] == 0.0 and s3b["last_bl405_warn"] == 0.0
      and s3b["last_trigger"] == 42.0 and s3b["last_bridge_warn"] == 0.0
      and s3b["last_ext_warn"] == 0.0, str(s3b))

with open(_state_path, encoding="utf-8") as _f:
    _raw = _json.load(_f)
check("state file contains only the persisted keys",
      set(_raw.keys()) == {"last_cookie_warn", "last_bl405_warn", "last_trigger",
                           "last_bridge_warn", "last_ext_warn"},
      str(_raw))
_os.remove(_state_path)

# ── _maybe_expire_stale_bridge: abandoned claim -> warn + expire ──────────
expired = []
wd._expire_bridge = lambda port, sec: (expired.append((port, sec)),
                                       {"expired": True, "id": "abc123"})[1]
state_b = {"last_cookie_warn": 0.0, "last_bl405_warn": 0.0, "last_trigger": 0.0,
           "last_bridge_warn": 0.0}
ev = wd._maybe_expire_stale_bridge(bridge_old, state_b, lambda sec: wd._expire_bridge(8081, sec),
                                   stale_sec=150, warn_every=W_E, now=8000.0)
check("stale bridge -> warns", any("abandoned" in e for e in ev), str(ev))
check("stale bridge -> expires the claim", len(expired) == 1 and expired[0][0] == 8081, str(expired))
check("stale bridge -> logs the expiry", any("Expired" in e for e in ev), str(ev))

# debounced warn: a second stale poll within the cooldown does not re-warn,
# but the (idempotent) expire is still attempted
n_exp = len(expired)
ev2 = wd._maybe_expire_stale_bridge(bridge_old, state_b, lambda sec: wd._expire_bridge(8081, sec),
                                    stale_sec=150, warn_every=W_E, now=8010.0)
check("stale bridge -> warn debounced (no re-warn)", not any("abandoned" in e for e in ev2), str(ev2))
check("stale bridge -> expire retried (idempotent)", len(expired) == n_exp + 1, str(expired))

# a still-legit (fresh) claim: no warn, no expire
n_exp2 = len(expired)
state_c = dict(state_b, last_bridge_warn=0.0)
ev3 = wd._maybe_expire_stale_bridge(bridge_fresh, state_c,
                                    lambda sec: wd._expire_bridge(8081, sec),
                                    stale_sec=150, warn_every=W_E, now=9000.0)
check("fresh claim -> no events", ev3 == [], str(ev3))
check("fresh claim -> no expire", len(expired) == n_exp2)

# idle bridge resets the warn timer so the next abandonment is fresh news
state_d = dict(state_b, last_bridge_warn=777.0)
wd._maybe_expire_stale_bridge(bridge_idle, state_d,
                              lambda sec: wd._expire_bridge(8081, sec),
                              stale_sec=150, warn_every=W_E, now=9500.0)
check("idle bridge resets the bridge warn timer", state_d["last_bridge_warn"] == 0.0)

# server without the expire endpoint (None from _expire_bridge) -> warns, no crash
wd._expire_bridge = lambda port, sec: None
state_e = dict(state_b, last_bridge_warn=0.0)
ev4 = wd._maybe_expire_stale_bridge(bridge_old, state_e,
                                    lambda sec: wd._expire_bridge(8081, sec),
                                    stale_sec=150, warn_every=W_E, now=10000.0)
check("expire endpoint missing -> graceful warning",
      any("self-expire" in e or "Could not reach" in e for e in ev4), str(ev4))

# ── _version_tuple / stale-extension detection ────────────────────────────
check("version_tuple parses dotted versions",
      wd._version_tuple("1.14") == (1, 14)
      and wd._version_tuple("1.15") == (1, 15)
      and wd._version_tuple("1.9") == (1, 9))
check("version_tuple rejects junk",
      wd._version_tuple("n/a") is None and wd._version_tuple(None) is None
      and wd._version_tuple("") is None)

# _analyze surfaces the last result's extension version
bridge_with_ext = health(image_bridge={
    "claimed": False, "claimed_age_sec": None,
    "last_result": {"ok": False, "error": "boom", "ext_version": "1.13",
                     "ts": 1234.0}})
s = wd._analyze(bridge_with_ext, 24, 3)
check("analyze reads the last result's ext_version",
      s["bridge_ext_version"] == "1.13", str(s))
check("analyze tolerates a missing last_result",
      wd._analyze(bridge_idle, 24, 3)["bridge_ext_version"] is None)

# _maybe_warn_stale_extension: older reported version -> debounced warning
# The nudge (open the extensions page) is stubbed so the unit test never
# launches a real browser.
_real_open_extensions = wd._open_extensions_page
opened = []
wd._open_extensions_page = lambda: (opened.append(1), True)[1]
stale_ext_events = []
state_ext = {"last_cookie_warn": 0.0, "last_bl405_warn": 0.0, "last_trigger": 0.0,
             "last_bridge_warn": 0.0, "last_ext_warn": 0.0}
ev = wd._maybe_warn_stale_extension(bridge_with_ext, state_ext, "1.15",
                                    warn_every=W_E, now=11000.0)
check("older reported ext version -> warns",
      any("v1.13" in e and "1.15" in e and "Reload" in e for e in ev), str(ev))
check("ext warn debounced (persisted timer set)", state_ext["last_ext_warn"] == 11000.0)
check("stale ext -> nudge opens the extensions page",
      len(opened) == 1 and any("Opened" in e for e in ev), str(ev))

ev2 = wd._maybe_warn_stale_extension(bridge_with_ext, state_ext, "1.15",
                                     warn_every=W_E, now=11010.0)
check("ext warn debounced within cooldown (no re-warn, no re-nudge)",
      ev2 == [] and len(opened) == 1, str(ev2))

# current/newer version -> no warning and the timer resets for fresh news
state_ext2 = dict(state_ext, last_ext_warn=777.0)
bridge_current = health(image_bridge={
    "claimed": False, "claimed_age_sec": None,
    "last_result": {"ok": True, "text": "hi", "ext_version": "1.15",
                     "ts": 1234.0}})
ev3 = wd._maybe_warn_stale_extension(bridge_current, state_ext2, "1.15",
                                     warn_every=W_E, now=12000.0)
check("current ext version -> no warning", ev3 == [], str(ev3))
check("current ext version resets the warn timer", state_ext2["last_ext_warn"] == 0.0)

# no last_result / no version -> silent (nothing to compare)
state_ext3 = dict(state_ext, last_ext_warn=0.0)
ev4 = wd._maybe_warn_stale_extension(bridge_idle, state_ext3, "1.15",
                                     warn_every=W_E, now=13000.0)
check("no bridge result -> no ext warning", ev4 == [], str(ev4))
no_ver = health(image_bridge={"claimed": False, "claimed_age_sec": None,
                              "last_result": {"ok": True, "ts": 1.0}})
ev5 = wd._maybe_warn_stale_extension(no_ver, dict(state_ext, last_ext_warn=0.0),
                                     "1.15", warn_every=W_E, now=13100.0)
check("result without ext_version -> no warning", ev5 == [], str(ev5))

# nudge fails (no browser detected) -> explicit fallback note, no crash
nudge_fail = []
wd._open_extensions_page = lambda: (nudge_fail.append(1), False)[1]
state_ext6 = dict(state_ext, last_ext_warn=0.0)
ev6 = wd._maybe_warn_stale_extension(bridge_with_ext, state_ext6, "1.15",
                                     warn_every=W_E, now=13200.0)
check("nudge failure -> manual-reload fallback note",
      len(nudge_fail) == 1 and any("manually" in e for e in ev6), str(ev6))

# an injected nudge_fn is used instead of the default opener
custom_opened = []
custom_nudge = lambda: (custom_opened.append(1), True)[1]
state_ext7 = dict(state_ext, last_ext_warn=0.0)
ev7 = wd._maybe_warn_stale_extension(bridge_with_ext, state_ext7, "1.15",
                                     warn_every=W_E, now=13300.0,
                                     nudge_fn=custom_nudge)
check("injected nudge_fn is called (default opener not used)",
      len(custom_opened) == 1 and len(opened) == 1, str(custom_opened))

# restore the real opener for the dedicated _open_extensions_page tests
wd._open_extensions_page = _real_open_extensions

# ── _open_extensions_page: maps the browser to its extensions URL ─────────
# Drive the real function with a FAKE browser dict and a stubbed Popen so no
# browser is ever launched in the test.
launched = []
real_popen = wd.subprocess.Popen
wd.subprocess.Popen = lambda cmd, **kw: (launched.append(cmd), type("P", (), {"pid": 1})())[1]
try:
    ok = wd._open_extensions_page({"name": "brave", "exe": "C:/fake/brave.exe"})
    check("brave -> brave://extensions launched",
          ok and launched and launched[-1][0].endswith("brave.exe")
          and launched[-1][1] == "brave://extensions", str(launched))
    ok2 = wd._open_extensions_page({"name": "chrome", "exe": "C:/fake/chrome.exe"})
    check("chrome -> chrome://extensions launched",
          ok2 and launched[-1][1] == "chrome://extensions", str(launched))
    ok3 = wd._open_extensions_page({"name": "msedge", "exe": "C:/fake/msedge.exe"})
    check("msedge -> edge://extensions launched",
          ok3 and launched[-1][1] == "edge://extensions", str(launched))
    ok4 = wd._open_extensions_page({"name": "firefox", "exe": "C:/fake/firefox.exe"})
    check("unknown browser -> no launch (returns False)",
          not ok4 and len(launched) == 3, str(launched))
    ok5 = wd._open_extensions_page({})
    check("empty browser info -> no launch", not ok5)
finally:
    wd.subprocess.Popen = real_popen

# _on_disk_ext_version reads the real manifest
real_ver = wd._on_disk_ext_version()
check("on-disk manifest version is readable",
      isinstance(real_ver, str) and wd._version_tuple(real_ver) is not None,
      str(real_ver))
check("on-disk manifest version is current (1.15+)",
      wd._version_tuple(real_ver) >= (1, 15), str(real_ver))

# ── _health_summary ───────────────────────────────────────────────────────
summ = wd._health_summary(stale)
check("summary shows bl + cookie age + 405 count",
      "bl_1" in summ and "30.0h" in summ and "405s=0" in summ, summ)
summ2 = wd._health_summary(inflight)
check("summary notes refresh in flight", "in flight" in summ2, summ2)
summ3 = wd._health_summary(bridge_old)
check("summary shows a claimed bridge with its age",
      "bridge=claimed(8.3m)" in summ3 or "bridge=claimed" in summ3, summ3)
summ4 = wd._health_summary(bridge_idle)
check("summary shows an idle bridge", "bridge=idle" in summ4, summ4)
summ5 = wd._health_summary(bridge_with_ext)
check("summary shows the last result's ext version", "ext=1.13" in summ5, summ5)
summ6 = wd._health_summary(bridge_current)
check("summary shows the current ext version", "ext=1.15" in summ6, summ6)

# ── _paths_for: per-port files, legacy names on the default port ──────────
p8081 = wd._paths_for(8081)
check("port 8081 keeps legacy file names",
      p8081["pidfile"].endswith("watchdog.pid")
      and p8081["server_log"].endswith("server.log")
      and p8081["watchdog_log"].endswith("watchdog.log")
      and p8081["state_file"].endswith("watchdog-state.json"), str(p8081))

p9000 = wd._paths_for(9000)
check("non-default port gets per-port pidfile",
      p9000["pidfile"].endswith("watchdog-9000.pid"), str(p9000))
check("non-default port gets per-port logs",
      p9000["server_log"].endswith("server-9000.log")
      and p9000["watchdog_log"].endswith("watchdog-9000.log"), str(p9000))
check("non-default port gets per-port state",
      p9000["state_file"].endswith("watchdog-9000-state.json"), str(p9000))
check("per-port files differ from legacy",
      p9000["pidfile"] != p8081["pidfile"]
      and p9000["state_file"] != p8081["state_file"])

p_custom = wd._paths_for(9000, state_file=r"C:/tmp/custom-state.json")
check("explicit --state-file overrides the per-port default",
      p_custom["state_file"] == r"C:/tmp/custom-state.json"
      and p_custom["pidfile"].endswith("watchdog-9000.pid"), str(p_custom))

# ── _default_refresh_cmd: prefer manage.bat cookies ───────────────────────
cmd = wd._default_refresh_cmd()
check("default refresh command is manage.bat cookies",
      cmd[0] == "cmd" and cmd[1] == "/c" and cmd[-1] == "cookies"
      and cmd[-2].endswith("manage.bat"), str(cmd))

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
