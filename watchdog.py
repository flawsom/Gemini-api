#!/usr/bin/env python3
"""watchdog.py - run gemini_web2api.py as an auto-restarting background service.

Automates everything:
  * Starts the API server as a subprocess (no console window with pythonw).
  * Auto-restarts it if it crashes.
  * Health-checks GET / every N seconds; if the server hangs (3 missed
    checks) it is killed and restarted.
  * Reads the / health payload: logs a warning when the Gemini cookies are
    older than --cookie-age-h (default 24h), and triggers `manage.bat cookies`
    (detached, so the watchdog keeps health-checking) when the cookies go
    stale or the build label starts returning HTTP 405 repeatedly
    (--bl-405-trigger, default 3). Both are debounced and skip while a cookie
    refresh is already in flight (cookie.refresh_requested).
  * Logs server output to server.log and watchdog activity to watchdog.log.
  * Writes watchdog.pid so manage.bat / stop can find and kill it.
  * Persists the debounce timers (watchdog-state.json) so a watchdog restart
    - e.g. after a reboot - does not immediately re-trigger a cookie refresh
    or spam warnings that were already issued within their cooldown windows.
  * Multi-instance: a non-default --port gets its own per-port pidfile, logs,
    and debounce-state file (watchdog-<port>.pid, server-<port>.log,
    watchdog-<port>.log, watchdog-<port>-state.json), so several servers on
    different ports can each run their own watchdog without sharing state.
    --state-file overrides the state path explicitly.

Usage:
    pythonw.exe watchdog.py [--port 8081] [--config config.json]
                            [--cookie-age-h 24] [--bl-405-trigger 3]
                            [--state-file watchdog-state.json]
    python.exe  watchdog.py --foreground     # watch in the current window
"""
import argparse
import atexit
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "gemini_web2api.py")
# Default (port 8081) file names - kept legacy so manage.bat / autostart.py /
# the test suites keep working unchanged. Non-default ports get per-port
# suffixed files (see _paths_for) so multiple watchdogs can run side by side
# on different ports without sharing pidfiles, logs, or debounce state.
PIDFILE = os.path.join(HERE, "watchdog.pid")
SERVER_LOG = os.path.join(HERE, "server.log")
WATCHDOG_LOG = os.path.join(HERE, "watchdog.log")
STATE_FILE = os.path.join(HERE, "watchdog-state.json")

DEFAULT_PORT = 8081


def _paths_for(port: int, state_file: str = None) -> dict:
    """Per-instance file paths for a watchdog on `port`.

    Port 8081 (the default) keeps the legacy unsuffixed names so existing
    tooling keeps working; any other port gets ``-<port>`` suffixed pidfile,
    logs, and state file so several servers on different ports can each run
    their own watchdog without sharing debounce state. An explicit
    ``--state-file`` always wins for the state path.
    """
    # Return LITERAL legacy names (never the mutable module globals): a second
    # main() call in the same process - e.g. port 9090 then 8081 - must not see
    # the previous call's reassigned STATE_FILE/PIDFILE values.
    if port == DEFAULT_PORT and state_file is None:
        return {"pidfile": os.path.join(HERE, "watchdog.pid"),
                "server_log": os.path.join(HERE, "server.log"),
                "watchdog_log": os.path.join(HERE, "watchdog.log"),
                "state_file": os.path.join(HERE, "watchdog-state.json")}
    suffix = f"-{port}"
    return {
        "pidfile": os.path.join(HERE, f"watchdog{suffix}.pid"),
        "server_log": os.path.join(HERE, f"server{suffix}.log"),
        "watchdog_log": os.path.join(HERE, f"watchdog{suffix}.log"),
        "state_file": state_file or os.path.join(HERE, f"watchdog{suffix}-state.json"),
    }

# Debounce timers that survive a watchdog restart (see load_state/save_state).
PERSISTED_STATE_KEYS = ("last_cookie_warn", "last_bl405_warn", "last_trigger",
                        "last_bridge_warn", "last_ext_warn")

