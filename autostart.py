#!/usr/bin/env python3
"""autostart.py - install/uninstall Windows auto-start for the API server.

Strategy (no admin required for the fallback):
  1. Try a Task Scheduler ONLOGON task (schtasks) - cleanest option.
  2. If the OS denies it, create a Startup-folder shortcut to
     pythonw.exe + watchdog.py - fully silent (pythonw has no console).

Usage:
    python autostart.py install
    python autostart.py uninstall
    python autostart.py status
    python autostart.py health [port]   # health summary from GET /
"""
import json
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TASK = "GeminiWeb2API"
PORT = "8081"
WATCHDOG = os.path.join(HERE, "watchdog.py")
CONFIG = os.path.join(HERE, "config.json")
LNK = os.path.join(
    os.environ.get("APPDATA", ""),
    "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
    "GeminiWeb2API.lnk",
)

PS_SCRIPT = r"""
$q = [char]34
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($env:GW2A_LNK)
$s.TargetPath = $env:GW2A_PYW
$s.Arguments = $q + $env:GW2A_WATCHDOG + $q + ' --port ' + $env:GW2A_PORT + ' --config ' + $q + $env:GW2A_CONFIG + $q
$s.WorkingDirectory = $env:GW2A_HERE
$s.Description = 'Gemini Web2API server (auto-starts at login)'
$s.Save()
""".strip()


def _find_pythonw():
    try:
        r = subprocess.run(["where", "pythonw"], capture_output=True, text=True)
    except OSError:
        return None
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            if line.strip():
                return line.strip()
    return None


def _task_exists():
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK],
                       capture_output=True, text=True)
    return r.returncode == 0


def install() -> int:
    pythonw = _find_pythonw()
    if not pythonw:
        print("[ERROR] pythonw.exe not found - cannot install silent autostart.")
        print("        Reinstall Python with the launcher and PATH options.")
        return 1

    # 1) Try Task Scheduler (no cmd shell -> no quoting surprises)
    tr = f'"{pythonw}" "{WATCHDOG}" --port {PORT} --config "{CONFIG}"'
    r = subprocess.run(
        ["schtasks", "/Create", "/TN", TASK, "/TR", tr,
         "/SC", "ONLOGON", "/RL", "LIMITED", "/F"],
        capture_output=True, text=True)
    if r.returncode == 0:
        print("Installed: auto-start at login via Task Scheduler (%s)." % TASK)
        print("Gemini Web2API will start silently in the background at every login.")
        return 0

    # 2) Fallback: Startup-folder shortcut (no admin needed)
    print("Task Scheduler denied (%s) - using the Startup folder instead."
          % (r.stderr.strip() or "access denied"))
    env = os.environ.copy()
    env.update({
        "GW2A_PYW": pythonw,
        "GW2A_WATCHDOG": WATCHDOG,
        "GW2A_CONFIG": CONFIG,
        "GW2A_HERE": HERE,
        "GW2A_PORT": PORT,
        "GW2A_LNK": LNK,
    })
    r = subprocess.run(["powershell", "-NoProfile", "-Command", PS_SCRIPT],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print("[ERROR] Could not create the Startup shortcut:",
              r.stderr.strip() or r.stdout.strip())
        print("        Try:  manage.bat install   from an Administrator console.")
        return 1
    print("Installed: auto-start at login via Startup folder.")
    print("Gemini Web2API will start silently in the background at every login.")
    return 0


def stop_watchdog(port: int = 8081) -> int:
    """Safely stop the running watchdog and server.

    Safety: before killing the PID listed in watchdog.pid, verify its command
    line actually contains watchdog.py - a stale pidfile must never take down
    an unrelated process. Only the process owning our port is killed.
    """
    # Kill every live watchdog - the command-line match ('*watchdog.py*') IS
    # the safety check, so a stale pidfile can never take down an unrelated
    # process. Handles the pidfile-free / multi-watchdog edge cases too.
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -like 'python*' -and "
        "$_.CommandLine -like '*watchdog.py*' -and $_.ProcessId -ne $PID } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, text=True, timeout=30)
    # stop whatever listens on our port (the server itself)
    ps2 = (
        "Get-NetTCPConnection -LocalPort %d -State Listen -ErrorAction SilentlyContinue | "
        "ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }" % port
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps2],
                   capture_output=True, text=True, timeout=30)
    # Remove the pidfile for this port only (a Stop-Process -Force bypasses
    # atexit, so the file lingers): the legacy watchdog.pid for the default
    # port, the per-port watchdog-<port>.pid otherwise. Never touch another
    # port's pidfile - a stop of one instance must not orphan another's guard.
    name = "watchdog.pid" if port == 8081 else f"watchdog-{port}.pid"
    try:
        os.remove(os.path.join(HERE, name))
    except OSError:
        pass
    print("Gemini Web2API stopped.")
    return 0


def uninstall() -> int:
    if _task_exists():
        subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"],
                       capture_output=True, text=True)
        print("Removed Task Scheduler autostart entry.")
    if os.path.exists(LNK):
        try:
            os.remove(LNK)
            print("Removed Startup-folder shortcut.")
        except OSError as e:
            print("[ERROR] Could not remove shortcut:", e)
    print("Auto-start removed.")
    return 0


def status() -> int:
    if _task_exists():
        print("autostart: INSTALLED via Task Scheduler")
        return 0
    if os.path.exists(LNK):
        print("autostart: INSTALLED via Startup folder")
        return 0
    print("autostart: NOT installed")
    return 1


# ─── health summary (manage.bat status / manage.bat health) ────────────────

def fetch_health(port: int = 8081, timeout: int = 5) -> dict | None:
    """GET / on the local server; parsed payload or None if unreachable."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/",
                                     headers={"User-Agent": "manage.bat"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def format_health_summary(health: dict, age_warn_h: float = 24.0) -> str:
    """Human-readable health summary for manage.bat status (pure, tested)."""
    cookie = health.get("cookie") or {}
    age = cookie.get("age_sec")
    if isinstance(age, (int, float)):
        age_s = f"{age / 3600:.1f}h ({int(age)}s)"
        stale = age > age_warn_h * 3600
    else:
        age_s = "n/a (no cookie file)"
        stale = False
    bl405 = health.get("bl_405_count", 0) or 0
    lines = [
        f"  status:      {health.get('status', '?')}",
        f"  build label: {health.get('gemini_bl') or 'n/a'}",
        f"  cookie age:  {age_s}",
        f"  refresh:     {'IN FLIGHT' if cookie.get('refresh_requested') else 'not in flight'}",
        f"  405 streak:  {bl405}",
        f"  proxy plan:  {health.get('proxy', {}).get('plan')}",
    ]
    if stale:
        lines.append(f"  WARNING: cookies are {age / 3600:.1f}h old - refresh "
                     f"recommended (manage.bat cookies)")
    if bl405 >= 3:
        lines.append("  WARNING: build label is 405-ing repeatedly - cookies "
                     "may be stale")
    return "\n".join(lines)


def health(port: int = 8081) -> int:
    h = fetch_health(port)
    if h is None:
        print(f"Could not reach http://127.0.0.1:{port}/ - is the server "
              f"running? (manage.bat start)")
        return 1
    print(f"Health (http://127.0.0.1:{port}/):")
    print(format_health_summary(h))
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "status"
    if arg == "install":
        sys.exit(install())
    elif arg == "uninstall":
        sys.exit(uninstall())
    elif arg == "stop-watchdog":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8081
        sys.exit(stop_watchdog(port))
    elif arg == "health":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8081
        sys.exit(health(port))
    else:
        sys.exit(status())
