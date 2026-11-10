"""Long-term event history in SQLite (data/history.sqlite3).

Why names are stored, not looked up later: the controller reuses user slots
(a new person can take a deleted person's slot), so resolving old events
against today's user list could attribute someone else's history to them.
An event gets a name only when its badge number matches the badge of the
user in that slot at the time it is recorded.

The controller keeps its log in a ring (15,000 positions on this one): after
the last position it starts again at 1, overwriting the oldest events. Each lap
of the ring is an "epoch" here; a wrap, or the log being cleared, starts a new
epoch so old rows are never overwritten. Rows are addressed by
key = epoch * 10**9 + position, which sorts chronologically.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .client import ALARM, DELETED_USER_ID, GRANTED, REFUSED, RawEvent

EPOCH_SPAN = 10 ** 9
DEFAULT_CAPACITY = 15000   # ring size seen on a VISOR+2 / VERSO+ (measured, then stored in meta)
NO_IDENTIFIER_TYPE = 9
# Badge use that proves a person was physically there.
PRESENCE_CODES = (10, 27, 33)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    epoch       INTEGER NOT NULL,
    no          INTEGER NOT NULL,
    at          TEXT,              -- controller local time, ISO 'YYYY-MM-DDTHH:MM:SS'
    raw_date    TEXT NOT NULL,     -- exactly as the controller wrote it
    code        INTEGER NOT NULL,
    reader      INTEGER NOT NULL,
    identifier  TEXT NOT NULL,
    user_index  INTEGER NOT NULL,
    person      TEXT NOT NULL,     -- name when recorded ('' if unknown or not verifiable)
    verified    INTEGER NOT NULL,  -- 1 = event badge matched the user's badge when recorded
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (epoch, no)
);
CREATE INDEX IF NOT EXISTS events_at ON events (at);
CREATE INDEX IF NOT EXISTS events_identifier ON events (identifier, at);
CREATE INDEX IF NOT EXISTS events_code ON events (code, at);
"""


def _iso(raw: str) -> Optional[str]:
    try:
        return datetime.strptime(raw.strip(), "%d/%m/%Y %H:%M:%S").isoformat()
    except ValueError:
        return None


def key_of(epoch: int, no: int) -> int:
    return epoch * EPOCH_SPAN + no


def split_key(key: int) -> Tuple[int, int]:
    return divmod(int(key), EPOCH_SPAN)


@dataclass
class Row:
    key: int
    at: Optional[str]
    raw_date: str
    code: int
    reader: int
    identifier: str
    user_index: int
    person: str
    verified: bool

    def as_raw(self) -> RawEvent:
        return RawEvent(self.raw_date, self.code, self.reader, self.identifier, self.user_index)


def resolve_person(ev: RawEvent, users_by_index: Dict[int, object], deleted_label: str) -> Tuple[str, bool]:
    """Name to record with an event, and whether the badge confirmed it."""
    if ev.user_index == DELETED_USER_ID and 10 <= ev.code < 40:
        return deleted_label, False
    u = users_by_index.get(ev.user_index)
    if u is None or ev.user_index == 0:
        return "", False
    name = " ".join(p for p in (u.last_name.strip(), u.first_name.strip()) if p)
    ident = ev.identifier.strip()
    if ident and ident != "0" and u.id_type != NO_IDENTIFIER_TYPE:
        if ident == str(u.identifier):
            return name, True
        return "", False   # slot now belongs to someone else: don't guess
    return name, False      # no badge in the event (or person without badge): name from slot


