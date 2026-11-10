"""Offline tests for backups and badge-swipe enrolment.  Run: python3 -m unittest -v"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from visor_dash.backup import rotate, run_backup, status_summary
from visor_dash.client import ControllerError
from visor_dash.server import Dashboard
from visor_dash.users import build_form
from tests.test_dashboard import FakeClient, user_record
from tests.test_users import BLANK, EXISTING, GROUPS, SLOTS, TR, page


class FakeBackupClient:
    def __init__(self, fail=False):
        self.fail = fail

    def download_backup(self):
        if self.fail:
            raise ControllerError("timed out")
        return b"CENTRALE\0" + bytes(4000)

    def get(self, path, query=""):
        assert path == "/assets/Users"
        return user_record(40000001, 1, (2, 0, 0), "DUPONT ", "Jean", 1)


class FakeFormManager:
    def _form_for(self, u):
        return build_form(page(EXISTING.replace("id').value=7", "id').value=1")), BLANK, GROUPS, SLOTS, TR)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "backups"

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_backup_writes_private_files_and_status(self):
        st = run_backup(FakeBackupClient(), FakeFormManager(), self.dir, log=lambda m: None)
        self.assertTrue(st["ok"], st)
        self.assertEqual((st["people"], st["problems"]), (1, 0))
        if os.name != "nt":   # Windows has no Unix permission bits (per-user folders are private already)
            for name in (st["official_file"], st["forms_file"], "last-backup.json"):
                mode = os.stat(self.dir / name).st_mode & 0o777
                self.assertEqual(mode, 0o600, name)
        snap = json.loads((self.dir / st["forms_file"]).read_text())
        self.assertEqual(snap["forms"][0]["form"]["lastname"], "DUPONT ")
        self.assertEqual(status_summary(self.dir)["state"], "ok")

    def test_failure_keeps_last_success(self):
        ok = run_backup(FakeBackupClient(), FakeFormManager(), self.dir, log=lambda m: None)
        bad = run_backup(FakeBackupClient(fail=True), FakeFormManager(), self.dir, log=lambda m: None)
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["last_success"], ok["last_success"])
        s = status_summary(self.dir)
        self.assertEqual(s["state"], "failed")
        self.assertIn("timed out", s["last_error"])

    def test_stale_and_none(self):
        self.assertEqual(status_summary(self.dir)["state"], "none")
        run_backup(FakeBackupClient(), FakeFormManager(), self.dir, log=lambda m: None)
        later = datetime.now() + timedelta(hours=40)
        self.assertEqual(status_summary(self.dir, now=later)["state"], "stale")

    def test_rotation_only_touches_automatic_files(self):
        self.dir.mkdir()
        for i in range(5):
            (self.dir / f"auto-vauban-2026010{i}-000000.BACKUP").write_bytes(b"x")
        (self.dir / "vauban-20250101-000000.BACKUP").write_bytes(b"manual")
        deleted = rotate(self.dir, "auto-vauban-", ".BACKUP", keep=2)
        self.assertEqual(len(deleted), 3)
        left = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(left, ["auto-vauban-20260103-000000.BACKUP", "auto-vauban-20260104-000000.BACKUP",
                                "vauban-20250101-000000.BACKUP"])


class SwipeClient(FakeClient):
    """Event log whose newest event is an unknown badge swipe."""

    def _page(self, top):
        evs = []
        for n in range(top, max(top - 50, 0), -1):
            code, ident = (0, "777000111") if n == self.total and self.swiped else (10, str(n))
            evs.append(f"10/11/2026 12:{n // 60 % 60:02d}:{n % 60:02d}${code}$1${ident}$0")
        nxt = top - 50 if top - 50 > 0 else -1
        return ("#".join(evs) + f"#{nxt}#").encode()


class SwipeTests(unittest.TestCase):
    def test_reports_only_new_unknown_badges(self):
        fake = SwipeClient(total=60)
        fake.swiped = False
        d = Dashboard(fake, "en")
        first = d.badge_swipe(0)
        self.assertEqual((first["latest_no"] % 10**9, first["badge"]), (60, None))

        fake.total, fake.swiped = 61, True          # someone swipes an unknown badge
        d.last_poll = float("-inf")
        r = d.badge_swipe(first["latest_no"])
        self.assertEqual(r["badge"]["identifier"], "777000111")
        self.assertEqual(r["badge"]["door"], "Front door")
        self.assertFalse(r["badge"]["already_known"])

        d.last_poll = float("-inf")
        self.assertIsNone(d.badge_swipe(r["latest_no"])["badge"])  # nothing newer than the swipe


if __name__ == "__main__":
    unittest.main()
