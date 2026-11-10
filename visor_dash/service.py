"""Run the dashboard in the background, and the backup every night.

  macOS    launchd agents   ~/Library/LaunchAgents/local.visor-dash.{dashboard,backup}.plist
  Linux    systemd --user   ~/.config/systemd/user/visor-dash{,-backup}.{service,timer}
  Windows  Startup folder   visor-dash.vbs (hidden, at login) + daily task "visor-dash backup"

All of them run for the current user only and need no administrator rights.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List

from . import settings

LABEL_DASH = "local.visor-dash.dashboard"
LABEL_BACKUP = "local.visor-dash.backup"
BACKUP_HOUR, BACKUP_MINUTE = 3, 15


def base_command(home: Path) -> List[str]:
    """How to start this program again: the app itself, or python -m visor_dash from source."""
    exe = [sys.executable] if settings.is_frozen() else [sys.executable, "-m", "visor_dash"]
    return exe + ["--home", str(home)]


def working_dir(home: Path) -> Path:
    return home if settings.is_frozen() else settings.source_root()


class ServiceError(Exception):
    """A system command needed to set up the background jobs failed."""


def _run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise ServiceError(f"{' '.join(cmd[:3])} failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()}")
    return r


# --------------------------------------------------------------------------- macOS

def _plist(label: str, args: List[str], cwd: Path, log: Path, extra: str) -> str:
    from xml.sax.saxutils import escape
    arg_xml = "".join(f"<string>{escape(a)}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>{arg_xml}</array>
  <key>WorkingDirectory</key><string>{escape(str(cwd))}</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>StandardOutPath</key><string>{escape(str(log))}</string>
  <key>StandardErrorPath</key><string>{escape(str(log))}</string>
{extra}
</dict>
</plist>
"""


def _mac_install(home: Path) -> List[str]:
    agents = Path.home() / "Library" / "LaunchAgents"
    logs = settings.logs_dir(home)
    agents.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    cmd, cwd, domain = base_command(home), working_dir(home), f"gui/{os.getuid()}"
    jobs = {
        LABEL_DASH: _plist(LABEL_DASH, cmd + ["serve"], cwd, logs / "dashboard.log",
                           "  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n"
                           "  <key>ThrottleInterval</key><integer>30</integer>"),
        LABEL_BACKUP: _plist(LABEL_BACKUP, cmd + ["backup"], cwd, logs / "backup.log",
                             f"  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>{BACKUP_HOUR}</integer>"
                             f"<key>Minute</key><integer>{BACKUP_MINUTE}</integer></dict>"),
    }
    for label, text in jobs.items():
        path = agents / f"{label}.plist"
        path.write_text(text, encoding="utf-8")
        _run(["plutil", "-lint", str(path)])
        _launchd_remove(domain, label)
        _launchd_load(domain, label, path)
    return [f"Dashboard starts at login and restarts if it stops (launchd: {LABEL_DASH}).",
            f"Backup every night at {BACKUP_HOUR:02d}:{BACKUP_MINUTE:02d}, or at wake-up if the Mac was asleep.",
            f"Logs: {logs}"]


def _launchd_remove(domain: str, label: str) -> None:
    """bootout is asynchronous: wait until the job is really gone (or 10 s)."""
    _run(["launchctl", "bootout", f"{domain}/{label}"], check=False)
    for _ in range(50):
        if _run(["launchctl", "print", f"{domain}/{label}"], check=False).returncode != 0:
            return
        time.sleep(0.2)


def _launchd_load(domain: str, label: str, path: Path) -> None:
    for attempt in range(5):
        r = _run(["launchctl", "bootstrap", domain, str(path)], check=False)
        if r.returncode == 0:
            break
        if attempt == 4:
            raise ServiceError(f"launchctl could not load {label} (error {r.returncode}): {r.stderr.strip()}")
        time.sleep(1)
    _run(["launchctl", "enable", f"{domain}/{label}"])


def _mac_uninstall() -> List[str]:
    domain = f"gui/{os.getuid()}"
    for label in (LABEL_DASH, LABEL_BACKUP):
        _launchd_remove(domain, label)
        (Path.home() / "Library" / "LaunchAgents" / f"{label}.plist").unlink(missing_ok=True)
    return ["Background jobs removed."]


# --------------------------------------------------------------------------- Linux

def _systemd_quote(args: List[str]) -> str:
    return " ".join('"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"' for a in args)


