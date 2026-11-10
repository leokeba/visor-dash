"""Build release archives.

  python packaging/build.py --version 0.1.7 --target macos-arm64        # one-file app (needs PyInstaller)
  python packaging/build.py --version 0.1.7 --target any --pyz          # visor-dash.pyz (needs only Python)

Archives land in dist/release/.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tarfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
RELEASE = DIST / "release"


def write_version(version: str) -> None:
    (ROOT / "visor_dash" / "_version.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")


def docs(dest: Path, how_to_run: str) -> None:
    shutil.copy(ROOT / "LICENSE", dest / "LICENSE.txt")
    shutil.copy(ROOT / ".env.example", dest / "settings-example.env")
    (dest / "README.txt").write_text(f"""visor-dash: local dashboard for Vauban VISOR+ / VERSO+ access controllers
Unofficial. Not affiliated with or endorsed by Vauban Systems.

1. Set up (asks for the controller's address and login, tests them):
     {how_to_run} setup
2. Run it, then open http://localhost:8080 :
     {how_to_run}
3. Optional: start it at login and back up every night:
     {how_to_run} install-service

"{how_to_run} where" shows where settings, backups and history are kept.
Full documentation: https://github.com/leokeba/visor-dash
""", encoding="utf-8")


def build_binary(version: str, target: str) -> Path:
    import PyInstaller.__main__  # noqa: imported only for binary builds

    exe = "visor-dash.exe" if os.name == "nt" else "visor-dash"
    work = ROOT / "build"
    PyInstaller.__main__.run([
        str(ROOT / "packaging" / "visor-dash.py"),
        "--name", "visor-dash", "--onefile", "--console", "--clean", "--noconfirm",
        "--add-data", f"{ROOT / 'visor_dash' / 'static'}{os.pathsep}visor_dash/static",
        "--paths", str(ROOT),
        "--distpath", str(DIST / "bin"), "--workpath", str(work), "--specpath", str(work),
    ])
    folder = DIST / f"visor-dash-{version}-{target}"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    shutil.copy2(DIST / "bin" / exe, folder / exe)
    docs(folder, exe if os.name == "nt" else "./visor-dash")
    RELEASE.mkdir(parents=True, exist_ok=True)
    if target.startswith("linux"):
        out = RELEASE / f"{folder.name}.tar.gz"
        with tarfile.open(out, "w:gz") as tar:
            tar.add(folder, arcname=folder.name)
    else:
        out = Path(shutil.make_archive(str(RELEASE / folder.name), "zip", DIST, folder.name))
    print(f"built {out}")
    return folder / exe


def build_pyz(version: str) -> Path:
    stage = DIST / "pyz-stage"
    shutil.rmtree(stage, ignore_errors=True)
    shutil.copytree(ROOT / "visor_dash", stage / "visor_dash",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (stage / "__main__.py").write_text("import sys\nfrom visor_dash.cli import main\nsys.exit(main())\n", encoding="utf-8")
    RELEASE.mkdir(parents=True, exist_ok=True)
    out = RELEASE / f"visor-dash-{version}.pyz"
    zipapp.create_archive(stage, out, interpreter="/usr/bin/env python3", compressed=True)
    print(f"built {out}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--pyz", action="store_true", help="also build the cross-platform .pyz")
    args = ap.parse_args()
    write_version(args.version)
    built = []
    if args.target != "any":
        built.append(build_binary(args.version, args.target))
    if args.pyz:
        built.append(build_pyz(args.version))
    # Paths for the smoke test step.
    (DIST / "built.txt").write_text("\n".join(str(b) for b in built), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
