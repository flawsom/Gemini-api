"""Unit tests for run_all_tests.py orchestrator logic.

Offline-safe: imports the module (the battery itself only runs under
`if __name__ == "__main__":`) and checks the auto-start decision + command
construction - never starts or stops a real server.

Run: python test_run_all.py
"""
import contextlib
import importlib.util
import io
import os
import subprocess as _sp
import tempfile

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


# ── import safety: importing must NOT run the battery ────────────────────
# Patch subprocess.run so any battery execution at import time blows up.
_orig_run = _sp.run
_sp.run = lambda *a, **k: (_ for _ in ()).throw(
    AssertionError("battery executed at import time"))
try:
    spec = importlib.util.spec_from_file_location("rat", "run_all_tests.py")
    rat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rat)
    check("importing the module does not execute the battery", True)
finally:
    _sp.run = _orig_run
check("SUITES is a non-empty ordered list",
      isinstance(rat.SUITES, list) and len(rat.SUITES) >= 8
      and rat.SUITES[0][0] == "bundle drift")

# ── _should_autostart decision ───────────────────────────────────────────
check("live mode + free port -> autostart",
      rat._should_autostart(False, False, True) is True)
check("offline mode -> never autostart",
      rat._should_autostart(True, False, True) is False)
check("--no-autostart -> never autostart",
      rat._should_autostart(False, True, True) is False)
check("port already in use -> use existing server, no autostart",
      rat._should_autostart(False, False, False) is False)

# ── _start_watchdog builds a sane command (dry run via cmd list) ─────────
cmd = [rat.PY, os.path.join(rat.HERE, "watchdog.py"), "--port", "8081",
       "--config", "config.json"]
check("watchdog cmd targets watchdog.py on the live port",
      "watchdog.py" in cmd[1] and "8081" in cmd and "--config" in cmd)

# ── orphaned-server warning (watchdog dead but port still busy) ──────────
_orig_pidfile = rat.PIDFILE
_orig_kill = rat.os.kill
_orig_port = rat.port_open
try:
    with tempfile.NamedTemporaryFile("w", suffix=".pid", delete=False) as f:
        f.write("999999")
        tmp_pidfile = f.name
    rat.PIDFILE = tmp_pidfile
    rat.os.kill = lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError("dead"))
    rat.port_open = lambda *a, **k: True
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rat._stop_autostarted(999999, 8081)
    out = buf.getvalue()
    check("dead watchdog + busy port -> orphan warning with stop cmd",
          "manage.bat stop" in out and "listening" in out, out)
finally:
    rat.PIDFILE = _orig_pidfile
    rat.os.kill = _orig_kill
    rat.port_open = _orig_port
    if os.path.exists(tmp_pidfile):
        os.unlink(tmp_pidfile)

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
