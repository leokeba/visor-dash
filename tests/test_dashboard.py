"""Offline tests with synthetic data (no real names or badges).  Run: python3 -m unittest -v"""
import unittest

from visor_dash.client import (VaubanClient, event_category, parse_events, parse_label_list,
                               parse_translations, parse_users)
from visor_dash.server import Dashboard


def user_record(identifier, id_type, groups, last, first, status):
    rec = identifier.to_bytes(7, "little") + bytes([id_type])
    for g in groups:
        rec += g.to_bytes(2, "little")
    rec += last.encode().ljust(17, b"\0") + first.encode().ljust(17, b"\0") + bytes([status])
    assert len(rec) == 49
    return rec


class ParserTests(unittest.TestCase):
    def test_users(self):
        data = (user_record(40000001, 1, (1, 0, 3), "DUPONT", "Jean", 0)
                + bytes(49)                                   # empty slot 2
                + user_record(0, 9, (2, 0, 0), "Élodie", "", 1))
        users = parse_users(data)
        self.assertEqual([u.index for u in users], [1, 3])
        self.assertEqual(users[0].identifier, 40000001)
        self.assertEqual(users[0].groups, (1, 0, 3))
        self.assertEqual((users[0].last_name, users[0].first_name, users[0].status), ("DUPONT", "Jean", 0))
        self.assertEqual(users[1].last_name, "Élodie")
        self.assertEqual(users[1].status, 1)

    def test_identifier_matches_vauban_readval(self):
        # Outils.js ReadVal: bytes 1..6 little-endian, times 256, plus byte 0.
        raw = bytes([0x01, 0x23, 0x45, 0x67, 0x89, 0xAB, 0x0C])
        entier = 0
        for i in range(6, 0, -1):
            entier = entier * 256 + raw[i]
        expected = entier * 256 + raw[0]
        u = parse_users(raw + bytes([1]) + bytes(41))[0]
        self.assertEqual(u.identifier, expected)

    def test_label_list_defaults(self):
        data = "Entrée nord".encode() + b"#" + b"\0\0\0" + b"#" + b"Atelier\0\0#"
        self.assertEqual(parse_label_list(data, "Reader"),
                         {1: "Entrée nord", 2: "Reader 2", 3: "Atelier"})

    def test_events_and_next_index(self):
        data = b"10/11/2026 17:35:23$10$1$123$4#10/11/2026 17:35:21$75$0$0$0#6419#\r\n"
        events, nxt = parse_events(data)
        self.assertEqual(len(events), 2)
        self.assertEqual((events[0].code, events[0].reader, events[0].identifier, events[0].user_index),
                         (10, 1, "123", 4))
        self.assertEqual(nxt, 6419)
        self.assertIsNone(parse_events(b"01/01/2026 00:00:00$10$1$1$1#-1#")[1])

    def test_translations_with_bom(self):
        t = parse_translations("﻿74=Défaut Alimentation\r\nstrS= (S)\r\nbad line\r\n".encode())
        self.assertEqual(t["74"], "Défaut Alimentation")
        self.assertEqual(t["strS"], " (S)")

    def test_categories(self):
        self.assertEqual(event_category(10), "granted")
        self.assertEqual(event_category(0), "refused")
        self.assertEqual(event_category(74), "alarm")
        self.assertEqual(event_category(75), "info")
        self.assertEqual(event_category(25), "info")


class ReadOnlyGuardTests(unittest.TestCase):
    def test_refuses_write_endpoints(self):
        c = VaubanClient("192.0.2.1", "u", "p")   # TEST-NET address, never contacted
        for path in ("/FR/open.cgi", "/FR/update.cgi", "/FR/delete.cgi", "/FR/deleteAll.cgi",
                     "/FR/upload.html", "/FR/backup.cgi", "/assets/../FR/open.cgi"):
            with self.assertRaises(PermissionError, msg=path):
                c.get(path)


class FakeClient:
    """Serves a 120-event log in pages of 50, like GetEvenements.cgi."""

    def __init__(self, total=120):
        self.total = total
        self.calls = []

    def _page(self, top):
        evs = []
        for n in range(top, max(top - 50, 0), -1):
            evs.append(f"10/11/2026 12:{n // 60 % 60:02d}:{n % 60:02d}$10$1${n}$1")
        nxt = top - 50 if top - 50 > 0 else -1
        return ("#".join(evs) + f"#{nxt}#").encode()

    def events(self, index=0):
        self.calls.append(index)
        return parse_events(self._page(self.total if index == 0 else index))

    def users(self):
        return parse_users(user_record(555, 1, (0, 0, 0), "TEST", "User", 0))

    def labels(self, name, prefix):
        return {1: "Front door"}

    def translations(self, lang):
        return {"10": "Access granted", "strS": " (S)", "strLecteur": "Reader"}

    def versions(self):
        return {"web_server": "3.5", "controller": "3.8"}


class EventStoreTests(unittest.TestCase):
    def test_numbering_backfill_and_incremental(self):
        fake = FakeClient(total=120)
        d = Dashboard(fake, "en")
        page, nxt = d._fetch_page(0)
        self.assertEqual(page[0][:2], (0, 120))  # newest event: current lap, position 120
        self.assertEqual(page[0][2].identifier, "120")
        self.assertEqual(nxt, 70)

        d.poll_events(force=True)
        rows = d.archive.query(None, None, None, None, None, None, None, 1000)
        self.assertTrue(rows)
        # Every stored event number must equal the id the fake log gave it.
        self.assertTrue(all(str(r.key % 10**9) == r.identifier for r in rows))

        fake.total = 130                         # 10 new events arrive
        d.last_poll = float("-inf")              # skip the rate limit between polls
        d.poll_events(force=True)
        rows = d.archive.query(None, None, None, None, None, None, None, 1000)
        self.assertEqual(rows[0].key % 10**9, 130)
        self.assertTrue(all(str(r.key % 10**9) == r.identifier for r in rows))

    def test_enrich_resolves_door(self):
        d = Dashboard(FakeClient(), "en")
        _, _, ev = d._fetch_page(0)[0][0]
        out = d.enrich(120, ev, "TEST User")
        self.assertEqual((out["door"], out["person"], out["category"]), ("Front door", "TEST User", "granted"))


if __name__ == "__main__":
    unittest.main()
