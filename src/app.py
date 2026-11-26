"""Desktop application entry point for 数据查重程序.

Handles high-DPI initialization, frozen-path resource discovery, on-demand icon
generation in source mode, global exception reporting, and Qt6 lifecycle.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Optional

# Ensure project root is importable when executed as `python src/app.py`.
if not getattr(sys, "frozen", False):
    _ROOT = Path(__file__).resolve().parent.parent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from src.core.config import APP_NAME, ConfigManager, is_frozen
from src.ui.app_context import AppContext
from src.ui.icon_gen import ensure_app_icon
from src.ui.main_window import MainWindow
from src.ui.widgets import ScrollableDetailDialog


def _install_exception_hook(app: QApplication) -> None:
    """Display uncaught exceptions in a clean scrollable dialog instead of silent crash."""

    def _excepthook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        sys.stderr.write(detail + "\n")
        try:
            dlg = ScrollableDetailDialog(
                title=f"{APP_NAME} - 发生未预期错误",
                summary=f"程序遇到异常：{exc_value}",
                detail_text=detail,
            )
            dlg.exec()
        except Exception:
            pass

    sys.excepthook = _excepthook


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="data-dedup-tool", add_help=True)
    parser.add_argument("--db", type=str, default="", help="指定启动时打开的数据库文件路径")
    parser.add_argument("--data-dir", type=str, default="", help="指定用户配置与数据目录")
    parser.add_argument("--theme", choices=["light", "dark"], default="", help="覆盖启动主题")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    # High-DPI policy for crisp Windows scaling
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("DataDedupTool")
    app.setFont(QFont("Microsoft YaHei UI", 10))

    # Generate app.ico automatically in non-frozen source runs if missing
    icon_path = ensure_app_icon(force=False) if not is_frozen() else ensure_app_icon()
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))

    _install_exception_hook(app)

    cfg_mgr = ConfigManager(data_dir=args.data_dir or None)
    if args.db:
        cfg_mgr.add_recent_db(args.db, set_current=True)
    if args.theme:
        cfg_mgr.config.theme = args.theme
    cfg_mgr.save()

    ctx = AppContext(config_manager=cfg_mgr)
    window = MainWindow(ctx)
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
