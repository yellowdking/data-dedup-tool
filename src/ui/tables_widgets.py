"""Reusable UI components, preview tab mixin, and async export helpers for Multi-Table Studio."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QFontMetrics,
    QIcon,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.core.doc_reader import SUPPORTED_TABLE_EXTENSIONS
from src.core.table_data import DataTable, TableRowRef, generate_table_id
from src.core.table_exporter import (
    export_multiple_tables_workbook,
    export_table_csv,
    export_table_excel,
    export_table_txt,
    export_table_word,
)
from src.ui.delegates import apply_slim_scrollbars, enhance_table_view
from src.ui.icons import get_icon, render_icon_pixmap
from src.ui.table_copy import format_rows_as_tsv
from src.ui.table_header import ColumnSpec, TableHeaderManager
from src.ui.table_preview_model import DynamicTableModel
from src.ui.tables_dialogs import (
    _ColumnManagerDialog,
    _ImportOptionsDialog,
    _PerTableKeyDialog,
    _RowDetailDialog,
    _escape_mnemonic,
)
from src.ui.theme import get_mono_font, get_palette, get_ui_font

__all__ = [
    "TablesPreviewAndExportMixin",
    "_BadgeIconCache",
    "_ColumnManagerDialog",
    "_CompactMetricCard",
    "_CompactTableDropZone",
    "_DropAwareListWidget",
    "_EmptyPreviewDropCanvas",
    "_FILE_PICKER_FILTER",
    "_ImportOptionsDialog",
    "_InlineMetricBar",
    "_PerTableKeyDialog",
    "_RowDetailDialog",
    "_SOURCE_BADGES",
    "_escape_mnemonic",
    "_format_row_summary",
]

# Source type label & semantic tone key (mapped to Option B soft-tinted 1px-bordered pills)
_SOURCE_BADGES: dict[str, tuple[str, str, str]] = {
    "excel": ("XLSX", "success_soft", "success"),
    "word": ("DOCX", "accent_soft", "accent"),
    "csv": ("CSV", "csv", "csv"),
    "txt": ("TXT", "bg_surface_alt", "text_secondary"),
    "database": ("DB", "accent_soft", "accent"),
    "merged": ("合并", "purple_soft", "purple"),
}

_FILE_PICKER_FILTER = (
    "支持的数据文件 (*.xlsx *.xlsm *.docx *.csv *.tsv *.txt *.log *.efu);;"
    "Excel 工作簿 (*.xlsx *.xlsm);;"
    "Word 文档 (*.docx);;"
    "CSV / TSV 表格 (*.csv *.tsv);;"
    "文本文件 (*.txt *.log *.efu);;"
    "所有文件 (*.*)"
)

_ALLOWED_DROP_EXTS: frozenset[str] = frozenset(SUPPORTED_TABLE_EXTENSIONS) | {".xls", ".doc"}


def _extract_dropped_table_paths(urls: list[Any]) -> list[str]:
    """Filter dropped local file URLs to supported table/document formats."""
    valid: list[str] = []
    for url in urls:
        if not url.isLocalFile():
            continue
        p = Path(url.toLocalFile())
        if p.is_file() and p.suffix.lower() in _ALLOWED_DROP_EXTS:
            valid.append(str(p))
    return valid


def _format_row_summary(ref: TableRowRef, max_chars: int = 180) -> str:
    """Build a concise human-readable preview string for a TableRowRef."""
    parts: list[str] = []
    if ref.headers:
        for idx, header in enumerate(ref.headers):
            val = ref.row_data[idx].strip() if idx < len(ref.row_data) else ""
            if val:
                clean_val = val.replace("\r\n", " ").replace("\n", " ")
                parts.append(f"{header}: {clean_val}")
    else:
        for val in ref.row_data:
            s = str(val or "").strip()
            if s:
                parts.append(s.replace("\r\n", " ").replace("\n", " "))
    text = " | ".join(parts)
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text or "-"


def _set_leading_search_icon(line_edit: QLineEdit, theme: str = "light") -> QAction:
    """Attach or refresh a crisp vector leading search icon on a QLineEdit."""
    act = getattr(line_edit, "_leading_search_action", None)
    p = get_palette(theme)
    icon = get_icon("search", 14, p["icon_muted"], theme=theme)
    if isinstance(act, QAction):
        act.setIcon(icon)
        return act
    act = line_edit.addAction(icon, QLineEdit.ActionPosition.LeadingPosition)
    setattr(line_edit, "_leading_search_action", act)
    return act


class _ElidedLabel(QLabel):
    """Single-line label that smoothly right-elides long text without clipping or pushing siblings."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self.setWordWrap(False)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.fontMetrics().height() + 2)

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        return QSize(fm.horizontalAdvance(self.text()) + 4, fm.height() + 2)

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            rect = self.contentsRect()
            fm = QFontMetrics(self.font())
            elided = fm.elidedText(self.text(), Qt.TextElideMode.ElideRight, max(8, rect.width()))
            self.style().drawItemText(
                painter,
                rect,
                int(self.alignment() | Qt.AlignmentFlag.AlignVCenter),
                self.palette(),
                self.isEnabled(),
                elided,
                self.foregroundRole(),
            )
        finally:
            painter.end()


