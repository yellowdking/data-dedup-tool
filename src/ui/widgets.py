"""Reusable UI widgets and background worker helpers for 数据查重程序."""

from __future__ import annotations

import traceback
import warnings
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from PySide6.QtCore import QPoint, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QDragEnterEvent, QDropEvent, QFontMetrics, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui.delegates import apply_slim_scrollbars
from src.ui.icons import get_icon, render_icon_pixmap
from src.ui.proxy_style import AppProxyStyle
from src.ui.theme import get_palette


def _detect_active_theme(widget: Optional[QWidget] = None, fallback: str = "light") -> str:
    cur = widget
    while cur is not None:
        theme = getattr(getattr(getattr(cur, "ctx", None), "config", None), "theme", None)
        if theme in ("light", "dark"):
            return str(theme)
        cur = cur.parentWidget()
    app = QApplication.instance()
    proxy = getattr(app, "_app_proxy_style", None) if app is not None else None
    if isinstance(proxy, AppProxyStyle):
        return proxy.theme()
    return "dark" if fallback == "dark" else "light"


def _button_icon_color(variant: str, theme: str) -> str:
    p = get_palette(theme)
    v = (variant or "").lower()
    if v == "primary":
        return p["cta_text"]
    if v in ("danger", "success", "accent"):
        return "#FFFFFF"
    if v in ("danger-outline", "danger-ghost"):
        return p["danger"]
    return p["icon_default"]


def _repolish(widget: QWidget) -> None:
    st = widget.style()
    if st is not None:
        st.unpolish(widget)
        st.polish(widget)


def _make_icon_well(size: int = 48) -> tuple[QFrame, QLabel]:
    well = QFrame()
    well.setObjectName("EmptyStateIconWell")
    well.setFixedSize(size, size)
    lay = QVBoxLayout(well)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl = QLabel()
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(lbl)
    return well, lbl


class WorkerThread(QThread):
    """Generic background thread executing a callable safely off the UI thread."""

    progress = Signal(int, int, str)
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
        self.cancel()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            for sig in (self.progress, self.succeeded, self.failed, self.cancelled):
                try:
                    sig.disconnect()
                except (RuntimeError, TypeError):
                    pass
        try:
            return True if not self.isRunning() else bool(self.wait(timeout_ms))
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
                self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")


class PageHeaderBar(QFrame):
    """Sleek 48px flat view header with 1px bottom hairline, title, inline meta, and CTA slot."""

    def __init__(self, title: str, subtitle: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("PageHeaderBar")
        self.setFixedHeight(48)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 0, 20, 0)
        layout.setSpacing(10)

        self._title_label = QLabel(title)
        self._title_label.setProperty("role", "page-title")
        layout.addWidget(self._title_label, 0, Qt.AlignmentFlag.AlignVCenter)

        self._subtitle_label = QLabel(subtitle)
        self._subtitle_label.setProperty("role", "muted")
        self._subtitle_label.setVisible(bool(subtitle))
        layout.addWidget(self._subtitle_label, 0, Qt.AlignmentFlag.AlignVCenter)

        self._meta_layout = QHBoxLayout()
        self._meta_layout.setContentsMargins(0, 0, 0, 0)
        self._meta_layout.setSpacing(8)
        layout.addLayout(self._meta_layout)

        layout.addStretch(1)

        self._actions_layout = QHBoxLayout()
        self._actions_layout.setContentsMargins(0, 0, 0, 0)
        self._actions_layout.setSpacing(8)
        layout.addLayout(self._actions_layout)

    @property
    def title_label(self) -> QLabel:
        return self._title_label

    @property
    def subtitle_label(self) -> QLabel:
        return self._subtitle_label

    @property
    def meta_layout(self) -> QHBoxLayout:
        return self._meta_layout

    @property
    def actions_layout(self) -> QHBoxLayout:
        return self._actions_layout

    def set_title(self, title: str) -> None:
        self._title_label.setText(title)

    def set_subtitle(self, subtitle: str) -> None:
        self._subtitle_label.setText(subtitle)
        self._subtitle_label.setVisible(bool(subtitle))

    def set_metrics(self, items: Sequence[str] | str) -> None:
        text = items.strip() if isinstance(items, str) else "  ·  ".join(str(x).strip() for x in items if str(x).strip())
        self.set_subtitle(text)

    def add_meta_widget(self, widget: QWidget) -> None:
        self._meta_layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)

    def add_left_widget(self, widget: QWidget) -> None:
        self.add_meta_widget(widget)

    def add_action_widget(self, widget: QWidget) -> None:
        self._actions_layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)

    def add_action(self, widget: QWidget) -> None:
        self.add_action_widget(widget)


