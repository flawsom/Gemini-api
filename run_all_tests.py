#!/usr/bin/env python3
"""Run the full test battery: every Python + Node suite, one summary.

Usage:
    python run_all_tests.py            # full battery. If nothing is listening
                                       # on 127.0.0.1:8081 it AUTO-STARTS the
                                       # server (watchdog.py, same command as
                                       # manage.bat start), runs everything,
                                       # then STOPS the watchdog it started.
                                       # An existing server is used and left
                                       # untouched.
    python run_all_tests.py --no-autostart   # never start/stop a server
    python run_all_tests.py --offline  # mock/no-cookie mode - unit tests, a
                                       # stubbed ephemeral-port integration test,
                                       # no browser, no server, no config.json
                                       # needed (this is what CI runs)

Exit code 0 only if every suite passed.
"""
import os
import shutil
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
OFFLINE = "--offline" in sys.argv
NO_AUTOSTART = "--no-autostart" in sys.argv
LIVE_PORT = 8081
LIVE_CONFIG = "config.json"
BOOT_TIMEOUT_SEC = 60      # wait this long for an auto-started server to answer
BOOT_POLL_SEC = 2
PIDFILE = os.path.join(HERE, "watchdog.pid")

SUITES = [
    ("bundle drift", [PY, "bundle.py", "--check"]),
    ("main suite", [PY, "test_suite.py"] + (["--offline"] if OFFLINE else [])),
    ("proxy fallback", [PY, "test_proxy_fallback.py"]),
    ("multimodal proxy", [PY, "test_multimodal_proxy.py"]),
    ("payload format", [PY, "test_payload_format.py"]),
    ("image bridge", [PY, "test_image_bridge.py"]),
    ("image bridge cdp", [PY, "test_image_bridge_cdp.py"]),
    ("cookie refresh", [PY, "test_cookie_refresh.py"] + (["--offline"] if OFFLINE else [])),
    ("watchdog", [PY, "test_watchdog.py"]),
    ("orchestrator", [PY, "test_run_all.py"]),
    ("autostart", [PY, "test_autostart.py"]),
    ("server integration", [PY, "test_integration.py"]),
    ("sse protocol", [PY, "test_sse.py"]),
    ("extension", ["node", "test_extension.js"]),
    ("popup", ["node", "test_popup.js"]),
]

NODE_MISSING = shutil.which("node") is None