class _BadgeIconCache:
    """Generates soft-tinted 1px-bordered @2x rounded pill QIcons for table source types."""

    _cache: dict[tuple[str, str], QIcon] = {}

    @classmethod
    def _resolve_colors(cls, key: str, theme: str) -> tuple[str, str, str, str]:
        p = get_palette(theme)
        is_dark = theme == "dark"
        if key == "excel":
            return "XLSX", p["success_soft"], p["success_border"], p["success"]
        if key == "word":
            return "DOCX", p["accent_soft"], p["accent_border"], p["accent"]
        if key == "csv":
            if is_dark:
                return "CSV", "#102329", "#1B4652", "#38BDF8"
            return "CSV", "#F0F9FF", "#BAE6FD", "#0369A1"
        if key == "database":
            return "DB", p["accent_soft"], p["accent_border"], p["accent"]
        if key == "merged":
            return "合并", p["purple_soft"], p["purple_border"], p["purple"]
        label = "TXT" if key == "txt" else "TBL"
        return label, p["bg_surface_alt"], p["border_default"], p["text_secondary"]

    @classmethod
    def get(cls, source_type: str, theme: str = "light") -> QIcon:
        key = (source_type or "txt").lower()
        t_norm = "dark" if theme == "dark" else "light"
        cache_key = (key, t_norm)
        if cache_key in cls._cache:
            return cls._cache[cache_key]

        label, bg_hex, border_hex, fg_hex = cls._resolve_colors(key, t_norm)
        w, h = 42, 20
        pm = QPixmap(w * 2, h * 2)
        pm.setDevicePixelRatio(2.0)
        pm.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pm)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setBrush(QColor(bg_hex))
            painter.setPen(QPen(QColor(border_hex), 1.0))
            painter.drawRoundedRect(QRectF(0.5, 1.5, w - 1.0, h - 3.0), 5.0, 5.0)

            font = get_ui_font(7.5, QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.setPen(QColor(fg_hex))
            painter.drawText(QRectF(0.0, 0.0, float(w), float(h)), Qt.AlignmentFlag.AlignCenter, label)
        finally:
            painter.end()

        icon = QIcon(pm)
        cls._cache[cache_key] = icon
        return icon


class _CompactTableDropZone(QFrame):
    """Single-row compact drag-and-drop bar with vector upload icon for the Left Panel."""

    files_dropped = Signal(list)
    clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None, *, theme: str = "light") -> None:
        super().__init__(parent)
        self._theme = "dark" if theme == "dark" else "light"
        self._drag_active = False
        self.setObjectName("DropZone")
        self.setProperty("compact", True)
        self.setAcceptDrops(True)
        self.setFixedHeight(38)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(
            "点击浏览或拖入 Excel (.xlsx/.xlsm)、Word (.docx)、CSV、TXT，支持多 Sheet 与表头行选择"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(7)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._icon_lbl = QLabel()
        self._icon_lbl.setFixedSize(16, 16)
        self._icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._icon_lbl)

        title = QLabel("拖入或点击添加文件")
        title.setProperty("role", "subtitle")
        layout.addWidget(title)

        self._refresh_icon()

    def _refresh_icon(self) -> None:
        p = get_palette(self._theme)
        color = p["accent"] if self._drag_active else p["icon_default"]
        self._icon_lbl.setPixmap(render_icon_pixmap("upload", 15, color, stroke_width=1.6))

    def apply_theme(self, theme: str) -> None:
        self._theme = "dark" if theme == "dark" else "light"
        self._refresh_icon()

    def _set_drag_active(self, active: bool) -> None:
        self._drag_active = bool(active)
        self.setProperty("dragActive", self._drag_active)
        self._refresh_icon()
        st = self.style()
        if st is not None:
            st.unpolish(self)
            st.polish(self)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and _extract_dropped_table_paths(event.mimeData().urls()):
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
            paths = _extract_dropped_table_paths(event.mimeData().urls())
            if paths:
                event.acceptProposedAction()
                self.files_dropped.emit(paths)
                return
        event.ignore()