class WorkspaceSwitcher(QPushButton):
    """Notion/Linear-style sidebar database workspace switcher with rich QMenu."""

    database_selected = Signal(str)
    new_database_requested = Signal()
    open_database_requested = Signal()
    open_folder_requested = Signal()

    def __init__(self, theme: str = "light", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("WorkspaceSwitcher")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(48)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._theme = "dark" if theme == "dark" else "light"
        self._current_path = ""
        self._display_name = "default"
        self._record_count = 0
        self._recent_paths: list[str] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 6, 8, 6)
        layout.setSpacing(8)

        self._icon_label = QLabel()
        self._icon_label.setFixedSize(18, 18)
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._icon_label, 0, Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        self._title_label = QLabel(self._display_name)
        self._title_label.setObjectName("WorkspaceSwitcherTitle")
        self._title_label.setMinimumWidth(0)
        text_col.addWidget(self._title_label)
        self._sub_label = QLabel("0 条记录")
        self._sub_label.setObjectName("WorkspaceSwitcherSub")
        self._sub_label.setMinimumWidth(0)
        text_col.addWidget(self._sub_label)
        layout.addLayout(text_col, 1)

        self._chevron_label = QLabel()
        self._chevron_label.setFixedSize(16, 16)
        self._chevron_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._chevron_label, 0, Qt.AlignmentFlag.AlignVCenter)

        for child in (self._icon_label, self._title_label, self._sub_label, self._chevron_label):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._refresh_icons()
        self.clicked.connect(self._open_menu)

    @property
    def current_path(self) -> str:
        return self._current_path

    @property
    def recent_paths(self) -> list[str]:
        return list(self._recent_paths)

    @property
    def display_name(self) -> str:
        return self._display_name

    @staticmethod
    def _clean_stem(path_str: str) -> str:
        p = Path(path_str)
        if p.suffix.lower() in (".db", ".sqlite", ".sqlite3") and p.stem:
            return p.stem
        return p.stem or p.name or "default"

    @staticmethod
    def _same_path(a: str, b: str) -> bool:
        if not a or not b:
            return False
        try:
            return Path(a).resolve() == Path(b).resolve()
        except OSError:
            return a.strip().lower() == b.strip().lower()

    def _update_elided_title(self) -> None:
        avail_w = max(72, self.width() - 64)
        fm = QFontMetrics(self._title_label.font())
        self._title_label.setText(fm.elidedText(self._display_name, Qt.TextElideMode.ElideRight, avail_w))

    def _refresh_icons(self) -> None:
        p = get_palette(self._theme)
        self._icon_label.setPixmap(render_icon_pixmap("database", 16, p["icon_default"], stroke_width=1.55))
        self._chevron_label.setPixmap(render_icon_pixmap("chevrons-up-down", 14, p["icon_muted"], stroke_width=1.5))

    def apply_theme(self, theme: str) -> None:
        self._theme = "dark" if theme == "dark" else "light"
        self._refresh_icons()

    def set_workspace(
        self,
        current_path: str | Path,
        record_count: int = 0,
        recent_paths: Optional[Sequence[str]] = None,
    ) -> None:
        cur_str = str(Path(current_path).resolve()) if current_path else ""
        self._current_path = cur_str
        self._display_name = self._clean_stem(cur_str) if cur_str else "default"
        self._record_count = max(0, int(record_count))

        recents: list[str] = []
        for item in recent_paths or ():
            s = str(item).strip()
            if s and not any(self._same_path(s, prev) for prev in recents):
                recents.append(s)
        if cur_str and not any(self._same_path(cur_str, prev) for prev in recents):
            recents.insert(0, cur_str)
        self._recent_paths = recents

        self._update_elided_title()
        self._sub_label.setText(f"{self._record_count:,} 条记录")
        self.setToolTip(f"当前数据库：{cur_str}\n点击切换或管理本地数据库" if cur_str else "点击切换或管理本地数据库")

    def set_record_count(self, record_count: int) -> None:
        self._record_count = max(0, int(record_count))
        self._sub_label.setText(f"{self._record_count:,} 条记录")

    def build_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setMinimumWidth(max(self.width(), 232))
        p = get_palette(self._theme)

        stem_counts: dict[str, int] = {}
        for item in self._recent_paths:
            key = self._clean_stem(item).lower()
            stem_counts[key] = stem_counts.get(key, 0) + 1

        if self._recent_paths:
            hdr = QAction("最近数据库", menu)
            hdr.setEnabled(False)
            menu.addAction(hdr)
            for item in self._recent_paths:
                path_obj = Path(item)
                stem = self._clean_stem(item)
                is_current = self._same_path(item, self._current_path)
                is_missing = not is_current and not path_obj.is_file()
                label = f"{stem}  ({path_obj.parent.name})" if stem_counts.get(stem.lower(), 0) > 1 and path_obj.parent.name else stem
                if is_missing:
                    label = f"{label}  [缺失]"
                act = QAction(label, menu)
                act.setCheckable(True)
                act.setChecked(is_current)
                act.setToolTip(item)
                if is_missing:
                    act.setIcon(get_icon("alert-triangle", 14, p["warning"], theme=self._theme))
                act.triggered.connect(lambda _=False, target=item: self.database_selected.emit(target))
                menu.addAction(act)
            menu.addSeparator()

        for icon_name, label, sig in (
            ("plus", "新建数据库...", self.new_database_requested),
            ("folder-open", "打开本地数据库...", self.open_database_requested),
            ("external-link", "打开所在目录", self.open_folder_requested),
        ):
            act = QAction(get_icon(icon_name, 15, theme=self._theme), label, menu)
            act.triggered.connect(sig.emit)
            menu.addAction(act)
        return menu

    def _open_menu(self) -> None:
        self.build_menu().exec(self.mapToGlobal(QPoint(0, self.height() + 4)))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_elided_title()


