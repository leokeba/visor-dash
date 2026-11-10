"""Scheduled backups of the controller.

    python3 -m visor_dash backup

Saves into backups/:
  auto-vauban-<stamp>.BACKUP      the controller's own backup (restorable by Vauban tools)
  auto-user-forms-<stamp>.json    every person's full form + the raw user list
  last-backup.json                status read by the dashboard

Only files starting with "auto-" are rotated (newest KEEP of each kind are
kept). Backups you make by hand are never deleted.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .client import ControllerError, VaubanClient, parse_users
from .users import FormError, UserManager, record_matches_form

KEEP = 30
STATUS_FILE = "last-backup.json"
STALE_AFTER_HOURS = 36


def _private_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def rotate(folder: Path, prefix: str, suffix: str, keep: int = KEEP) -> List[str]:
    """Delete all but the newest `keep` files named <prefix>*<suffix>. Returns deleted names."""
    files = sorted(p for p in folder.glob(f"{prefix}*{suffix}") if p.is_file())
    doomed = files[:-keep] if keep > 0 else files
    for p in doomed:
        p.unlink()
    return [p.name for p in doomed]


def snapshot_forms(client: VaubanClient, manager: UserManager, log: Callable[[str], None]) -> dict:
    raw = client.get("/assets/Users")
    users = parse_users(raw)
    forms, problems = [], []
    for u in users:
        try:
            f = manager._form_for(u)
        except (FormError, ControllerError) as e:
            problems.append({"slot": u.index, "error": str(e)})
            continue
        diffs = record_matches_form(u, f)
        if diffs:
            problems.append({"slot": u.index, "error": "form disagrees with user list: " + ", ".join(diffs)})
        forms.append({"slot": u.index, "form": f.values()})
    log(f"  {len(forms)} of {len(users)} people saved, {len(problems)} problem(s)")
    return {"users_file_hex": raw.hex(), "forms": forms, "problems": problems, "people": len(users)}


def run_backup(client: VaubanClient, manager: UserManager, folder: Path, keep: int = KEEP,
               log: Callable[[str], None] = print) -> dict:
    folder.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    status: Dict[str, object] = {"started": datetime.now().isoformat(timespec="seconds"), "ok": False}
    t0 = time.monotonic()
    try:
        log("Official backup…")
        data = client.download_backup()
        official = folder / f"auto-vauban-{stamp}.BACKUP"
        _private_write(official, data)
        status["official_file"] = official.name
        status["official_bytes"] = len(data)
        log(f"  {official.name} ({len(data)} bytes)")

        log("Per-person snapshot…")
        snap = snapshot_forms(client, manager, log)
        snap["taken"] = stamp
        forms_file = folder / f"auto-user-forms-{stamp}.json"
        _private_write(forms_file, json.dumps(snap, ensure_ascii=False).encode("utf-8"))
        status["forms_file"] = forms_file.name
        status["people"] = snap["people"]
        status["problems"] = len(snap["problems"])

        deleted = rotate(folder, "auto-vauban-", ".BACKUP", keep) + rotate(folder, "auto-user-forms-", ".json", keep)
        if deleted:
            log(f"Removed {len(deleted)} old automatic backup file(s).")
        status["ok"] = True
    except (ControllerError, OSError) as e:
        status["error"] = str(e)
        log(f"Backup failed: {e}")
    status["seconds"] = round(time.monotonic() - t0, 1)
    return _save_status(folder, status)


def _save_status(folder: Path, status: dict) -> dict:
    status["finished"] = datetime.now().isoformat(timespec="seconds")
    # Keep the last successful time even when this run fails.
    previous = read_status(folder) or {}
    status["last_success"] = status["finished"] if status.get("ok") else previous.get("last_success")
    folder.mkdir(mode=0o700, exist_ok=True)
    _private_write(folder / STATUS_FILE, json.dumps(status, indent=2).encode("utf-8"))
    return status


def read_status(folder: Path) -> Optional[dict]:
    try:
        return json.loads((folder / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status_summary(folder: Path, now: Optional[datetime] = None) -> dict:
    """What the dashboard shows: is there a recent, successful backup?"""
    s = read_status(folder)
    now = now or datetime.now()
    if not s:
        return {"state": "none", "last_success": None}
    last = s.get("last_success")
    age_h = None
    if last:
        try:
            age_h = (now - datetime.fromisoformat(last)).total_seconds() / 3600
        except ValueError:
            age_h = None
    if not s.get("ok"):
        state = "failed"
    elif age_h is None or age_h > STALE_AFTER_HOURS:
        state = "stale"
    else:
        state = "ok"
    return {"state": state, "last_success": last, "age_hours": None if age_h is None else round(age_h, 1),
            "last_error": s.get("error"), "problems": s.get("problems", 0)}


def main(cfg: Dict[str, str], root: Path) -> int:
    folder = root / "backups"
    log = lambda m: print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {m}", flush=True)
    client = VaubanClient(cfg["VAUBAN_HOST"], cfg["VAUBAN_USER"], cfg["VAUBAN_PASSWORD"],
                          port=int(cfg.get("VAUBAN_PORT", "80")))   # read-only client
    try:
        trans = client.translations(cfg.get("VAUBAN_LANG", "en"))
        groups = client.labels("Groupes", trans.get("strGroupe", "Group"))
        slots = client.labels("Plages", trans.get("strPlage", "Slot"))
    except ControllerError as e:
        log(f"Backup failed: controller unreachable ({e})")
        status = _save_status(folder, {"started": datetime.now().isoformat(timespec="seconds"),
                                       "ok": False, "error": f"controller unreachable ({e})"})
        print(json.dumps(status), flush=True)
        return 1
    manager = UserManager(client, lambda: trans, lambda: groups, lambda: slots)
    keep = int(cfg.get("BACKUP_KEEP", str(KEEP)))
    status = run_backup(client, manager, folder, keep=keep, log=log)
    print(json.dumps(status), flush=True)
    return 0 if status["ok"] else 1