class _DropAwareListWidget(QListWidget):
    """QListWidget with drag-and-drop support, zero horizontal scrollbar, and slim vertical scrollbar."""

    files_dropped = Signal(list)
    resized = Signal()

    def __init__(self, parent: Optional[QWidget] = None, *, theme: str = "light") -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        apply_slim_scrollbars(self, theme=theme)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.resized.emit()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and _extract_dropped_table_paths(event.mimeData().urls()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: Any) -> None:
        if event.mimeData().hasUrls() and _extract_dropped_table_paths(event.mimeData().urls()):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if event.mimeData().hasUrls():
            paths = _extract_dropped_table_paths(event.mimeData().urls())
            if paths:
                event.acceptProposedAction()
                self.files_dropped.emit(paths)
                return
        super().dropEvent(event)


class _EmptyPreviewDropCanvas(QFrame):
    """Actionable zero-emoji empty-state drop canvas with circular vector icon well."""

    files_dropped = Signal(list)
    add_files_clicked = Signal()
    load_db_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None, *, theme: str = "light") -> None:
        super().__init__(parent)
        self._theme = "dark" if theme == "dark" else "light"
        self._drag_active = False
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 36, 32, 36)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._icon_well = QFrame()
        self._icon_well.setObjectName("EmptyStateIconWell")
        self._icon_well.setFixedSize(48, 48)
        well_lay = QVBoxLayout(self._icon_well)
        well_lay.setContentsMargins(0, 0, 0, 0)
        well_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon_lbl = QLabel()
        self._icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        well_lay.addWidget(self._icon_lbl)
        layout.addWidget(self._icon_well, 0, Qt.AlignmentFlag.AlignHCenter)

        title = QLabel("拖入表格或文档到此区域，开始多表查重与合并")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setProperty("role", "page-title")
        layout.addWidget(title)

        sub = QLabel(
            "支持 Excel (.xlsx/.xlsm 多工作表挑选)、Word (.docx 表格与正文)、CSV / TSV、TXT，以及直接载入当前数据总库"
        )
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setWordWrap(True)
        sub.setMaximumWidth(460)
        sub.setProperty("role", "subtitle")
        layout.addWidget(sub, 0, Qt.AlignmentFlag.AlignHCenter)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._btn_add = QPushButton("添加表格 / 文档")
        self._btn_add.setProperty("variant", "primary")
        self._btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_add.setIconSize(QSize(15, 15))
        self._btn_add.clicked.connect(self.add_files_clicked.emit)
        self._btn_db = QPushButton("载入当前数据总库")
        self._btn_db.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_db.setIconSize(QSize(15, 15))
        self._btn_db.clicked.connect(self.load_db_clicked.emit)
        btn_row.addWidget(self._btn_add)
        btn_row.addWidget(self._btn_db)
        layout.addSpacing(4)
        layout.addLayout(btn_row)
        self.apply_theme(self._theme)

    def apply_theme(self, theme: str) -> None:
        self._theme = "dark" if theme == "dark" else "light"
        p = get_palette(self._theme)
        self._icon_lbl.setPixmap(render_icon_pixmap("table", 22, p["accent"] if self._drag_active else p["icon_default"], stroke_width=1.65))
        self._btn_add.setIcon(get_icon("plus", 15, p["cta_text"], theme=self._theme))
        self._btn_db.setIcon(get_icon("database", 15, p["icon_default"], theme=self._theme))

    def _set_drag_active(self, active: bool) -> None:
        self._drag_active = bool(active)
        self.setProperty("dragActive", self._drag_active)
        self.apply_theme(self._theme)
        if (st := self.style()) is not None:
            st.unpolish(self)
            st.polish(self)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and _extract_dropped_table_paths(event.mimeData().urls()):
            self._set_drag_active(True)
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event: Any) -> None:
        self._set_drag_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_drag_active(False)
        paths = _extract_dropped_table_paths(event.mimeData().urls()) if event.mimeData().hasUrls() else []
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)
            return
        event.ignore()


