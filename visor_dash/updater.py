"""Check GitHub releases and safely replace a packaged visor-dash installation."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Dict, Optional, Tuple

from . import __version__, settings

REPOSITORY = "leokeba/visor-dash"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_PAGE = f"https://github.com/{REPOSITORY}/releases/latest"
MAX_RELEASE_BYTES = 100 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+){1,2})$")
_update_lock = threading.Lock()


class UpdateError(Exception):
    """The latest release could not be checked or installed."""


def version_parts(version: str) -> Tuple[int, ...]:
    match = _VERSION_RE.fullmatch(version)
    if not match:
        raise UpdateError(f"Unrecognized release version: {version}")
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(latest: str, current: str) -> bool:
    if current == "dev":
        return True
    latest_parts, current_parts = version_parts(latest), version_parts(current)
    width = max(len(latest_parts), len(current_parts))
    return latest_parts + (0,) * (width - len(latest_parts)) > \
        current_parts + (0,) * (width - len(current_parts))


def _read_url(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "visor-dash-update-check",
    })
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme != "https" or final.hostname not in {
                    "api.github.com", "github.com", "objects.githubusercontent.com",
                    "release-assets.githubusercontent.com"}:
                raise UpdateError("GitHub redirected the update request to an unexpected host.")
            data = response.read(limit + 1)
    except (OSError, urllib.error.URLError, TimeoutError) as e:
        raise UpdateError(f"Could not reach GitHub: {e}") from e
    if len(data) > limit:
        raise UpdateError("The GitHub response is larger than expected.")
    return data


def _release() -> dict:
    try:
        release = json.loads(_read_url(RELEASE_API, MAX_METADATA_BYTES))
    except (ValueError, UnicodeDecodeError) as e:
        raise UpdateError("GitHub returned invalid release information.") from e
    if not isinstance(release, dict):
        raise UpdateError("GitHub returned invalid release information.")
    tag = release.get("tag_name")
    if not isinstance(tag, str):
        raise UpdateError("The latest GitHub release has no version tag.")
    version_parts(tag)
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("The latest GitHub release has no downloadable assets.")
    by_name = {asset.get("name"): asset for asset in assets
               if isinstance(asset, dict) and isinstance(asset.get("name"), str)}
    return {"version": tag.removeprefix("v"), "assets": by_name}


def _runtime() -> Tuple[Optional[str], Optional[Path]]:
    if settings.is_frozen():
        return "binary", Path(sys.executable).resolve()
    launched = Path(sys.argv[0])
    if launched.suffix.lower() == ".pyz" and launched.is_file() and zipfile.is_zipfile(launched):
        return "pyz", launched.resolve()
    return None, None


def _platform_asset() -> Tuple[Optional[str], Optional[str]]:
    machine = platform.machine().lower()
    if sys.platform == "darwin" and machine in ("arm64", "aarch64"):
        return "macos-arm64", "zip"
    if os.name == "nt" and machine in ("amd64", "x86_64"):
        return "windows-x64", "zip"
    if sys.platform.startswith("linux") and machine in ("amd64", "x86_64"):
        return "linux-x64", "tar.gz"
    return None, None


def update_status() -> dict:
    """Check the latest stable release and whether this installation can update itself."""
    release = _release()
    available = is_newer(release["version"], __version__)
    kind, _ = _runtime()
    platform_target, extension = _platform_asset()
    if kind == "pyz":
        package = f"visor-dash-{release['version']}.pyz"
    elif kind == "binary" and platform_target and extension:
        package = f"visor-dash-{release['version']}-{platform_target}.{extension}"
    else:
        package = None
    installable = available and package in release["assets"] and "SHA256SUMS.txt" in release["assets"]
    return {
        "current": __version__,
        "latest": release["version"],
        "available": available,
        "installable": installable,
        "release_url": RELEASE_PAGE,
        "kind": kind,
    }


def _download_asset(assets: Dict[str, dict], name: str, limit: int) -> bytes:
    asset = assets.get(name)
    if not asset:
        raise UpdateError(f"The latest release is missing {name}.")
    url = asset.get("browser_download_url")
    if not isinstance(url, str):
        raise UpdateError(f"GitHub did not provide a download URL for {name}.")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise UpdateError(f"GitHub provided an unexpected download URL for {name}.")
    declared_size = asset.get("size")
    if isinstance(declared_size, int) and declared_size > limit:
        raise UpdateError(f"{name} is larger than the allowed download size.")
    return _read_url(url, limit)


def _verified_payload(assets: Dict[str, dict], name: str) -> bytes:
    payload = _download_asset(assets, name, MAX_RELEASE_BYTES)
    try:
        sums = _download_asset(assets, "SHA256SUMS.txt", MAX_METADATA_BYTES).decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise UpdateError("GitHub returned an invalid checksum file.") from e
    expected = None
    for line in sums.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            expected = parts[0].lower()
            break
    actual = hashlib.sha256(payload).hexdigest()
    if not expected or not re.fullmatch(r"[0-9a-f]{64}", expected) or \
            not hmac.compare_digest(expected, actual):
        raise UpdateError(f"The SHA-256 checksum for {name} is missing or does not match.")
    return payload


def _binary_from_archive(payload: bytes, version: str, target: str, extension: str) -> bytes:
    filename = "visor-dash.exe" if target == "windows-x64" else "visor-dash"
    member_name = f"visor-dash-{version}-{target}/{filename}"
    if extension == "zip":
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                matches = [item for item in archive.infolist() if item.filename == member_name]
                if len(matches) != 1 or matches[0].is_dir() or \
                        matches[0].file_size > MAX_RELEASE_BYTES or \
                        stat.S_ISLNK(matches[0].external_attr >> 16):
                    raise UpdateError("The release archive does not contain the expected executable.")
                data = archive.read(matches[0])
        except (OSError, RuntimeError, zipfile.BadZipFile, zlib.error) as e:
            raise UpdateError("The downloaded release archive is invalid.") from e
    else:
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
                matches = [item for item in archive.getmembers()
                           if str(PurePosixPath(item.name)) == member_name]
                if len(matches) != 1 or not matches[0].isfile():
                    raise UpdateError("The release archive does not contain the expected executable.")
                source = archive.extractfile(matches[0])
                if source is None:
                    raise UpdateError("The release archive executable could not be read.")
                data = source.read(MAX_RELEASE_BYTES + 1)
        except (EOFError, OSError, tarfile.TarError, zlib.error) as e:
            raise UpdateError("The downloaded release archive is invalid.") from e
    if not data or len(data) > MAX_RELEASE_BYTES:
        raise UpdateError("The release archive executable has an invalid size.")
    return data


def _stage(target: Path, payload: bytes) -> Path:
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
        fd, name = tempfile.mkstemp(prefix=".visor-dash-update-", dir=target.parent)
        stage = Path(name)
        with os.fdopen(fd, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(stage, mode)
        if target.suffix.lower() == ".pyz":
            try:
                with zipfile.ZipFile(stage) as archive:
                    if "__main__.py" not in archive.namelist():
                        raise UpdateError("The downloaded Python app is not a valid .pyz archive.")
            except zipfile.BadZipFile as e:
                raise UpdateError("The downloaded Python app is not a valid .pyz archive.") from e
        return stage
    except OSError as e:
        try:
            stage.unlink(missing_ok=True)
        except (UnboundLocalError, OSError):
            pass
        raise UpdateError(f"Could not stage the update beside {target}: {e}") from e
    except UpdateError:
        stage.unlink(missing_ok=True)
        raise


def _restart_command(target: Path) -> list:
    if settings.is_frozen():
        return [str(target), *sys.argv[1:]]
    return [sys.executable, str(target), *sys.argv[1:]]


def _dashboard_systemd_service() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        groups = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError as e:
        if os.environ.get("INVOCATION_ID"):
            raise UpdateError(f"Could not identify the systemd service for this process: {e}") from e
        return False
    if _in_dashboard_user_service(groups):
        return True
    if any("visor-dash.service" in group.rsplit(":", 1)[-1].strip("/").split("/")
           for group in groups.splitlines()):
        raise UpdateError("Self-update is supported only from the visor-dash user service.")
    if os.environ.get("INVOCATION_ID"):
        raise UpdateError("Self-update is supported only from the visor-dash user service.")
    return False


def _in_dashboard_user_service(groups: str) -> bool:
    for group in groups.splitlines():
        path = group.rsplit(":", 1)[-1]
        parts = path.strip("/").split("/")
        if "visor-dash.service" in parts:
            return any(part.startswith("user@") and part.endswith(".service") for part in parts)
    return False


def _schedule_systemd_restart(stage: Path, target: Path, service_unit: str,
                              pid: Optional[int] = None, log: Optional[Path] = None) -> str:
    pid = pid or os.getpid()
    log = log or settings.logs_dir(settings.home()) / "update.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise UpdateError(f"Could not prepare the update log: {e}") from e
    backup = target.with_name(f".{target.name}.update-backup-{pid}")
    helper_fd, helper_name = tempfile.mkstemp(prefix=".visor-dash-update-helper-", suffix=".sh",
                                               dir=target.parent)
    helper = Path(helper_name)
    script = """#!/bin/sh
