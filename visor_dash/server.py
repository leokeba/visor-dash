"""Local dashboard for the Vauban VISOR+2 / VERSO+ controller.

Run:  python3 -m visor_dash          (settings come from .env or environment variables)

Standard library only. Reading uses an allowlist of data files. Changing users
is off unless DASH_ALLOW_WRITES=1; it then goes through users.UserManager,
which verifies every write and logs it to backups/audit.jsonl.
"""
from __future__ import annotations

import base64
import pkgutil
import csv
import re
import hmac
import io
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from .client import (DELETED_USER_ID, GRANTED, IDENTIFIER_TYPES, ControllerError, RawEvent,
                     VaubanClient, event_category)
from . import settings
from . import updater
from .backup import status_summary
from .history import CATEGORY_CODES, EventArchive, Row, key_of
from .schedules import (ANY_TIME, DAY_KEYS, NO_ACCESS, ScheduleManager)
from .users import (interpret_response, ID_TYPES, SLOT_ALWAYS, SLOT_TIME_FROM_GROUP, FormError, UserChange,
                    UserManager, ValidationError, WriteFailed)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {  # explicit map: no path traversal possible
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

# Cache lifetimes, in seconds. The controller is a small embedded device.
TTL_USERS = 60
TTL_LABELS = 300
TTL_TRANSLATIONS = 3600
TTL_VERSIONS = 300
TTL_UPDATES = 60 * 60
MIN_POLL_INTERVAL = 10       # never fetch the newest events page more often than this
MIN_FORCED_POLL_INTERVAL = 2 # ...except while someone waits for a badge swipe or presses Refresh
UNKNOWN_BADGE_CODE = 0       # event "Identifiant inconnu": a badge the controller doesn't know
REMOTE_OPEN_CODE = 49        # event "Ouverture à distance": logged when a door is opened remotely
PUSH_BUTTON_CODE = 54
OPEN_CONFIRM_WAIT_S = 10     # how long to look for the controller's confirmation in its log
OPEN_COOLDOWN_S = 5          # at most one open command per door every few seconds
MAX_GAP_PAGES = 200          # after downtime, fetch up to 200 x 50 = 10,000 missed events at once
IMPORT_PAUSE_S = 0.3         # pause between pages of the background history import
PURGE_EVERY_S = 3600
MAX_BODY = 16 * 1024


# --------------------------------------------------------------------------- config

# --------------------------------------------------------------------------- service

class Cached:
    def __init__(self, ttl: float, loader: Callable):
        self.ttl, self.loader = ttl, loader
        self.value, self.at = None, 0.0
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            if self.value is None or time.monotonic() - self.at > self.ttl:
                self.value = self.loader()
                self.at = time.monotonic()
            return self.value


def parse_date(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s.strip(), "%d/%m/%Y %H:%M:%S")
    except ValueError:
        return None


class Dashboard:
    def __init__(self, client: VaubanClient, lang: str, site_name: str = "",
                 archive: Optional[EventArchive] = None):
        self.client = client
        self.site_name = site_name
        self.manager: Optional[UserManager] = None   # set when writes are enabled
        self.request_local = threading.local()       # who is making the current write
        self.backup_dir: Optional[Path] = None
        self.door_open_enabled = False
        self.audit: Callable[[dict], None] = lambda entry: None
        self._last_open: Dict[int, float] = {}
        self._open_lock = threading.Lock()
        self.users = Cached(TTL_USERS, client.users)
        self.groups = Cached(TTL_LABELS, lambda: client.labels("Groupes", self.t("strGroupe")))
        self.readers = Cached(TTL_LABELS, lambda: client.labels("Lecteurs", self.t("strLecteur")))
        self.slots = Cached(TTL_LABELS, lambda: client.labels("Plages", self.t("strPlage")))
        self.lifts = Cached(TTL_LABELS, lambda: client.labels("Lifts", self.t("strLift")))
        self.trans = Cached(TTL_TRANSLATIONS, lambda: client.translations(lang))
        self.versions = Cached(TTL_VERSIONS, client.versions)
        self.release_update = Cached(TTL_UPDATES, updater.update_status)
        self.schedules = ScheduleManager(client, self.trans.get, self.groups.get, self.slots.get,
                                         self.lifts.get, self._refresh_schedule_labels)
        self.access_cache = Cached(TTL_USERS, self._load_access)
        self.archive = archive or EventArchive(Path(":memory:"), retention_days=0)
        self.events_lock = threading.Lock()          # one controller log reader at a time
        self.import_state = {"running": False, "pages": 0, "done": False, "error": None}
        self.last_purge = float("-inf")
        self.last_poll = float("-inf")  # monotonic() can start near 0 on macOS
        self.last_ok: Optional[str] = None
        self.last_error: Optional[str] = None

    def t(self, key: str) -> str:
        return self.trans.get().get(key, key).strip()

    # ---- events ---------------------------------------------------------
    # The controller's log is a ring: a page asked for at position X holds X, X-1, ...
    # and, past position 1, continues with the last positions of the previous lap.
    def _fetch_page(self, index: int):
        """Return [(lap, position, event)], next_index. lap is 0 for the lap of the
        requested position and -1 for events that wrapped into the previous lap."""
        evs, nxt = self.client.events(index)
        cap = self.archive.capacity()
        if index > 0 and nxt is not None and nxt > index:      # wrapped: measure the ring
            cap = nxt + len(evs) - index
            self.archive.set_capacity(cap)
        if index == 0:
            top = ((nxt + len(evs) - 1) % cap) + 1 if nxt is not None else len(evs)
        else:
            top = index
        out = []
        for i, e in enumerate(evs):
            pos = top - i
            out.append((0, pos, e) if pos > 0 else (-1, pos + cap, e))
        return out, nxt

    def _users_for(self, page) -> Dict[int, object]:
        """Users by slot; refreshed if an event names a slot we don't know yet (just added)."""
        by_index = {u.index: u for u in self.users.get()}
        if any(ev.user_index not in by_index and 0 < ev.user_index < DELETED_USER_ID for *_, ev in page):
            self.users.value = None
            by_index = {u.index: u for u in self.users.get()}
        return by_index

    def _ingest(self, page, epoch: int, floor: Optional[Tuple[int, int]] = None) -> int:
        """Store a fetched page under `epoch` (lap -1 events go to epoch - 1).
        `floor` = (epoch, position): skip events at or below it (already overwritten)."""
        rows = []
        for lap, pos, ev in page:
            e = epoch + lap
            if floor and (e < floor[0] or (e == floor[0] and pos <= floor[1])):
                continue
            rows.append((e, pos, ev))
        return self.archive.ingest(rows, self._users_for(page), self.t("strUtilisateurSupprime"))

    def poll_events(self, force: bool = False) -> None:
        """Fetch the newest events into the archive, filling any gap since the last poll."""
        with self.events_lock:
            interval = MIN_FORCED_POLL_INTERVAL if force else MIN_POLL_INTERVAL
            if time.monotonic() - self.last_poll < interval:
                return
            self.last_poll = time.monotonic()
            page, nxt = self._fetch_page(0)
            if not page:
                return
            # Ring wrapped, or log cleared: positions start again in a new epoch.
            self.archive.check_continuity([(pos, ev) for lap, pos, ev in page if lap == 0])
            epoch = self.archive.epoch()
            known = self.archive.latest_key()
            self._ingest(page, epoch)

            # First run: go back to midnight now (the importer does the rest in the background).
            # Later: fill the gap down to what we already have.
            midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            cursor_epoch, cursor_index, pages = epoch, page[0][1], 1
            while nxt is not None and pages < MAX_GAP_PAGES:
                lap, pos, ev = page[-1]
                lowest = key_of(cursor_epoch + lap, pos)
                if known and self.archive.prev_key(lowest) <= known:
                    break
                d = parse_date(ev.date)
                if not known and d is not None and d < midnight:
                    break
                if nxt > cursor_index:           # this page ran past position 1
                    cursor_epoch -= 1
                cursor_index = nxt
                page, nxt = self._fetch_page(nxt)
                pages += 1
                if not page:
                    break
                self._ingest(page, cursor_epoch)

        if time.monotonic() - self.last_purge > PURGE_EVERY_S:
            self.last_purge = time.monotonic()
            self.archive.purge()

    def import_history(self, stop: Optional[threading.Event] = None) -> None:
        """Copy everything the controller still holds into the archive: the rest of
        the current lap down to position 1, then the part of the previous lap that
        hasn't been overwritten yet. Stops at the retention limit."""
        st = self.import_state
        st.update(running=True, error=None)
        try:
            self.poll_events(force=True)
            current = self.archive.epoch()
            epoch = current
            while not (stop and stop.is_set()):
                with self.events_lock:
                    cap = self.archive.capacity()
                    newest = self.archive.max_no(current) or 0
                    low = self.archive.min_no(epoch)
                    if epoch == current:
                        if low is None:
                            break                               # nothing recorded yet
                        if low <= 1:                            # start of this lap: continue in the previous one
                            epoch = current - 1
                            low = self.archive.min_no(epoch) or cap + 1
                    else:
                        low = low or cap + 1
                    index = low - 1
                    if index < 1 or (epoch < current and index <= newest):
                        break                                   # the rest now holds this lap's events
                    page, _ = self._fetch_page(index)
                    first_current = self._oldest_at(current)
                    # Sanity check: an earlier lap can only hold events older than this one.
                    page = [(lap, pos, ev) for lap, pos, ev in page
                            if epoch + lap == current or not first_current
                            or (_iso_of(ev.date) or "9999") <= first_current]
                    added = self._ingest(page, epoch, floor=(current - 1, newest))
                    oldest = parse_date(page[-1][2].date) if page else None
                st["pages"] += 1
                if oldest:
                    st["oldest"] = oldest.isoformat()
                if not added:
                    break                                       # end of log, retention limit or nonsense
                time.sleep(IMPORT_PAUSE_S)
            st["done"] = True
        except ControllerError as e:
            st["error"] = str(e)
        finally:
            st["running"] = False

    def _oldest_at(self, epoch: int) -> Optional[str]:
        with self.archive.lock:
            r = self.archive.db.execute("SELECT MIN(at) FROM events WHERE epoch=?", (epoch,)).fetchone()
        return r[0]

    def enrich(self, key: int, ev: RawEvent, person: str) -> dict:
        """Port of the event rendering rules in the controller's Outils.js."""
        t = self.t
        readers = self.readers.get()
        code, reader, ident = ev.code, ev.reader, ev.identifier
        detail = ""

        ident_int = _to_int(ident)
        if code in (46, 47, 48):
            ident = ""
        elif code == 53:
            detail = {1: "strErreurNombreBits", 2: "strErreurParité1", 3: "strErreurParité2",
                      4: "strErreurCanal"}.get(ident_int, "")
            detail, ident = (t(detail) if detail else ""), ""
        elif code == 57:
            if ident_int > 0:
                hx = f"{ident_int % 256:02X}"
                detail = f"V{hx[0]}.{hx[1]} ID {(ident_int // 256) % 65536:05d}"
            ident = ""
        elif code == 59:
            detail = {1: t("strBatterieAlarme"), 2: t("strBatterieWarning")}.get(ident_int, "")
            ident = ""
        elif code in (70, 71):
            detail, ident = f"{t('strAutomatisme')} {ident}", ""
        elif 72 <= code <= 75:
            if reader > 0:
                detail, reader = f"{t('strModule')} {reader}", 0
            ident = ""
        elif code == 76:
            detail, ident = f"{t('strNiveau')} {ident_int + 1}", ""
        elif code in (77, 78):
            detail = f"{t('strModule')} {ident_int & 0xFF}"
            if ident_int >> 8:
                hx = f"{ident_int >> 8:02X}"
                detail += f" V{hx[0]}.{hx[1]}"
            ident = ""
        elif code == 87:
            detail, ident = f"{t('strCode')} {ident_int:X}", ""
        elif ident_int == 0:
            ident = ""

        d = parse_date(ev.date)
        return {
            "no": key,
            "date": ev.date,
            "iso": d.isoformat() if d else None,
            "code": code,
            "label": t(str(code)) if str(code) in self.trans.get() else f"Event {code}",
            "category": event_category(code),
            "door": self.door_name(reader),
            "identifier": ident,
            "person": person,
            "detail": detail,
        }

    def door_name(self, reader: int) -> str:
        if reader <= 0:
            return ""
        door = self.readers.get().get(reader % 128, f"{self.t('strLecteur')} {reader % 128}")
        return door + (" " + self.t("strS").strip() if reader > 128 else "")

    def enrich_row(self, r: Row) -> dict:
        return self.enrich(r.key, r.as_raw(), r.person)

    def history(self, q: Dict[str, str], limit: int) -> dict:
        """Filtered archive page for the Activity tab (and the CSV export)."""
        start = end = None
        try:
            if q.get("from"):
                start = datetime.strptime(q["from"], "%Y-%m-%d").isoformat()
            if q.get("to"):
                end = (datetime.strptime(q["to"], "%Y-%m-%d") + timedelta(days=1)).isoformat()
        except ValueError:
            raise ValidationError("Dates must look like 2026-11-10.") from None
        codes = CATEGORY_CODES.get(q.get("filter", "all"), None)
        text = (q.get("q") or "").strip()
        readers = label_codes = None
        if text:
            low = text.lower()
            readers = [k for k, v in self.readers.get().items() if low in v.lower()]
            label_codes = [int(k) for k, v in self.trans.get().items() if k.isdigit() and low in v.lower()]
        before = _to_int(q.get("before")) or None
        rows = self.archive.query(start, end, codes, text or None, readers, label_codes, before, limit)
        return {"events": [self.enrich_row(r) for r in rows], "has_more": len(rows) == limit,
                "archive": self.archive.summary(), "import": dict(self.import_state)}

    def today(self, day: Optional[str] = None) -> dict:
        """Archived events for one calendar day, for the ribbon and door cards."""
        try:
            selected = datetime.strptime(day, "%Y-%m-%d").date() if day else datetime.now().date()
            if day and selected.isoformat() != day:
                raise ValueError
        except ValueError:
            raise ValidationError("Dates must look like 2026-11-10.") from None
        midnight = datetime.combine(selected, datetime.min.time())
        end = midnight + timedelta(days=1)
        rows = self.archive.since(midnight.isoformat(), end.isoformat())
        return {"events": [self.enrich_row(r) for r in rows],
                "date": selected.isoformat(), "midnight": midnight.isoformat()}

    # ---- overview -------------------------------------------------------
    def overview(self) -> dict:
        users = self.users.get()
        groups = self.groups.get()
        readers = self.readers.get()
        named_default = (self.t("strLecteur"), self.t("strGroupe"), self.t("strPlage"))
        return {
            "site_name": self.site_name,
            "versions": self.versions.get(),
            "users": {
                "total": len(users),
                "authorised": sum(1 for u in users if u.status == 0),
                "forbidden": sum(1 for u in users if u.status == 1),
            },
            "doors": [{"id": k, "name": v} for k, v in sorted(readers.items())
                      if not v.startswith(named_default[0] + " ")],
            "groups": [{"id": k, "name": v} for k, v in sorted(groups.items())
                       if not v.startswith(named_default[1] + " ")],
            "time_slots": [{"id": k, "name": v} for k, v in sorted(self.slots.get().items())
                           if not v.startswith(named_default[2] + " ")],
            "last_ok": self.last_ok,
            "backup": status_summary(self.backup_dir) if self.backup_dir else None,
            "history": {**self.archive.summary(), "import": dict(self.import_state)},
            "server_time": datetime.now().isoformat(timespec="seconds"),
        }

    def badge_swipe(self, after: int) -> dict:
        """Newest archive key, and the first unknown badge seen after `after` (for enrolment)."""
        self.poll_events(force=True)
        known = {u.identifier for u in self.users.get()}
        found = None
        if after > 0:
            for r in self.archive.after_key(after):
                if r.code == UNKNOWN_BADGE_CODE and r.identifier.isdigit() and int(r.identifier) > 0:
                    found = r
                    break
        badge = None
        if found:
            badge = {"no": found.key, "identifier": found.identifier, "date": found.raw_date,
                     "door": self.readers.get().get(found.reader % 128, "") if found.reader > 0 else "",
                     "already_known": int(found.identifier) in known}
        return {"latest_no": self.archive.latest_key(), "badge": badge}

    # ---- groups and time slots -----------------------------------------
    def _refresh_schedule_labels(self) -> None:
        self.groups.value = None
        self.slots.value = None
        self.access_cache.value = None

    @staticmethod
    def _default_name(label: str, idx: int) -> bool:
        """Unnamed entries read like "Groupe 12" / "Plage 3" (the word varies by language)."""
        m = re.match(r"^[^\d]+ (\d+)$", label.strip())
        return not label.strip() or (m is not None and int(m.group(1)) == idx)

    def _members(self) -> Dict[int, List[object]]:
        out: Dict[int, List[object]] = {}
        for u in self.users.get():
            for g in set(u.groups):
                if g:
                    out.setdefault(g, []).append(u)
        return out

    def _holidays(self) -> int:
        html = self.client.get("/FR/horaires-jours-feries.cgi").decode("utf-8", errors="replace")
        return len(re.findall(r"^\s*AjouteFerie\(", html, re.M))

    def _load_access(self) -> dict:
        members = self._members()
        groups = self.groups.get()
        slots = self.slots.get()
        wanted = sorted(set(members) | {k for k, v in groups.items() if not self._default_name(v, k)})
        group_rows = []
        for gid in wanted:
            g = self.schedules.read_group(gid)
            people = members.get(gid, [])
            g["members"] = len(people)
            g["authorised_members"] = sum(1 for u in people if u.status == 0)
            group_rows.append(g)
        used = {v for g in group_rows for v in g["doors"].values() if v not in (NO_ACCESS, ANY_TIME)}
        slot_ids = sorted(used | {k for k, v in slots.items() if not self._default_name(v, k)})
        slot_rows = []
        for sid in slot_ids:
            s = self.schedules.read_slot(sid)
            s["used_by"] = [g["id"] for g in group_rows if sid in g["doors"].values()]
            slot_rows.append(s)
        return {"groups": group_rows, "time_slots": slot_rows, "holidays": self._holidays()}

    def access(self) -> dict:
        data = self.access_cache.get()
        return {**data, "doors": [{"id": k, "name": v} for k, v in sorted(self.door_ids().items())],
                "writes_enabled": self.manager is not None}

    def _person_slots_in_use(self) -> set:
        """Time slots chosen on individual people, from the latest per-person backup."""
        if not self.backup_dir:
            return set()
        files = sorted(self.backup_dir.glob("auto-user-forms-*.json")) or sorted(self.backup_dir.glob("user-forms-*.json"))
        if not files:
            return set()
        try:
            snap = json.loads(files[-1].read_text(encoding="utf-8"))
            return {int(f["form"].get("plage", 255)) for f in snap.get("forms", [])}
        except (OSError, ValueError, KeyError):
            return set()

    def free_group(self) -> int:
        members = self._members()
        groups = self.groups.get()
        for gid in range(1, 1025):
            if gid in members or not self._default_name(groups.get(gid, ""), gid):
                continue
            g = self.schedules.read_group(gid)
            if not g["enabled"] and all(v == NO_ACCESS for v in g["doors"].values()):
                return gid
        raise ValidationError("All 1,024 groups are in use.")

    def free_slot(self) -> int:
        slots = self.slots.get()
        referenced = {v for g in self.access_cache.get()["groups"] for v in g["doors"].values()}
        referenced |= self._person_slots_in_use()
        for sid in range(1, 129):
            if sid in referenced or not self._default_name(slots.get(sid, ""), sid):
                continue
            if not any(self.schedules.read_slot(sid)["days"].values()):
                return sid
        raise ValidationError("All 128 time slots are in use.")

    # ---- doors ----------------------------------------------------------
    def door_ids(self) -> Dict[int, str]:
        """Doors that exist on this controller: readers with a name of their own."""
        default = self.t("strLecteur") + " "
        return {k: v for k, v in self.readers.get().items() if not v.startswith(default)}

    def door_detail(self, door: int) -> dict:
        doors = self.door_ids()
        if door not in doors:
            raise ValidationError(f"There is no door {door}.")
        self.poll_events()
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        week = midnight - timedelta(days=6)
        def counts(since):
            by_code = self.archive.door_stats(door, since.isoformat())
            cat = lambda c: event_category(c)
            return {
                "openings": sum(n for c, n in by_code.items() if cat(c) == "granted"),
                "badge": sum(n for c, n in by_code.items() if cat(c) == "granted" and c not in (PUSH_BUTTON_CODE, REMOTE_OPEN_CODE)),
                "push_button": by_code.get(PUSH_BUTTON_CODE, 0),
                "remote": by_code.get(REMOTE_OPEN_CODE, 0),
                "refused": sum(n for c, n in by_code.items() if cat(c) == "refused"),
                "alarms": sum(n for c, n in by_code.items() if cat(c) == "alarm"),
            }
        recent = self.archive.query(None, None, None, None, None, None, None, 25, door=door)
        last_open = self.archive.query(None, None, sorted(GRANTED), None, None, None, None, 1, door=door)
        return {
            "id": door,
            "name": doors[door],
            "today": counts(midnight),
            "week": counts(week),
            "last_opening": self.enrich_row(last_open[0]) if last_open else None,
            "recent": [self.enrich_row(r) for r in recent],
            "open_enabled": self.door_open_enabled,
        }

    def open_door(self, door: int) -> dict:
        """Send the controller's open command, then look for its confirmation in the log."""
        doors = self.door_ids()
        if door not in doors:
            raise ValidationError(f"There is no door {door}.")
        with self._open_lock:
            wait = OPEN_COOLDOWN_S - (time.monotonic() - self._last_open.get(door, float("-inf")))
            if wait > 0:
                raise ValidationError(f"This door was just opened. Wait {int(wait) + 1} s before trying again.")
            self._last_open[door] = time.monotonic()
        self.last_poll = float("-inf")
        self.poll_events(force=True)
        before = self.archive.latest_key()
        reply = self.client.open_door(door).decode("utf-8", errors="replace")
        ok, detail = interpret_response(reply, self.trans.get())
        confirmed = None
        if ok:
            deadline = time.monotonic() + OPEN_CONFIRM_WAIT_S
            while time.monotonic() < deadline and confirmed is None:
                time.sleep(MIN_FORCED_POLL_INTERVAL)
                self.poll_events(force=True)
                for r in self.archive.after_key(before):
                    if r.code == REMOTE_OPEN_CODE and r.reader % 128 == door:
                        confirmed = r
                        break
        result = {"door": door, "name": doors[door], "accepted": ok,
                  "confirmed": confirmed is not None,
                  "event": self.enrich_row(confirmed) if confirmed else None,
                  "detail": detail}
        self.audit({"action": "open_door", "door": door, "name": doors[door], "accepted": ok,
                    "confirmed": confirmed is not None, "controller_detail": detail, "response": reply[:2000]})
        return result

    def options(self) -> dict:
        """Choices for the person form: groups and time slots worth offering."""
        users = self.users.get()
        groups, slots = self.groups.get(), self.slots.get()
        default_g, default_s = self.t("strGroupe"), self.t("strPlage")
        used = {g for u in users for g in u.groups if g}
        group_ids = sorted({k for k, v in groups.items() if not v.startswith(default_g + " ")} | used)
        slot_ids = sorted(k for k, v in slots.items() if not v.startswith(default_s + " "))
        return {
            "writes_enabled": self.manager is not None,
            "door_open_enabled": self.door_open_enabled,
            "id_types": [{"id": k, "name": v} for k, v in ID_TYPES.items()],
            "groups": [{"id": k, "name": groups.get(k, f"{default_g} {k}")} for k in group_ids],
            "time_slots": ([{"id": SLOT_TIME_FROM_GROUP, "name": "Same as their group"},
                            {"id": SLOT_ALWAYS, "name": "Any time"}]
                           + [{"id": k, "name": slots[k]} for k in slot_ids]),
            "all_time_slots": {str(k): v for k, v in slots.items()},
        }

    def people(self) -> dict:
        users = self.users.get()
        groups = self.groups.get()
        seen = self.archive.last_seen()
        def last(u):
            hit = seen.get(str(u.identifier)) if u.id_type != 9 else None
            return {"last_seen": hit[0] if hit else None, "last_door": self.door_name(hit[1]) if hit else ""}
        return {"history_since": self.archive.summary()["oldest"], "people": [{
            **last(u),
            "index": u.index,
            "identifier": "" if u.id_type == 9 else str(u.identifier),
            "type": self.t(IDENTIFIER_TYPES[u.id_type]) if u.id_type in IDENTIFIER_TYPES else f"Type {u.id_type}",
            "last_name": u.last_name.strip(),
            "first_name": u.first_name.strip(),
            "groups": [groups.get(g, str(g)) for g in u.groups if g],
            "status": "authorised" if u.status == 0 else "forbidden" if u.status == 1 else f"status {u.status}",
        } for u in users]}


def read_static(name: str) -> bytes:
    """Page files: from disk, or from inside a .pyz archive / packaged app."""
    path = STATIC_DIR / name
    if path.is_file():
        return path.read_bytes()
    data = pkgutil.get_data("visor_dash", f"static/{name}")
    if data is None:
        raise FileNotFoundError(name)
    return data


def _iso_of(raw: str) -> Optional[str]:
    d = parse_date(raw)
    return d.isoformat() if d else None


def _to_int(s) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- HTTP

def make_handler(dash: Dashboard, dash_password: Optional[str], allowed_hosts: Optional[set]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "visor-dash"

        def _host_ok(self) -> bool:
            # Blocks DNS-rebinding when bound to localhost.
            if allowed_hosts is None:
                return True
            if self.headers.get("Host", "") in allowed_hosts:
                return True
            self._json({"error": "Unexpected Host header."}, 421)
            return False

        def _user(self) -> str:
            header = self.headers.get("Authorization", "")
            if header.startswith("Basic "):
                try:
                    return base64.b64decode(header[6:]).decode("utf-8").partition(":")[0] or "dashboard"
                except ValueError:
                    pass
            return "local"

        def log_message(self, fmt, *args):
            # Log only changes and failures (no query strings): a service log stays small.
            status = str(args[1]) if len(args) > 1 else ""
            if self.command == "GET" and status.startswith(("2", "3")):
                return
            sys.stderr.write(f"{self.log_date_time_string()} {self.command} {urlparse(self.path).path} {status}\n")

        def _authorised(self) -> bool:
            if not dash_password:
                return True
            header = self.headers.get("Authorization", "")
            if header.startswith("Basic "):
                try:
                    _, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                    if hmac.compare_digest(pw, dash_password):
                        return True
                except ValueError:
                    pass
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Badge dashboard"')
            self.end_headers()
            return False

        def _send(self, status: int, body: bytes, ctype: str):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _csv(self, events: List[dict], q: Dict[str, str]):
            buf = io.StringIO()
            w = csv.writer(buf, delimiter=";")   # ';' opens cleanly in French Excel
            w.writerow(["Date", "Time", "Event", "Door", "Badge", "Person", "Detail"])
            for e in events:
                day, _, hms = e["date"].partition(" ")
                w.writerow([_cell(v) for v in (day, hms, e["label"], e["door"], e["identifier"], e["person"], e["detail"])])
            body = ("\ufeff" + buf.getvalue()).encode("utf-8")   # BOM so Excel reads UTF-8
            name = f"badge-history-{q.get('from') or 'start'}-to-{q.get('to') or 'now'}.csv"
            name = "".join(c for c in name if c.isalnum() or c in "-_.")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):
            if not self._host_ok() or not self._authorised():
                return
            url = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            if url.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if url.path in STATIC_FILES:
                name, ctype = STATIC_FILES[url.path]
                self._send(200, read_static(name), ctype)
                return
            try:
                if url.path == "/api/overview":
                    data = dash.overview()
                elif url.path == "/api/update":
                    try:
                        data = dash.release_update.get()
                    except updater.UpdateError as e:
                        self._json({"error": str(e)}, 502)
                        return
                elif url.path == "/api/today":
                    dash.poll_events(force=q.get("refresh") == "1")
                    data = dash.today(q.get("date"))
                elif url.path == "/api/people":
                    data = dash.people()
                elif url.path == "/api/options":
                    data = dash.options()
                elif url.path == "/api/badge-swipe":
                    data = dash.badge_swipe(_to_int(q.get("after")))
                elif url.path.startswith("/api/people/"):
                    slot = _to_int(url.path.rsplit("/", 1)[1])
                    reader = dash.manager or UserManager(dash.client, dash.trans.get, dash.groups.get, dash.slots.get)
                    data = reader.read(slot)
                    if data.get("identifier"):
                        stats = dash.archive.person_stats(data["identifier"])
                        stats["last_door"] = dash.door_name(stats.pop("last_reader") or 0)
                        data["history"] = stats
                        rows = dash.archive.query(None, None, None, None, None, None, None, 15,
                                                  identifier=data["identifier"])
                        data["recent"] = [dash.enrich_row(r) for r in rows]
                elif url.path == "/api/access":
                    data = dash.access()
                elif url.path.startswith("/api/groups/"):
                    data = dash.schedules.read_group(_to_int(url.path.rsplit("/", 1)[1]))
                elif url.path.startswith("/api/time-slots/"):
                    data = dash.schedules.read_slot(_to_int(url.path.rsplit("/", 1)[1]))
                elif url.path.startswith("/api/doors/"):
                    data = dash.door_detail(_to_int(url.path.rsplit("/", 1)[1]))
                elif url.path == "/api/history":
                    limit = min(max(_to_int(q.get("limit")) or 200, 1), 1000)
                    data = dash.history(q, limit)
                elif url.path == "/api/history.csv":
                    self._csv(dash.history(q, CSV_MAX_ROWS)["events"], q)
                    return
                else:
                    self._json({"error": "Not found"}, 404)
                    return
                dash.last_ok = datetime.now().isoformat(timespec="seconds")
                dash.last_error = None
                self._json(data)
            except ControllerError as e:
                dash.last_error = str(e)
                self._json({"error": f"The controller did not answer ({e}). Check that it is powered "
                                     f"and reachable on the network, then refresh."}, 502)
            except ValidationError as e:
                self._json({"error": str(e)}, 404)
            except FormError as e:
                self._json({"error": str(e)}, 409)

        # ---- writes ----------------------------------------------------
        def _write_guard(self, enabled: bool = True, off_message: str = "") -> Optional[dict]:
            """CSRF and sanity checks shared by every action. Returns the JSON body."""
            if not self._host_ok() or not self._authorised():
                return None
            if not enabled:
                self._json({"error": off_message}, 403)
                return None
            if self.headers.get("X-Dashboard") != "1" or \
                    not self.headers.get("Content-Type", "").startswith("application/json"):
                self._json({"error": "Missing dashboard headers."}, 403)
                return None
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{self.headers.get('Host', '')}":
                self._json({"error": "Cross-site request refused."}, 403)
                return None
            length = _to_int(self.headers.get("Content-Length"))
            if length > MAX_BODY:
                self._json({"error": "Request too large."}, 413)
                return None
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json({"error": "Body is not valid JSON."}, 400)
                return None
            if not isinstance(data, dict):
                self._json({"error": "Body must be a JSON object."}, 400)
                return None
            return data

        def _run_write(self, fn):
            try:
                result = fn()
                dash.users.value = None   # force a fresh people list
                self._json(result)
            except ValidationError as e:
                self._json({"error": str(e)}, 400)
            except FormError as e:
                self._json({"error": str(e)}, 409)
            except WriteFailed as e:
                self._json({"error": str(e)}, 502)
            except ControllerError as e:
                self._json({"error": f"The controller did not answer ({e}). Nothing was confirmed; "
                                     f"reload the person before trying again."}, 502)

        def do_POST(self):
            url = urlparse(self.path)
            if url.path == "/api/update":
                if self._write_guard() is None:
                    return
                try:
                    result = updater.install_latest()
                except updater.UpdateError as e:
                    self._json({"error": str(e)}, 502)
                    return
                self._json(result)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if url.path.startswith("/api/doors/") and url.path.endswith("/open"):
                self._open_door(url.path)
                return
            if url.path.startswith(("/api/groups", "/api/time-slots")):
                self._schedule_write(url.path)
                return
            data = self._write_guard(dash.manager is not None, EDITING_OFF)
            if data is None:
                return
            try:
                change = change_from_json(data)
            except ValidationError as e:
                self._json({"error": str(e)}, 400)
                return
            dash.request_local.user = self._user()
            if url.path == "/api/people":
                self._run_write(lambda: dash.manager.create(change))
            elif url.path.startswith("/api/people/"):
                slot = _to_int(url.path.rsplit("/", 1)[1])
                self._run_write(lambda: dash.manager.update(slot, change))
            else:
                self._json({"error": "Not found"}, 404)

        def _schedule_write(self, path: str):
            data = self._write_guard(dash.manager is not None, EDITING_OFF)
            if data is None:
                return
            dash.request_local.user = self._user()
            parts = path.strip("/").split("/")          # api, groups|time-slots, [id]
            try:
                name = data.get("name")
                if name is not None and not isinstance(name, str):
                    raise ValidationError("name must be text.")
                if parts[1] == "groups":
                    enabled = data.get("enabled")
                    if enabled is not None and not isinstance(enabled, bool):
                        raise ValidationError("enabled must be true or false.")
                    doors = data.get("doors")
                    access = None
                    if doors is not None:
                        if not isinstance(doors, dict) or not all(
                                isinstance(v, int) and not isinstance(v, bool) for v in doors.values()):
                            raise ValidationError("doors must map door numbers to access values.")
                        access = {_to_int(k): v for k, v in doors.items()}
                    gid = _to_int(parts[2]) if len(parts) > 2 else dash.free_group()
                    if len(parts) <= 2 and name is None:
                        raise ValidationError("Give the new group a name.")
                    result = dash.schedules.update_group(gid, name, enabled, access)
                else:
                    days = data.get("days")
                    if days is not None:
                        if not isinstance(days, dict) or set(days) - set(DAY_KEYS) or not all(
                                isinstance(v, list) and all(isinstance(r, str) for r in v) for v in days.values()):
                            raise ValidationError("days must map mon..sun/holiday to lists of 'HH:MM-HH:MM'.")
                    sid = _to_int(parts[2]) if len(parts) > 2 else dash.free_slot()
                    if len(parts) <= 2 and name is None:
                        raise ValidationError("Give the new time slot a name.")
                    result = dash.schedules.update_slot(sid, name, days)
                dash.access_cache.value = None
                self._json(result)
            except ValidationError as e:
                self._json({"error": str(e)}, 400)
            except FormError as e:
                self._json({"error": str(e)}, 409)
            except WriteFailed as e:
                dash.access_cache.value = None
                self._json({"error": str(e)}, 502)
            except ControllerError as e:
                self._json({"error": f"The controller did not answer ({e}). Reload before trying again."}, 502)

        def _open_door(self, path: str):
            data = self._write_guard(dash.door_open_enabled,
                                     "Opening doors is turned off. Set DASH_ALLOW_DOOR_OPEN=1 in .env and restart.")
            if data is None:
                return
            door = _to_int(path.split("/")[3])
            if _to_int(data.get("confirm_door")) != door or door <= 0:
                self._json({"error": "Opening must confirm the door number."}, 400)
                return
            dash.request_local.user = self._user()
            try:
                self._json(dash.open_door(door))
            except ValidationError as e:
                self._json({"error": str(e)}, 400)
            except (ControllerError, PermissionError) as e:
                self._json({"error": f"The open command could not be sent ({e})."}, 502)

        def do_DELETE(self):
            url = urlparse(self.path)
            data = self._write_guard(dash.manager is not None, EDITING_OFF)
            if data is None:
                return
            if not url.path.startswith("/api/people/"):
                self._json({"error": "Not found"}, 404)
                return
            slot = _to_int(url.path.rsplit("/", 1)[1])
            if _to_int(data.get("confirm_slot")) != slot or slot <= 0:
                self._json({"error": "Deletion must confirm the slot number."}, 400)
                return
            dash.request_local.user = self._user()
            self._run_write(lambda: dash.manager.delete(slot))

    return Handler


CSV_MAX_ROWS = 200_000
EDITING_OFF = "Editing is turned off. Set DASH_ALLOW_WRITES=1 in .env and restart."


def _cell(v: str) -> str:
    """Neutralise spreadsheet formulas in exported text (CSV injection)."""
    v = str(v or "")
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def change_from_json(d: dict) -> UserChange:
    def opt_str(k):
        v = d.get(k)
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValidationError(f"{k} must be text.")
        return v
    def opt_int(k):
        v = d.get(k)
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValidationError(f"{k} must be a number.")
        return v
    groups = d.get("groups")
    if groups is not None:
        if not (isinstance(groups, list) and len(groups) == 3 and all(isinstance(g, int) and not isinstance(g, bool) for g in groups)):
            raise ValidationError("groups must be three numbers.")
        groups = tuple(groups)
    authorised = d.get("authorised")
    if authorised is not None and not isinstance(authorised, bool):
        raise ValidationError("authorised must be true or false.")
    return UserChange(last_name=opt_str("last_name"), first_name=opt_str("first_name"),
                      identifier=opt_str("identifier"), id_type=opt_int("id_type"),
                      authorised=authorised, groups=groups, time_slot=opt_int("time_slot"),
                      valid_from=opt_str("valid_from"), valid_until=opt_str("valid_until"))


def main(root: Path, port_override: Optional[int] = None, open_browser: bool = False) -> int:
    """Run the dashboard with the settings and data in `root` (see settings.home())."""
    cfg = settings.load(root)
    missing = [k for k in ("VAUBAN_HOST", "VAUBAN_USER", "VAUBAN_PASSWORD") if not cfg.get(k)]
    if missing:
        print(f"Missing settings ({', '.join(missing)}) in {settings.env_file(root)}.\n"
              f"Run \"visor-dash setup\" to create them.", file=sys.stderr)
        return 2

    writes = cfg.get("DASH_ALLOW_WRITES", "0") == "1"
    door_open = cfg.get("DASH_ALLOW_DOOR_OPEN", "0") == "1"
    client = VaubanClient(cfg["VAUBAN_HOST"], cfg["VAUBAN_USER"], cfg["VAUBAN_PASSWORD"],
                          port=int(cfg.get("VAUBAN_PORT", "80")), allow_writes=writes,
                          allow_door_open=door_open)
    retention = int(cfg.get("HISTORY_DAYS", "90"))
    archive = EventArchive(root / "data" / "history.sqlite3", retention_days=retention)
    dash = Dashboard(client, cfg.get("VAUBAN_LANG", "en"), cfg.get("DASH_SITE_NAME", "Badge controller"), archive)
    dash.backup_dir = root / "backups"
    threading.Thread(target=dash.import_history, name="history-import", daemon=True).start()

    audit_path = root / "backups" / "audit.jsonl"
    audit_lock = threading.Lock()

    def audit(entry: dict) -> None:
        entry = {"at": datetime.now().isoformat(timespec="seconds"),
                 "by": getattr(dash.request_local, "user", "local"), **entry}
        with audit_lock:
            audit_path.parent.mkdir(mode=0o700, exist_ok=True)
            new = not audit_path.exists()
            with open(audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if new:
                os.chmod(audit_path, 0o600)

    dash.audit = audit
    dash.door_open_enabled = door_open
    if writes:
        dash.manager = UserManager(client, dash.trans.get, dash.groups.get, dash.slots.get, audit)
    dash.schedules.audit = audit

    bind = cfg.get("DASH_BIND", "127.0.0.1")
    port = port_override or int(cfg.get("DASH_PORT", "8080"))
    password = cfg.get("DASH_PASSWORD") or None
    if bind not in ("127.0.0.1", "localhost", "::1") and not password:
        print("Refusing to listen on the network without DASH_PASSWORD: the dashboard shows "
              "people's names and badge numbers. Set DASH_PASSWORD in .env.", file=sys.stderr)
        return 2

    local = bind in ("127.0.0.1", "localhost", "::1")
    allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"} if local else None
    httpd = ThreadingHTTPServer((bind, port), make_handler(dash, password, allowed_hosts))
    mode = ", ".join(m for m, on in (("editing people ON", writes), ("door opening ON", door_open)) if on) or "read-only"
    url = f"http://{'localhost' if local else bind}:{port}"
    print(f"Badge dashboard on {url} (controller {cfg['VAUBAN_HOST']}, {mode}). Ctrl+C to stop.", flush=True)
    if open_browser:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