def _linux_install(home: Path) -> List[str]:
    units = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "systemd" / "user"
    units.mkdir(parents=True, exist_ok=True)
    cmd, cwd = base_command(home), working_dir(home)
    files = {
        "visor-dash.service": f"""[Unit]
Description=visor-dash badge dashboard
After=network-online.target

[Service]
ExecStart={_systemd_quote(cmd + ["serve"])}
WorkingDirectory={cwd}
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
""",
        "visor-dash-backup.service": f"""[Unit]
Description=visor-dash nightly backup

[Service]
Type=oneshot
ExecStart={_systemd_quote(cmd + ["backup"])}
WorkingDirectory={cwd}
""",
        "visor-dash-backup.timer": f"""[Unit]
Description=visor-dash nightly backup

[Timer]
OnCalendar=*-*-* {BACKUP_HOUR:02d}:{BACKUP_MINUTE:02d}:00
Persistent=true

[Install]
WantedBy=timers.target
""",
    }
    for name, text in files.items():
        (units / name).write_text(text, encoding="utf-8")
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "--now", "visor-dash.service", "visor-dash-backup.timer"])
    return ["Dashboard runs as a systemd user service (visor-dash.service) and restarts if it stops.",
            f"Backup every night at {BACKUP_HOUR:02d}:{BACKUP_MINUTE:02d} (visor-dash-backup.timer).",
            "Logs: journalctl --user -u visor-dash",
            "To keep it running while nobody is logged in: loginctl enable-linger $USER"]


def _linux_uninstall() -> List[str]:
    _run(["systemctl", "--user", "disable", "--now", "visor-dash.service", "visor-dash-backup.timer"], check=False)
    units = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "systemd" / "user"
    for name in ("visor-dash.service", "visor-dash-backup.service", "visor-dash-backup.timer"):
        (units / name).unlink(missing_ok=True)
    _run(["systemctl", "--user", "daemon-reload"], check=False)
    return ["Background jobs removed."]


# --------------------------------------------------------------------------- Windows

WIN_TASK = "visor-dash backup"


def _startup_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _vbs(args: List[str]) -> str:
    """A script that starts a command with no window (WScript Run style 0)."""
    cmdline = " ".join('""' + a + '""' for a in args)
    return f'CreateObject("WScript.Shell").Run "{cmdline}", 0, False\r\n'


def _windows_install(home: Path) -> List[str]:
    logs = settings.logs_dir(home)
    logs.mkdir(parents=True, exist_ok=True)
    cmd = base_command(home)
    if not settings.is_frozen() and cmd[0].lower().endswith("python.exe"):
        cmd[0] = cmd[0][:-10] + "pythonw.exe"           # no console window from source either
    startup = _startup_dir() / "visor-dash.vbs"
    startup.write_text(_vbs(cmd + ["serve", "--log-file", str(logs / "dashboard.log")]), encoding="utf-8")
    backup_vbs = home / "visor-dash-backup.vbs"
    backup_vbs.write_text(_vbs(cmd + ["backup", "--log-file", str(logs / "backup.log")]), encoding="utf-8")
    _run(["schtasks", "/Create", "/F", "/TN", WIN_TASK, "/SC", "DAILY",
          "/ST", f"{BACKUP_HOUR:02d}:{BACKUP_MINUTE:02d}", "/TR", f'wscript.exe "{backup_vbs}"'])
    subprocess.Popen(["wscript.exe", str(startup)])          # start it now as well
    return [f"Dashboard starts hidden at login ({startup}). It has been started now too.",
            f"Backup every day at {BACKUP_HOUR:02d}:{BACKUP_MINUTE:02d} (Task Scheduler: \"{WIN_TASK}\").",
            f"Logs: {logs}"]


def _windows_uninstall(home: Path) -> List[str]:
    (_startup_dir() / "visor-dash.vbs").unlink(missing_ok=True)
    (home / "visor-dash-backup.vbs").unlink(missing_ok=True)
    _run(["schtasks", "/Delete", "/F", "/TN", WIN_TASK], check=False)
    return ["Background jobs removed. A dashboard that is running now keeps running until you log out "
            "(or end visor-dash in Task Manager)."]


# --------------------------------------------------------------------------- entry points

def install(home: Path) -> List[str]:
    if not settings.env_file(home).exists():
        raise SystemExit(f"No settings in {home}. Run \"visor-dash setup\" first.")
    if sys.platform == "darwin":
        return _mac_install(home)
    if os.name == "nt":
        return _windows_install(home)
    if sys.platform.startswith("linux"):
        return _linux_install(home)
    raise SystemExit(f"Background service isn't supported on {sys.platform}.")


def uninstall(home: Path) -> List[str]:
    if sys.platform == "darwin":
        return _mac_uninstall()
    if os.name == "nt":
        return _windows_uninstall(home)
    if sys.platform.startswith("linux"):
        return _linux_uninstall()
    raise SystemExit(f"Background service isn't supported on {sys.platform}.")