trap 'rm -f "$0"' EXIT
umask 077
pid=$1
stage=$2
target=$3
backup=$4
log=$5
unit=$6
exec >>"$log" 2>&1
start_service() {
    if ! systemctl --user start "$unit"; then return 1; fi
    attempt=0
    healthy=0
    while [ "$attempt" -lt 100 ]; do
        state=$(systemctl --user show --property=ActiveState --value "$unit")
        substate=$(systemctl --user show --property=SubState --value "$unit")
        if [ "$state" = active ]; then
            healthy=$((healthy + 1))
            if [ "$healthy" -ge 10 ]; then return 0; fi
        else
            healthy=0
            case "$state:$substate" in
                failed:*|inactive:*|activating:auto-restart) return 1 ;;
            esac
        fi
        sleep 0.2
        attempt=$((attempt + 1))
    done
    return 1
}
printf '%s\\n' 'Waiting for the dashboard process to exit.'
while kill -0 "$pid" 2>/dev/null; do sleep 0.2; done
if ! systemctl --user stop "$unit"; then
    printf '%s\\n' 'Update failed: could not stop the dashboard service.'
    exit 1
fi
if ! cp -p "$target" "$backup"; then
    printf '%s\\n' 'Update failed: could not preserve the current executable.'
    exit 1
