"""Desktop application entry point for 数据查重程序.

Handles early crash-proof exception hooks & faulthandler logging, Qt CLI flag
forwarding, per-data-dir single-instance locking (via QLockFile), high-DPI
initialization, on-demand icon generation, and headless ``--smoke-test``
verification for CI frozen-EXE checks.
"""

from __future__ import annotations

import argparse
import faulthandler
import io
import sqlite3
import sys
import tempfile
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import IO, Optional

# Ensure project root is importable when executed as `python src/app.py`.
if not getattr(sys, "frozen", False):
    _ROOT = Path(__file__).resolve().parent.parent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import QLockFile, QObject, Qt, QThread, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from src.core.config import APP_NAME, ConfigManager
from src.ui.app_context import AppContext
from src.ui.icon_gen import ensure_app_icon
from src.ui.main_window import MainWindow
from src.ui.widgets import ScrollableDetailDialog

_CRASH_LOG_PATH: Optional[Path] = None
_FAULT_LOG_HANDLE: Optional[IO[str]] = None
_SMOKE_TEST_MODE: bool = False
_SHOWING_CRASH_DIALOG: bool = False
_CRASH_BRIDGE: Optional["_CrashDialogBridge"] = None


class _CrashDialogBridge(QObject):
    """Marshals crash dialog presentation safely onto the Qt main thread."""

    show_requested = Signal(str, str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.show_requested.connect(
            self._display_dialog, Qt.ConnectionType.QueuedConnection
        )

    def _display_dialog(self, summary: str, detail: str) -> None:
        _show_crash_dialog_on_main_thread(summary, detail)


def _safe_stderr_write(text: str) -> None:
    """Write safely to sys.stderr even in windowed PyInstaller builds where stderr is None."""
    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write(text)
        stream.flush()
    except Exception:
        pass


def _safe_stdout_write(text: str) -> None:
    """Write safely to sys.stdout even in windowed PyInstaller builds where stdout is None."""
    stream = sys.stdout
    if stream is None:
        return
    try:
        stream.write(text)
        stream.flush()
    except Exception:
        pass


def _configure_crash_logging(data_dir: Optional[Path]) -> Optional[Path]:
    """Configure <data_dir>/logs/crash.log and enable Python faulthandler safely."""
    global _CRASH_LOG_PATH, _FAULT_LOG_HANDLE

    if data_dir is not None:
        log_dir = Path(data_dir) / "logs"
    else:
        log_dir = Path(tempfile.gettempdir()) / "DataDedupTool" / "logs"

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        crash_log = log_dir / "crash.log"
        _CRASH_LOG_PATH = crash_log

        old_handle = _FAULT_LOG_HANDLE
        fh = open(crash_log, "a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=fh, all_threads=True)
        _FAULT_LOG_HANDLE = fh
        if old_handle is not None and not old_handle.closed:
            try:
                old_handle.close()
            except Exception:
                pass
        return crash_log
    except Exception:
        return _CRASH_LOG_PATH


def _close_fault_log_handle() -> None:
    global _FAULT_LOG_HANDLE
    try:
        faulthandler.disable()
    except Exception:
        pass
    if _FAULT_LOG_HANDLE is not None and not _FAULT_LOG_HANDLE.closed:
        try:
            _FAULT_LOG_HANDLE.close()
        except Exception:
            pass
    _FAULT_LOG_HANDLE = None


def _append_crash_log(detail: str, source: str = "main") -> Optional[Path]:
    """Append a timestamped traceback entry to crash.log."""
    target = _CRASH_LOG_PATH
    if target is None:
        target = _configure_crash_logging(None)
    if target is None:
        return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"\n=== [{ts}] Unhandled exception ({source}) ===\n{detail.rstrip()}\n"
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(entry)
        return target
    except Exception:
        return None


def _show_crash_dialog_on_main_thread(summary: str, detail: str) -> None:
    global _SHOWING_CRASH_DIALOG
    if _SMOKE_TEST_MODE or _SHOWING_CRASH_DIALOG:
        return
    if QApplication.instance() is None:
        return
    _SHOWING_CRASH_DIALOG = True
    try:
        dlg = ScrollableDetailDialog(
            title=f"{APP_NAME} - 发生未预期错误",
            summary=summary,
            detail_text=detail,
        )
        dlg.exec()
    except Exception:
        pass
    finally:
        _SHOWING_CRASH_DIALOG = False


def _handle_uncaught_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_tb,
    *,
    source: str = "main",
) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return

    detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    log_path = _append_crash_log(detail, source=source)
    _safe_stderr_write(detail + "\n")

    if _SMOKE_TEST_MODE:
        return

    summary = f"程序遇到异常：{exc_value}"
    if log_path is not None:
        detail_display = f"崩溃日志已写入：{log_path}\n\n{detail}"
    else:
        detail_display = detail

    app = QApplication.instance()
    if app is None:
        return

    try:
        if QThread.currentThread() == app.thread():
            _show_crash_dialog_on_main_thread(summary, detail_display)
        elif _CRASH_BRIDGE is not None:
            _CRASH_BRIDGE.show_requested.emit(summary, detail_display)
    except Exception:
        pass


def _install_exception_hooks() -> None:
    """Install crash-proof global sys & threading exception hooks early."""

    def _sys_hook(exc_type, exc_value, exc_tb) -> None:
        _handle_uncaught_exception(exc_type, exc_value, exc_tb, source="main")

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        thread_name = args.thread.name if args.thread is not None else "unknown-thread"
        _handle_uncaught_exception(
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            source=f"thread:{thread_name}",
        )

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook


def _attach_qt_crash_bridge(app: QApplication) -> None:
    global _CRASH_BRIDGE
    _CRASH_BRIDGE = _CrashDialogBridge(app)


def parse_args(
    argv: Optional[list[str]] = None,
) -> tuple[argparse.Namespace, list[str]]:
    """Parse known application CLI options while forwarding remaining args to Qt."""
    parser = argparse.ArgumentParser(prog="data-dedup-tool", add_help=True)
    parser.add_argument("--db", type=str, default="", help="指定启动时打开的数据库文件路径")
    parser.add_argument("--data-dir", type=str, default="", help="指定用户配置与数据目录")
    parser.add_argument("--theme", choices=["light", "dark"], default="", help="覆盖启动主题")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="执行启动与核心依赖自检（sqlite3 / openpyxl / docx / 图标 / 主窗口）后退出",
    )
    return parser.parse_known_args(argv)