class EventArchive:
    def __init__(self, path: Path, retention_days: int = 90):
        self.path = path
        self.retention_days = retention_days
        in_memory = str(path) == ":memory:"
        if not in_memory:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        new = not in_memory and not path.exists()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        if new:
            os.chmod(path, 0o600)   # names and movements of people: owner-only
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    # ---- epochs ------------------------------------------------------------
    def epoch(self) -> int:
        r = self.db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()
        return int(r[0]) if r else 1

    def _set_epoch(self, e: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('epoch', ?)", (str(e),))

    def new_epoch(self) -> int:
        with self.lock:
            e = self.epoch() + 1
            self._set_epoch(e)
            return e

    def capacity(self) -> int:
        r = self.db.execute("SELECT value FROM meta WHERE key='capacity'").fetchone()
        return int(r[0]) if r else DEFAULT_CAPACITY

    def set_capacity(self, cap: int) -> None:
        if cap > 0 and cap != self.capacity():
            self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('capacity', ?)", (str(cap),))

    def prev_key(self, key: int) -> int:
        """Key of the event just before `key` in the ring."""
        e, n = split_key(key)
        return key_of(e, n - 1) if n > 1 else key_of(e - 1, self.capacity())

    def max_no(self, epoch: Optional[int] = None) -> Optional[int]:
        e = self.epoch() if epoch is None else epoch
        r = self.db.execute("SELECT MAX(no) FROM events WHERE epoch=?", (e,)).fetchone()
        return r[0]

    def min_no(self, epoch: Optional[int] = None) -> Optional[int]:
        e = self.epoch() if epoch is None else epoch
        r = self.db.execute("SELECT MIN(no) FROM events WHERE epoch=?", (e,)).fetchone()
        return r[0]

    def check_continuity(self, page: Sequence[Tuple[int, RawEvent]]) -> bool:
        """Compare the controller's newest page with what we stored. Starts a new
        epoch (and returns True) if the counter went back or the events differ."""
        if not page:
            return False
        with self.lock:
            e = self.epoch()
            top = self.max_no(e)
            if top is None:
                return False
            reset = page[0][0] < top
            if not reset:
                for no, ev in page:
                    r = self.db.execute("SELECT raw_date, code, identifier FROM events WHERE epoch=? AND no=?",
                                        (e, no)).fetchone()
                    if r is not None:
                        reset = (r[0], r[1], r[2]) != (ev.date, ev.code, ev.identifier)
                        break
            if reset:
                self._set_epoch(e + 1)
            return reset

    # ---- writing -----------------------------------------------------------
    def ingest(self, page: Iterable[tuple], users_by_index: Dict[int, object],
               deleted_label: str = "Deleted user") -> int:
        """Store events not seen before. Items are (position, event) for the current
        epoch, or (epoch, position, event). Returns how many were new."""
        now = datetime.now().isoformat(timespec="seconds")
        cutoff = self.cutoff()
        with self.lock:
            current = self.epoch()
            rows = []
            for item in page:
                e, no, ev = item if len(item) == 3 else (current, *item)
                if no <= 0:
                    raise ValueError(f"invalid log position {no}")
                at = _iso(ev.date)
                if cutoff and at and at < cutoff:
                    continue
                person, verified = resolve_person(ev, users_by_index, deleted_label)
                rows.append((e, no, at, ev.date, ev.code, ev.reader, ev.identifier, ev.user_index,
                             person, int(verified), now))
            before = self.db.total_changes
            self.db.execute("BEGIN")
            self.db.executemany("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.db.execute("COMMIT")
            return self.db.total_changes - before

    def cutoff(self) -> Optional[str]:
        if not self.retention_days:
            return None
        return (datetime.now() - timedelta(days=self.retention_days)).replace(microsecond=0).isoformat()

    def purge(self) -> int:
        c = self.cutoff()
        if not c:
            return 0
        with self.lock:
            cur = self.db.execute("DELETE FROM events WHERE at IS NOT NULL AND at < ?", (c,))
            return cur.rowcount

    # ---- reading -----------------------------------------------------------
    def _rows(self, sql: str, args: Sequence) -> List[Row]:
        with self.lock:
            out = self.db.execute(sql, args).fetchall()
        return [Row(key_of(r[0], r[1]), r[2], r[3], r[4], r[5], r[6], r[7], r[8], bool(r[9])) for r in out]

    _COLS = "epoch, no, at, raw_date, code, reader, identifier, user_index, person, verified"

    def since(self, start_iso: str, end_iso: Optional[str] = None) -> List[Row]:
        if end_iso is None:
            sql = f"SELECT {self._COLS} FROM events WHERE at >= ? ORDER BY epoch DESC, no DESC"
            args = (start_iso,)
        else:
            sql = f"SELECT {self._COLS} FROM events WHERE at >= ? AND at < ? ORDER BY epoch DESC, no DESC"
            args = (start_iso, end_iso)
        return self._rows(sql, args)

    def after_key(self, key: int) -> List[Row]:
        e, n = split_key(key)
        return self._rows(f"SELECT {self._COLS} FROM events WHERE epoch > ? OR (epoch = ? AND no > ?) "
                          f"ORDER BY epoch, no", (e, e, n))

    def latest_key(self) -> int:
        with self.lock:
            r = self.db.execute("SELECT epoch, no FROM events ORDER BY epoch DESC, no DESC LIMIT 1").fetchone()
        return key_of(*r) if r else 0

    def query(self, start: Optional[str], end: Optional[str], codes: Optional[Sequence[int]],
              text_person_ident: Optional[str], readers: Optional[Sequence[int]], label_codes: Optional[Sequence[int]],
              before: Optional[int], limit: int, *, identifier: Optional[str] = None,
              door: Optional[int] = None) -> List[Row]:
        """Filtered page of history, newest first. Text search matches the person,
        the badge number, a door (readers) or an event name (label_codes).
        `identifier` and `door` are exact filters (one badge; one door, both sides)."""
        where, args = [], []
        if identifier is not None:
            where.append("identifier = ?"); args.append(identifier)
        if door is not None:
            where.append("reader % 128 = ? AND reader > 0"); args.append(int(door))
        if start:
            where.append("at >= ?"); args.append(start)
        if end:
            where.append("at < ?"); args.append(end)
        if codes is not None:
            where.append(f"code IN ({','.join('?' * len(codes))})" if codes else "0"); args += list(codes)
        if text_person_ident is not None:
            like = "%" + text_person_ident.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            parts = ["person LIKE ? ESCAPE '\\'", "identifier LIKE ? ESCAPE '\\'"]
            args += [like, like]
            if readers:
                parts.append(f"(reader % 128) IN ({','.join('?' * len(readers))})"); args += list(readers)
            if label_codes:
                parts.append(f"code IN ({','.join('?' * len(label_codes))})"); args += list(label_codes)
            where.append("(" + " OR ".join(parts) + ")")
        if before is not None:
            e, n = split_key(before)
            where.append("(epoch < ? OR (epoch = ? AND no < ?))"); args += [e, e, n]
        sql = f"SELECT {self._COLS} FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY epoch DESC, no DESC LIMIT ?"
        args.append(int(limit))
        return self._rows(sql, args)

    def last_seen(self) -> Dict[str, Tuple[str, int]]:
        """badge number -> (time of last badge use, reader), for verified presence events."""
        with self.lock:
            rows = self.db.execute(
                f"SELECT identifier, MAX(at), reader FROM events WHERE code IN ({','.join('?' * len(PRESENCE_CODES))}) "
                f"AND identifier != '' AND at IS NOT NULL GROUP BY identifier", PRESENCE_CODES).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    def person_stats(self, identifier: str, days: int = 30) -> dict:
        since = (datetime.now() - timedelta(days=days)).replace(microsecond=0).isoformat()
        codes = ",".join("?" * len(PRESENCE_CODES))
        with self.lock:
            last = self.db.execute(
                f"SELECT at, reader FROM events WHERE identifier=? AND code IN ({codes}) AND at IS NOT NULL "
                f"ORDER BY at DESC LIMIT 1", (identifier, *PRESENCE_CODES)).fetchone()
            count = self.db.execute(
                f"SELECT COUNT(*) FROM events WHERE identifier=? AND code IN ({codes}) AND at >= ?",
                (identifier, *PRESENCE_CODES, since)).fetchone()[0]
            refused = self.db.execute(
                f"SELECT COUNT(*) FROM events WHERE identifier=? AND code IN ({','.join('?' * len(REFUSED))}) AND at >= ?",
                (identifier, *sorted(REFUSED), since)).fetchone()[0]
        return {"last_seen": last[0] if last else None, "last_reader": last[1] if last else None,
                "entries": count, "refused": refused, "days": days}

    def door_stats(self, door: int, since_iso: str) -> Dict[int, int]:
        """Event counts by code at one door (both sides) since a time."""
        with self.lock:
            rows = self.db.execute("SELECT code, COUNT(*) FROM events WHERE reader > 0 AND reader % 128 = ? "
                                   "AND at >= ? GROUP BY code", (int(door), since_iso)).fetchall()
        return dict(rows)

    def summary(self) -> dict:
        with self.lock:
            r = self.db.execute("SELECT COUNT(*), MIN(at), MAX(at) FROM events").fetchone()
        return {"events": r[0], "oldest": r[1], "newest": r[2], "retention_days": self.retention_days}


CATEGORY_CODES = {
    "access": sorted(GRANTED | REFUSED),
    "refused": sorted(REFUSED),
    "alarm": sorted(ALARM),
    "all": None,
}
