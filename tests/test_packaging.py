"""Settings, background-service files and command line.  Run: python3 -m unittest -v"""
import contextlib
import io
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
import zipapp
from pathlib import Path
from unittest import mock

from visor_dash import cli, service, settings

ROOT = Path(__file__).resolve().parent.parent


class SettingsTests(unittest.TestCase):
    def test_example_file_matches_template(self):
        self.assertEqual((ROOT / ".env.example").read_text(encoding="utf-8"), settings.example_text())

    def test_write_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "new home"
            path = settings.write(home, {"host": "10.0.0.5", "password": "p@ss word", "writes": "1"})
            if os.name != "nt":
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            cfg = settings.load(home)
            self.assertEqual((cfg["VAUBAN_HOST"], cfg["VAUBAN_PASSWORD"], cfg["DASH_ALLOW_WRITES"],
                              cfg["DASH_ALLOW_DOOR_OPEN"]), ("10.0.0.5", "p@ss word", "1", "0"))
            with mock.patch.dict(os.environ, {"DASH_PORT": "9999"}):
                self.assertEqual(settings.load(home)["DASH_PORT"], "9999")     # environment wins

    def test_home_selection(self):
        with mock.patch.dict(os.environ, {"VISOR_DASH_HOME": "/tmp/x"}):
            self.assertEqual(settings.home(), Path("/tmp/x").resolve())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VISOR_DASH_HOME", None)
            self.assertEqual(settings.home(), ROOT)                          # from source
            with mock.patch.object(sys, "frozen", True, create=True):
                self.assertIn("visor-dash", str(settings.home()))           # installed app: per-user folder


class ServiceFileTests(unittest.TestCase):
    def test_plist_is_valid_with_spaces(self):
        text = service._plist("local.test", ["/Apps/My Tools/visor-dash", "--home", "/Users/a b/x&y", "serve"],
                              Path("/Users/a b"), Path("/tmp/log file.log"), "  <key>RunAtLoad</key><true/>")
        data = plistlib.loads(text.encode())
        self.assertEqual(data["ProgramArguments"][2], "/Users/a b/x&y")
        self.assertTrue(data["RunAtLoad"])

    def test_systemd_quoting(self):
        self.assertEqual(service._systemd_quote(["/opt/v d/visor-dash", "--home", 'a"b']),
                         '"/opt/v d/visor-dash" "--home" "a\\"b"')

    def test_vbs_quotes_every_argument(self):
        line = service._vbs([r"C:\Program Files\visor-dash.exe", "serve"])
        self.assertEqual(line, 'CreateObject("WScript.Shell").Run """C:\\Program Files\\visor-dash.exe"" ""serve""", 0, False\r\n')

    def test_service_commands_pass_home(self):
        cmd = service.base_command(Path("/data/home"))
        self.assertEqual(cmd[-2:], ["--home", str(Path("/data/home"))])
        self.assertIn("visor_dash", cmd)                                     # from source: python -m visor_dash

    def test_install_needs_settings(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit):
                service.install(Path(d))


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                code = cli.main(list(args))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_version_and_where(self):
        code, out = self.run_cli("--version")
        self.assertEqual(code, 0)
        self.assertIn("visor-dash", out)
        with tempfile.TemporaryDirectory() as d:
            code, out = self.run_cli("--home", d, "where")
            self.assertEqual(code, 0)
            self.assertIn("not created yet", out)

    def test_backup_without_settings(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_cli("--home", d, "backup")[0], 2)


class PyzTests(unittest.TestCase):
    def test_pages_are_served_from_inside_an_archive(self):
        with tempfile.TemporaryDirectory() as d:
            stage = Path(d) / "stage"
            import shutil
            shutil.copytree(ROOT / "visor_dash", stage / "visor_dash", ignore=shutil.ignore_patterns("__pycache__"))
            (stage / "__main__.py").write_text("import sys\nfrom visor_dash.cli import main\nsys.exit(main())\n")
            pyz = Path(d) / "v.pyz"
            zipapp.create_archive(stage, pyz)
            code = ("import sys; sys.path.insert(0, sys.argv[1]); from visor_dash.server import read_static;"
                    "print(len(read_static('index.html')) > 100, b'function' in read_static('app.js'))")
            out = subprocess.run([sys.executable, "-I", "-c", code, str(pyz)], capture_output=True, text=True, cwd=d)
            self.assertEqual(out.stdout.strip(), "True True", out.stderr)


if __name__ == "__main__":
    unittest.main()
