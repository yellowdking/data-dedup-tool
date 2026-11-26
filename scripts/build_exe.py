"""Helper script to package data-dedup-tool into a single-file Windows executable.

Steps performed:
1. Ensure ``src/resources/app.ico`` exists (generating via ``src.ui.icon_gen`` if missing).
2. Generate Windows PE ``VSVersionInfo`` metadata from ``pyproject.toml``.
3. Run PyInstaller against ``build.spec``.
4. Create the localized ``dist/数据查重程序.exe`` copy and report exact MB size.
5. Verify PE version metadata (on Windows) and execute ``dist/data-dedup-tool.exe --smoke-test``
   in offscreen mode with an isolated temporary data directory.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.version_info import read_project_version, write_version_info_file

SPEC_FILE = PROJECT_ROOT / "build.spec"
DIST_ASCII_EXE = PROJECT_ROOT / "dist" / "data-dedup-tool.exe"
DIST_CN_EXE = PROJECT_ROOT / "dist" / "数据查重程序.exe"
ICON_FILE = PROJECT_ROOT / "src" / "resources" / "app.ico"
VERSION_INFO_FILE = PROJECT_ROOT / "build_version_info.txt"


def _utf8_env(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def ensure_icon() -> None:
    """Generate ``src/resources/app.ico`` automatically if missing."""
    if ICON_FILE.exists() and ICON_FILE.stat().st_size > 0:
        return

    icon_script = PROJECT_ROOT / "src" / "ui" / "icon_gen.py"
    if icon_script.exists():
        cmd = [sys.executable, "-m", "src.ui.icon_gen"]
        print("Generating application icon:", " ".join(cmd))
        res = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=_utf8_env())
        if res.returncode != 0:
            print(f"Warning: icon generator exited with code {res.returncode}", file=sys.stderr)

    if not ICON_FILE.exists():
        print(f"Warning: icon file not found at {ICON_FILE}; building with default icon.")


def verify_pe_version_info(exe_path: Path) -> bool:
    """Return True if the Windows executable embeds a non-empty VS_VERSION_INFO resource."""
    if sys.platform != "win32":
        return True
    try:
        version_dll = ctypes.windll.version
        dummy = ctypes.c_uint(0)
        size = version_dll.GetFileVersionInfoSizeW(str(exe_path), ctypes.byref(dummy))
        print(f"PE VersionInfo size: {size} bytes")
        return int(size) > 0
    except Exception as exc:
        print(f"Warning: failed to query PE VersionInfo: {exc}", file=sys.stderr)
        return False


def run_frozen_smoke_test(exe_path: Path, timeout: int = 120) -> int:
    """Launch the frozen EXE with ``--smoke-test`` under offscreen Qt platform."""
    with tempfile.TemporaryDirectory(prefix="dedup_exe_smoke_") as tmp_dir:
        cmd = [str(exe_path), "--smoke-test", "--data-dir", tmp_dir]
        env = _utf8_env({"QT_QPA_PLATFORM": "offscreen"})
        print("Running frozen EXE smoke test:", " ".join(cmd))
        result = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=env,
            timeout=timeout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.stdout.strip():
            print(result.stdout.strip())
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        if result.returncode != 0:
            print(
                f"Error: frozen EXE smoke test failed with exit code {result.returncode}",
                file=sys.stderr,
            )
        else:
            print("Frozen EXE smoke test PASSED (exit code 0).")
        return result.returncode


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build single-file Windows EXE for data-dedup-tool.")
    parser.add_argument(
        "--skip-smoke-test",
        action="store_true",
        help="Skip running the post-build frozen EXE --smoke-test check.",
    )
    args = parser.parse_args(argv)

    if not SPEC_FILE.exists():
        print(f"Error: spec file not found at {SPEC_FILE}", file=sys.stderr)
        return 1

    ensure_icon()

    version_str = read_project_version(PROJECT_ROOT)
    write_version_info_file(
        output_path=VERSION_INFO_FILE,
        version_str=version_str,
        project_root=PROJECT_ROOT,
    )
    print(f"Prepared PE VersionInfo ({version_str}): {VERSION_INFO_FILE}")

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

    size_bytes = DIST_ASCII_EXE.stat().st_size
    size_mb = size_bytes / (1024 * 1024)
    print(f"Build succeeded: {DIST_ASCII_EXE} ({size_mb:.2f} MB / {size_bytes:,} bytes)")
    print(f"Localized copy created: {DIST_CN_EXE.name} ({size_mb:.2f} MB)")

    if not verify_pe_version_info(DIST_ASCII_EXE):
        print("Error: built executable is missing Windows PE VSVersionInfo metadata.", file=sys.stderr)
        return 1

    if not args.skip_smoke_test:
        smoke_rc = run_frozen_smoke_test(DIST_ASCII_EXE)
        if smoke_rc != 0:
            return smoke_rc

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
