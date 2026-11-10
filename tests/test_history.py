"""Offline tests for the long-term event history.  Run: python3 -m unittest -v"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from visor_dash.client import RawEvent, parse_users
from visor_dash.history import EventArchive, key_of, resolve_person
from visor_dash.server import Dashboard, _cell
from tests.test_dashboard import FakeClient, user_record


def ev(date, code=10, reader=1, ident="555", user=1):
    return RawEvent(date, code, reader, ident, user)


def stamp(dt):
    return dt.strftime("%d/%m/%Y %H:%M:%S")


NOW = datetime.now().replace(microsecond=0)
USERS = {u.index: u for u in parse_users(user_record(555, 1, (1, 0, 0), "DUPONT", "Jean", 0)
                                         + user_record(777, 1, (1, 0, 0), "MARTIN", "Lea", 0))}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "data" / "history.sqlite3"
        self.a = EventArchive(self.path, retention_days=90)

    def tearDown(self):
        self.a.db.close()
        self.tmp.cleanup()

    def test_file_is_private(self):
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.path.parent).st_mode & 0o777, 0o700)

    def test_ingest_is_idempotent(self):
        page = [(3, ev(stamp(NOW))), (2, ev(stamp(NOW - timedelta(minutes=1)))), (1, ev(stamp(NOW - timedelta(minutes=2))))]
        self.assertEqual(self.a.ingest(page, USERS), 3)
        self.assertEqual(self.a.ingest(page, USERS), 0)
        self.assertEqual((self.a.min_no(), self.a.max_no()), (1, 3))

    def test_name_recorded_only_when_badge_matches(self):
        self.assertEqual(resolve_person(ev("x", ident="555", user=1), USERS, "Deleted"), ("DUPONT Jean", True))
        # The slot now holds someone else (badge differs): never attribute the event to them.
        self.assertEqual(resolve_person(ev("x", ident="999", user=1), USERS, "Deleted"), ("", False))
        self.assertEqual(resolve_person(ev("x", code=12, ident="1", user=65535), USERS, "Deleted"), ("Deleted", False))
        self.assertEqual(resolve_person(ev("x", code=54, ident="0", user=0), USERS, "Deleted"), ("", False))

    def test_names_survive_slot_reuse(self):
        self.a.ingest([(1, ev(stamp(NOW - timedelta(days=2)), ident="555", user=1))], USERS)
        # Slot 1 is later reused by a different person with another badge.
        reused = {1: parse_users(user_record(888, 1, (0, 0, 0), "NEW", "Person", 0))[0]}
        self.a.ingest([(2, ev(stamp(NOW), ident="888", user=1))], reused)
        rows = {r.key % 10**9: r.person for r in self.a.query(None, None, None, None, None, None, None, 10)}
        self.assertEqual(rows, {1: "DUPONT Jean", 2: "NEW Person"})

    def test_counter_reset_starts_new_epoch(self):
        self.a.ingest([(500, ev(stamp(NOW - timedelta(hours=1)))), (499, ev(stamp(NOW - timedelta(hours=2))))], USERS)
        newer = [(3, ev(stamp(NOW))), (2, ev(stamp(NOW)))]
        self.assertTrue(self.a.check_continuity(newer))       # 3 < 500: controller log restarted
        self.a.ingest(newer, USERS)
        keys = [r.key for r in self.a.query(None, None, None, None, None, None, None, 10)]
        self.assertEqual(keys, [key_of(2, 3), key_of(2, 2), key_of(1, 500), key_of(1, 499)])

    def test_same_number_different_event_is_a_reset(self):
        self.a.ingest([(10, ev(stamp(NOW - timedelta(hours=1)), code=10))], USERS)
        self.assertTrue(self.a.check_continuity([(10, ev(stamp(NOW), code=74))]))

    def test_matching_page_is_not_a_reset(self):
        self.a.ingest([(10, ev(stamp(NOW - timedelta(hours=1)), code=10))], USERS)
        self.assertFalse(self.a.check_continuity([(11, ev(stamp(NOW))), (10, ev(stamp(NOW - timedelta(hours=1)), code=10))]))
        self.assertEqual(self.a.epoch(), 1)

    def test_retention(self):
        old = [(1, ev(stamp(NOW - timedelta(days=100))))]
        self.assertEqual(self.a.ingest(old, USERS), 0)           # never stored
        self.a.retention_days = 0
        self.a.ingest(old, USERS)
        self.a.retention_days = 90
        self.assertEqual(self.a.purge(), 1)                      # removed by purge

    def test_query_filters_and_search(self):
        page = [
            (4, ev(stamp(NOW), code=0, ident="123", user=0, reader=2)),       # unknown badge, door 2
            (3, ev(stamp(NOW - timedelta(minutes=1)), code=74, ident="0", user=0, reader=0)),
            (2, ev(stamp(NOW - timedelta(days=1)), ident="777", user=2)),     # MARTIN yesterday
            (1, ev(stamp(NOW - timedelta(days=3)), ident="555", user=1)),     # DUPONT 3 days ago
        ]
        self.a.ingest(page, USERS)
        q = lambda **k: [r.key % 10**9 for r in self.a.query(k.get("start"), k.get("end"), k.get("codes"), k.get("text"),
                                                              k.get("readers"), k.get("labels"), k.get("before"), 50)]
        self.assertEqual(q(), [4, 3, 2, 1])
        self.assertEqual(q(codes=[10]), [2, 1])
        self.assertEqual(q(text="martin"), [2])                  # case-insensitive name search
        self.assertEqual(q(text="12"), [4])                      # badge number
        self.assertEqual(q(text="zz", readers=[2]), [4])         # door name matched
        self.assertEqual(q(text="zz", labels=[74]), [3])         # event name matched
        self.assertEqual(q(text="%"), [])                        # LIKE wildcards are literal
        self.assertEqual(q(start=(NOW - timedelta(days=2)).isoformat()), [4, 3, 2])
        self.assertEqual(q(before=key_of(1, 3)), [2, 1])

    def test_last_seen_and_stats(self):
        self.a.ingest([(2, ev(stamp(NOW - timedelta(hours=1)), ident="555", user=1, reader=2)),
                       (1, ev(stamp(NOW - timedelta(days=5)), ident="555", user=1)),
                       (3, ev(stamp(NOW), code=0, ident="555", user=0))], USERS)
        seen = self.a.last_seen()
        self.assertEqual(seen["555"][1], 2)
        st = self.a.person_stats("555")
        self.assertEqual((st["entries"], st["refused"]), (2, 1))


class ImportTests(unittest.TestCase):
    def test_imports_whole_controller_log(self):
        fake = FakeClient(total=230)                # dated 10/11/2026: beyond "today" logic, all kept
        d = Dashboard(fake, "en")
        d.import_history()
        self.assertTrue(d.import_state["done"], d.import_state)
        self.assertEqual((d.archive.min_no(), d.archive.max_no()), (1, 230))

    def test_history_endpoint_shape(self):
        d = Dashboard(FakeClient(total=60), "en")
        d.import_history()
        out = d.history({"filter": "access", "q": "front"}, 20)    # matches the door "Front door"
        self.assertEqual(len(out["events"]), 20)
        self.assertTrue(out["has_more"])
        nxt = d.history({"filter": "access", "q": "front", "before": str(out["events"][-1]["no"])}, 50)
        self.assertEqual(len(nxt["events"]), 40)
        self.assertFalse(nxt["has_more"])


class RingClient(FakeClient):
    """A controller log ring of `cap` positions after `written` events.
    Each event's badge field holds its sequence number (1 = first ever)."""

    def __init__(self, cap=200, written=260):
        super().__init__()
        self.cap, self.written = cap, written
        self.base = datetime.now().replace(microsecond=0) - timedelta(days=2)

    def _seq_at(self, pos):
        top = ((self.written - 1) % self.cap) + 1
        lap_start = self.written - top
        seq = lap_start + pos if pos <= top else lap_start - self.cap + pos
        return seq if seq >= 1 else None

    def events(self, index=0):
        pos = ((self.written - 1) % self.cap) + 1 if index == 0 else index
        evs = []
        for _ in range(50):
            seq = self._seq_at(pos)
            if seq is not None:
                evs.append(RawEvent(stamp(self.base + timedelta(minutes=seq)), 10, 1, str(seq), 0))
            pos = pos - 1 if pos > 1 else self.cap
        return evs, pos


