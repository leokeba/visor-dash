import hashlib
import io
import json
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from visor_dash import updater
from visor_dash.server import Cached, Dashboard, make_handler


class EmptyClient:
    def users(self):
        return []

    def labels(self, _name, _prefix):
        return {}

    def translations(self, _lang):
        return {}

    def versions(self):
        return {}


class UpdateVersionTests(unittest.TestCase):
    def test_compares_numeric_release_versions(self):
        self.assertTrue(updater.is_newer("v0.1.10", "0.1.9"))
        self.assertFalse(updater.is_newer("0.1.9", "0.1.10"))
        self.assertFalse(updater.is_newer("1.0", "1.0.0"))
        self.assertTrue(updater.is_newer("0.1.1", "dev"))

    def test_rejects_non_release_versions(self):
        with self.assertRaises(updater.UpdateError):
            updater.version_parts("v0.1.0-beta")

    def test_update_status_marks_supported_packages(self):
        release = {"version": "0.2.0",
                   "assets": {"visor-dash-0.2.0.pyz": {}, "SHA256SUMS.txt": {}}}
        with patch.object(updater, "_release", return_value=release), \
                patch.object(updater, "__version__", "0.1.9"), \
                patch.object(updater, "_runtime", return_value=("pyz", None)):
            self.assertTrue(updater.update_status()["installable"])
        with patch.object(updater, "_release", return_value=release), \
                patch.object(updater, "__version__", "0.1.9"), \
                patch.object(updater, "_runtime", return_value=(None, None)):
            self.assertFalse(updater.update_status()["installable"])


class UpdateArchiveTests(unittest.TestCase):
    def test_extracts_only_expected_release_binary(self):
        name = "visor-dash-0.2.0-macos-arm64/visor-dash"
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr(name, b"binary")
        self.assertEqual(updater._binary_from_archive(out.getvalue(), "0.2.0", "macos-arm64", "zip"),
                         b"binary")

    def test_rejects_archive_without_expected_binary(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr("../visor-dash", b"unexpected")
        with self.assertRaises(updater.UpdateError):
            updater._binary_from_archive(out.getvalue(), "0.2.0", "macos-arm64", "zip")

    def test_stages_only_valid_python_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "visor-dash.pyz"
            target.write_bytes(b"old")
            package = io.BytesIO()
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("__main__.py", "pass")
            stage = updater._stage(target, package.getvalue())
            try:
                with zipfile.ZipFile(stage) as archive:
                    self.assertIn("__main__.py", archive.namelist())
            finally:
                stage.unlink()

            with self.assertRaises(updater.UpdateError):
                updater._stage(target, b"not a zip")

    def test_checks_download_against_published_checksum(self):
        payload = b"release archive"
        digest = hashlib.sha256(payload).hexdigest()
        assets = {"visor-dash-0.2.0.pyz": {}, "SHA256SUMS.txt": {}}

        def download(_assets, name, _limit):
            return payload if name.endswith(".pyz") else f"{digest}  visor-dash-0.2.0.pyz\n".encode()

        with patch.object(updater, "_download_asset", side_effect=download):
            self.assertEqual(updater._verified_payload(assets, "visor-dash-0.2.0.pyz"), payload)

        def altered_download(_assets, name, _limit):
            return b"bad" if name.endswith(".pyz") else f"{digest}  visor-dash-0.2.0.pyz\n".encode()

        with patch.object(updater, "_download_asset", side_effect=altered_download):
            with self.assertRaises(updater.UpdateError):
                updater._verified_payload(assets, "visor-dash-0.2.0.pyz")


class UpdateHttpTests(unittest.TestCase):
    def setUp(self):
        self.dash = Dashboard(EmptyClient(), "en")
        status = {"current": "0.1.9", "latest": "0.2.0", "available": True,
                  "installable": True, "release_url": updater.RELEASE_PAGE, "kind": "pyz"}
        self.dash.release_update = Cached(3600, lambda: status)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.dash, None, None))
        port = self.httpd.server_address[1]
        self.httpd.RequestHandlerClass = make_handler(self.dash, None, {f"127.0.0.1:{port}"})
        self.base = f"http://127.0.0.1:{port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_update_status_endpoint(self):
        with urllib.request.urlopen(self.base + "/api/update", timeout=5) as response:
            self.assertTrue(json.loads(response.read())["available"])

    def test_update_install_requires_dashboard_headers(self):
        request = urllib.request.Request(self.base + "/api/update", data=b"{}",
                                         method="POST", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(error.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