# The extension reports its own manifest version with every image-bridge
# result; this file is the on-disk reference the watchdog compares it against
# (a result posted by an older build than what is on disk = the extension was
# not reloaded after an update).
MANIFEST_FILE = os.path.join(HERE, "gemini-cookie-sync-extension", "manifest.json")

WINDOW_SEC = 300      # restart-count window (5 minutes)
MAX_RESTARTS = 5      # max restarts per window before backing off
BACKOFF_SEC = 600     # sleep after too many restarts
BOOT_GRACE_SEC = 15   # don't health-check until the server had time to boot
ROTATE_BYTES = 5 * 1024 * 1024  # rotate logs past 5 MB

# Debouncing: warn at most every 4h per signal; trigger a refresh at most every
# 30 min; log a periodic health summary every 1h. A 405 streak only counts as
# a storm while the most recent 405 is fresh, so an idle server with an old
# streak cannot re-trigger a browser window every cooldown.
WARN_EVERY_SEC = 4 * 3600
TRIGGER_COOLDOWN_SEC = 30 * 60
SUMMARY_EVERY_SEC = 3600
BL_405_FRESH_SEC = 30 * 60

# Image bridge: a claim the extension has held unanswered for this long is
# considered abandoned (stuck window, suspended service worker). The watchdog
# expires it so the waiting client fails fast and the single bridge slot frees
# up for the next image request instead of blocking for the full timeout.
# The claim age is measured from CLAIM time, which precedes any processing:
# poll (<=30s) + cold window load (~20-60s) + attach/send (~10s) + Gemini
# answer (up to the content script's ~300s budget). A live extension can
# legitimately take ~5 min end-to-end, so this sits ABOVE the server's own
# image_bridge_timeout (default 300s) - the watchdog only fires for claims
# that outlived the server budget, i.e. genuinely dead ones.
BRIDGE_STALE_SEC = 360


def _version_tuple(v) -> tuple | None:
    """'1.14' -> (1, 14) for numeric comparison; None on junk.

    Plain string comparison would call '1.9' > '1.14', so versions are
    compared as integer tuples."""
    try:
        parts = [int(x) for x in str(v).split(".") if x.isdigit()]
        return tuple(parts) if parts else None
    except (TypeError, ValueError):
        return None