def seqs_in_key_order(d):
    rows = d.archive.query(None, None, None, None, None, None, None, 100000)
    return [int(r.identifier) for r in reversed(rows)], rows


class RingTests(unittest.TestCase):
    def test_import_reads_previous_lap_in_order(self):
        d = Dashboard(RingClient(cap=200, written=260), "en")
        d.import_history()
        seqs, rows = seqs_in_key_order(d)
        self.assertEqual(seqs, list(range(61, 261)))          # the whole ring, oldest first
        self.assertTrue(all(r.key % 10**9 >= 1 for r in rows))
        self.assertEqual(d.archive.capacity(), 200)            # measured from the paging

    def test_wrap_while_running(self):
        ring = RingClient(cap=200, written=150)
        d = Dashboard(ring, "en")
        d.import_history()
        self.assertEqual(seqs_in_key_order(d)[0], list(range(1, 151)))
        ring.written = 230                                      # 80 new events: the ring wraps
        d.last_poll = float("-inf")
        d.poll_events(force=True)
        seqs, _ = seqs_in_key_order(d)
        self.assertEqual(seqs, list(range(1, 231)))             # nothing lost, nothing doubled
        self.assertEqual(d.archive.epoch(), 2)

    def test_fresh_log_stops_at_position_one(self):
        d = Dashboard(RingClient(cap=200, written=120), "en")
        d.import_history()
        self.assertEqual(seqs_in_key_order(d)[0], list(range(1, 121)))
        self.assertTrue(d.import_state["done"])


class CsvTests(unittest.TestCase):
    def test_formula_cells_are_neutralised(self):
        self.assertEqual(_cell("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertEqual(_cell("-1"), "'-1")
        self.assertEqual(_cell("DUPONT"), "DUPONT")


if __name__ == "__main__":
    unittest.main()
