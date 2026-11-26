"""Reusable UI components, preview tab mixin, and export helpers for Multi-Table Studio."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QAction, QColor, QDragEnterEvent, QDropEvent, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.core.doc_reader import SUPPORTED_TABLE_EXTENSIONS
from src.core.exporter import ExportError
from src.core.table_data import DataTable, TableRowRef
from src.core.table_exporter import (
    export_multiple_tables_workbook,
    export_table_csv,
    export_table_excel,
    export_table_txt,
    export_table_word,
)
from src.ui.table_preview_model import DynamicTableModel
from src.ui.widgets import ScrollableDetailDialog


# Visual badge definitions: source_type -> (label, bg_hex, fg_hex)
_SOURCE_BADGES: dict[str, tuple[str, str, str]] = {
    "excel": ("XLSX", "#059669", "#FFFFFF"),
    "word": ("DOCX", "#2563EB", "#FFFFFF"),
    "csv": ("CSV", "#0891B2", "#FFFFFF"),
    "txt": ("TXT", "#64748B", "#FFFFFF"),
    "database": ("DB", "#4F46E5", "#FFFFFF"),
    "merged": ("合并", "#7C3AED", "#FFFFFF"),
}

_FILE_PICKER_FILTER = (
    "支持的数据文件 (*.xlsx *.xlsm *.docx *.csv *.tsv *.txt *.log *.efu);;"
    "Excel 工作簿 (*.xlsx *.xlsm);;"
    "Word 文档 (*.docx);;"
    "CSV / TSV 表格 (*.csv *.tsv);;"
    "文本文件 (*.txt *.log *.efu);;"
    "所有文件 (*.*)"
)


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
    """Compact drag-and-drop file zone tailored for the Left Panel."""

    files_dropped = Signal(list)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("支持拖入 Excel (.xlsx/.xlsm)、Word (.docx)、CSV、TXT，自动拆分多 Sheet 与 Word 表格")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 10, 8, 10)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("📂 拖入表格或文档")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setProperty("role", "section")
        title.setWordWrap(False)
        title.setStyleSheet("font-size: 12px;")
        layout.addWidget(title)

        sub = QLabel("支持多格式表格与文档导入")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setProperty("role", "muted")
        sub.setWordWrap(False)
        sub.setStyleSheet("font-size: 11px;")
        layout.addWidget(sub)

    def _set_drag_active(self, active: bool) -> None:
        self.setProperty("dragActive", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def _extract_paths(self, urls: list[Any]) -> list[str]:
        valid: list[str] = []
        allowed = set(SUPPORTED_TABLE_EXTENSIONS) | {".xls", ".doc"}
        for url in urls:
            if not url.isLocalFile():
                continue
            p = Path(url.toLocalFile())
            if p.is_file() and p.suffix.lower() in allowed:
                valid.append(str(p))
        return valid

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and self._extract_paths(event.mimeData().urls()):
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
            paths = self._extract_paths(event.mimeData().urls())
            if paths:
                event.acceptProposedAction()
                self.files_dropped.emit(paths)
                return
        event.ignore()


class _CompactMetricCard(QFrame):
    """Compact metric card designed to fit 6 cards cleanly in one horizontal row."""

    def __init__(
        self,
        title: str,
        value: str = "0",
        accent_color: str = "#2563EB",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("statCard", True)
        self.setMinimumWidth(78)
        self.setFixedHeight(62)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(5)

        dot = QFrame()
        dot.setFixedSize(7, 7)
        dot.setStyleSheet(f"background-color: {accent_color}; border-radius: 3px; border: none;")
        top.addWidget(dot)

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

    def set_value(self, value: str) -> None:
        self._val_lbl.setText(value)


class _PerTableKeyDialog(QDialog):
    """Modal dialog allowing per-table single or multi-column composite key selection."""

    def __init__(
        self,
        tables: list[DataTable],
        current_mapping: dict[str, list[str]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("配置各表关键列（支持多列组合键）")
        self.resize(580, 440)
        self._tables = tables
        self._checks_by_table: dict[str, list[QCheckBox]] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        hint = QLabel("为每张参与表勾选 1 个或多个关键列；若勾选多列，将自动按顺序组合为复合键比对。")
        hint.setWordWrap(True)
        hint.setProperty("role", "subtitle")
        root.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        cont_layout = QVBoxLayout(container)
        cont_layout.setContentsMargins(2, 2, 6, 2)
        cont_layout.setSpacing(10)

        for tbl in tables:
            card = QFrame()
            card.setProperty("card", True)
            c_layout = QVBoxLayout(card)
            c_layout.setContentsMargins(12, 10, 12, 10)
            c_layout.setSpacing(6)

            hdr = QLabel(f"{tbl.name}  ({tbl.row_count:,} 行 × {tbl.col_count} 列)")
            hdr.setProperty("role", "section")
            c_layout.addWidget(hdr)

            selected = set(current_mapping.get(tbl.table_id, []))
            if not selected and tbl.headers:
                selected.add(tbl.headers[0])

            chk_list: list[QCheckBox] = []
            row_layout: Optional[QHBoxLayout] = None
            for idx, col_name in enumerate(tbl.headers):
                if idx % 3 == 0:
                    row_layout = QHBoxLayout()
                    row_layout.setSpacing(12)
                    c_layout.addLayout(row_layout)
                chk = QCheckBox(col_name)
                chk.setChecked(col_name in selected)
                chk_list.append(chk)
                assert row_layout is not None
                row_layout.addWidget(chk)
            if row_layout is not None:
                row_layout.addStretch()

            self._checks_by_table[tbl.table_id] = chk_list
            cont_layout.addWidget(card)

        cont_layout.addStretch()
        scroll.setWidget(container)
        root.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("应用关键列配置")
        btn_ok.setProperty("variant", "primary")
        btn_ok.clicked.connect(self.accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_ok)
        root.addLayout(btn_row)

    def get_key_mapping(self) -> dict[str, list[str]]:
        mapping: dict[str, list[str]] = {}
        for tbl in self._tables:
            chosen = [chk.text() for chk in self._checks_by_table.get(tbl.table_id, []) if chk.isChecked()]
            if chosen:
                mapping[tbl.table_id] = chosen
            elif tbl.headers:
                mapping[tbl.table_id] = [tbl.headers[0]]
        return mapping


class TablesPreviewAndExportMixin:
    """Mixin implementing Sub-Tab 1 (数据预览), shared QTableView helpers, and exports."""

    def _build_preview_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        top_card = QFrame()
        top_card.setProperty("card", True)
        top_layout = QHBoxLayout(top_card)
        top_layout.setContentsMargins(12, 10, 12, 10)
        top_layout.setSpacing(10)

        info_col = QVBoxLayout()
        info_col.setSpacing(2)
        self._preview_title = QLabel("尚未选择表格")
        self._preview_title.setProperty("role", "section")
        info_col.addWidget(self._preview_title)

        self._preview_meta = QLabel("请在左侧拖入或添加 Excel / Word / CSV / TXT 文件，或载入数据总库")
        self._preview_meta.setProperty("role", "subtitle")
        info_col.addWidget(self._preview_meta)
        top_layout.addLayout(info_col, 2)

        self._preview_search = QLineEdit()
        self._preview_search.setPlaceholderText("🔍 实时搜索当前表内容（支持空格多关键词）...")
        self._preview_search.setClearButtonEnabled(True)
        self._preview_search.setMinimumWidth(220)
        self._preview_search.textChanged.connect(lambda _: self._preview_search_timer.start())
        top_layout.addWidget(self._preview_search, 1)

        layout.addWidget(top_card)

        self._preview_table = self._make_table_view(self._preview_model)
        layout.addWidget(self._preview_table, 1)

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
        self._btn_prev_xlsx.setToolTip("导出当前表为 Excel 工作簿 (.xlsx)")
        self._btn_prev_xlsx.setStyleSheet("padding: 5px 10px; font-size: 12px;")
        self._btn_prev_xlsx.setEnabled(False)
        self._btn_prev_xlsx.clicked.connect(lambda: self._export_single_table(self.active_table(), "xlsx"))
        bottom_row.addWidget(self._btn_prev_xlsx)

        self._btn_prev_docx = QPushButton("Word")
        self._btn_prev_docx.setToolTip("导出当前表为 Word 文档 (.docx)")
        self._btn_prev_docx.setStyleSheet("padding: 5px 10px; font-size: 12px;")
        self._btn_prev_docx.setEnabled(False)
        self._btn_prev_docx.clicked.connect(lambda: self._export_single_table(self.active_table(), "docx"))
        bottom_row.addWidget(self._btn_prev_docx)

        self._btn_prev_csv = QPushButton("CSV")
        self._btn_prev_csv.setStyleSheet("padding: 5px 10px; font-size: 12px;")
        self._btn_prev_csv.setEnabled(False)
        self._btn_prev_csv.clicked.connect(lambda: self._export_single_table(self.active_table(), "csv"))
        bottom_row.addWidget(self._btn_prev_csv)

        self._btn_prev_txt = QPushButton("TXT")
        self._btn_prev_txt.setStyleSheet("padding: 5px 10px; font-size: 12px;")
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
        tv.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tv.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tv.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tv.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tv.customContextMenuRequested.connect(lambda pos, view=tv: self._show_table_context_menu(view, pos))
        tv.verticalHeader().setDefaultSectionSize(29)
        tv.verticalHeader().setVisible(True)
        tv.verticalHeader().setMinimumWidth(36)
        tv.horizontalHeader().setStretchLastSection(True)
        tv.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        return tv

    def _auto_size_columns(self, tv: QTableView, model: DynamicTableModel, max_w: int = 260) -> None:
        col_count = model.columnCount()
        if col_count == 0:
            return
        sample_rows = min(model.rowCount(), 40)
        for c in range(col_count):
            hdr = str(model.headerData(c, Qt.Orientation.Horizontal) or "")
            max_len = len(hdr)
            for r in range(sample_rows):
                val = str(model.data(model.index(r, c), Qt.ItemDataRole.DisplayRole) or "")
                if len(val) > max_len:
                    max_len = len(val)
            width = max(84, min(max_w, max_len * 10 + 36))
            tv.setColumnWidth(c, width)

    def _show_table_context_menu(self, tv: QTableView, pos) -> None:
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
        act_row = QAction(f"复制选中行 ({len(sel_rows)} 行)", self)
        act_row.triggered.connect(
            lambda: self._copy_text(
                "\n".join("\t".join(model.row_values(r)) for r in sel_rows),
                f"已复制 {len(sel_rows)} 行数据",
            )
        )
        menu.addAction(act_row)
        menu.exec(tv.viewport().mapToGlobal(pos))

    def _copy_text(self, text: str, toast: str) -> None:
        QApplication.clipboard().setText(text)
        self.ctx.notify(toast, "success")

    def _show_preview_for_table(self, table_id: Optional[str]) -> None:
        self._active_table_id = table_id
        tbl = self._tables_by_id.get(table_id) if table_id else None
        if tbl is None:
            self._preview_model.clear()
            self._preview_title.setText("尚未选择表格")
            self._preview_meta.setText("请在左侧拖入或添加 Excel / Word / CSV / TXT 文件，或载入数据总库")
            self._preview_footer.setText("显示 0 / 0 行")
            for btn in (
                self._btn_prev_xlsx,
                self._btn_prev_docx,
                self._btn_prev_csv,
                self._btn_prev_txt,
            ):
                btn.setEnabled(False)
            return

        self._preview_search.blockSignals(True)
        self._preview_search.clear()
        self._preview_search.blockSignals(False)

        badge_lbl = _SOURCE_BADGES.get(tbl.source_type, ("TBL", "", ""))[0]
        self._preview_title.setText(f"[{badge_lbl}]  {tbl.name}")
        meta_parts = [f"{tbl.row_count:,} 行 × {tbl.col_count} 列"]
        if tbl.sheet_name:
            meta_parts.append(f"子表：{tbl.sheet_name}")
        if tbl.source_path:
            meta_parts.append(f"文件：{Path(tbl.source_path).name}")
        self._preview_meta.setText(" · ".join(meta_parts))
        self._preview_meta.setToolTip(tbl.source_path or tbl.name)

        mono_cols: set[int] = set()
        hl_cols: set[int] = set()
        for idx, h in enumerate(tbl.headers):
            if h in ("标准编码", "_标准编码"):
                mono_cols.add(idx)
                hl_cols.add(idx)

        self._preview_model.set_table(tbl, highlight_columns=hl_cols, mono_columns=mono_cols)
        self._auto_size_columns(self._preview_table, self._preview_model)
        self._update_preview_footer()

        for btn in (
            self._btn_prev_xlsx,
            self._btn_prev_docx,
            self._btn_prev_csv,
            self._btn_prev_txt,
        ):
            btn.setEnabled(True)

    def _apply_preview_filter(self) -> None:
        self._preview_model.set_filter(self._preview_search.text())
        self._update_preview_footer()

    def _update_preview_footer(self) -> None:
        shown = self._preview_model.filtered_row_count()
        total = self._preview_model.total_row_count()
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
        try:
            if fmt == "xlsx":
                export_table_excel(table, save_path)
            elif fmt == "docx":
                export_table_word(table, save_path, title=table.name)
            elif fmt == "csv":
                export_table_csv(table, save_path)
            else:
                export_table_txt(table, save_path)
            self.ctx.notify(f"导出成功：{Path(save_path).name}", "success")
        except ExportError as exc:
            ScrollableDetailDialog("导出失败", str(exc), parent=self).exec()
        except Exception as exc:
            ScrollableDetailDialog("导出异常", "导出数据表时遇到错误：", str(exc), parent=self).exec()

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
        try:
            export_multiple_tables_workbook(tables, save_path)
            self.ctx.notify(f"已将 {len(tables)} 张表打包导出至 {Path(save_path).name}", "success")
        except ExportError as exc:
            ScrollableDetailDialog("打包导出失败", str(exc), parent=self).exec()
        except Exception as exc:
            ScrollableDetailDialog("打包导出异常", "打包导出时遇到错误：", str(exc), parent=self).exec()