def _acquire_single_instance_lock(data_dir: Path) -> Optional[QLockFile]:
    """Acquire a per-data_dir QLockFile without requiring QtNetwork."""
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / ".app.lock"
    lock = QLockFile(str(lock_path))
    # Rely on PID liveness check so dead crashes recover while long-running instances stay locked
    lock.setStaleLockTime(0)
    if lock.tryLock(100):
        return lock
    return None


def _run_smoke_test(
    app: QApplication,
    data_dir_arg: str = "",
    db_arg: str = "",
    theme_arg: str = "",
) -> int:
    """Verify bundled runtime dependencies, resources, and clean MainWindow lifecycle."""
    temp_ctx: Optional[tempfile.TemporaryDirectory[str]] = None
    window: Optional[MainWindow] = None
    ctx: Optional[AppContext] = None
    try:
        # 1. Verify sqlite3
        conn = sqlite3.connect(":memory:")
        try:
            sqlite_ver = conn.execute("SELECT sqlite_version()").fetchone()[0]
        finally:
            conn.close()

        # 2. Verify openpyxl workbook creation & serialization
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = "smoke-test"
        xlsx_buf = io.BytesIO()
        wb.save(xlsx_buf)
        if xlsx_buf.tell() <= 0:
            raise RuntimeError("openpyxl failed to serialize workbook")

        # 3. Verify python-docx & bundled default.docx template loading
        import docx

        doc = docx.Document()
        doc.add_paragraph("smoke-test")
        docx_buf = io.BytesIO()
        doc.save(docx_buf)
        if docx_buf.tell() <= 0:
            raise RuntimeError("python-docx failed to serialize document")

        # 4. Verify app.ico resource availability
        icon_path = ensure_app_icon(force=False)
        if not icon_path.is_file() or icon_path.stat().st_size <= 0:
            raise RuntimeError(f"Application icon missing or empty: {icon_path}")

        # 5. Boot ConfigManager + AppContext + MainWindow and shut down cleanly
        if data_dir_arg:
            resolved_dir = Path(data_dir_arg).resolve()
        else:
            temp_ctx = tempfile.TemporaryDirectory(prefix="data_dedup_smoke_")
            resolved_dir = Path(temp_ctx.name)

        _configure_crash_logging(resolved_dir)
        cfg_mgr = ConfigManager(data_dir=resolved_dir)
        if db_arg:
            cfg_mgr.add_recent_db(db_arg, set_current=True)
        if theme_arg:
            cfg_mgr.config.theme = theme_arg
        cfg_mgr.save()

        ctx = AppContext(config_manager=cfg_mgr)
        window = MainWindow(ctx)
        app.processEvents()
        window.close()
        window.shutdown()
        app.processEvents()

        _safe_stdout_write(
            f"[SMOKE-TEST] OK (sqlite={sqlite_ver}, openpyxl={openpyxl.__version__}, "
            f"docx={docx.__version__}, icon={icon_path.name})\n"
        )
        return 0
    except Exception as exc:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _append_crash_log(detail, source="smoke-test")
        _safe_stderr_write(f"[SMOKE-TEST] FAILED: {exc}\n{detail}\n")
        return 1
    finally:
        if window is not None:
            try:
                window.shutdown()
            except Exception:
                pass
        elif ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass
        _close_fault_log_handle()
        if temp_ctx is not None:
            try:
                temp_ctx.cleanup()
            except Exception:
                pass


