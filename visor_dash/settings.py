"""Where the dashboard keeps its files, and its settings file.

Home folder (settings, backups, history, logs):
  * VISOR_DASH_HOME if set;
  * running from source: the project folder (next to visor_dash/);
  * installed app: the usual per-user data folder of the OS.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict

APP = "visor-dash"

TEMPLATE = """\
# visor-dash settings. Keep this file private: it holds the controller password.
# Create it with "visor-dash setup", or copy .env.example to .env and edit it.

# Controller
VAUBAN_HOST={host}
VAUBAN_PORT={port}
VAUBAN_USER={user}
VAUBAN_PASSWORD={password}
# Language of event labels, read from the controller: en, fr or it
VAUBAN_LANG={lang}

# Dashboard
DASH_SITE_NAME={site}
DASH_PORT=8080
# 127.0.0.1 = this computer only. To open it to other computers on the LAN,
# set DASH_BIND=0.0.0.0 AND a DASH_PASSWORD (any username works in the browser prompt).
DASH_BIND=127.0.0.1
DASH_PASSWORD=

# Editing people, groups and time slots. 0 = read-only.
# Every change is verified against the controller and logged to backups/audit.jsonl.
DASH_ALLOW_WRITES={writes}

# Opening doors from the dashboard (momentary unlock, like the door's exit button).
# Separate from DASH_ALLOW_WRITES. Every opening is logged to backups/audit.jsonl.
DASH_ALLOW_DOOR_OPEN={door_open}

# Long-term event history (data/history.sqlite3): how many days to keep.
# 90 follows the CNIL guidance for access-control logs (3 months). 0 = keep forever.
HISTORY_DAYS=90

# Automatic backups to keep (one per night).
BACKUP_KEEP=30
"""

EXAMPLE_VALUES = {"host": "192.168.1.50", "port": "80", "user": "admin", "password": "change-me",
                  "lang": "en", "site": "Main building", "writes": "0", "door_open": "0"}


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def home() -> Path:
    env = os.environ.get("VISOR_DASH_HOME")
    if env:
        return Path(env).expanduser().resolve()
    if not is_frozen():
        return source_root()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")) / APP
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / APP


def env_file(base: Path) -> Path:
    return base / ".env"


def logs_dir(base: Path) -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / APP
    return base / "logs"


def load(base: Path) -> Dict[str, str]:
    """Settings from <home>/.env, overridden by VAUBAN_* / DASH_* / HISTORY_* / BACKUP_* variables."""
    cfg: Dict[str, str] = {}
    f = env_file(base)
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():
        if k.startswith(("VAUBAN_", "DASH_", "HISTORY_", "BACKUP_")):
            cfg[k] = v
    return cfg


def write(base: Path, values: Dict[str, str]) -> Path:
    """Write a new settings file (owner-only permissions)."""
    base.mkdir(parents=True, exist_ok=True)
    f = env_file(base)
    text = TEMPLATE.format(**{**EXAMPLE_VALUES, **values})
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    return f


def example_text() -> str:
    return TEMPLATE.format(**EXAMPLE_VALUES)