fi
if ! mv -f "$stage" "$target"; then
    rm -f "$backup"
    printf '%s\\n' 'Update failed: could not atomically replace the executable.'
    exit 1
fi
if start_service; then
    rm -f "$backup"
    printf '%s\\n' 'Update installed and dashboard restarted.'
    exit 0
fi
printf '%s\\n' 'Update failed to restart; restoring the previous executable.'
systemctl --user stop "$unit" || true
if ! mv -f "$backup" "$target"; then
    printf '%s\\n' 'Update rollback failed; the previous executable remains in the backup file.'
    exit 1
fi
systemctl --user reset-failed "$unit" || true
if ! start_service; then
    printf '%s\\n' 'Update rollback restored the previous executable, but the service did not start.'
    exit 1
fi
printf '%s\\n' 'Previous executable restored and dashboard restarted.'
exit 1
"""
    try:
        with os.fdopen(helper_fd, "w", encoding="utf-8") as file:
            file.write(script)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(helper, 0o700)
    except OSError as e:
        helper.unlink(missing_ok=True)
        raise UpdateError(f"Could not prepare the systemd update helper: {e}") from e

    token = helper.stem.rsplit("-", 1)[-1]
    unit = f"visor-dash-update-{pid}-{token}.service"
    command = [
        "systemd-run", "--user", f"--unit={unit}", "--property=Type=exec",
        "--property=KillMode=control-group", "--collect", "/bin/sh", str(helper),
        str(pid), str(stage), str(target), str(backup), str(log), service_unit,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    except OSError as e:
        helper.unlink(missing_ok=True)
        raise UpdateError(f"Could not start the systemd update service: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise UpdateError("Timed out while starting the systemd update service; check the update log.") from e
    if result.returncode:
        helper.unlink(missing_ok=True)
        detail = (result.stderr or result.stdout).strip()
        raise UpdateError(f"Could not start the systemd update service: {detail or result.returncode}")
    return unit


def _schedule_restart(stage: Path, target: Path) -> None:
    command = _restart_command(target)
    log = settings.logs_dir(settings.home()) / "update.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise UpdateError(f"Could not prepare the update log: {e}") from e
    if _dashboard_systemd_service():
        _schedule_systemd_restart(stage, target, "visor-dash.service", log=log)
        return
    if os.name == "nt":
        data = {
            "pid": os.getpid(),
            "stage": str(stage),
            "target": str(target),
            "command": command[0],
            "arguments": subprocess.list2cmdline(command[1:]),
            "cwd": str(Path.cwd()),
            "log": str(log),
        }
        encoded_data = base64.b64encode(json.dumps(data).encode("utf-8")).decode("ascii")
        script = (
            "$ErrorActionPreference='Stop'; "
            f"$d=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_data}')) | ConvertFrom-Json; "
            "while (Get-Process -Id ([int]$d.pid) -ErrorAction SilentlyContinue) { Start-Sleep -Milliseconds 250 }; "
            "try { Move-Item -LiteralPath $d.stage -Destination $d.target -Force; "
            "Start-Process -FilePath $d.command -ArgumentList ([string]$d.arguments) "
            "-WorkingDirectory $d.cwd } "
            "catch { Add-Content -LiteralPath $d.log -Value ('Update restart failed: ' + $_) }"
        )
        encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        try:
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            )
        except OSError as e:
            raise UpdateError(f"Could not start the Windows update helper: {e}") from e
        return

    script = (
        'pid=$1; stage=$2; target=$3; log=$4; shift 4; '
        'while kill -0 "$pid" 2>/dev/null; do sleep 0.2; done; '
        'if ! mv -f "$stage" "$target"; then '
        'printf "%s\\n" "Update restart failed while replacing $target" >> "$log"; exit 1; fi; '
        'exec "$@"'
    )
    try:
        subprocess.Popen(
            ["/bin/sh", "-c", script, "visor-dash-update", str(os.getpid()),
             str(stage), str(target), str(log), *command],
            stdin=subprocess.DEVNULL, close_fds=True, start_new_session=True,
        )
    except OSError as e:
        raise UpdateError(f"Could not start the update helper: {e}") from e


def install_latest() -> dict:
    """Download, verify and stage the appropriate artifact, then schedule a restart."""
    if not _update_lock.acquire(blocking=False):
        raise UpdateError("An update is already in progress.")
    stage = None
    try:
        release = _release()
        if not is_newer(release["version"], __version__):
            raise UpdateError("This installation is already up to date.")
        kind, target_path = _runtime()
        if target_path is None:
            raise UpdateError("This source installation cannot update itself. Open the latest release page instead.")
        if kind == "binary" and not _platform_asset()[0]:
            raise UpdateError("There is no self-update package for this platform.")
        if kind == "pyz":
            name = f"visor-dash-{release['version']}.pyz"
            payload = _verified_payload(release["assets"], name)
        else:
            target, extension = _platform_asset()
            if not target or not extension:
                raise UpdateError("There is no self-update package for this platform.")
            name = f"visor-dash-{release['version']}-{target}.{extension}"
            archive = _verified_payload(release["assets"], name)
            payload = _binary_from_archive(archive, release["version"], target, extension)
        if not target_path.is_file() or not os.access(target_path.parent, os.W_OK):
            raise UpdateError(f"The application folder is not writable: {target_path.parent}")
        stage = _stage(target_path, payload)
        _schedule_restart(stage, target_path)
        return {"version": release["version"], "restarting": True}
    except UpdateError:
        if stage:
            stage.unlink(missing_ok=True)
        raise
    finally:
        _update_lock.release()
