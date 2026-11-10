"""Offline tests for door features (synthetic data only).  Run: python3 -m unittest -v"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

import visor_dash.server as server
from visor_dash.client import RawEvent, VaubanClient
from visor_dash.server import Dashboard, make_handler
from tests.test_dashboard import FakeClient

NOW = datetime.now().replace(microsecond=0)


def stamp(dt):
    return dt.strftime("%d/%m/%Y %H:%M:%S")


class DoorClient(FakeClient):
    """Event log kept as a list (oldest first). open_door() can log a remote opening."""

    def __init__(self, log, logs_opening=True, reply="<script>\nSucces();\n</script>"):
        super().__init__()
        self.log, self.logs_opening, self.reply = list(log), logs_opening, reply
        self.opened = []

    def events(self, index=0):
        top = len(self.log) if index == 0 else index
        page = [self.log[p - 1] for p in range(top, max(top - 50, 0), -1) if p <= len(self.log)]
        return page, (top - 50 if top - 50 > 0 else None)

    def labels(self, name, prefix):
        return {1: "Front door", 2: "Office"} if name == "Lecteurs" else {1: "Group A"}

    def open_door(self, reader):
        self.opened.append(reader)
        if self.logs_opening:
            self.log.append(RawEvent(stamp(datetime.now()), 49, reader, "0", 0))
        return self.reply.encode()


LOG = [
    RawEvent(stamp(NOW - timedelta(days=3)), 10, 1, "555", 1),       # badge, 3 days ago, door 1
    RawEvent(stamp(NOW - timedelta(minutes=30)), 10, 1, "555", 1),   # badge today, door 1
    RawEvent(stamp(NOW - timedelta(minutes=20)), 54, 1, "0", 0),     # push button today, door 1
    RawEvent(stamp(NOW - timedelta(minutes=10)), 0, 129, "999", 0),  # unknown badge, exit side of door 1
    RawEvent(stamp(NOW - timedelta(minutes=5)), 10, 2, "777", 0),    # another door
    RawEvent(stamp(NOW - timedelta(minutes=1)), 74, 0, "0", 0),      # power fault, no door
]


class FastPolling(unittest.TestCase):
    def setUp(self):
        self._saved = (server.MIN_FORCED_POLL_INTERVAL, server.OPEN_CONFIRM_WAIT_S)
        server.MIN_FORCED_POLL_INTERVAL, server.OPEN_CONFIRM_WAIT_S = 0.01, 0.3

    def tearDown(self):
        server.MIN_FORCED_POLL_INTERVAL, server.OPEN_CONFIRM_WAIT_S = self._saved


class DoorLogicTests(FastPolling):
    def test_door_detail_counts(self):
        d = Dashboard(DoorClient(LOG), "en")
        d.import_history()
        det = d.door_detail(1)
        self.assertEqual(det["name"], "Front door")
        self.assertEqual((det["today"]["openings"], det["today"]["badge"], det["today"]["push_button"],
                          det["today"]["refused"]), (2, 1, 1, 1))
        self.assertEqual(det["week"]["openings"], 3)
        self.assertEqual(len(det["recent"]), 4)                  # both sides of door 1, nothing else
        self.assertEqual(det["last_opening"]["code"], 54)
        with self.assertRaises(server.ValidationError):
            d.door_detail(7)

    def test_open_confirmed_by_log(self):
        c = DoorClient(LOG)
        d = Dashboard(c, "en")
        d.door_open_enabled = True
        r = d.open_door(1)
        self.assertEqual(c.opened, [1])
        self.assertTrue(r["accepted"] and r["confirmed"], r)
        self.assertEqual(r["event"]["code"], 49)

    def test_open_accepted_but_not_logged(self):
        d = Dashboard(DoorClient(LOG, logs_opening=False), "en")
        r = d.open_door(1)
        self.assertEqual((r["accepted"], r["confirmed"]), (True, False))

    def test_open_refused_by_controller(self):
        reply = ("<script>\nfunction Erreur(t)\n{\nvar message = '[strEchecOuverture]';\n}\nErreur(1);\n</script>")
        d = Dashboard(DoorClient(LOG, logs_opening=False, reply=reply), "en")
        r = d.open_door(2)
        self.assertFalse(r["accepted"])

    def test_cooldown_and_unknown_door(self):
        c = DoorClient(LOG)
        d = Dashboard(c, "en")
        d.open_door(1)
        with self.assertRaises(server.ValidationError):
            d.open_door(1)
        with self.assertRaises(server.ValidationError):
            d.open_door(9)
        self.assertEqual(c.opened, [1])

    def test_person_recent_is_exact_badge(self):
        d = Dashboard(DoorClient(LOG + [RawEvent(stamp(NOW), 10, 1, "5551", 0)]), "en")
        d.import_history()
        rows = d.archive.query(None, None, None, None, None, None, None, 50, identifier="555")
        self.assertEqual(len(rows), 2)


class ClientDoorGuardTests(unittest.TestCase):
    def test_off_by_default_and_validated(self):
        c = VaubanClient("192.0.2.1", "u", "p", allow_writes=True)
        with self.assertRaises(PermissionError):
            c.open_door(1)                                       # user editing does not imply door opening
        c = VaubanClient("192.0.2.1", "u", "p", allow_door_open=True)
        for bad in (0, -1, 128, "1", True, 1.5):
            with self.assertRaises(PermissionError, msg=repr(bad)):
                c.open_door(bad)


class DoorHttpTests(FastPolling):
    def setUp(self):
        super().setUp()
        self.client = DoorClient(LOG)
        self.dash = Dashboard(self.client, "en")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.dash, None, None))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def post(self, path, body, headers=None):
        h = {"Content-Type": "application/json", "X-Dashboard": "1", **(headers or {})}
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={k: v for k, v in h.items() if v is not None})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_guards(self):
        self.assertEqual(self.post("/api/doors/1/open", {"confirm_door": 1})[0], 403)   # switched off
        self.dash.door_open_enabled = True
        self.assertEqual(self.post("/api/doors/1/open", {"confirm_door": 1}, {"X-Dashboard": None})[0], 403)
        self.assertEqual(self.post("/api/doors/1/open", {"confirm_door": 1}, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.post("/api/doors/1/open", {"confirm_door": 2})[0], 400)
        self.assertEqual(self.post("/api/doors/9/open", {"confirm_door": 9})[0], 400)
        self.assertEqual(self.client.opened, [])
        status, body = self.post("/api/doors/1/open", {"confirm_door": 1})
        self.assertEqual(status, 200)
        self.assertTrue(body["confirmed"])
        self.assertEqual(self.client.opened, [1])

    def test_editing_switch_does_not_enable_doors(self):
        self.dash.manager = object()                                                  # people editing on
        self.assertEqual(self.post("/api/doors/1/open", {"confirm_door": 1})[0], 403)


if __name__ == "__main__":
    unittest.main()
