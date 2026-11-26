"""Reusable UI components, preview tab mixin, and async export helpers for Multi-Table Studio."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QRect, Qt, Signal
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
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
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
from src.ui.table_preview_model import DynamicTableModel
from src.ui.tables_dialogs import (
    _ColumnManagerDialog,
    _ImportOptionsDialog,
    _PerTableKeyDialog,
    _RowDetailDialog,
    _escape_mnemonic,
)

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
    "_PerTableKeyDialog",
    "_RowDetailDialog",
    "_SOURCE_BADGES",
    "_escape_mnemonic",
    "_format_row_summary",
]

# Visual badge definitions: source_type -> (label, bg_hex, fg_hex)
_SOURCE_BADGES: dict[str, tuple[str, str, str]] = {
    "excel": ("XLSX", "#059669", "#FFFFFF"),
    "word": ("DOCX", "#2563EB", "#FFFFFF"),
    "csv": ("CSV", "#0891B2", "#FFFFFF"),
    "txt": ("TXT", "#64748B", "#FFFFFF"),
    "database": ("DB", "#4F46E5", "#FFFFFF"),
    "merged": ("合并", "#7C3AED", "#FFFFFF"),
}

# Light -> Dark accent mapping for compact metric card indicator dots
_DARK_ACCENT_MAP: dict[str, str] = {
    "#2563EB": "#3B82F6",
    "#64748B": "#94A3B8",
    "#059669": "#10B981",
    "#D97706": "#F59E0B",
    "#7C3AED": "#A78BFA",
    "#0891B2": "#22D3EE",
    "#DC2626": "#EF4444",
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


class _BadgeIconCache:
    """Generates crisp rounded pill badge QIcons for table source types."""

    _cache: dict[str, QIcon] = {}

    @classmethod
    def get(cls, source_type: str) -> QIcon:
        key = (source_type or "txt").lower()
        if key in cls._cache:
            return cls._cache[key]

        label, bg_hex, fg_hex = _SOURCE_BADGES.get(key, ("TBL", "#64748B", "#FFFFFF"))
        w, h = 44, 20
        pm = QPixmap(w * 2, h * 2)
        pm.setDevicePixelRatio(2.0)
        pm.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QColor(bg_hex))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(QRect(0, 1, w, h - 2), 5, 5)

        font = QFont("Segoe UI", 8)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(fg_hex))
        painter.drawText(QRect(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, label)
        painter.end()

        icon = QIcon(pm)
        cls._cache[key] = icon
        return icon


class _CompactTableDropZone(QFrame):
    """Compact drag-and-drop & click-to-browse file zone tailored for the Left Panel."""

    files_dropped = Signal(list)
    clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(
            "点击浏览或拖入 Excel (.xlsx/.xlsm)、Word (.docx)、CSV、TXT，支持多 Sheet 与表头行选择"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 10, 8, 10)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("📂 拖入或点击导入表格")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setProperty("role", "section")
        title.setWordWrap(False)
        title.setStyleSheet("font-size: 12px;")
        layout.addWidget(title)

        sub = QLabel("支持 Excel / Word / CSV / TXT")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setProperty("role", "muted")
        sub.setWordWrap(False)
        sub.setStyleSheet("font-size: 11px;")
        layout.addWidget(sub)

    def _set_drag_active(self, active: bool) -> None:
        self.setProperty("dragActive", active)
        self.style().unpolish(self)
        self.style().polish(self)

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
    """QListWidget that also accepts drag-and-dropped table/document files."""

    files_dropped = Signal(list)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

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
    """Actionable empty-state guide canvas that accepts drag-and-drop files."""

    files_dropped = Signal(list)
    add_files_clicked = Signal()
    load_db_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 36, 32, 36)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        icon_lbl = QLabel("📊")
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet("font-size: 36px;")
        layout.addWidget(icon_lbl)

        title = QLabel("拖入表格或文档到此区域，开始多表查重与合并")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setProperty("role", "title")
        layout.addWidget(title)

        sub = QLabel(
            "支持 Excel (.xlsx/.xlsm 多工作表挑选)、Word (.docx 表格与正文)、CSV / TSV、TXT，以及直接载入当前数据总库"
        )
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setWordWrap(True)
        sub.setProperty("role", "subtitle")
        layout.addWidget(sub)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.setAlignment(Qt.AlignmentFlag.AlignCenter)

        btn_add = QPushButton("＋ 添加表格 / 文档")
        btn_add.setProperty("variant", "primary")
        btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_add.clicked.connect(self.add_files_clicked.emit)
        btn_row.addWidget(btn_add)

        btn_db = QPushButton("载入当前数据总库")
        btn_db.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_db.clicked.connect(self.load_db_clicked.emit)
        btn_row.addWidget(btn_db)

        layout.addSpacing(4)
        layout.addLayout(btn_row)

    def _set_drag_active(self, active: bool) -> None:
        self.setProperty("dragActive", active)
        self.style().unpolish(self)
        self.style().polish(self)

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


class _CompactMetricCard(QFrame):
    """Compact metric card with Light/Dark adaptive accent dot and DPI-safe height."""

    def __init__(
        self,
        title: str,
        value: str = "0",
        accent_color: str = "#2563EB",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._light_accent = accent_color
        self._dark_accent = _DARK_ACCENT_MAP.get(accent_color.upper(), accent_color)
        self._theme = "light"

        self.setProperty("statCard", True)
        self.setMinimumWidth(78)
        self.setMinimumHeight(62)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(5)

        self._dot = QFrame()
        self._dot.setFixedSize(7, 7)
        top.addWidget(self._dot)

        self._title_lbl = QLabel(title)
        self._title_lbl.setProperty("statTitle", True)
        self._title_lbl.setStyleSheet("font-size: 11px;")
        top.addWidget(self._title_lbl)
        top.addStretch()
        layout.addLayout(top)

        self._val_lbl = QLabel(value)
        self._val_lbl.setProperty("statValue", True)
        self._val_lbl.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(self._val_lbl)

        self.apply_theme("light")

    def apply_theme(self, theme: str) -> None:
        self._theme = "dark" if theme == "dark" else "light"
        color = self._dark_accent if self._theme == "dark" else self._light_accent
        self._dot.setStyleSheet(f"background-color: {color}; border-radius: 3px; border: none;")

    def set_value(self, value: str) -> None:
        self._val_lbl.setText(value)


class TablesPreviewAndExportMixin:
    """Mixin implementing Sub-Tab 1 (数据预览), shared QTableView helpers, and background exports."""

    def _build_preview_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        top_card = QFrame()
        top_card.setProperty("card", True)
        top_layout = QHBoxLayout(top_card)
        top_layout.setContentsMargins(12, 10, 12, 10)
        top_layout.setSpacing(8)

        info_col = QVBoxLayout()
        info_col.setSpacing(2)
        self._preview_title = QLabel("尚未选择表格")
        self._preview_title.setProperty("role", "section")
        info_col.addWidget(self._preview_title)

        self._preview_meta = QLabel("请在左侧拖入或添加 Excel / Word / CSV / TXT 文件，或载入数据总库")
        self._preview_meta.setProperty("role", "subtitle")
        info_col.addWidget(self._preview_meta)
        top_layout.addLayout(info_col, 2)

        self._btn_prev_cols = QPushButton("列管理")
        self._btn_prev_cols.setProperty("compact", True)
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

        self._preview_search = QLineEdit()
        self._preview_search.setPlaceholderText("🔍 实时搜索当前表内容（支持空格多关键词）...")
        self._preview_search.setClearButtonEnabled(True)
        self._preview_search.setMinimumWidth(180)
        self._preview_search.textChanged.connect(lambda _: self._preview_search_timer.start())
        top_layout.addWidget(self._preview_search, 1)

        layout.addWidget(top_card)

        # Stacked empty-state drop canvas vs virtualized preview table
        self._preview_stack = QStackedWidget()
        self._empty_preview_canvas = _EmptyPreviewDropCanvas(self)
        self._empty_preview_canvas.files_dropped.connect(self.load_files)
        self._empty_preview_canvas.add_files_clicked.connect(self._pick_files)
        self._empty_preview_canvas.load_db_clicked.connect(self.load_database_table)
        self._preview_stack.addWidget(self._empty_preview_canvas)

        self._preview_table = self._make_table_view(self._preview_model)
        self._preview_table.doubleClicked.connect(
            lambda idx: self._inspect_table_view_row(self._preview_table, idx.row())
        )
        self._preview_stack.addWidget(self._preview_table)
        self._preview_stack.setCurrentIndex(0)
        layout.addWidget(self._preview_stack, 1)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(8)
        self._preview_footer = QLabel("显示 0 / 0 行")
        self._preview_footer.setProperty("role", "subtitle")
        bottom_row.addWidget(self._preview_footer)
        bottom_row.addStretch()

        lbl_exp = QLabel("导出当前表：")
        lbl_exp.setProperty("role", "subtitle")
        bottom_row.addWidget(lbl_exp)

        self._btn_prev_xlsx = QPushButton("Excel")
        self._btn_prev_xlsx.setProperty("compact", True)
        self._btn_prev_xlsx.setToolTip("导出当前表为 Excel 工作簿 (.xlsx)")
        self._btn_prev_xlsx.setEnabled(False)
        self._btn_prev_xlsx.clicked.connect(lambda: self._export_single_table(self.active_table(), "xlsx"))
        bottom_row.addWidget(self._btn_prev_xlsx)

        self._btn_prev_docx = QPushButton("Word")
        self._btn_prev_docx.setProperty("compact", True)
        self._btn_prev_docx.setToolTip("导出当前表为 Word 文档 (.docx)")
        self._btn_prev_docx.setEnabled(False)
        self._btn_prev_docx.clicked.connect(lambda: self._export_single_table(self.active_table(), "docx"))
        bottom_row.addWidget(self._btn_prev_docx)

        self._btn_prev_csv = QPushButton("CSV")
        self._btn_prev_csv.setProperty("compact", True)
        self._btn_prev_csv.setEnabled(False)
        self._btn_prev_csv.clicked.connect(lambda: self._export_single_table(self.active_table(), "csv"))
        bottom_row.addWidget(self._btn_prev_csv)

        self._btn_prev_txt = QPushButton("TXT")
        self._btn_prev_txt.setProperty("compact", True)
        self._btn_prev_txt.setEnabled(False)
        self._btn_prev_txt.clicked.connect(lambda: self._export_single_table(self.active_table(), "txt"))
        bottom_row.addWidget(self._btn_prev_txt)

        layout.addLayout(bottom_row)
        return tab

    def _make_table_view(self, model: DynamicTableModel) -> QTableView:
        tv = QTableView()
        tv.setModel(model)
        tv.setAlternatingRowColors(True)
        tv.setSortingEnabled(True)
        tv.setWordWrap(False)
        tv.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tv.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tv.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tv.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tv.customContextMenuRequested.connect(lambda pos, view=tv: self._show_table_context_menu(view, pos))

        vh = tv.verticalHeader()
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vh.setDefaultSectionSize(29)
        vh.setVisible(True)
        vh.setMinimumWidth(38)

        hh = tv.horizontalHeader()
        hh.setStretchLastSection(True)
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)

        # Ctrl+C multi-row copy shortcut scoped to this table view
        copy_sc = QShortcut(QKeySequence.StandardKey.Copy, tv)
        copy_sc.setContext(Qt.ShortcutContext.WidgetShortcut)
        copy_sc.activated.connect(lambda view=tv: self._copy_selected_rows(view, with_headers=False))
        tv._copy_shortcut = copy_sc  # type: ignore[attr-defined]
        return tv

    def _reset_table_view_sort_indicator(self, tv: QTableView) -> None:
        hh = tv.horizontalHeader()
        hh.blockSignals(True)
        hh.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        hh.blockSignals(False)

    def _auto_size_columns(self, tv: QTableView, model: DynamicTableModel, max_w: int = 280) -> None:
        col_count = model.columnCount()
        if col_count == 0:
            return
        row_count = model.rowCount()
        # Sample top + middle + bottom rows for balanced width estimation across 50k rows
        if row_count <= 60:
            sample_indices = list(range(row_count))
        else:
            mid_start = max(20, (row_count // 2) - 10)
            sample_indices = (
                list(range(20))
                + list(range(mid_start, min(row_count, mid_start + 20)))
                + list(range(max(0, row_count - 20), row_count))
            )

        fm = QFontMetrics(tv.font())
        hdr_font = QFont(tv.font())
        hdr_font.setBold(True)
        hdr_fm = QFontMetrics(hdr_font)

        for c in range(col_count):
            hdr_text = str(model.headerData(c, Qt.Orientation.Horizontal) or "")
            max_px = hdr_fm.horizontalAdvance(hdr_text) + 38
            for r in sample_indices:
                val = str(model.data(model.index(r, c), Qt.ItemDataRole.DisplayRole) or "")
                if not val:
                    continue
                snippet = val[:80]
                px = fm.horizontalAdvance(snippet) + 26
                if px > max_px:
                    max_px = px
                    if max_px >= max_w:
                        max_px = max_w
                        break
            tv.setColumnWidth(c, max(84, min(max_w, max_px)))

    def _copy_selected_rows(self, tv: QTableView, with_headers: bool = False) -> None:
        model = tv.model()
        if not isinstance(model, DynamicTableModel):
            return
        sel_model = tv.selectionModel()
        if sel_model is None:
            return
        sel_rows = sorted({i.row() for i in sel_model.selectedRows()})
        if not sel_rows:
            cur = tv.currentIndex()
            if cur.isValid():
                sel_rows = [cur.row()]
        if not sel_rows:
            return
        lines: list[str] = []
        if with_headers:
            lines.append("\t".join(model.headers()))
        for r in sel_rows:
            lines.append("\t".join(model.row_values(r)))
        label = f"已复制 {len(sel_rows)} 行数据" + ("（含表头）" if with_headers else "")
        self._copy_text("\n".join(lines), label)

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
        if not idx.isValid():
            return
        model = tv.model()
        if not isinstance(model, DynamicTableModel):
            return

        menu = QMenu(self)
        cell_val = str(model.data(idx, Qt.ItemDataRole.DisplayRole) or "")
        act_cell = QAction("复制当前单元格", self)
        act_cell.triggered.connect(lambda: self._copy_text(cell_val, "已复制单元格内容"))
        menu.addAction(act_cell)

        sel_rows = sorted({i.row() for i in tv.selectionModel().selectedRows()})
        if not sel_rows:
            sel_rows = [idx.row()]

        act_row = QAction(f"复制选中行 ({len(sel_rows)} 行)  Ctrl+C", self)
        act_row.triggered.connect(lambda: self._copy_selected_rows(tv, with_headers=False))
        menu.addAction(act_row)

        act_row_hdr = QAction(f"复制选中行（含表头，{len(sel_rows)} 行）", self)
        act_row_hdr.triggered.connect(lambda: self._copy_selected_rows(tv, with_headers=True))
        menu.addAction(act_row_hdr)

        menu.addSeparator()
        act_detail = QAction("查看完整行详情...", self)
        act_detail.triggered.connect(lambda: self._inspect_table_view_row(tv, idx.row()))
        menu.addAction(act_detail)

        act_exp_sel = QAction(f"导出选中行 ({len(sel_rows)} 行) 为 CSV...", self)
        act_exp_sel.triggered.connect(lambda: self._export_selected_rows_csv(tv, sel_rows))
        menu.addAction(act_exp_sel)

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
            self._reset_table_view_sort_indicator(self._preview_table)
            self._preview_stack.setCurrentIndex(0)
            self._preview_title.setText("尚未选择表格")
            self._preview_meta.setText("请在左侧拖入或添加 Excel / Word / CSV / TXT 文件，或载入数据总库")
            self._preview_footer.setText("显示 0 / 0 行")
            self._sync_controls_state()
            return

        self._preview_stack.setCurrentIndex(1)
        self._preview_title.setText(self._format_preview_title(tbl))
        meta_parts = [f"{tbl.row_count:,} 行 × {tbl.col_count} 列"]
        if tbl.sheet_name:
            meta_parts.append(f"子表：{tbl.sheet_name}")
        if tbl.source_path:
            meta_parts.append(f"文件：{Path(tbl.source_path).name}")
        uncached = int(tbl.metadata.get("uncached_formula_count", 0) or 0)
        if uncached > 0:
            meta_parts.append(f"⚠ {uncached} 个未缓存公式单元")
        self._preview_meta.setText(" · ".join(meta_parts))
        self._preview_meta.setToolTip(tbl.source_path or tbl.name)

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
            reset_filter=True,
        )
        self._reset_table_view_sort_indicator(self._preview_table)
        self._auto_size_columns(self._preview_table, self._preview_model)
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
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出数据表",
            str(self._default_export_dir() / default_name),
            file_filter,
        )
        if not save_path:
            return
        self._remember_export_dir(save_path)
        snapshot = table.clone(new_id=table.table_id)

        def _task() -> Path:
            if fmt == "xlsx":
                return export_table_excel(snapshot, save_path)
            if fmt == "docx":
                return export_table_word(snapshot, save_path, title=snapshot.name)
            if fmt == "csv":
                return export_table_csv(snapshot, save_path)
            return export_table_txt(snapshot, save_path)

        self._start_export_worker(
            _task,
            success_msg=f"导出成功：{Path(save_path).name}",
            error_title="导出失败",
        )

    def _export_multi_sheet_workbook(self) -> None:
        tables = self.checked_tables() or list(self._tables)
        if not tables:
            self.ctx.notify("工作台暂无可打包导出的表格", "warning")
            return
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "打包导出为多 Sheet Excel",
            str(self._default_export_dir() / "多表打包导出.xlsx"),
            "Excel 工作簿 (*.xlsx)",
        )
        if not save_path:
            return
        self._remember_export_dir(save_path)
        snapshots = [t.clone(new_id=t.table_id) for t in tables]
        self._start_export_worker(
            lambda: export_multiple_tables_workbook(snapshots, save_path),
            success_msg=f"已将 {len(snapshots)} 张表打包导出至 {Path(save_path).name}",
            error_title="打包导出失败",
        )