class _InlineMetricBar(QFrame):
    """Single 32px crisp inline metric bar replacing 6 heavy KPI boxes in Dedup tab."""

    _ITEMS: tuple[tuple[str, str, str], ...] = (
        ("tables", "参与表", "accent"),
        ("rows", "总行数", "text_secondary"),
        ("unique", "唯一键", "success"),
        ("cross", "跨表重复", "warning"),
        ("intra", "表内重复", "purple"),
        ("inter", "全表交集", "accent"),
    )

    def __init__(self, parent: Optional[QWidget] = None, *, theme: str = "light") -> None:
        super().__init__(parent)
        self._theme = "dark" if theme == "dark" else "light"
        self.setProperty("summaryStrip", True)
        self.setFixedHeight(32)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(14)

        mono = get_mono_font(9.0, QFont.Weight.DemiBold)
        self._dots: dict[str, QFrame] = {}
        self._val_labels: dict[str, QLabel] = {}

        for idx, (key, title, _tone) in enumerate(self._ITEMS):
            if idx > 0:
                sep = QLabel("·")
                sep.setProperty("role", "muted")
                layout.addWidget(sep)

            dot = QFrame()
            dot.setFixedSize(6, 6)
            layout.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
            self._dots[key] = dot

            t_lbl = QLabel(title)
            t_lbl.setProperty("role", "subtitle")
            layout.addWidget(t_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

            v_lbl = QLabel("0")
            v_lbl.setFont(mono)
            v_lbl.setProperty("role", "section")
            layout.addWidget(v_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
            self._val_labels[key] = v_lbl

        layout.addStretch(1)
        self.apply_theme(self._theme)

    def apply_theme(self, theme: str) -> None:
        self._theme = "dark" if theme == "dark" else "light"
        p = get_palette(self._theme)
        for key, _title, tone_key in self._ITEMS:
            color = p.get(tone_key, p["accent"])
            self._dots[key].setStyleSheet(f"background-color: {color}; border-radius: 3px; border: none;")

    def set_values(
        self,
        *,
        tables: int = 0,
        rows: int = 0,
        unique: int = 0,
        cross: int = 0,
        intra: int = 0,
        inter: int = 0,
    ) -> None:
        for k, v in (("tables", tables), ("rows", rows), ("unique", unique), ("cross", cross), ("intra", intra), ("inter", inter)):
            self._val_labels[k].setText(f"{v:,}")

    def reset(self) -> None:
        self.set_values()


class _CompactMetricCard(QFrame):
    """Minimal metric card retained for backwards-compatible exports."""

    def __init__(self, title: str, value: str = "0", accent_color: str = "#5E6AD2", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._accent = accent_color
        self.setProperty("statCard", True)
        self.setMinimumSize(78, 58)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(5)
        self._dot = QFrame()
        self._dot.setFixedSize(6, 6)
        self._title_lbl = QLabel(title)
        self._title_lbl.setProperty("statTitle", True)
        top.addWidget(self._dot)
        top.addWidget(self._title_lbl)
        top.addStretch()
        lay.addLayout(top)
        self._val_lbl = QLabel(value)
        self._val_lbl.setProperty("statValue", True)
        lay.addWidget(self._val_lbl)
        self.apply_theme("light")

    def apply_theme(self, theme: str) -> None:
        p = get_palette(theme)
        color = p["accent"] if theme == "dark" and self._accent == "#5E6AD2" else self._accent
        self._dot.setStyleSheet(f"background-color: {color}; border-radius: 3px; border: none;")

    def set_value(self, value: str) -> None:
        self._val_lbl.setText(value)


class TablesPreviewAndExportMixin:
    """Mixin implementing Sub-Tab 1 (数据预览), shared QTableView helpers, and background exports."""

    def _build_preview_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        theme = self.ctx.config.theme

        # Borderless compact toolbar (kills box-in-box border soup)
        top_bar = QFrame()
        top_bar.setProperty("toolbar", True)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(2, 2, 2, 2)
        top_layout.setSpacing(8)

        info_col = QVBoxLayout()
        info_col.setContentsMargins(0, 0, 0, 0)
        info_col.setSpacing(1)
        self._preview_title = _ElidedLabel("尚未选择表格")
        self._preview_title.setProperty("role", "section")
        info_col.addWidget(self._preview_title)

        self._preview_meta = _ElidedLabel("拖入或点击右上角「添加文件」导入表格，或载入数据总库")
        self._preview_meta.setProperty("role", "muted")
        info_col.addWidget(self._preview_meta)
        top_layout.addLayout(info_col, 2)

        self._preview_search = QLineEdit()
        self._preview_search.setPlaceholderText("搜索当前表内容（支持空格多词）...")
        self._preview_search.setClearButtonEnabled(True)
        self._preview_search.setMinimumWidth(175)
        self._preview_search.setMaximumWidth(280)
        _set_leading_search_icon(self._preview_search, theme)
        self._preview_search.textChanged.connect(lambda _: self._preview_search_timer.start())
        top_layout.addWidget(self._preview_search, 1)

        self._btn_prev_cols = QPushButton("列管理")
        self._btn_prev_cols.setProperty("compact", True)
        self._btn_prev_cols.setIconSize(QSize(14, 14))
        self._btn_prev_cols.setToolTip("查看列统计、重命名列名对齐合并字段或删除多余列")
        self._btn_prev_cols.setEnabled(False)
        self._btn_prev_cols.clicked.connect(lambda: self._open_column_manager(self.active_table()))
        top_layout.addWidget(self._btn_prev_cols)

        self._btn_prev_single_dedup = QPushButton("单表去重")
        self._btn_prev_single_dedup.setProperty("compact", True)
        self._btn_prev_single_dedup.setToolTip("快速对当前表执行表内查重并在查重视图查看结果")
        self._btn_prev_single_dedup.setEnabled(False)
        self._btn_prev_single_dedup.clicked.connect(lambda: self._run_single_table_dedup(self.active_table()))
        top_layout.addWidget(self._btn_prev_single_dedup)

        self._btn_prev_col_menu = QPushButton()
        self._btn_prev_col_menu.setProperty("iconOnly", True)
        self._btn_prev_col_menu.setIconSize(QSize(15, 15))
        self._btn_prev_col_menu.setToolTip("自定义显示列、自动调整列宽或恢复默认顺序")
        self._btn_prev_col_menu.setEnabled(False)
        self._btn_prev_col_menu.clicked.connect(
            lambda: self._open_header_menu_below(self._btn_prev_col_menu, self._preview_header_mgr)
        )
        top_layout.addWidget(self._btn_prev_col_menu)

        layout.addWidget(top_bar)

        # Stacked empty-state drop canvas vs virtualized preview table
        self._preview_stack = QStackedWidget()
        self._empty_preview_canvas = _EmptyPreviewDropCanvas(self, theme=theme)
        self._empty_preview_canvas.files_dropped.connect(self.load_files)
        self._empty_preview_canvas.add_files_clicked.connect(self._pick_files)
        self._empty_preview_canvas.load_db_clicked.connect(self.load_database_table)
        self._preview_stack.addWidget(self._empty_preview_canvas)

        self._preview_table, self._preview_header_mgr = self._make_table_view(
            self._preview_model, dynamic=True
        )
        self._preview_table.doubleClicked.connect(
            lambda idx: self._inspect_table_view_row(self._preview_table, idx.row())
        )
        self._preview_stack.addWidget(self._preview_table)
        self._preview_stack.setCurrentIndex(0)
        layout.addWidget(self._preview_stack, 1)

        # Slim bottom row with consolidated Export dropdown (AppProxyStyle paints chevron)
        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(2, 0, 2, 0)
        bottom_row.setSpacing(8)
        self._preview_footer = QLabel("显示 0 / 0 行")
        self._preview_footer.setProperty("role", "muted")
        bottom_row.addWidget(self._preview_footer)
        bottom_row.addStretch()

        hint_lbl = QLabel("双击行查看详情 · Ctrl+C 复制 · 右键表头自定义列")
        hint_lbl.setProperty("role", "muted")
        bottom_row.addWidget(hint_lbl)

        self._btn_prev_export = QPushButton("导出  ")
        self._btn_prev_export.setProperty("compact", True)
        self._btn_prev_export.setIconSize(QSize(14, 14))
        self._btn_prev_export.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_prev_export.setEnabled(False)
        self._btn_prev_export.setToolTip("导出当前预览表为 Excel / Word / CSV / TXT")
        prev_exp_menu = QMenu(self._btn_prev_export)
        for label, fmt in (
            ("Excel 工作簿 (.xlsx)", "xlsx"),
            ("Word 文档 (.docx)", "docx"),
            ("CSV 表格 (.csv)", "csv"),
            ("纯文本 (.txt)", "txt"),
        ):
            prev_exp_menu.addAction(
                label, lambda _=False, f=fmt: self._export_single_table(self.active_table(), f)
            )
        self._btn_prev_export.setMenu(prev_exp_menu)
        bottom_row.addWidget(self._btn_prev_export)

        layout.addLayout(bottom_row)
        return tab

    def _on_inline_code_copied(self, text: str) -> None:
        self.ctx.notify(f"已复制：{text}", "success")

    def _open_header_menu_below(self, btn: QPushButton, mgr: Optional[TableHeaderManager]) -> None:
        if mgr is None:
            return
        pos = btn.mapToGlobal(QPoint(0, btn.height() + 4))
        mgr.open_column_menu(pos)

    def _make_table_view(
        self,
        model: DynamicTableModel,
        *,
        fixed_table_id: Optional[str] = None,
        fixed_columns: Optional[Sequence[ColumnSpec]] = None,
        dynamic: bool = False,
    ) -> tuple[QTableView, Optional[TableHeaderManager]]:
        tv = QTableView()
        tv.setModel(model)
        tv.setSortingEnabled(True)
        tv.setWordWrap(False)
        tv.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tv.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tv.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tv.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tv.customContextMenuRequested.connect(lambda pos, view=tv: self._show_table_context_menu(view, pos))

        enhance_table_view(
            tv,
            theme=self.ctx.config.theme,
            on_code_copied=self._on_inline_code_copied,
            row_height=34,
            header_height=34,
            hide_vertical_header=True,
            slim_scrollbars=True,
        )

        mgr: Optional[TableHeaderManager] = None
        if fixed_table_id and fixed_columns:
            if model.columnCount() == 0:
                model.set_data([spec.label for spec in fixed_columns], [])
            mgr = TableHeaderManager.attach(tv, fixed_table_id, fixed_columns, self.ctx, persist=True)
        elif dynamic:
            mgr = TableHeaderManager.attach_dynamic(tv, self.ctx)

        if mgr is not None and hasattr(self, "_header_managers"):
            self._header_managers.append(mgr)

        copy_sc = QShortcut(QKeySequence.StandardKey.Copy, tv)
        copy_sc.setContext(Qt.ShortcutContext.WidgetShortcut)
        copy_sc.activated.connect(lambda view=tv: self._copy_selected_rows(view, with_headers=False))
        tv._copy_shortcut = copy_sc  # type: ignore[attr-defined]
        return tv, mgr

    def _reset_table_view_sort_indicator(self, tv: QTableView) -> None:
        hh = tv.horizontalHeader()
        hh.blockSignals(True)
        hh.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        hh.blockSignals(False)

    def _clear_fixed_model(self, model: DynamicTableModel, specs: Sequence[ColumnSpec]) -> None:
        """Clear fixed-schema report rows while keeping header sections intact for TableHeaderManager."""
        model.set_data([spec.label for spec in specs], [], reset_filter=True)

    def _copy_selected_rows(self, tv: QTableView, with_headers: bool = False) -> None:
        model = tv.model()
        sel_model = tv.selectionModel()
        if not isinstance(model, DynamicTableModel) or sel_model is None:
            return
        sel_rows = sorted({i.row() for i in sel_model.selectedRows()})
        if not sel_rows and tv.currentIndex().isValid():
            sel_rows = [tv.currentIndex().row()]
        if not sel_rows:
            return
        mgr = getattr(tv, "_table_header_manager", None)
        tsv = format_rows_as_tsv(
            tv,
            sel_rows,
            header_manager=mgr if isinstance(mgr, TableHeaderManager) else None,
            include_headers=with_headers,
        )
        if tsv:
            label = f"已复制 {len(sel_rows)} 行数据" + ("（含表头）" if with_headers else "")
            self._copy_text(tsv, label)

    def _inspect_table_view_row(self, tv: QTableView, view_row: int) -> None:
        model = tv.model()
        if not isinstance(model, DynamicTableModel) or view_row < 0 or view_row >= model.rowCount():
            return
        orig_idx = model.original_row_index(view_row)
        dlg = _RowDetailDialog(
            title=f"行内容完整详情（原行号 #{orig_idx + 1}）",
            headers=model.headers(),
            values=model.row_values(view_row),
            parent=self,
        )
        dlg.exec()

    def _show_table_context_menu(self, tv: QTableView, pos: Any) -> None:
        idx = tv.indexAt(pos)
        model = tv.model()
        if not idx.isValid() or not isinstance(model, DynamicTableModel):
            return
        menu = QMenu(self)
        cell_val = str(model.data(idx, Qt.ItemDataRole.DisplayRole) or "")
        menu.addAction("复制当前单元格", lambda: self._copy_text(cell_val, "已复制单元格内容"))
        sel_rows = sorted({i.row() for i in tv.selectionModel().selectedRows()}) or [idx.row()]
        menu.addAction(f"复制选中行 ({len(sel_rows)} 行)  Ctrl+C", lambda: self._copy_selected_rows(tv, False))
        menu.addAction(f"复制选中行（含表头，{len(sel_rows)} 行）", lambda: self._copy_selected_rows(tv, True))
        menu.addSeparator()
        menu.addAction("查看完整行详情...", lambda: self._inspect_table_view_row(tv, idx.row()))
        menu.addAction(f"导出选中行 ({len(sel_rows)} 行) 为 CSV...", lambda: self._export_selected_rows_csv(tv, sel_rows))
        menu.exec(tv.viewport().mapToGlobal(pos))

    def _export_selected_rows_csv(self, tv: QTableView, sel_rows: list[int]) -> None:
        model = tv.model()
        if not isinstance(model, DynamicTableModel) or not sel_rows:
            return
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出选中行为 CSV",
            str(self._default_export_dir() / "选中行数据.csv"),
            "CSV 表格 (*.csv)",
        )
        if not save_path:
            return
        self._remember_export_dir(save_path)
        subset = DataTable(
            table_id=generate_table_id(),
            name="选中行数据",
            headers=model.headers(),
            rows=[model.row_values(r) for r in sel_rows],
            source_type="merged",
        )
        self._start_export_worker(
            lambda: export_table_csv(subset, save_path),
            success_msg=f"已导出 {len(sel_rows)} 行选中数据至 {Path(save_path).name}",
            error_title="导出选中行失败",
        )

    def _copy_text(self, text: str, toast: str) -> None:
        QApplication.clipboard().setText(text)
        self.ctx.notify(toast, "success")

    def _format_preview_title(self, tbl: DataTable) -> str:
        badge_lbl = _SOURCE_BADGES.get(tbl.source_type, ("TBL", "", ""))[0]
        return f"[{badge_lbl}]  {tbl.name}"

    def _show_preview_for_table(self, table_id: Optional[str]) -> None:
        self._preview_search_timer.stop()
        self._active_table_id = table_id
        tbl = self._tables_by_id.get(table_id) if table_id else None

        self._preview_search.blockSignals(True)
        self._preview_search.clear()
        self._preview_search.blockSignals(False)

        if tbl is None:
            self._preview_model.clear()
            if self._preview_header_mgr is not None:
                self._preview_header_mgr.sync_dynamic_columns(auto_fit=False)
            self._reset_table_view_sort_indicator(self._preview_table)
            self._preview_stack.setCurrentIndex(0)
            self._preview_title.setText("尚未选择表格")
            self._preview_title.setToolTip("")
            self._preview_meta.setText("拖入或点击右上角「添加文件」导入表格，或载入数据总库")
            self._preview_meta.setToolTip("")
            self._preview_footer.setText("显示 0 / 0 行")
            self._sync_controls_state()
            return

        self._preview_stack.setCurrentIndex(1)
        title_text = self._format_preview_title(tbl)
        self._preview_title.setText(title_text)
        self._preview_title.setToolTip(title_text)
        meta_parts = [f"{tbl.row_count:,} 行 × {tbl.col_count} 列"]
        if tbl.sheet_name:
            meta_parts.append(f"子表：{tbl.sheet_name}")
        if tbl.source_path:
            meta_parts.append(f"文件：{Path(tbl.source_path).name}")
        uncached = int(tbl.metadata.get("uncached_formula_count", 0) or 0)
        if uncached > 0:
            meta_parts.append(f"含 {uncached} 个未缓存公式单元")
        meta_text = " · ".join(meta_parts)
        self._preview_meta.setText(meta_text)
        self._preview_meta.setToolTip(f"{meta_text}\n{tbl.source_path}" if tbl.source_path else meta_text)

        mono_cols: set[int] = set()
        hl_cols: set[int] = set()
        for idx, h in enumerate(tbl.headers):
            if h in ("标准编码", "_标准编码"):
                mono_cols.add(idx)
                hl_cols.add(idx)

        self._preview_model.set_table(
            tbl,
            highlight_columns=hl_cols,
            mono_columns=mono_cols,
            copy_columns=mono_cols,
            reset_filter=True,
        )
        if self._preview_header_mgr is not None:
            self._preview_header_mgr.sync_dynamic_columns(auto_fit=True)
        self._reset_table_view_sort_indicator(self._preview_table)
        self._update_preview_footer()
        self._sync_controls_state()

    def _apply_preview_filter(self) -> None:
        self._preview_model.set_filter(self._preview_search.text())
        self._update_preview_footer()

    def _update_preview_footer(self) -> None:
        shown = self._preview_model.filtered_row_count()
        total = self._preview_model.total_row_count()
        if self._preview_model.filter_query():
            self._preview_footer.setText(f"筛选命中 {shown:,} / {total:,} 行")
        else:
            self._preview_footer.setText(f"显示 {shown:,} / {total:,} 行")

    def _default_export_dir(self) -> Path:
        raw = self.ctx.config.default_export_dir
        return Path(raw) if raw else Path.home()

    def _remember_export_dir(self, file_path: str) -> None:
        self.ctx.config.default_export_dir = str(Path(file_path).parent)
        self.ctx.config_manager.save()

    def _export_single_table(self, table: Optional[DataTable], fmt: str) -> None:
        if table is None:
            return
        safe_stem = "".join(c if c not in r'\/:*?"<>|' else "_" for c in (table.name or "导出数据表"))
        ext_map = {
            "xlsx": ("Excel 工作簿 (*.xlsx)", f"{safe_stem}.xlsx"),
            "docx": ("Word 文档 (*.docx)", f"{safe_stem}.docx"),
            "csv": ("CSV 表格 (*.csv)", f"{safe_stem}.csv"),
            "txt": ("文本文件 (*.txt)", f"{safe_stem}.txt"),
        }
        file_filter, default_name = ext_map[fmt]
        save_path, _ = QFileDialog.getSaveFileName(self, "导出数据表", str(self._default_export_dir() / default_name), file_filter)
        if not save_path:
            return
        self._remember_export_dir(save_path)
        snap = table.clone(new_id=table.table_id)
        exporters = {
            "xlsx": lambda: export_table_excel(snap, save_path),
            "docx": lambda: export_table_word(snap, save_path, title=snap.name),
            "csv": lambda: export_table_csv(snap, save_path),
            "txt": lambda: export_table_txt(snap, save_path),
        }
        self._start_export_worker(exporters[fmt], success_msg=f"导出成功：{Path(save_path).name}", error_title="导出失败")

    def _export_multi_sheet_workbook(self) -> None:
        tables = self.checked_tables() or list(self._tables)
        if not tables:
            self.ctx.notify("工作台暂无可打包导出的表格", "warning")
            return
        save_path, _ = QFileDialog.getSaveFileName(
            self, "打包导出为多 Sheet Excel", str(self._default_export_dir() / "多表打包导出.xlsx"), "Excel 工作簿 (*.xlsx)"
        )
        if not save_path:
            return
        self._remember_export_dir(save_path)
        snaps = [t.clone(new_id=t.table_id) for t in tables]
        self._start_export_worker(
            lambda: export_multiple_tables_workbook(snaps, save_path),
            success_msg=f"已将 {len(snaps)} 张表打包导出至 {Path(save_path).name}",
            error_title="打包导出失败",
        )
