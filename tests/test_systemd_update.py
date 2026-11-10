import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from visor_dash import updater


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("systemd-run") and
                     shutil.which("systemctl"), "requires Linux systemd")
class SystemdUpdateIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show-environment"],
                capture_output=True, timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise unittest.SkipTest("no responsive systemd user manager")
        if result.returncode:
            raise unittest.SkipTest("no systemd user manager")

    def setUp(self):
        self.units = []
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        for unit in self.units:
            subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, timeout=10)
        self.temp.cleanup()

    def _start_unit(self, command):
        unit = f"visor-dash-test-{uuid.uuid4().hex}.service"
        result = subprocess.run(
            ["systemd-run", "--user", f"--unit={unit}", "--property=Type=exec",
             "--property=KillMode=control-group", "--property=Restart=always",
             "--property=RestartSec=1s", "--quiet", *command],
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.units.append(unit)
        return unit

    def _main_pid(self, unit):
        result = subprocess.run(
            ["systemctl", "--user", "show", "--property=MainPID", "--value", unit],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return int(result.stdout.strip())

    def _wait_for(self, predicate, message, diagnostics=None):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        if diagnostics:
            message = f"{message}\n{diagnostics()}"
        self.fail(message)

    def _diagnostics(self, log, helper_unit, dashboard_unit):
        contents = log.read_text(encoding="utf-8") if log.exists() else "<no updater log>"
        details = [f"Updater log:\n{contents}"]
        for unit in (helper_unit, dashboard_unit):
            status = subprocess.run(
                ["systemctl", "--user", "status", unit, "--no-pager"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            journal = subprocess.run(
                ["journalctl", "--user", "-u", unit, "--no-pager", "-n", "20"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            details.extend((f"{unit} status:\n{status.stdout}{status.stderr}",
                            f"{unit} journal:\n{journal.stdout}{journal.stderr}"))
        return "\n".join(details)

    def _exercise_update(self, stage_exists, fail_new_start=False):
        target = self.root / "visor-dash"
        target.write_text("old-version\n", encoding="utf-8")
        target.chmod(0o755)
        stage = self.root / ".visor-dash-update-stage"
        if stage_exists:
            stage.write_text("new-version\n", encoding="utf-8")
            stage.chmod(0o755)
        marker = self.root / "started-version"
        app = self.root / "fake-dashboard"
        app.write_text(
            f"#!/bin/sh\n"
            f"cat {shlex.quote(str(target))} > {shlex.quote(str(marker))}\n"
            f"if [ \"$(cat {shlex.quote(str(target))})\" = new-version ] && "
            f"{'true' if fail_new_start else 'false'}; then exit 1; fi\n"
            "exec /bin/sleep 60\n",
            encoding="utf-8",
        )
        app.chmod(0o755)
        dashboard_unit = self._start_unit([str(app)])
        self._wait_for(lambda: marker.exists(), "fake dashboard did not start")
        pid = self._main_pid(dashboard_unit)
        log = self.root / "update.log"
        helper_unit = updater._schedule_systemd_restart(stage, target, dashboard_unit, pid, log)
        self.units.append(helper_unit)
        stopped = subprocess.run(
            ["systemctl", "--user", "stop", dashboard_unit],
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(stopped.returncode, 0, stopped.stderr)

        if stage_exists:
            if fail_new_start:
                self._wait_for(
                    lambda: log.exists() and
                    "Previous executable restored and dashboard restarted" in log.read_text(encoding="utf-8"),
                    "failed new version was not rolled back",
                    diagnostics=lambda: self._diagnostics(log, helper_unit, dashboard_unit),
                )
                self.assertEqual(marker.read_text(encoding="utf-8"), "old-version\n")
                self.assertEqual(target.read_text(encoding="utf-8"), "old-version\n")
                self.assertFalse(target.with_name(f".{target.name}.update-backup-{pid}").exists())
                return
            self._wait_for(
                lambda: marker.read_text(encoding="utf-8") == "new-version\n",
                "new executable was not installed and started",
                diagnostics=lambda: self._diagnostics(log, helper_unit, dashboard_unit),
            )
            self.assertEqual(target.read_text(encoding="utf-8"), "new-version\n")
            self.assertFalse(target.with_name(f".{target.name}.update-backup-{pid}").exists())
        else:
            self._wait_for(
                lambda: log.exists() and "could not atomically replace" in log.read_text(encoding="utf-8"),
                "replacement failure was not logged",
                diagnostics=lambda: self._diagnostics(log, helper_unit, dashboard_unit),
            )
            self.assertEqual(target.read_text(encoding="utf-8"), "old-version\n")
            self.assertFalse(target.with_name(f".{target.name}.update-backup-{pid}").exists())

    def test_helper_survives_dashboard_shutdown_and_starts_replacement(self):
        self._exercise_update(stage_exists=True)

    def test_failed_replacement_preserves_old_executable(self):
        self._exercise_update(stage_exists=False)

    def test_failed_new_service_start_rolls_back_old_executable(self):
        self._exercise_update(stage_exists=True, fail_new_start=True)


if __name__ == "__main__":
    unittest.main()
