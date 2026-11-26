"""View 1: 数据总库 — High-performance searchable, filterable, exportable table."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.core.exporter import (
    ExportError,
    export_codes_txt,
    export_records_csv,
    export_records_excel,
)
from src.core.models import DataRecord
from src.ui.app_context import AppContext
from src.ui.table_models import RecordTableModel
from src.ui.widgets import ScrollableDetailDialog


class LibraryView(QWidget):
    """Main database browsing view with instant search, filters, context menu, and export."""

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._model = RecordTableModel(self)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self.refresh_table)

        self._setup_ui()
        self.ctx.data_changed.connect(self.refresh_all)
        self.refresh_all()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # Toolbar card
        toolbar_card = QFrame()
        toolbar_card.setProperty("card", True)
        tb_layout = QHBoxLayout(toolbar_card)
        tb_layout.setContentsMargins(14, 10, 14, 10)
        tb_layout.setSpacing(10)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("🔍 搜索标准编码、文件名、路径、来源或标签（例如 ABC-123 / 4K）...")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.textChanged.connect(lambda _: self._search_timer.start())
        tb_layout.addWidget(self._search_input, 3)

        self._filter_combo = QComboBox()
        self._filter_combo.addItem("全部记录", "all")
        self._filter_combo.addItem("仅重复项", "duplicates")
        self._filter_combo.addItem("疑似分集", "multipart")
        self._filter_combo.addItem("仅唯一项", "unique")
        self._filter_combo.setMinimumWidth(115)
        self._filter_combo.currentIndexChanged.connect(lambda _: self.refresh_table())
        tb_layout.addWidget(self._filter_combo)

        self._prefix_combo = QComboBox()
        self._prefix_combo.setMinimumWidth(125)
        self._prefix_combo.addItem("全部前缀", "")
        self._prefix_combo.currentIndexChanged.connect(lambda _: self.refresh_table())
        tb_layout.addWidget(self._prefix_combo)

        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh_all)
        tb_layout.addWidget(refresh_btn)

        export_btn = QPushButton("导出数据 ▾")
        export_btn.setProperty("variant", "primary")
        export_menu = QMenu(self)
        export_menu.addAction("导出为 Excel (.xlsx)", lambda: self._export("xlsx"))
        export_menu.addAction("导出为 CSV (.csv)", lambda: self._export("csv"))
        export_menu.addAction("导出编码清单 (.txt)", lambda: self._export("txt"))
        export_btn.setMenu(export_menu)
        tb_layout.addWidget(export_btn)

        root.addWidget(toolbar_card)

        # Virtualized TableView
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)
        self._table.verticalHeader().setDefaultSectionSize(32)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        for col_idx, (_, width) in enumerate(RecordTableModel.COLUMNS):
            self._table.setColumnWidth(col_idx, width)

        self._table.selectionModel().selectionChanged.connect(self._update_footer)
        root.addWidget(self._table, 1)

        # Footer status row
        footer = QHBoxLayout()
        self._footer_label = QLabel("当前显示 0 条记录")
        self._footer_label.setProperty("role", "subtitle")
        footer.addWidget(self._footer_label)
        footer.addStretch()

        hint_label = QLabel("提示：右键表格行可复制编码/文件名、打开所在目录或删除选中记录")
        hint_label.setProperty("role", "muted")
        footer.addWidget(hint_label)
        root.addLayout(footer)

    def refresh_all(self) -> None:
        self._reload_prefixes()
        self.refresh_table()

    def _reload_prefixes(self) -> None:
        current_prefix = self._prefix_combo.currentData() or ""
        prefixes = self.ctx.db.get_distinct_prefixes()
        self._prefix_combo.blockSignals(True)
        self._prefix_combo.clear()
        self._prefix_combo.addItem("全部前缀", "")
        for p in prefixes:
            self._prefix_combo.addItem(p, p)
        idx = self._prefix_combo.findData(current_prefix)
        self._prefix_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._prefix_combo.blockSignals(False)

    def refresh_table(self) -> None:
        search = self._search_input.text().strip()
        filter_mode = self._filter_combo.currentData() or "all"
        prefix = self._prefix_combo.currentData() or ""

        records = self.ctx.db.query_records(
            search=search,
            filter_mode=filter_mode,
            prefix=prefix,
            sort_by="id",
            sort_desc=True,
        )
        code_counts = self.ctx.db.get_code_counts()
        dup_codes = {k for k, v in code_counts.items() if v > 1}
        self._model.set_records(records, duplicate_codes=dup_codes)
        self._update_footer()

    def _update_footer(self) -> None:
        total = self._model.rowCount()
        selected = len(self._selected_records())
        if selected > 0:
            self._footer_label.setText(f"当前显示 {total:,} 条记录 · 已选中 {selected} 条")
        else:
            self._footer_label.setText(f"当前显示 {total:,} 条记录")

    def _selected_records(self) -> list[DataRecord]:
        sel_model = self._table.selectionModel()
        if sel_model is None:
            return []
        records: list[DataRecord] = []
        for idx in sel_model.selectedRows():
            rec = self._model.record_at(idx.row())
            if rec is not None:
                records.append(rec)
        return records

    def _show_context_menu(self, pos) -> None:
        records = self._selected_records()
        if not records:
            return
        menu = QMenu(self)

        act_copy_code = QAction(f"复制标准编码 ({len(records)} 条)", self)
        act_copy_code.triggered.connect(lambda: self._copy_field(records, "code"))
        menu.addAction(act_copy_code)

        act_copy_name = QAction("复制原始文件名", self)
        act_copy_name.triggered.connect(lambda: self._copy_field(records, "name"))
        menu.addAction(act_copy_name)

        if len(records) == 1 and records[0].full_path:
            act_open_dir = QAction("打开所在目录", self)
            act_open_dir.triggered.connect(lambda: self._open_containing_folder(records[0].full_path))
            menu.addAction(act_open_dir)

        menu.addSeparator()

        act_delete = QAction(f"删除选中记录 ({len(records)} 条)", self)
        act_delete.triggered.connect(lambda: self._delete_selected(records))
        menu.addAction(act_delete)

        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _copy_field(self, records: list[DataRecord], field_type: str) -> None:
        if field_type == "code":
            lines = [r.canonical_code for r in records if r.canonical_code]
        else:
            lines = [r.file_name or r.raw_text for r in records]
        QApplication.clipboard().setText("\n".join(lines))
        self.ctx.notify(f"已复制 {len(lines)} 条到剪贴板", "success")

    def _open_containing_folder(self, full_path: str) -> None:
        p = Path(full_path)
        if p.exists():
            if os.name == "nt":
                if p.is_file():
                    subprocess.Popen(["explorer", "/select,", str(p)])
                else:
                    os.startfile(str(p))
            else:
                self.ctx.notify(f"路径：{p.parent}", "info")
        elif p.parent.exists():
            if os.name == "nt":
                os.startfile(str(p.parent))
        else:
            self.ctx.notify("未在本地磁盘找到该文件或目录（可能来源于文本清单导入）", "warning")

    def _delete_selected(self, records: list[DataRecord]) -> None:
        ids = [r.id for r in records if r.id is not None]
        if not ids:
            return
        preview_lines = [
            f"[{r.canonical_code}] {r.file_name or r.raw_text}" for r in records[:100]
        ]
        if len(records) > 100:
            preview_lines.append(f"... 共 {len(records)} 条记录")
        dlg = ScrollableDetailDialog(
            title="确认删除记录",
            summary=f"即将从当前数据库永久删除 {len(ids)} 条记录（删除前将自动创建数据库备份）：",
            detail_text="\n".join(preview_lines),
            confirm_mode=True,
            confirm_label="确认删除",
            danger=True,
            parent=self,
        )
        if dlg.exec() == ScrollableDetailDialog.DialogCode.Accepted:
            deleted = self.ctx.db.delete_records(ids)
            self.ctx.notify(f"已删除 {deleted} 条记录（已自动备份）", "success")
            self.ctx.data_changed.emit()

    def _export(self, fmt: str) -> None:
        records = self._model.all_records()
        if not records:
            self.ctx.notify("当前列表没有可导出的记录", "warning")
            return

        default_dir = self.ctx.config.default_export_dir or str(Path.home() / "Desktop")
        ext_map = {
            "xlsx": ("Excel 工作簿 (*.xlsx)", "数据总库导出.xlsx"),
            "csv": ("CSV 表格 (*.csv)", "数据总库导出.csv"),
            "txt": ("编码清单 (*.txt)", "标准编码清单.txt"),
        }
        file_filter, default_name = ext_map[fmt]
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "选择导出保存位置",
            str(Path(default_dir) / default_name),
            file_filter,
        )
        if not save_path:
            return

        self.ctx.config.default_export_dir = str(Path(save_path).parent)
        self.ctx.config_manager.save()

        try:
            if fmt == "xlsx":
                code_counts = self.ctx.db.get_code_counts()
                stats = self.ctx.db.get_stats()
                export_records_excel(records, save_path, code_counts=code_counts, stats=stats)
            elif fmt == "csv":
                code_counts = self.ctx.db.get_code_counts()
                export_records_csv(records, save_path, code_counts=code_counts)
            else:
                export_codes_txt(records, save_path, unique=True, sort_codes=True)
            self.ctx.notify(f"导出成功：{Path(save_path).name}", "success")
        except ExportError as exc:
            dlg = ScrollableDetailDialog("导出失败", str(exc), parent=self)
            dlg.exec()
        except Exception as exc:
            dlg = ScrollableDetailDialog("导出异常", "导出过程中发生错误：", str(exc), parent=self)
            dlg.exec()
