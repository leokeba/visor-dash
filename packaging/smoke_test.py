"""Start a built app against a fake controller and check that the dashboard is served.

  python packaging/smoke_test.py EXPECTED_VERSION PATH [PATH ...]   (a binary, or a .pyz)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PORT = 18765


def command(path: str):
    return [sys.executable, path] if path.endswith(".pyz") else [path]


def check(path: str, version: str) -> None:
    cmd = command(path)
    out = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and version in out.stdout, f"--version said: {out.stdout!r} {out.stderr!r}"
    home = tempfile.mkdtemp()
    try:
        # Controller on a closed local port: every request fails fast; the page must still load.
        Path(home, ".env").write_text("VAUBAN_HOST=127.0.0.1\nVAUBAN_PORT=9\nVAUBAN_USER=a\nVAUBAN_PASSWORD=b\n",
                                      encoding="utf-8")
        where = subprocess.run(cmd + ["--home", home, "where"], capture_output=True, text=True, timeout=120)
        # Compare resolved paths: Windows may hand out the temp folder in short 8.3 form.
        shown = where.stdout.splitlines()[0].split(":", 1)[1].strip() if where.stdout else ""
        assert where.returncode == 0 and os.path.normcase(os.path.realpath(shown)) == \
            os.path.normcase(os.path.realpath(home)), where.stdout + where.stderr
        proc = subprocess.Popen(cmd + ["--home", home, "serve", "--port", str(PORT)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.time() + 60
            while True:
                try:
                    for page, marker in (("/", b"<title>"), ("/app.js", b"function"), ("/style.css", b":root")):
                        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{page}", timeout=5) as r:
                            body = r.read()
                            assert r.status == 200 and marker in body, f"{page}: unexpected content"
                    break
                except OSError:
                    if time.time() > deadline or proc.poll() is not None:
                        proc.kill()
                        raise SystemExit(f"{path}: dashboard did not start.\n{proc.stdout.read()}")
                    time.sleep(1)
        finally:
            stop(proc)
    finally:
        remove(home)
    print(f"ok  {path}  ({version})")


def stop(proc: subprocess.Popen) -> None:
    """Stop the app and anything it started (a one-file app runs as launcher + child)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)


def remove(folder: str) -> None:
    """Delete the temporary home; files can stay locked briefly on Windows after the process exits."""
    for _ in range(20):
        try:
            shutil.rmtree(folder)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.5)
    shutil.rmtree(folder, ignore_errors=True)


def main() -> int:
    version, paths = sys.argv[1], sys.argv[2:]
    for p in paths:
        check(p, version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