def _on_disk_ext_version(path: str = MANIFEST_FILE) -> str | None:
    """The extension version currently on disk (None if unreadable)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("version")
    except (OSError, ValueError):
        return None


def log(msg: str):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def fetch_health(port: int, timeout: int = 4) -> dict | None:
    """GET / and return the parsed health payload, or None on any failure."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/", headers={"User-Agent": "watchdog"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _analyze(health: dict, cookie_age_h: float, bl_405_trigger: int,
             now: float = None, bl_405_fresh_sec: int = BL_405_FRESH_SEC,
             bridge_stale_sec: float = BRIDGE_STALE_SEC) -> dict:
    """Classify a health payload into the signals the watchdog acts on.

    Pure function - unit-tested directly. A 405 streak only counts as a storm
    while the most recent 405 (bl_405_last_ts) is younger than bl_405_fresh_sec,
    so an idle server with an old streak is not treated as an active storm.
    """
    cookie = health.get("cookie") or {}
    age = cookie.get("age_sec")
    now = now if now is not None else time.time()
    last_ts = health.get("bl_405_last_ts")
    try:
        fresh = last_ts is None or now - float(last_ts) <= bl_405_fresh_sec
    except (TypeError, ValueError):
        fresh = True  # unparseable timestamp - don't block on it
    count = health.get("bl_405_count", 0) or 0
    bridge = health.get("image_bridge") or {}
    b_age = bridge.get("claimed_age_sec")
    last = bridge.get("last_result")
    if not isinstance(last, dict):
        last = {}
    return {
        "server_ok": health.get("status") == "ok",
        "cookie_exists": bool(cookie.get("exists")),
        "cookie_age_sec": age,
        "refresh_inflight": bool(cookie.get("refresh_requested")),
        "stale_cookies": (bool(cookie.get("exists"))
                           and isinstance(age, (int, float))
                           and age > cookie_age_h * 3600),
        "bl_405_count": count,
        "bl_405_last_ts": last_ts,
        "bl_405_storm": count >= bl_405_trigger and fresh,
        "bridge_claimed": bool(bridge.get("claimed")),
        "bridge_claimed_age_sec": b_age,
        # A claim the extension has held past its budget is abandoned - the
        # extension is stuck and will never post a result.
        "bridge_stale": (bool(bridge.get("claimed"))
                         and isinstance(b_age, (int, float))
                         and b_age > bridge_stale_sec),
        # The extension version that produced the last bridge result (None
        # when nothing was posted yet or the result carried no version).
        "bridge_ext_version": last.get("ext_version"),
    }


def load_state(path: str = None) -> dict:
    """Load the persisted debounce timers; defaults on any problem.

    ``path`` resolves to the current STATE_FILE at call time (None), so
    per-port watchdogs each read their own state file. Missing/corrupt file
    or non-numeric values all fall back to fresh defaults, so a bad state
    file can never wedge the watchdog - the worst case is a single
    re-debounce after the next cooldown.
    """
    path = path or STATE_FILE
    state = {k: 0.0 for k in PERSISTED_STATE_KEYS}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for k in PERSISTED_STATE_KEYS:
            v = data.get(k)
            # bool is an int subclass in Python: reject it explicitly, else a
            # tampered file with "last_trigger": true would load as epoch 1.0
            # and make the cooldown always expired (immediate re-trigger).
            if (isinstance(v, (int, float)) and not isinstance(v, bool)
                    and v >= 0):
                state[k] = float(v)
    except (OSError, ValueError, TypeError):
        pass
    return state


def save_state(state: dict, path: str = None):
    """Persist the debounce timers atomically (tmp file + rename).

    ``path`` resolves to the current STATE_FILE at call time (None), so
    per-port watchdogs each write their own state file. Best-effort: losing
    the state only means one extra debounce after a crash.
    """
    path = path or STATE_FILE
    try:
        # Process-unique tmp name: two writers racing can never collide on the
        # same .tmp file (the pidfile guard already makes this unlikely).
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: state.get(k, 0.0) for k in PERSISTED_STATE_KEYS}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _default_refresh_cmd() -> list:
    """The cookie-refresh command: manage.bat cookies, else the raw script."""
    if os.path.exists(os.path.join(HERE, "manage.bat")):
        return ["cmd", "/c", os.path.join(HERE, "manage.bat"), "cookies"]
    return [sys.executable, os.path.join(HERE, "cookie_autorefresh.py")]