def main(argv: Optional[list[str]] = None) -> int:
    global _SMOKE_TEST_MODE

    # Install crash-proof exception hooks as early as possible
    _install_exception_hooks()

    args, qt_args = parse_args(argv)
    _SMOKE_TEST_MODE = bool(args.smoke_test)

    if args.data_dir:
        _configure_crash_logging(Path(args.data_dir).resolve())

    existing_app = QApplication.instance()
    if existing_app is None:
        # High-DPI policy for crisp Windows scaling (must be set before QApplication)
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

    prog_name = sys.argv[0] if sys.argv else "data-dedup-tool"
    qt_argv = [prog_name, *qt_args]
    app = existing_app if isinstance(existing_app, QApplication) else QApplication(qt_argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("DataDedupTool")
    app.setFont(QFont("Microsoft YaHei UI", 10))

    _attach_qt_crash_bridge(app)

    # Generate app.ico automatically in non-frozen source runs if missing
    icon_path = ensure_app_icon(force=False)
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))

    if args.smoke_test:
        try:
            return _run_smoke_test(
                app,
                data_dir_arg=args.data_dir,
                db_arg=args.db,
                theme_arg=args.theme,
            )
        finally:
            _close_fault_log_handle()

    cfg_mgr = ConfigManager(data_dir=Path(args.data_dir).resolve() if args.data_dir else None)
    _configure_crash_logging(cfg_mgr.data_dir)

    instance_lock = _acquire_single_instance_lock(cfg_mgr.data_dir)
    if instance_lock is None:
        ScrollableDetailDialog(
            title=f"{APP_NAME} - 程序已在运行",
            summary=f"检测到另一个 {APP_NAME} 实例正在使用当前数据目录。",
            detail_text=(
                f"数据目录：{cfg_mgr.data_dir}\n\n"
                "为防止数据库与配置文件并发写入冲突，同一数据目录仅允许运行一个实例。\n"
                "请切换至已打开的窗口，或通过 --data-dir 参数指定独立的数据目录。"
            ),
        ).exec()
        _close_fault_log_handle()
        return 0

    try:
        if args.db:
            cfg_mgr.add_recent_db(args.db, set_current=True)
        if args.theme:
            cfg_mgr.config.theme = args.theme
        cfg_mgr.save()

        ctx = AppContext(config_manager=cfg_mgr)
        window = MainWindow(ctx)
        window.show()

        return app.exec()
    finally:
        try:
            instance_lock.unlock()
        except Exception:
            pass
        _close_fault_log_handle()


if __name__ == "__main__":
    raise SystemExit(main())