class EmptyStateWidget(QWidget):
    """Centered empty-state placeholder with circular vector icon well, title, subtitle, and actions."""

    def __init__(
        self,
        icon_name: str = "database",
        title: str = "暂无数据",
        subtitle: str = "",
        *,
        icon_tone: str = "default",
        theme: str = "light",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._icon_tone = icon_tone
        self._theme = "dark" if theme == "dark" else "light"
        self._action_meta: list[tuple[QPushButton, Optional[str], str]] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 32, 32, 32)
        outer.addStretch(1)

        inner = QVBoxLayout()
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(8)
        inner.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._icon_well, self._icon_label = _make_icon_well(48)
        inner.addWidget(self._icon_well, 0, Qt.AlignmentFlag.AlignHCenter)
        inner.addSpacing(4)

        self._title_label = QLabel(title)
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_label.setProperty("role", "page-title")
        self._title_label.setWordWrap(True)
        self._title_label.setMaximumWidth(420)
        inner.addWidget(self._title_label, 0, Qt.AlignmentFlag.AlignHCenter)

        self._subtitle_label = QLabel(subtitle)
        self._subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._subtitle_label.setProperty("role", "subtitle")
        self._subtitle_label.setWordWrap(True)
        self._subtitle_label.setMaximumWidth(420)
        self._subtitle_label.setVisible(bool(subtitle))
        inner.addWidget(self._subtitle_label, 0, Qt.AlignmentFlag.AlignHCenter)

        self._button_container = QWidget()
        self._button_row = QHBoxLayout(self._button_container)
        self._button_row.setContentsMargins(0, 8, 0, 0)
        self._button_row.setSpacing(10)
        self._button_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._button_container.setVisible(False)
        inner.addWidget(self._button_container, 0, Qt.AlignmentFlag.AlignHCenter)

        outer.addLayout(inner)
        outer.addStretch(1)
        self._refresh_icon()

    def _refresh_icon(self) -> None:
        p = get_palette(self._theme)
        t = (self._icon_tone or "default").lower()
        color = p.get(t if t in ("success", "warning", "danger", "accent") else "icon_default", p["icon_default"])
        self._icon_label.setPixmap(render_icon_pixmap(self._icon_name, 22, color, stroke_width=1.65))

    def set_content(
        self,
        title: Optional[str] = None,
        subtitle: Optional[str] = None,
        icon_name: Optional[str] = None,
        icon_tone: Optional[str] = None,
    ) -> None:
        if title is not None:
            self._title_label.setText(title)
        if subtitle is not None:
            self._subtitle_label.setText(subtitle)
            self._subtitle_label.setVisible(bool(subtitle))
        if icon_name is not None:
            self._icon_name = icon_name
        if icon_tone is not None:
            self._icon_tone = icon_tone
        self._refresh_icon()

    def add_action(
        self,
        text_or_button: str | QPushButton,
        callback: Optional[Callable[[], Any]] = None,
        *,
        variant: str = "primary",
        icon_name: Optional[str] = None,
    ) -> QPushButton:
        if isinstance(text_or_button, QPushButton):
            btn = text_or_button
            eff_variant = str(btn.property("variant") or "")
        else:
            btn = QPushButton(text_or_button)
            eff_variant = variant
            if variant:
                btn.setProperty("variant", variant)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)

        if callback is not None:
            btn.clicked.connect(callback)
        if icon_name:
            btn.setIcon(get_icon(icon_name, 15, _button_icon_color(eff_variant, self._theme), theme=self._theme))
            btn.setIconSize(QSize(15, 15))

        self._button_row.addWidget(btn)
        self._button_container.setVisible(True)
        self._action_meta.append((btn, icon_name, eff_variant))
        return btn

    def add_action_button(self, button: QPushButton) -> QPushButton:
        return self.add_action(button)

    def clear_actions(self) -> None:
        while self._button_row.count():
            item = self._button_row.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._action_meta.clear()
        self._button_container.setVisible(False)

    def apply_theme(self, theme: str) -> None:
        self._theme = "dark" if theme == "dark" else "light"
        self._refresh_icon()
        for btn, icon_name, variant in self._action_meta:
            if icon_name:
                btn.setIcon(get_icon(icon_name, 15, _button_icon_color(variant, self._theme), theme=self._theme))


