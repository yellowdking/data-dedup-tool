"""Reusable UI widgets and background worker helpers for 数据查重程序."""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class WorkerThread(QThread):
    """Generic background thread executing a callable safely off the UI thread."""

    progress = Signal(int, int, str)  # current, total, message
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        fn: Callable[..., Any],
        *args: Any,
        parent: Optional[QWidget] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def emit_progress(self, current: int, total: int, message: str = "") -> None:
        self.progress.emit(current, total, message)

    def run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
            if not self._cancelled:
                self.succeeded.emit(result)
        except Exception as exc:
            detail = f"{exc}\n\n{traceback.format_exc()}"
            self.failed.emit(detail)


class StatCard(QFrame):
    """Compact metric card displaying a title, prominent numeric value, and accent dot."""

    def __init__(
        self,
        title: str,
        value: str = "0",
        accent_color: str = "#2563EB",
        subtitle: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("statCard", True)
        self.setMinimumWidth(145)
        self.setFixedHeight(78)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(4)

        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        dot = QFrame()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet(f"background-color: {accent_color}; border-radius: 4px; border: none;")
        top_row.addWidget(dot)

        self._title_label = QLabel(title)
        self._title_label.setProperty("statTitle", True)
        top_row.addWidget(self._title_label)
        top_row.addStretch()
        layout.addLayout(top_row)

        self._value_label = QLabel(value)
        self._value_label.setProperty("statValue", True)
        layout.addWidget(self._value_label)

        self._sub_label = QLabel(subtitle)
        self._sub_label.setProperty("role", "muted")
        self._sub_label.setVisible(bool(subtitle))
        layout.addWidget(self._sub_label)

    def set_value(self, value: str, subtitle: Optional[str] = None) -> None:
        self._value_label.setText(value)
        if subtitle is not None:
            self._sub_label.setText(subtitle)
            self._sub_label.setVisible(bool(subtitle))


class DropZoneWidget(QFrame):
    """Modern dashed drag-and-drop area accepting files and/or directories."""

    paths_dropped = Signal(list)  # list[str]

    def __init__(
        self,
        title: str = "拖拽 TXT / 目录树 / CSV 文件或文件夹到此处",
        subtitle: str = "支持多文件同时拖入，自动识别编码与层级目录结构",
        allow_dirs: bool = True,
        allowed_exts: Optional[set[str]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self._allow_dirs = allow_dirs
        self._allowed_exts = {e.lower() for e in allowed_exts} if allowed_exts else None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._icon_label = QLabel("📂")
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon_label.setStyleSheet("font-size: 26px;")
        layout.addWidget(self._icon_label)

        self._title_label = QLabel(title)
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_label.setProperty("role", "section")
        layout.addWidget(self._title_label)

        self._sub_label = QLabel(subtitle)
        self._sub_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub_label.setProperty("role", "subtitle")
        layout.addWidget(self._sub_label)

        self._button_row = QHBoxLayout()
        self._button_row.setSpacing(10)
        self._button_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addLayout(self._button_row)

    def add_action_button(self, button: QPushButton) -> None:
        self._button_row.addWidget(button)

    def _set_drag_active(self, active: bool) -> None:
        self.setProperty("dragActive", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def _extract_valid_paths(self, urls: list[Any]) -> list[str]:
        paths: list[str] = []
        for url in urls:
            if not url.isLocalFile():
                continue
            local_path = url.toLocalFile()
            p = Path(local_path)
            if p.is_dir():
                if self._allow_dirs:
                    paths.append(str(p))
            elif p.is_file():
                if self._allowed_exts is None or p.suffix.lower() in self._allowed_exts:
                    paths.append(str(p))
        return paths

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            valid = self._extract_valid_paths(event.mimeData().urls())
            if valid:
                self._set_drag_active(True)
                event.acceptProposedAction()
                return
        event.ignore()

    def dragLeaveEvent(self, event: Any) -> None:
        self._set_drag_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_drag_active(False)
        if event.mimeData().hasUrls():
            valid = self._extract_valid_paths(event.mimeData().urls())
            if valid:
                event.acceptProposedAction()
                self.paths_dropped.emit(valid)
                return
        event.ignore()


class ToastNotification(QFrame):
    """Non-blocking floating toast banner anchored to top-right of the main window."""

    _LEVEL_STYLES: dict[str, tuple[str, str, str]] = {
        "info": ("#EFF6FF", "#2563EB", "#1E3A8A"),
        "success": ("#ECFDF5", "#059669", "#065F46"),
        "warning": ("#FFFBEB", "#D97706", "#92400E"),
        "error": ("#FEF2F2", "#DC2626", "#991B1B"),
    }

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("ToastBanner")
        self.setWindowFlags(Qt.WindowType.SubWindow)
        self.hide()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(10)

        self._label = QLabel("")
        self._label.setWordWrap(False)
        self._label.setMaximumWidth(520)
        layout.addWidget(self._label)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_message(self, message: str, level: str = "info", duration_ms: int = 3200) -> None:
        bg, border, fg = self._LEVEL_STYLES.get(level, self._LEVEL_STYLES["info"])
        self.setStyleSheet(
            f"QFrame#ToastBanner {{ background-color: {bg}; border: 1px solid {border}; border-radius: 8px; }}"
            f"QFrame#ToastBanner QLabel {{ color: {fg}; font-weight: 600; font-size: 13px; background: transparent; border: none; }}"
        )
        self._label.setText(message)
        self._label.adjustSize()
        self.adjustSize()
        self._reposition()
        self.show()
        self.raise_()
        self._timer.start(duration_ms)

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        x = max(230, (parent.width() - self.width()) // 2)
        y = max(12, parent.height() - self.height() - 40)
        self.move(x, y)


class ScrollableDetailDialog(QDialog):
    """Clean scrollable dialog for long summaries, confirmations, or error details."""

    def __init__(
        self,
        title: str,
        summary: str,
        detail_text: str = "",
        confirm_mode: bool = False,
        confirm_label: str = "确认执行",
        danger: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(620, 420 if detail_text else 200)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        summary_label = QLabel(summary)
        summary_label.setWordWrap(True)
        summary_label.setProperty("role", "section")
        layout.addWidget(summary_label)

        if detail_text:
            text_box = QPlainTextEdit()
            text_box.setReadOnly(True)
            text_box.setPlainText(detail_text)
            layout.addWidget(text_box, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        if confirm_mode:
            cancel_btn = QPushButton("取消")
            cancel_btn.clicked.connect(self.reject)
            ok_btn = QPushButton(confirm_label)
            ok_btn.setProperty("variant", "danger" if danger else "primary")
            ok_btn.clicked.connect(self.accept)
            btn_row.addWidget(cancel_btn)
            btn_row.addWidget(ok_btn)
        else:
            ok_btn = QPushButton("知道了")
            ok_btn.setProperty("variant", "primary")
            ok_btn.clicked.connect(self.accept)
            btn_row.addWidget(ok_btn)

        layout.addLayout(btn_row)


def format_file_size(size_bytes: int) -> str:
    """Format byte size into human-readable string."""
    if size_bytes <= 0:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    val = float(size_bytes)
    for unit in units:
        if val < 1024.0 or unit == units[-1]:
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{size_bytes} B"
