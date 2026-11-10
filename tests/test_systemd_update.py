import os
import signal
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
        self.helper_units = []
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self.unit_dir = config_home / "systemd" / "user"
        self.unit_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for unit in self.helper_units:
            subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, timeout=10)
        for unit, unit_file in self.units:
            subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, timeout=10)
            unit_file.unlink(missing_ok=True)
        if self.units:
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10)
        self.temp.cleanup()

    def _start_unit(self, command):
        self.assertEqual(len(command), 1)
        unit = f"visor-dash-test-{uuid.uuid4().hex}.service"
        unit_file = self.unit_dir / unit
        unit_file.write_text(
            "[Unit]\n"
            "Description=visor-dash updater integration test\n"
            "[Service]\n"
            "Type=exec\n"
            f"ExecStart={command[0]}\n"
            "Restart=always\n"
            "RestartSec=1s\n"
            "KillMode=control-group\n",
            encoding="utf-8",
        )
        self.units.append((unit, unit_file))
        result = subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(
            ["systemctl", "--user", "start", unit],
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
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
        self.helper_units.append(helper_unit)
        os.kill(pid, signal.SIGTERM)

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
                lambda: log.exists() and
                "Update installed and dashboard restarted" in log.read_text(encoding="utf-8"),
                "new executable was not installed and started",
                diagnostics=lambda: self._diagnostics(log, helper_unit, dashboard_unit),
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "new-version\n")
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
