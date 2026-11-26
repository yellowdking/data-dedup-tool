"""Helper script to package data-dedup-tool into a single-file Windows executable."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPEC_FILE = PROJECT_ROOT / "build.spec"
DIST_ASCII_EXE = PROJECT_ROOT / "dist" / "data-dedup-tool.exe"
DIST_CN_EXE = PROJECT_ROOT / "dist" / "数据查重程序.exe"
ICON_FILE = PROJECT_ROOT / "src" / "resources" / "app.ico"


def _utf8_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def ensure_icon() -> None:
    """Generate src/resources/app.ico automatically if an icon generator exists."""
    if ICON_FILE.exists() and ICON_FILE.stat().st_size > 0:
        return

    candidates = [
        (PROJECT_ROOT / "src" / "ui" / "icon_gen.py", [sys.executable, "-m", "src.ui.icon_gen"]),
        (PROJECT_ROOT / "scripts" / "gen_icon.py", [sys.executable, str(PROJECT_ROOT / "scripts" / "gen_icon.py")]),
    ]
    for script_path, cmd in candidates:
        if script_path.exists():
            print("Generating application icon:", " ".join(cmd))
            res = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=_utf8_env())
            if res.returncode != 0:
                print(f"Warning: icon generator exited with code {res.returncode}", file=sys.stderr)
            break

    if not ICON_FILE.exists():
        print(f"Warning: icon file not found at {ICON_FILE}; building with default icon.")


def main() -> int:
    if not SPEC_FILE.exists():
        print(f"Error: spec file not found at {SPEC_FILE}", file=sys.stderr)
        return 1

    ensure_icon()

    build_dir = PROJECT_ROOT / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        str(SPEC_FILE),
    ]
    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=_utf8_env())
    if result.returncode != 0:
        print(f"PyInstaller failed with exit code {result.returncode}", file=sys.stderr)
        return result.returncode

    if not DIST_ASCII_EXE.exists():
        print(f"Error: expected output executable not found at {DIST_ASCII_EXE}", file=sys.stderr)
        return 1

    shutil.copy2(DIST_ASCII_EXE, DIST_CN_EXE)

    size_mb = DIST_ASCII_EXE.stat().st_size / (1024 * 1024)
    print(f"Build succeeded: {DIST_ASCII_EXE} ({size_mb:.2f} MB)")
    print(f"Localized copy created: {DIST_CN_EXE.name} ({size_mb:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
