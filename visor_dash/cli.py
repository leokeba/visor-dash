"""Command line:  visor-dash [--home DIR] [command]

  (no command)        run the dashboard (asks for the settings first if there are none)
  serve               run the dashboard
  setup               ask for the controller's address and login, test them, save the settings
  backup              take a backup now (the nightly job runs this)
  install-service     start the dashboard at login and back up every night
  uninstall-service   remove those background jobs
  where               show where settings, backups, history and logs are kept
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__, settings


def _redirect_output(log_file: Optional[str]) -> None:
    """Background runs on Windows have no console: write output to a file instead."""
    if not log_file:
        return
    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = fh


def _ask(prompt: str, default: str = "") -> str:
    shown = f" [{default}]" if default else ""
    answer = input(f"{prompt}{shown}: ").strip()
    return answer or default


def _yes(prompt: str, default: bool = False) -> bool:
    answer = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not answer else answer in ("y", "yes", "o", "oui")


def setup(home: Path) -> int:
    from .client import ControllerError, VaubanClient

    current = settings.load(home)
    print(f"visor-dash {__version__}: settings are saved in {settings.env_file(home)}\n")
    if current.get("VAUBAN_HOST") and not _yes("Settings already exist. Replace them?"):
        return 0
    host = _ask("Controller IP address", current.get("VAUBAN_HOST", ""))
    port = _ask("Controller web port", current.get("VAUBAN_PORT", "80"))
    user = _ask("Controller login", current.get("VAUBAN_USER", "admin"))
    password = getpass.getpass("Controller password: ") or current.get("VAUBAN_PASSWORD", "")
    print("\nChecking the connection…")
    try:
        versions = VaubanClient(host, user, password, port=int(port), timeout=10).versions()
        print(f"Connected. Controller firmware {versions.get('controller', '?')}, "
              f"web server {versions.get('web_server', '?')}.")
    except (ControllerError, ValueError) as e:
        print(f"Could not reach the controller: {e}")
        if not _yes("Save these settings anyway?"):
            return 1
    site = _ask("\nName shown at the top of the dashboard", current.get("DASH_SITE_NAME", "Badge controller"))
    lang = _ask("Language of event names (en, fr, it)", current.get("VAUBAN_LANG", "en"))
    writes = _yes("Allow editing people, groups and time slots from the dashboard?",
                  current.get("DASH_ALLOW_WRITES") == "1")
    doors = _yes("Allow opening doors from the dashboard?", current.get("DASH_ALLOW_DOOR_OPEN") == "1")
    path = settings.write(home, {"host": host, "port": port, "user": user, "password": password,
                                 "lang": lang, "site": site, "writes": "1" if writes else "0",
                                 "door_open": "1" if doors else "0"})
    print(f"\nSaved {path} (readable by you only).")
    print("Next: run \"visor-dash\" to start the dashboard, or \"visor-dash install-service\" "
          "to have it start at login with a nightly backup.")
    return 0


def where(home: Path) -> int:
    print(f"Home:      {home}")
    print(f"Settings:  {settings.env_file(home)}{'' if settings.env_file(home).exists() else '  (not created yet)'}")
    print(f"Backups:   {home / 'backups'}")
    print(f"History:   {home / 'data' / 'history.sqlite3'}")
    print(f"Logs:      {settings.logs_dir(home)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="visor-dash", description="Local dashboard for Vauban VISOR+ / VERSO+ access controllers.")
    p.add_argument("--version", action="version", version=f"visor-dash {__version__}")
    p.add_argument("--home", help="folder for settings, backups and history (default: see 'visor-dash where')")
    sub = p.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="run the dashboard")
    serve.add_argument("--port", type=int, help="override DASH_PORT")
    serve.add_argument("--open", action="store_true", help="open it in the browser")
    serve.add_argument("--log-file", help="write output to this file")
    backup = sub.add_parser("backup", help="take a backup now")
    backup.add_argument("--log-file", help="write output to this file")
    sub.add_parser("setup", help="create the settings")
    sub.add_parser("install-service", help="start at login, back up every night")
    sub.add_parser("uninstall-service", help="remove the background jobs")
    sub.add_parser("where", help="show where files are kept")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    home = Path(args.home).expanduser().resolve() if args.home else settings.home()
    command = args.command

    if command == "setup":
        return setup(home)
    if command == "where":
        return where(home)
    if command in ("install-service", "uninstall-service"):
        from . import service
        try:
            lines = service.install(home) if command == "install-service" else service.uninstall(home)
        except (service.ServiceError, OSError) as e:
            print(f"Could not {command.replace('-', ' ')}: {e}", file=sys.stderr)
            return 1
        for line in lines:
            print(line)
        return 0
    if command == "backup":
        _redirect_output(args.log_file)
        from . import backup
        cfg = settings.load(home)
        if not all(cfg.get(k) for k in ("VAUBAN_HOST", "VAUBAN_USER", "VAUBAN_PASSWORD")):
            print(f"No controller settings in {settings.env_file(home)}. Run \"visor-dash setup\".", file=sys.stderr)
            return 2
        return backup.main(cfg, home)

    from . import server
    if command is None:
        # Started without a command (e.g. double-clicked): set up first if needed, then open the browser.
        if not settings.env_file(home).exists() and sys.stdin and sys.stdin.isatty():
            if setup(home) != 0:
                return 1
        return server.main(home, open_browser=bool(sys.stdin and sys.stdin.isatty()))
    _redirect_output(args.log_file)
    return server.main(home, port_override=args.port, open_browser=args.open)