class StatCard(QFrame):
    """Minimal metric card retained for backwards compatibility."""

    def __init__(
        self,
        title: str,
        value: str = "0",
        accent_color: str = "#5E6AD2",
        subtitle: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("statCard", True)
        self.setMinimumSize(136, 76)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)

        top_row = QHBoxLayout()
        top_row.setSpacing(6)
        self._dot = QFrame()
        self._dot.setFixedSize(7, 7)
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
        self._dot.setStyleSheet(f"background-color: {accent_color}; border-radius: 3px; border: none;")

    def set_value(self, value: str, subtitle: Optional[str] = None) -> None:
        self._value_label.setText(value)
        if subtitle is not None:
            self._sub_label.setText(subtitle)
            self._sub_label.setVisible(bool(subtitle))


class DropZoneWidget(QFrame):
    """Zero-emoji vector drag-and-drop canvas collapsing cleanly to a 48px horizontal bar."""

    paths_dropped = Signal(list)

    def __init__(
        self,
        title: str = "拖入 TXT / Word / Excel / CSV 文件或文件夹到此处",
        subtitle: str = "支持多文件同时拖入，自动识别编码与目录结构",
        allow_dirs: bool = True,
        allowed_exts: Optional[set[str]] = None,
        *,
        icon_name: str = "upload",
        theme: str = "light",
        compact: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self._allow_dirs = allow_dirs
        self._allowed_exts = {e.lower() for e in allowed_exts} if allowed_exts else None
        self._icon_name = icon_name
        self._theme = "dark" if theme == "dark" else "light"
        self._compact = False
        self._drag_active = False
        self._expanded_title = title
        self._expanded_subtitle = subtitle
        self._compact_text = ""

        self._root_layout = QBoxLayout(QBoxLayout.Direction.TopToBottom, self)
        self._root_layout.setContentsMargins(24, 22, 24, 20)
        self._root_layout.setSpacing(8)

        self._icon_well, self._icon_label = _make_icon_well(48)
        self._root_layout.addWidget(self._icon_well, 0, Qt.AlignmentFlag.AlignHCenter)

        self._title_label = QLabel(title)
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_label.setProperty("role", "section")
        self._root_layout.addWidget(self._title_label)

        self._sub_label = QLabel(subtitle)
        self._sub_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub_label.setProperty("role", "subtitle")
        self._sub_label.setWordWrap(True)
        self._sub_label.setVisible(bool(subtitle))
        self._root_layout.addWidget(self._sub_label)

        self._button_row = QHBoxLayout()
        self._button_row.setContentsMargins(0, 4, 0, 0)
        self._button_row.setSpacing(8)
        self._button_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._root_layout.addLayout(self._button_row)

        self._refresh_icon()
        if compact:
            self.set_compact(True)

    def _refresh_icon(self) -> None:
        p = get_palette(self._theme)
        color = p["accent"] if self._drag_active else p["icon_default"]
        bg = p["accent_soft"] if self._drag_active else p["bg_surface_alt"]
        bd = p["accent_border"] if self._drag_active else p["border_default"]
        side, glyph, rad = (30, 15, 15) if self._compact else (48, 22, 24)
        self._icon_well.setFixedSize(side, side)
        self._icon_well.setStyleSheet(f"background-color: {bg}; border: 1px solid {bd}; border-radius: {rad}px;")
        self._icon_label.setPixmap(render_icon_pixmap(self._icon_name, glyph, color, stroke_width=1.65))

    def apply_theme(self, theme: str) -> None:
        self._theme = "dark" if theme == "dark" else "light"
        self._refresh_icon()

    def is_compact(self) -> bool:
        return self._compact

    def set_title(self, title: str) -> None:
        self._expanded_title = title
        if not self._compact or not self._compact_text:
            self._title_label.setText(title)

    def set_subtitle(self, subtitle: str) -> None:
        self._expanded_subtitle = subtitle
        self._sub_label.setText(subtitle)
        self._sub_label.setVisible((not self._compact) and bool(subtitle))

    def set_compact_text(self, text: str) -> None:
        self._compact_text = text
        if self._compact:
            self._title_label.setText(text or self._expanded_title)

    def set_compact(self, compact: bool, summary_text: Optional[str] = None) -> None:
        if summary_text is not None:
            self._compact_text = summary_text
        flag = bool(compact)
        self._compact = flag
        self.setProperty("compact", flag)

        if flag:
            self._root_layout.setDirection(QBoxLayout.Direction.LeftToRight)
            self._root_layout.setContentsMargins(12, 6, 12, 6)
            self._root_layout.setSpacing(10)
            self._root_layout.setAlignment(self._icon_well, Qt.AlignmentFlag.AlignVCenter)
            self._root_layout.setStretchFactor(self._title_label, 1)
            self._title_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self._title_label.setText(self._compact_text or self._expanded_title)
            self._sub_label.setVisible(False)
            self._button_row.setContentsMargins(0, 0, 0, 0)
            self._button_row.setSpacing(6)
            self._button_row.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.setMinimumHeight(48)
            self.setMaximumHeight(52)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        else:
            self._root_layout.setDirection(QBoxLayout.Direction.TopToBottom)
            self._root_layout.setContentsMargins(24, 22, 24, 20)
            self._root_layout.setSpacing(8)
            self._root_layout.setAlignment(self._icon_well, Qt.AlignmentFlag.AlignHCenter)
            self._root_layout.setStretchFactor(self._title_label, 0)
            self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._title_label.setText(self._expanded_title)
            self._sub_label.setVisible(bool(self._expanded_subtitle))
            self._button_row.setContentsMargins(0, 4, 0, 0)
            self._button_row.setSpacing(8)
            self._button_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self._refresh_icon()
        _repolish(self)

    def add_action_button(self, button: QPushButton) -> None:
        self._button_row.addWidget(button)

    def _set_drag_active(self, active: bool) -> None:
        self._drag_active = bool(active)
        self.setProperty("dragActive", self._drag_active)
        self._refresh_icon()
        _repolish(self)

    def _extract_valid_paths(self, urls: list[Any]) -> list[str]:
        paths: list[str] = []
        for url in urls:
            if not url.isLocalFile():
                continue
            p = Path(url.toLocalFile())
            if p.is_dir():
                if self._allow_dirs:
                    paths.append(str(p))
            elif p.is_file() and (self._allowed_exts is None or p.suffix.lower() in self._allowed_exts):
                paths.append(str(p))
        return paths

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and self._extract_valid_paths(event.mimeData().urls()):
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
    """Non-blocking click-through floating toast banner anchored to bottom-right."""

    _LEVEL_ICONS = {"info": "info", "success": "check-circle", "warning": "alert-triangle", "error": "x-circle"}
    _LIGHT_STYLES = {
        "info": ("#FFFFFF", "#C7CCF4", "#09090B", "#5E6AD2"),
        "success": ("#FFFFFF", "#BBF7D0", "#09090B", "#15803D"),
        "warning": ("#FFFFFF", "#FDE68A", "#09090B", "#B45309"),
        "error": ("#FFFFFF", "#FECACA", "#09090B", "#B91C1C"),
    }
    _DARK_STYLES = {
        "info": ("#16181D", "#343A68", "#F4F4F5", "#7C86E8"),
        "success": ("#16181D", "#1B432C", "#F4F4F5", "#4ADE80"),
        "warning": ("#16181D", "#473616", "#F4F4F5", "#FBBF24"),
        "error": ("#16181D", "#4E1F29", "#F4F4F5", "#FB7185"),
    }
    _LEVEL_STYLES = _LIGHT_STYLES

    def __init__(self, parent: QWidget, theme: str = "light") -> None:
        super().__init__(parent)
        self.setObjectName("ToastBanner")
        self.setWindowFlags(Qt.WindowType.SubWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._theme = "dark" if theme == "dark" else "light"
        self._last_level = "info"
        self.setMaximumWidth(460)
        self.hide()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 16, 10)
        layout.setSpacing(10)

        self._icon_label = QLabel()
        self._icon_label.setFixedSize(18, 18)
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self._icon_label, 0, Qt.AlignmentFlag.AlignVCenter)

        self._label = QLabel("")
        self._label.setWordWrap(True)
        self._label.setMaximumWidth(400)
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self._label, 1)

        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(18)
        self._shadow.setOffset(0, 4)
        self.setGraphicsEffect(self._shadow)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self._apply_level_style(self._last_level)

    def apply_theme(self, theme: str) -> None:
        self._theme = "dark" if theme == "dark" else "light"
        self._apply_level_style(self._last_level)

    def _apply_level_style(self, level: str) -> None:
        styles = self._DARK_STYLES if self._theme == "dark" else self._LIGHT_STYLES
        lvl = level if level in styles else "info"
        bg, border, fg, icon_col = styles[lvl]
        self.setStyleSheet(
            f"QFrame#ToastBanner {{ background-color: {bg}; border: 1px solid {border}; border-radius: 8px; }}"
            f"QFrame#ToastBanner QLabel {{ color: {fg}; font-weight: 500; font-size: 13px; background: transparent; border: none; }}"
        )
        self._icon_label.setPixmap(render_icon_pixmap(self._LEVEL_ICONS.get(lvl, "info"), 16, icon_col, stroke_width=1.7))
        self._shadow.setColor(QColor(0, 0, 0, 110 if self._theme == "dark" else 28))

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
        parent = self.parentWidget()
        max_w = min(420, max(200, parent.width() - 260)) if parent is not None and parent.width() > 300 else 420
        text_w = self._label.fontMetrics().horizontalAdvance(message) + 6
        target_w = min(max_w, max(120, text_w))
        self._label.setMinimumWidth(target_w)
        self._label.setMaximumWidth(max_w)
        self.setMaximumWidth(max_w + 56)
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
        self.move(max(16, parent.width() - self.width() - 20), max(16, parent.height() - self.height() - 20))