def _spawn_refresh(cmd: list) -> bool:
    """Launch the cookie-refresh flow DETACHED so health checks never block.

    Runs in a hidden window (CREATE_NO_WINDOW); only the refresh's own browser
    window is opened, never the user's. Returns True if it launched.
    """
    try:
        p = subprocess.Popen(cmd, cwd=HERE,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log(f"Cookie refresh triggered (PID {p.pid})")
        return True
    except Exception as e:
        log(f"Could not launch cookie refresh ({cmd}): {e}")
        return False


def _maybe_warn_and_refresh(health: dict, state: dict, cookie_age_h: float,
                            bl_405_trigger: int, refresh_cmd: list,
                            warn_every: int = WARN_EVERY_SEC,
                            trigger_cooldown: int = TRIGGER_COOLDOWN_SEC,
                            now: float = None) -> list:
    """Debounced warnings + detached cookie-refresh triggers for one health poll.

    `state` is a persistent dict the caller keeps between polls:
    {"last_cookie_warn", "last_bl405_warn", "last_trigger"}. Returns a list of
    human-readable events (empty when nothing happened) - unit-tested directly.
    """
    now = now if now is not None else time.time()
    events = []
    sig = _analyze(health, cookie_age_h, bl_405_trigger, now=now)

    if sig["stale_cookies"]:
        age_h = sig["cookie_age_sec"] / 3600.0
        if now - state["last_cookie_warn"] >= warn_every:
            state["last_cookie_warn"] = now
            events.append(f"Cookie age {age_h:.1f}h exceeds {cookie_age_h:.0f}h - "
                          "the session may die soon; refresh recommended")
        if sig["refresh_inflight"]:
            events.append("Cookie refresh already in flight - waiting")
        elif now - state["last_trigger"] >= trigger_cooldown:
            # Set last_trigger on success AND failure: a persistently broken
            # refresh command must not be retried every poll (30s) - only on
            # the next cooldown, so a broken manage.bat cannot spam the logs.
            spawned = _spawn_refresh(refresh_cmd)
            state["last_trigger"] = now
            if spawned:
                events.append(f"Triggered cookie refresh (cookies stale, {age_h:.1f}h)")
    elif sig["refresh_inflight"]:
        events.append("Cookie refresh already in flight - waiting")
    else:
        # healthy cookies: reset the timers so the next staleness is fresh news
        state["last_cookie_warn"] = 0.0
        state["last_trigger"] = 0.0

    if sig["bl_405_storm"]:
        n = sig["bl_405_count"]
        if now - state["last_bl405_warn"] >= warn_every:
            state["last_bl405_warn"] = now
            events.append(f"BL is 405-ing repeatedly ({n} consecutive) - "
                          "likely a stale build label or stale cookies")
        if sig["refresh_inflight"]:
            events.append("Cookie refresh already in flight - waiting")
        elif now - state["last_trigger"] >= trigger_cooldown:
            spawned = _spawn_refresh(refresh_cmd)
            state["last_trigger"] = now  # set on failure too - no retry storm
            if spawned:
                events.append("Triggered cookie refresh (repeated 405s)")
    elif sig["bl_405_count"] == 0:
        state["last_bl405_warn"] = 0.0

    return events


def _expire_bridge(port: int, min_age_sec: float) -> dict | None:
    """POST the server's loopback-only expire endpoint - returns the payload.

    The server decides by claim age (>= min_age_sec), so an old server
    without the endpoint returns 404 -> None here and the watchdog just warns
    (no crash)."""
    try:
        body = json.dumps({"min_age_sec": min_age_sec}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/internal/image-bridge/expire",
            data=body, headers={"Content-Type": "application/json",
                                "User-Agent": "watchdog"},
            method="POST")
        with urllib.request.urlopen(req, timeout=4) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _maybe_expire_stale_bridge(health: dict, state: dict, expire_fn,
                               stale_sec: float = BRIDGE_STALE_SEC,
                               warn_every: int = WARN_EVERY_SEC,
                               now: float = None) -> list:
    """Warn + expire an abandoned image-bridge claim for one health poll.

    `expire_fn(min_age_sec)` performs the actual expiry (stubbed in tests).
    Both the warning AND the reach-failure note are debounced by
    last_bridge_warn so an old server without the endpoint (or a transient
    POST failure) cannot log a line every 30s poll while the claim sits stale.
    The expiry itself is idempotent and one-shot - once the claim is gone the
    health payload stops reporting it. Returns events (empty when nothing
    happened)."""
    now = now if now is not None else time.time()
    events = []
    # Only the bridge fields matter here; compute them inline so no unrelated
    # cookie/405 signals are implied.
    bridge = (health or {}).get("image_bridge") or {}
    age = bridge.get("claimed_age_sec")
    stale = (bool(bridge.get("claimed"))
             and isinstance(age, (int, float))
             and age > stale_sec)
    if not stale:
        # Healthy/idle: reset the warn timer so the next abandonment is fresh
        # news. A still-in-budget claim keeps the timer so it can later warn.
        if not bridge.get("claimed"):
            state["last_bridge_warn"] = 0.0
        return events

    warned = False
    if now - state["last_bridge_warn"] >= warn_every:
        state["last_bridge_warn"] = now
        warned = True
        events.append(
            f"Image-bridge claim abandoned ({age:.0f}s > {stale_sec:.0f}s) - "
            "expiring it so the next image request is not blocked")
    res = expire_fn(stale_sec)
    if res and res.get("expired"):
        events.append(f"Expired the stale image-bridge claim (id {res.get('id')})")
    elif res is None and warned:
        # Debounced with the warning: at most one line per stale episode.
        events.append("Could not reach the server's image-bridge expire "
                      "endpoint (older server?) - the claim will self-expire "
                      "on its TTL")
    return events


def _open_extensions_page(browser: dict = None) -> bool:
    """Open the default browser's extensions page so the user can reload.

    The actionable nudge for a stale extension: launching the browser on its
    own ``<scheme>://extensions`` URL opens the extensions manager (a new tab
    when the browser is already running) where the reload button lives.
    Lazy-imports cookie_autorefresh's detection so watchdog startup never
    depends on that module. Returns True if a browser was launched."""
    try:
        from cookie_autorefresh import detect_default_browser
    except Exception:
        return False
    try:
        info = browser if browser is not None else detect_default_browser()
        name, exe = (info or {}).get("name"), (info or {}).get("exe")
    except Exception:
        return False
    scheme = {"brave": "brave", "chrome": "chrome", "msedge": "edge",
              "opera": "opera", "vivaldi": "vivaldi"}.get(name)
    if not scheme or not exe:
        return False
    try:
        subprocess.Popen([exe, f"{scheme}://extensions"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except Exception:
        return False


def _maybe_warn_stale_extension(health: dict, state: dict, on_disk_version: str,
                                warn_every: int = WARN_EVERY_SEC,
                                now: float = None,
                                nudge_fn: callable = None) -> list:
    """Warn when the last bridge result came from an OLDER extension build.

    The extension reports its own manifest version with every image-bridge
    result; the server echoes it in /health (image_bridge.last_result.
    ext_version). If that is numerically OLDER than the on-disk manifest, the
    user updated the extension files but never reloaded the extension - image
    requests may still run the old code. Debounced by the persisted
    last_ext_warn key so a stale build logs at most one warning per cooldown.
    When the warning fires, `nudge_fn` (default: open the extensions page in
    the default browser) is invoked so a missed reload actively nudges the
    user instead of only appearing in the log. No result / no version /
    current-or-newer version -> nothing (and the timer resets, so the next
    staleness is fresh news)."""
    now = now if now is not None else time.time()
    events = []
    bridge = (health or {}).get("image_bridge") or {}
    last = bridge.get("last_result")
    if not isinstance(last, dict):
        return events  # no bridge result yet - nothing to compare
    reported = last.get("ext_version")
    if not reported or not on_disk_version:
        return events
    rv, dv = _version_tuple(reported), _version_tuple(on_disk_version)
    if rv is None or dv is None:
        return events  # unparseable version - don't block on it
    if rv >= dv:
        state["last_ext_warn"] = 0.0  # current/newer - reset for fresh news
        return events
    if now - state["last_ext_warn"] >= warn_every:
        state["last_ext_warn"] = now
        events.append(
            f"Last image-bridge result came from extension v{reported} - "
            f"newer v{on_disk_version} is on disk. Reload the Gemini Cookie "
            "Sync extension (brave://extensions) so image-bridge fixes are live.")
        if nudge_fn is None:
            nudge_fn = _open_extensions_page
        try:
            if nudge_fn():
                events.append("Opened the browser's extensions page for reloading "
                              "the Gemini Cookie Sync extension")
            else:
                events.append("Could not open the extensions page automatically - "
                              "reload it manually (brave://extensions)")
        except Exception:
            events.append("Could not open the extensions page automatically - "
                          "reload it manually (brave://extensions)")
    return events


def _health_summary(health: dict) -> str:
    """One-line human summary of the health payload (periodic log)."""
    cookie = health.get("cookie") or {}
    age = cookie.get("age_sec")
    age_s = f"{age / 3600:.1f}h" if isinstance(age, (int, float)) else "n/a"
    inflight = " (refresh in flight)" if cookie.get("refresh_requested") else ""
    bridge = health.get("image_bridge") or {}
    b_age = bridge.get("claimed_age_sec")
    bridge_s = (f"bridge=claimed({b_age / 60:.1f}m)" if bridge.get("claimed")
                and isinstance(b_age, (int, float)) else "bridge=idle")
    last = bridge.get("last_result")
    ext_s = ""
    if isinstance(last, dict) and last.get("ext_version"):
        ext_s = f" ext={last['ext_version']}"
    return (f"bl={health.get('gemini_bl')} cookie_age={age_s}{inflight} "
            f"405s={health.get('bl_405_count', 0)} "
            f"proxy_plan={health.get('proxy', {}).get('plan')} {bridge_s}{ext_s}")


def _rotate(path: str, limit: int = ROTATE_BYTES):
    """Rename an oversized log to <name>.old so logs don't grow forever."""
    try:
        if os.path.exists(path) and os.path.getsize(path) > limit:
            os.replace(path, path + ".old")
    except OSError:
        pass


def _existing_watchdog() -> int | None:
    """Return the PID of a live watchdog listed in the pidfile, else None.

    Guards against two watchdogs racing (e.g. login autostart + a manual
    start): only one may run; the second exits immediately.
    """
    if not os.path.exists(PIDFILE):
        return None
    try:
        with open(PIDFILE) as f:
            pid = f.read().strip()
        if not pid.isdigit():
            return None
        ps = (
            "Get-CimInstance Win32_Process -Filter \"ProcessId=%s\" | "
            "Where-Object { $_.CommandLine -like '*watchdog.py*' } | "
            "Select-Object -ExpandProperty ProcessId" % pid
        )
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=15)
        for line in r.stdout.splitlines():
            if line.strip().isdigit():
                return int(line.strip())
    except Exception:
        pass
    return None


def main():
    ap = argparse.ArgumentParser(description="Auto-restarting service for gemini-web2api")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"port to serve on (default {DEFAULT_PORT}; non-default "
                         f"ports get per-port pidfile/logs/state files)")
    ap.add_argument("--config", default=None, help="path to config.json")
    ap.add_argument("--state-file", default=None,
                    help="explicit debounce-state file (default: per-port "
                         "watchdog-<port>-state.json, or watchdog-state.json "
                         f"on port {DEFAULT_PORT})")
    ap.add_argument("--interval", type=int, default=30,
                    help="health-check interval in seconds (0 disables health checks)")
    ap.add_argument("--max-restarts", type=int, default=MAX_RESTARTS)
    ap.add_argument("--cookie-age-h", type=float, default=24.0,
                    help="warn + refresh when cookies are older than this (hours)")
    ap.add_argument("--bl-405-trigger", type=int, default=3,
                    help="refresh when this many consecutive HTTP 405s accumulate")
    ap.add_argument("--bridge-stale-sec", type=float, default=BRIDGE_STALE_SEC,
                    help="expire an image-bridge claim the extension has held "
                         f"unanswered this long (default {BRIDGE_STALE_SEC}; 0 disables)")
    ap.add_argument("--foreground", action="store_true",
                    help="run in the current window (default is detached, e.g. pythonw)")
    args = ap.parse_args()

    # Multi-instance support: non-default ports get their own pidfile, logs,
    # and debounce-state file so several watchdogs can run side by side. These
    # module globals are read at call time by log()/_existing_watchdog()/
    # load_state()/save_state()/_shutdown(), so setting them here (before any
    # of those run) is sufficient.
    global PIDFILE, SERVER_LOG, WATCHDOG_LOG, STATE_FILE
    _p = _paths_for(args.port, args.state_file)
    PIDFILE, SERVER_LOG = _p["pidfile"], _p["server_log"]
    WATCHDOG_LOG, STATE_FILE = _p["watchdog_log"], _p["state_file"]

    existing = _existing_watchdog()
    if existing:
        log(f"Another watchdog is already running (PID {existing}) - exiting.")
        return

    _rotate(SERVER_LOG)
    _rotate(WATCHDOG_LOG)

    proc = None  # current server subprocess
    # Debounce timers persist across restarts (a reboot must not immediately
    # re-trigger a cookie refresh). last_summary stays in-memory on purpose:
    # a fresh "Health:" line is logged shortly after boot for visibility.
    state = load_state()
    state["last_summary"] = 0.0

    def _shutdown():
        """Kill the server child, persist state, remove the pidfile on exit.

        Fixes the orphaned-server problem: closing the watch window (Ctrl+C)
        or killing the watchdog now stops the server too.
        """
        nonlocal proc
        save_state(state)
        if proc is not None and proc.poll() is None:
            log("Watchdog stopping - shutting down the server...")
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except OSError:
                    pass
        try:
            if os.path.exists(PIDFILE):
                os.remove(PIDFILE)
        except OSError:
            pass

    atexit.register(_shutdown)

    try:
        with open(PIDFILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass

    cmd = [sys.executable, SERVER, "--port", str(args.port)]
    if args.config:
        cmd += ["--config", args.config]

    restarts = []  # timestamps of restarts in the current window
    refresh_cmd = _default_refresh_cmd()
    while True:
        misses = 0  # fresh counter per server incarnation
        log(f"Starting server: {' '.join(cmd)}")
        try:
            out = open(SERVER_LOG, "a", encoding="utf-8")
            proc = subprocess.Popen(cmd, cwd=HERE, stdout=out,
                                    stderr=subprocess.STDOUT,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            log(f"Failed to launch server: {e}")
            time.sleep(30)
            continue
        log(f"Server started (PID {proc.pid})")
        booted_at = time.time()
        poll_delay = args.interval if args.interval else 10
        while True:
            time.sleep(poll_delay)
            if proc.poll() is not None:
                break  # server exited on its own
            if args.interval and time.time() - booted_at > BOOT_GRACE_SEC:
                health = fetch_health(args.port)
                if health:
                    misses = 0
                    # Only persist when a debounce timer actually changed: in
                    # normal operation the timers mutate a few times a day, so
                    # this skips ~2880 pointless tiny writes per day and the
                    # (small) lock/rename error window they open on Windows.
                    before = tuple(state[k] for k in PERSISTED_STATE_KEYS)
                    for ev in _maybe_warn_and_refresh(
                            health, state, args.cookie_age_h, args.bl_405_trigger,
                            refresh_cmd):
                        log(ev)
                    if args.bridge_stale_sec > 0:
                        for ev in _maybe_expire_stale_bridge(
                                health, state,
                                lambda sec: _expire_bridge(args.port, sec),
                                stale_sec=args.bridge_stale_sec):
                            log(ev)
                    for ev in _maybe_warn_stale_extension(
                            health, state, _on_disk_ext_version()):
                        log(ev)
                    if time.time() - state["last_summary"] >= SUMMARY_EVERY_SEC:
                        state["last_summary"] = time.time()
                        log("Health: " + _health_summary(health))
                    if tuple(state[k] for k in PERSISTED_STATE_KEYS) != before:
                        # Hard kills (autostart's Stop-Process) never run
                        # atexit, so the loop is the reliable save point.
                        save_state(state)
                else:
                    misses += 1
                    if misses >= 3:
                        log(f"Server unresponsive ({misses} missed health checks) - killing PID {proc.pid}")
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        break
        code = proc.poll()
        out.close()
        now = time.time()
        restarts = [t for t in restarts if now - t < WINDOW_SEC]
        restarts.append(now)
        log(f"Server stopped (exit code {code}). Restarts in last 5 min: {len(restarts)}")
        if len(restarts) >= args.max_restarts:
            log(f"Too many restarts ({len(restarts)}) - sleeping {BACKOFF_SEC // 60} minutes")
            time.sleep(BACKOFF_SEC)
            restarts.clear()


if __name__ == "__main__":
    main()