def port_open(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _should_autostart(offline: bool, no_autostart: bool, port_free: bool) -> bool:
    """Decision helper (pure, unit-tested): live mode, not opted out, port free."""
    return not offline and not no_autostart and port_free


def _start_watchdog(port: int, config: str):
    """Launch the watchdog DETACHED (pythonw, hidden window on Windows)."""
    cmd = [PY, os.path.join(HERE, "watchdog.py"), "--port", str(port)]
    if config:
        cmd += ["--config", config]
    flags = 0
    if os.name == "nt":
        pyw = shutil.which("pythonw")
        if pyw:
            cmd[0] = pyw
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.Popen(cmd, cwd=HERE, creationflags=flags,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        print(f"Could not auto-start the watchdog: {e}")
        return None


def _wait_for_server(port: int, timeout: int = BOOT_TIMEOUT_SEC) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open("127.0.0.1", port, timeout=2):
            return True
        time.sleep(BOOT_POLL_SEC)
    return False


def _stop_autostarted(pid: int, port: int):
    """Stop ONLY the watchdog we launched (and the server it runs).

    Safety: stop_watchdog kills every watchdog it can find, so before calling
    it we require BOTH the watchdog.pid to match the PID we launched AND that
    process to still be alive. If anyone/anything took the port over, we leave
    it alone rather than killing someone else's server.
    """
    try:
        with open(PIDFILE) as f:
            cur = f.read().strip()
    except OSError:
        print("Auto-started watchdog left no pidfile - nothing to stop.")
        return
    if cur != str(pid):
        print(f"Watchdog on :{port} is PID {cur} (not ours, {pid}) - leaving it untouched.")
        return
    try:
        os.kill(pid, 0)  # liveness probe (works on Windows for same-user processes)
    except OSError:
        # Our watchdog is gone, but its server CHILD may still be running
        # orphaned on the port. Ownership of the port is now ambiguous (the
        # user may have started their own server in the meantime) - warn and
        # give the stop command instead of auto-killing something unknown.
        if port_open("127.0.0.1", port):
            print(f"Auto-started watchdog exited, but something is still "
                  f"listening on :{port} (possibly its orphaned server). "
                  f"If that is not your own server, stop it with: "
                  f"manage.bat stop")
        else:
            print("Auto-started watchdog already exited on its own.")
        return
    print(f"Stopping the auto-started watchdog (PID {pid})...")
    try:
        subprocess.run([PY, os.path.join(HERE, "autostart.py"), "stop-watchdog",
                        str(port)], capture_output=True, text=True, timeout=60)
    except Exception as e:
        print(f"Could not stop the auto-started watchdog: {e}")


def run(name, cmd):
    if NODE_MISSING and cmd[0] == "node":
        print(f"\n== {name} ==  SKIPPED (node not on PATH)")
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except FileNotFoundError:
        print(f"\n== {name} ==  FAIL (not found: {cmd[0]})")
        return False
    except subprocess.TimeoutExpired:
        print(f"\n== {name} ==  TIMEOUT after 900s")
        return False
    out = (r.stdout or "") + (r.stderr or "")
    lines = out.rstrip().splitlines()
    print(f"\n== {name} ==  exit={r.returncode}")
    # Always surface the diagnostic lines - a FAIL buried mid-output must never
    # be hidden by the tail window.
    diag = [ln for ln in lines if ("FAIL" in ln or "Traceback" in ln
                                   or "ERROR" in ln or "TIMEOUT" in ln)]
    shown = diag[-6:] if diag else lines[-6:]
    for ln in shown:
        print("   " + ln)
    return r.returncode == 0


def main():
    autostarted_pid = None

    if not OFFLINE:
        if port_open("127.0.0.1", LIVE_PORT):
            print(f"Using the existing server on 127.0.0.1:{LIVE_PORT} "
                  f"(left untouched).")
        elif NO_AUTOSTART:
            print(f"WARNING: nothing is listening on 127.0.0.1:{LIVE_PORT} and "
                  f"--no-autostart was given - the live sections of the main "
                  f"suite will fail. Use --offline to skip them.")
        else:
            print(f"No server on 127.0.0.1:{LIVE_PORT} - auto-starting the "
                  f"watchdog for this battery...")
            proc = _start_watchdog(LIVE_PORT, LIVE_CONFIG)
            if proc is None:
                print("Could not auto-start the server - the live sections of "
                      "the main suite will fail.")
            elif _wait_for_server(LIVE_PORT):
                autostarted_pid = proc.pid
                print(f"Auto-started server is up (watchdog PID {proc.pid}) - "
                      f"running the live battery.")
            else:
                print(f"Auto-started server did not answer within "
                      f"{BOOT_TIMEOUT_SEC}s - aborting.")
                _stop_autostarted(proc.pid, LIVE_PORT)
                sys.exit(1)

    results = {}
    try:
        for name, cmd in SUITES:
            results[name] = run(name, cmd)
    finally:
        if autostarted_pid is not None:
            _stop_autostarted(autostarted_pid, LIVE_PORT)
            print("Auto-started watchdog stopped - your machine is back to how "
                  "it was before the battery.")

    print("\n" + "=" * 60)
    print("SUMMARY" + (" (offline mode)" if OFFLINE else ""))
    print("=" * 60)
    failed = []
    for name, ok in results.items():
        mark = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
        print(f"  {mark:5s}  {name}")
        if ok is False:
            failed.append(name)
    print("=" * 60)
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        sys.exit(1)
    print("ALL SUITES PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
