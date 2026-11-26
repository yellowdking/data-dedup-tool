"""Reusable UI widgets and background worker helpers for 数据查重程序."""

from __future__ import annotations

import traceback
import warnings
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
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
    """Generic background thread executing a callable safely off the UI thread.

    Features:
    - Emits ``cancelled`` (and never ``succeeded`` or ``failed``) when cancelled.
    - Connects ``finished -> deleteLater`` by default (``auto_delete=True``).
    - Provides ``safe_stop(timeout_ms=3000)`` for clean view/window shutdown.
    """

    progress = Signal(int, int, str)  # current, total, message
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        fn: Callable[..., Any],
        *args: Any,
        parent: Optional[QWidget] = None,
        auto_delete: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._cancelled = False
        if auto_delete:
            self.finished.connect(self.deleteLater)

    def cancel(self) -> None:
        self._cancelled = True
        try:
            self.requestInterruption()
        except RuntimeError:
            pass

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def emit_progress(self, current: int, total: int, message: str = "") -> None:
        if not self._cancelled:
            self.progress.emit(current, total, message)

    def safe_stop(self, timeout_ms: int = 3000) -> bool:
        """Cancel execution, disconnect result signals, and wait up to timeout_ms."""
        self.cancel()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            for sig in (self.progress, self.succeeded, self.failed, self.cancelled):
                try:
                    sig.disconnect()
                except (RuntimeError, TypeError):
                    pass
        try:
            if not self.isRunning():
                return True
            return bool(self.wait(timeout_ms))
        except RuntimeError:
            return True

    def run(self) -> None:
        try:
            if self._cancelled:
                self.cancelled.emit()
                return
            result = self._fn(*self._args, **self._kwargs)
            if self._cancelled:
                self.cancelled.emit()
            else:
                self.succeeded.emit(result)
        except Exception as exc:
            if self._cancelled:
                self.cancelled.emit()
            else:
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
        self.setMinimumWidth(140)
        self.setMinimumHeight(82)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)

        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        self._dot = QFrame()
        self._dot.setFixedSize(8, 8)
        self.set_accent_color(accent_color)
        top_row.addWidget(self._dot)

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

    def set_accent_color(self, accent_color: str) -> None:
        self._dot.setStyleSheet(
            f"background-color: {accent_color}; border-radius: 4px; border: none;"
        )

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
    """Non-blocking click-through floating toast banner anchored to top-right."""

    _LIGHT_STYLES: dict[str, tuple[str, str, str]] = {
        "info": ("#EFF6FF", "#2563EB", "#1E3A8A"),
        "success": ("#ECFDF5", "#059669", "#065F46"),
        "warning": ("#FFFBEB", "#D97706", "#92400E"),
        "error": ("#FEF2F2", "#DC2626", "#991B1B"),
    }

    _DARK_STYLES: dict[str, tuple[str, str, str]] = {
        "info": ("#172554", "#3B82F6", "#DBEAFE"),
        "success": ("#064E3B", "#10B981", "#D1FAE5"),
        "warning": ("#451A03", "#F59E0B", "#FEF3C7"),
        "error": ("#450A0A", "#EF4444", "#FEE2E2"),
    }

    # Backwards-compatible alias
    _LEVEL_STYLES = _LIGHT_STYLES

    def __init__(self, parent: QWidget, theme: str = "light") -> None:
        super().__init__(parent)
        self.setObjectName("ToastBanner")
        self.setWindowFlags(Qt.WindowType.SubWindow)
        # Never block clicks on underlying action buttons
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._theme = "dark" if theme == "dark" else "light"
        self._last_level = "info"
        self.setMaximumWidth(500)
        self.hide()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(10)

        self._label = QLabel("")
        self._label.setWordWrap(True)
        self._label.setMaximumWidth(460)
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self._label)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def apply_theme(self, theme: str) -> None:
        """Adapt toast colors dynamically for Light or Dark mode."""
        self._theme = "dark" if theme == "dark" else "light"
        self._apply_level_style(self._last_level)

    def _apply_level_style(self, level: str) -> None:
        styles = self._DARK_STYLES if self._theme == "dark" else self._LIGHT_STYLES
        bg, border, fg = styles.get(level, styles["info"])
        self.setStyleSheet(
            f"QFrame#ToastBanner {{ background-color: {bg}; border: 1px solid {border}; border-radius: 8px; }}"
            f"QFrame#ToastBanner QLabel {{ color: {fg}; font-weight: 600; font-size: 13px; background: transparent; border: none; }}"
        )

    def show_message(
        self,
        message: str,
        level: str = "info",
        duration_ms: int = 3200,
        theme: Optional[str] = None,
    ) -> None:
        if theme is not None:
            self._theme = "dark" if theme == "dark" else "light"
        self._last_level = level
        self._apply_level_style(level)

        self._label.setText(message)
        # Fit wrapped text cleanly within maximum width
        parent = self.parentWidget()
        max_w = 460
        if parent is not None and parent.width() > 260:
            max_w = min(460, max(220, parent.width() - 250))
        self._label.setMaximumWidth(max_w)
        self.setMaximumWidth(max_w + 36)
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
        margin_right = 20
        margin_top = 14
        x = max(16, parent.width() - self.width() - margin_right)
        y = margin_top
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
        self._detail_text = detail_text
        self._summary_text = summary
        self.copy_button: Optional[QPushButton] = None
        self.cancel_button: Optional[QPushButton] = None
        self.ok_button: QPushButton

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
        if detail_text:
            copy_btn = QPushButton("复制详情")
            copy_btn.clicked.connect(self._copy_detail)
            btn_row.addWidget(copy_btn)
            self.copy_button = copy_btn

        btn_row.addStretch()

        if confirm_mode:
            cancel_btn = QPushButton("取消")
            cancel_btn.clicked.connect(self.reject)
            ok_btn = QPushButton(confirm_label)
            ok_btn.setProperty("variant", "danger" if danger else "primary")
            ok_btn.clicked.connect(self.accept)
            btn_row.addWidget(cancel_btn)
            btn_row.addWidget(ok_btn)
            self.cancel_button = cancel_btn
            self.ok_button = ok_btn

            if danger:
                ok_btn.setDefault(False)
                ok_btn.setAutoDefault(False)
                cancel_btn.setDefault(True)
                cancel_btn.setAutoDefault(True)
                cancel_btn.setFocus()
            else:
                ok_btn.setDefault(True)
        else:
            ok_btn = QPushButton("知道了")
            ok_btn.setProperty("variant", "primary")
            ok_btn.clicked.connect(self.accept)
            ok_btn.setDefault(True)
            btn_row.addWidget(ok_btn)
            self.ok_button = ok_btn

        layout.addLayout(btn_row)

    def _copy_detail(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            payload = (
                f"{self._summary_text}\n\n{self._detail_text}"
                if self._summary_text
                else self._detail_text
            )
            clipboard.setText(payload)
        if self.copy_button is not None:
            self.copy_button.setText("已复制 ✓")
            QTimer.singleShot(
                1500,
                lambda: self.copy_button.setText("复制详情") if self.copy_button is not None else None,
            )


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