class ScrollableDetailDialog(QDialog):
    """Clean Linear/Obsidian scrollable modal dialog for summaries, confirmations, or error details."""

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
        self.resize(600, 400 if detail_text else 196)
        self._detail_text = detail_text
        self._summary_text = summary
        self._theme = _detect_active_theme(parent)
        self.copy_button: Optional[QPushButton] = None
        self.cancel_button: Optional[QPushButton] = None
        self.ok_button: QPushButton

        p = get_palette(self._theme)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(10)
        icon_lbl = QLabel()
        icon_lbl.setFixedSize(20, 20)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setPixmap(render_icon_pixmap("alert-triangle" if danger else "info", 18, p["danger"] if danger else p["accent"], stroke_width=1.7))
        header_row.addWidget(icon_lbl, 0, Qt.AlignmentFlag.AlignTop)

        summary_label = QLabel(summary)
        summary_label.setWordWrap(True)
        summary_label.setProperty("role", "section")
        header_row.addWidget(summary_label, 1)
        layout.addLayout(header_row)

        if detail_text:
            text_box = QPlainTextEdit()
            text_box.setReadOnly(True)
            text_box.setProperty("mono", True)
            text_box.setPlainText(detail_text)
            apply_slim_scrollbars(text_box, theme=self._theme)
            layout.addWidget(text_box, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        if detail_text:
            copy_btn = QPushButton("复制详情")
            copy_btn.setProperty("variant", "ghost")
            copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            copy_btn.setIcon(get_icon("copy", 14, theme=self._theme))
            copy_btn.setIconSize(QSize(14, 14))
            copy_btn.clicked.connect(self._copy_detail)
            btn_row.addWidget(copy_btn)
            self.copy_button = copy_btn

        btn_row.addStretch()

        if confirm_mode:
            cancel_btn = QPushButton("取消")
            cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            cancel_btn.clicked.connect(self.reject)
            ok_btn = QPushButton(confirm_label)
            ok_btn.setProperty("variant", "danger" if danger else "primary")
            ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
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
            ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            ok_btn.clicked.connect(self.accept)
            ok_btn.setDefault(True)
            btn_row.addWidget(ok_btn)
            self.ok_button = ok_btn

        layout.addLayout(btn_row)

    def _copy_detail(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(f"{self._summary_text}\n\n{self._detail_text}" if self._summary_text else self._detail_text)
        if self.copy_button is not None:
            p = get_palette(self._theme)
            self.copy_button.setText("已复制")
            self.copy_button.setIcon(get_icon("check", 14, p["success"], theme=self._theme))
            QTimer.singleShot(1500, self._reset_copy_button)

    def _reset_copy_button(self) -> None:
        try:
            if self.copy_button is not None:
                self.copy_button.setText("复制详情")
                self.copy_button.setIcon(get_icon("copy", 14, theme=self._theme))
        except RuntimeError:
            pass


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
