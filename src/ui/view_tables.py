"""Multi-Table Studio view (多表工作台) for 数据查重程序 v1.1.0.

Provides unified multi-format table management (Excel/Word/CSV/TXT/Database),
virtualized preview & search, cross-table/intra-table deduplication,
union/dedup/coalesce/join merging, and multi-format report exports.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.doc_reader import (
    load_tables_from_file,
    read_database_as_table,
)
from src.core.table_data import (
    DataTable,
    TableDedupReport,
    TableMergeResult,
)
from src.ui.app_context import AppContext
from src.ui.table_preview_model import DynamicTableModel
from src.ui.tables_dedup_tab import TablesDedupTabMixin
from src.ui.tables_merge_tab import TablesMergeTabMixin
from src.ui.tables_widgets import (
    TablesPreviewAndExportMixin,
    _FILE_PICKER_FILTER,
    _BadgeIconCache,
    _CompactMetricCard,
    _CompactTableDropZone,
    _PerTableKeyDialog,
    _format_row_summary,
)
from src.ui.widgets import ScrollableDetailDialog, WorkerThread

__all__ = [
    "TablesView",
    "_BadgeIconCache",
    "_CompactMetricCard",
    "_CompactTableDropZone",
    "_PerTableKeyDialog",
    "_format_row_summary",
]


class TablesView(
    TablesPreviewAndExportMixin,
    TablesDedupTabMixin,
    TablesMergeTabMixin,
    QWidget,
):
    """Multi-Table Studio view: manage, preview, deduplicate, merge, and export tabular datasets."""

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx

        # Loaded tables registry (ordered)
        self._tables: list[DataTable] = []
        self._tables_by_id: dict[str, DataTable] = {}
        self._active_table_id: Optional[str] = None

        # Per-table custom key mappings for column_value mode
        self._dedup_custom_keys: dict[str, list[str]] = {}
        self._merge_custom_keys: dict[str, list[str]] = {}

        # Last computed results
        self._last_dedup_report: Optional[TableDedupReport] = None
        self._last_merge_result: Optional[TableMergeResult] = None

        # Background worker threads
        self._load_worker: Optional[WorkerThread] = None
        self._dedup_worker: Optional[WorkerThread] = None
        self._merge_worker: Optional[WorkerThread] = None

        theme = self.ctx.config.theme

        # Virtualized models
        self._preview_model = DynamicTableModel(theme=theme, parent=self)
        self._dedup_cross_model = DynamicTableModel(theme=theme, parent=self)
        self._dedup_intra_model = DynamicTableModel(theme=theme, parent=self)
        self._dedup_inter_model = DynamicTableModel(theme=theme, parent=self)
        self._dedup_unique_model = DynamicTableModel(theme=theme, parent=self)
        self._dedup_unrec_model = DynamicTableModel(theme=theme, parent=self)
        self._merge_result_model = DynamicTableModel(theme=theme, parent=self)
        self._merge_dropped_model = DynamicTableModel(theme=theme, parent=self)

        self._all_models: list[DynamicTableModel] = [
            self._preview_model,
            self._dedup_cross_model,
            self._dedup_intra_model,
            self._dedup_inter_model,
            self._dedup_unique_model,
            self._dedup_unrec_model,
            self._merge_result_model,
            self._merge_dropped_model,
        ]

        # Debounce timers for instant table search
        self._preview_search_timer = QTimer(self)
        self._preview_search_timer.setSingleShot(True)
        self._preview_search_timer.setInterval(120)
        self._preview_search_timer.timeout.connect(self._apply_preview_filter)

        self._dedup_search_timer = QTimer(self)
        self._dedup_search_timer.setSingleShot(True)
        self._dedup_search_timer.setInterval(120)
        self._dedup_search_timer.timeout.connect(self._apply_dedup_filter)

        self._merge_search_timer = QTimer(self)
        self._merge_search_timer.setSingleShot(True)
        self._merge_search_timer.setInterval(120)
        self._merge_search_timer.timeout.connect(self._apply_merge_filter)

        self._setup_ui()
        self.ctx.theme_changed.connect(self.apply_theme)
        self._sync_controls_state()

    # ------------------------------------------------------------------
    # Public API (theme sync & programmatic control)
    # ------------------------------------------------------------------

    def apply_theme(self, theme: str) -> None:
        """Propagate Light/Dark theme changes to all virtualized table models."""
        for model in self._all_models:
            model.set_theme(theme)

    def switch_sub_tab(self, index: int) -> None:
        """Programmatically switch right-panel sub-tab (0=Preview, 1=Dedup, 2=Merge)."""
        if 0 <= index < self._work_tabs.count():
            self._work_tabs.setCurrentIndex(index)

    def add_tables(self, tables: list[DataTable], select_first: bool = True) -> None:
        """Register one or more DataTable objects into the workspace list."""
        if not tables:
            return
        first_new_id: Optional[str] = None
        existing_names = {t.name for t in self._tables}

        self._table_list.blockSignals(True)
        for tbl in tables:
            if tbl.table_id in self._tables_by_id:
                tbl = tbl.clone()
            base_name = tbl.name.strip() or "未命名表格"
            candidate = base_name
            suffix = 2
            while candidate in existing_names:
                candidate = f"{base_name} ({suffix})"
                suffix += 1
            tbl.name = candidate
            existing_names.add(candidate)

            self._tables.append(tbl)
            self._tables_by_id[tbl.table_id] = tbl
            if first_new_id is None:
                first_new_id = tbl.table_id

            item = self._create_list_item(tbl)
            self._table_list.addItem(item)
        self._table_list.blockSignals(False)

        self._update_table_count_ui()
        self._refresh_column_and_table_selectors()
        self._sync_controls_state()

        if select_first and first_new_id is not None:
            self._select_table_by_id(first_new_id)

    def load_files(self, paths: list[str]) -> None:
        """Load one or more files in a background WorkerThread."""
        valid_paths = [str(p) for p in paths if Path(p).is_file()]
        if not valid_paths:
            return
        if self._load_worker is not None and self._load_worker.isRunning():
            self.ctx.notify("正在加载表格文件，请稍候...", "warning")
            return

        self._btn_add_files.setEnabled(False)
        self._left_progress.setVisible(True)
        self._left_progress.setRange(0, 0)
        parser_engine = self.ctx.parser

        def _task() -> tuple[list[DataTable], list[str]]:
            loaded: list[DataTable] = []
            errors: list[str] = []
            for p_str in valid_paths:
                try:
                    sub_tables = load_tables_from_file(p_str, parser_engine=parser_engine)
                    if not sub_tables:
                        errors.append(f"{Path(p_str).name}：未检测到有效数据表或非空内容")
                    else:
                        loaded.extend(sub_tables)
                except Exception as exc:
                    errors.append(f"{Path(p_str).name}：{exc}")
            return loaded, errors

        self._load_worker = WorkerThread(_task, parent=self)
        self._load_worker.succeeded.connect(self._on_files_loaded)
        self._load_worker.failed.connect(self._on_load_failed)
        self._load_worker.start()

    def load_database_table(self) -> None:
        """Load current SQLite Data Library into the workspace as a DataTable."""
        try:
            db_stem = Path(self.ctx.db.db_path).stem
            tbl = read_database_as_table(self.ctx.db, name=f"当前数据总库 ({db_stem})")
            if tbl.row_count == 0:
                self.ctx.notify("当前数据总库为空，暂无记录可载入", "warning")
                return
            self.add_tables([tbl], select_first=True)
            self.ctx.notify(f"已载入数据总库：{tbl.row_count:,} 条记录", "success")
        except Exception as exc:
            ScrollableDetailDialog("载入数据总库失败", str(exc), parent=self).exec()

    def checked_tables(self) -> list[DataTable]:
        """Return all currently checked tables in list order."""
        result: list[DataTable] = []
        for idx in range(self._table_list.count()):
            item = self._table_list.item(idx)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                tid = item.data(Qt.ItemDataRole.UserRole)
                tbl = self._tables_by_id.get(tid)
                if tbl is not None:
                    result.append(tbl)
        return result

    def active_table(self) -> Optional[DataTable]:
        if self._active_table_id:
            return self._tables_by_id.get(self._active_table_id)
        return None

    # ------------------------------------------------------------------
    # UI Construction (Left Panel & Right Panel Container)
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(0)

        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.setHandleWidth(6)

        self._main_splitter.addWidget(self._build_left_panel())
        self._main_splitter.addWidget(self._build_right_panel())
        self._main_splitter.setStretchFactor(0, 0)
        self._main_splitter.setStretchFactor(1, 1)
        self._main_splitter.setSizes([252, 800])

        root.addWidget(self._main_splitter, 1)

    def _build_left_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("TablesSourcePanel")
        panel.setProperty("card", True)
        panel.setMinimumWidth(232)
        panel.setMaximumWidth(360)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(7)

        hdr_row = QHBoxLayout()
        hdr_lbl = QLabel("数据源表格管理")
        hdr_lbl.setProperty("role", "section")
        hdr_row.addWidget(hdr_lbl)
        hdr_row.addStretch()
        self._table_count_lbl = QLabel("0 个表")
        self._table_count_lbl.setProperty("role", "muted")
        hdr_row.addWidget(self._table_count_lbl)
        layout.addLayout(hdr_row)

        self._drop_zone = _CompactTableDropZone(self)
        self._drop_zone.files_dropped.connect(self.load_files)
        layout.addWidget(self._drop_zone)

        self._btn_add_files = QPushButton("＋ 添加表格 / 文档")
        self._btn_add_files.setToolTip("支持多选 Excel (.xlsx/.xlsm)、Word (.docx)、CSV、TXT 文件")
        self._btn_add_files.setProperty("variant", "primary")
        self._btn_add_files.setStyleSheet("padding: 6px 8px; font-size: 12px;")
        self._btn_add_files.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_add_files.clicked.connect(self._pick_files)
        layout.addWidget(self._btn_add_files)

        self._btn_load_db = QPushButton("载入当前数据总库")
        self._btn_load_db.setStyleSheet("padding: 5px 8px; font-size: 12px;")
        self._btn_load_db.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_load_db.clicked.connect(self.load_database_table)
        layout.addWidget(self._btn_load_db)

        self._left_progress = QProgressBar()
        self._left_progress.setFixedHeight(10)
        self._left_progress.setTextVisible(False)
        self._left_progress.setVisible(False)
        layout.addWidget(self._left_progress)

        row1 = QHBoxLayout()
        row1.setSpacing(6)
        btn_sel_all = QPushButton("全选")
        btn_sel_all.setStyleSheet("padding: 4px 8px; font-size: 12px;")
        btn_sel_all.clicked.connect(self._select_all_tables)
        btn_invert = QPushButton("反选")
        btn_invert.setStyleSheet("padding: 4px 8px; font-size: 12px;")
        btn_invert.clicked.connect(self._invert_table_checks)
        btn_rename = QPushButton("重命名")
        btn_rename.setStyleSheet("padding: 4px 8px; font-size: 12px;")
        btn_rename.clicked.connect(self._rename_selected_table)
        row1.addWidget(btn_sel_all)
        row1.addWidget(btn_invert)
        row1.addWidget(btn_rename)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        btn_remove = QPushButton("移除选中")
        btn_remove.setStyleSheet("padding: 4px 8px; font-size: 12px;")
        btn_remove.clicked.connect(self._remove_selected_table)
        btn_clear = QPushButton("清空全部")
        btn_clear.setProperty("variant", "ghost")
        btn_clear.setStyleSheet("padding: 4px 8px; font-size: 12px;")
        btn_clear.clicked.connect(self._clear_all_tables)
        row2.addWidget(btn_remove)
        row2.addWidget(btn_clear)
        layout.addLayout(row2)

        self._table_list = QListWidget()
        self._table_list.setObjectName("TablesSourceList")
        self._table_list.setIconSize(QSize(44, 20))
        self._table_list.setSpacing(3)
        self._table_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table_list.customContextMenuRequested.connect(self._show_list_context_menu)
        self._table_list.currentItemChanged.connect(self._on_list_selection_changed)
        self._table_list.itemChanged.connect(self._on_list_item_checked)
        layout.addWidget(self._table_list, 1)

        self._checked_hint_lbl = QLabel("勾选参与查重或合并，点击表名可在右侧预览")
        self._checked_hint_lbl.setProperty("role", "muted")
        self._checked_hint_lbl.setWordWrap(True)
        layout.addWidget(self._checked_hint_lbl)

        self._btn_pack_xlsx = QPushButton("📦 打包多Sheet导出")
        self._btn_pack_xlsx.setToolTip("将已勾选表格打包导出为一个多 Sheet Excel 工作簿 (.xlsx)")
        self._btn_pack_xlsx.setStyleSheet("padding: 5px 8px; font-size: 12px;")
        self._btn_pack_xlsx.setEnabled(False)
        self._btn_pack_xlsx.clicked.connect(self._export_multi_sheet_workbook)
        layout.addWidget(self._btn_pack_xlsx)

        return panel

    def _build_right_panel(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 0, 0, 0)
        layout.setSpacing(0)

        self._work_tabs = QTabWidget()
        self._work_tabs.setObjectName("TablesWorkTabs")
        self._work_tabs.addTab(self._build_preview_tab(), "📊 数据预览")
        self._work_tabs.addTab(self._build_dedup_tab(), "🔍 多表查重")
        self._work_tabs.addTab(self._build_merge_tab(), "🧩 表格合并与导出")

        layout.addWidget(self._work_tabs, 1)
        return container

    # ------------------------------------------------------------------
    # Left Panel Actions & List Management
    # ------------------------------------------------------------------

    def _create_list_item(self, tbl: DataTable) -> QListWidgetItem:
        item = QListWidgetItem()
        item.setFlags(
            item.flags()
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsEnabled
        )
        item.setCheckState(Qt.CheckState.Checked)
        item.setData(Qt.ItemDataRole.UserRole, tbl.table_id)
        item.setIcon(_BadgeIconCache.get(tbl.source_type))
        self._format_list_item(item, tbl)
        return item

    def _format_list_item(self, item: QListWidgetItem, tbl: DataTable) -> None:
        item.setText(f"{tbl.name}\n{tbl.row_count:,} 行 × {tbl.col_count} 列")
        tooltip_parts = [
            f"表名：{tbl.name}",
            f"维度：{tbl.row_count:,} 行 × {tbl.col_count} 列",
            f"类型：{tbl.source_type}",
        ]
        if tbl.source_path:
            tooltip_parts.append(f"来源路径：{tbl.source_path}")
        if tbl.sheet_name:
            tooltip_parts.append(f"工作表/子表：{tbl.sheet_name}")
        item.setToolTip("\n".join(tooltip_parts))

    def _pick_files(self) -> None:
        start_dir = self.ctx.config.default_import_dir or str(Path.home())
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "添加表格或文档文件（支持多选）",
            start_dir,
            _FILE_PICKER_FILTER,
        )
        if paths:
            self.ctx.config.default_import_dir = str(Path(paths[0]).parent)
            self.ctx.config_manager.save()
            self.load_files(paths)

    def _on_files_loaded(self, payload: tuple[list[DataTable], list[str]]) -> None:
        loaded, errors = payload
        self._btn_add_files.setEnabled(True)
        self._left_progress.setVisible(False)

        if loaded:
            self.add_tables(loaded, select_first=True)
            total_rows = sum(t.row_count for t in loaded)
            self.ctx.notify(f"成功载入 {len(loaded)} 张表（共 {total_rows:,} 行）", "success")
        if errors:
            ScrollableDetailDialog(
                "部分文件未能加载",
                f"共有 {len(errors)} 个文件加载失败或为空：",
                "\n".join(errors),
                parent=self,
            ).exec()

    def _on_load_failed(self, detail: str) -> None:
        self._btn_add_files.setEnabled(True)
        self._left_progress.setVisible(False)
        ScrollableDetailDialog("加载文件出错", "后台读取表格文件时发生异常：", detail, parent=self).exec()

    def _select_table_by_id(self, table_id: str) -> None:
        for idx in range(self._table_list.count()):
            item = self._table_list.item(idx)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == table_id:
                self._table_list.setCurrentRow(idx)
                self._show_preview_for_table(table_id)
                return

    def _on_list_selection_changed(
        self,
        current: Optional[QListWidgetItem],
        _previous: Optional[QListWidgetItem],
    ) -> None:
        if current is None:
            self._active_table_id = None
            self._show_preview_for_table(None)
            return
        tid = current.data(Qt.ItemDataRole.UserRole)
        self._show_preview_for_table(tid)

    def _on_list_item_checked(self, _item: QListWidgetItem) -> None:
        self._update_table_count_ui()
        self._refresh_column_and_table_selectors()
        self._sync_controls_state()

    def _select_all_tables(self) -> None:
        self._table_list.blockSignals(True)
        for idx in range(self._table_list.count()):
            item = self._table_list.item(idx)
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked)
        self._table_list.blockSignals(False)
        self._update_table_count_ui()
        self._refresh_column_and_table_selectors()
        self._sync_controls_state()

    def _invert_table_checks(self) -> None:
        self._table_list.blockSignals(True)
        for idx in range(self._table_list.count()):
            item = self._table_list.item(idx)
            if item is not None:
                nxt = (
                    Qt.CheckState.Unchecked
                    if item.checkState() == Qt.CheckState.Checked
                    else Qt.CheckState.Checked
                )
                item.setCheckState(nxt)
        self._table_list.blockSignals(False)
        self._update_table_count_ui()
        self._refresh_column_and_table_selectors()
        self._sync_controls_state()

    def _rename_selected_table(self) -> None:
        item = self._table_list.currentItem()
        if item is None:
            self.ctx.notify("请先在左侧点击选择一张表格", "warning")
            return
        tid = item.data(Qt.ItemDataRole.UserRole)
        tbl = self._tables_by_id.get(tid)
        if tbl is None:
            return
        new_name, ok = QInputDialog.getText(self, "重命名表格", "请输入新的表格名称：", text=tbl.name)
        cleaned = new_name.strip()
        if not ok or not cleaned:
            return
        tbl.rename(cleaned)
        self._table_list.blockSignals(True)
        self._format_list_item(item, tbl)
        self._table_list.blockSignals(False)
        if self._active_table_id == tid:
            self._preview_title.setText(tbl.name)
        self._refresh_column_and_table_selectors()
        self.ctx.notify(f"已重命名为「{cleaned}」", "success")

    def _remove_selected_table(self) -> None:
        row = self._table_list.currentRow()
        if row < 0:
            self.ctx.notify("请先选择要移除的表格", "warning")
            return
        item = self._table_list.takeItem(row)
        if item is None:
            return
        tid = item.data(Qt.ItemDataRole.UserRole)
        self._tables_by_id.pop(tid, None)
        self._tables = [t for t in self._tables if t.table_id != tid]
        self._dedup_custom_keys.pop(tid, None)
        self._merge_custom_keys.pop(tid, None)
        if self._active_table_id == tid:
            self._active_table_id = None
            nxt = self._table_list.currentItem()
            self._show_preview_for_table(nxt.data(Qt.ItemDataRole.UserRole) if nxt else None)
        self._update_table_count_ui()
        self._refresh_column_and_table_selectors()
        self._sync_controls_state()

    def _clear_all_tables(self) -> None:
        if not self._tables:
            return
        self._table_list.clear()
        self._tables.clear()
        self._tables_by_id.clear()
        self._dedup_custom_keys.clear()
        self._merge_custom_keys.clear()
        self._active_table_id = None
        self._last_dedup_report = None
        self._last_merge_result = None

        for model in self._all_models:
            model.clear()

        self._show_preview_for_table(None)
        self._reset_dedup_ui()
        self._reset_merge_ui()
        self._update_table_count_ui()
        self._refresh_column_and_table_selectors()
        self._sync_controls_state()
        self.ctx.notify("已清空工作台全部表格", "info")

    def _show_list_context_menu(self, pos) -> None:
        item = self._table_list.itemAt(pos)
        if item is None:
            return
        self._table_list.setCurrentItem(item)
        tid = item.data(Qt.ItemDataRole.UserRole)
        tbl = self._tables_by_id.get(tid)
        if tbl is None:
            return

        menu = QMenu(self)
        menu.addAction("预览此表", lambda: (self._show_preview_for_table(tid), self.switch_sub_tab(0)))
        menu.addAction("重命名...", self._rename_selected_table)
        menu.addSeparator()
        menu.addAction("导出为 Excel (.xlsx)", lambda: self._export_single_table(tbl, "xlsx"))
        menu.addAction("导出为 Word (.docx)", lambda: self._export_single_table(tbl, "docx"))
        menu.addAction("导出为 CSV", lambda: self._export_single_table(tbl, "csv"))
        menu.addSeparator()
        menu.addAction("移除此表", self._remove_selected_table)
        menu.exec(self._table_list.viewport().mapToGlobal(pos))

    def _update_table_count_ui(self) -> None:
        total = len(self._tables)
        checked = len(self.checked_tables())
        self._table_count_lbl.setText(f"{checked}/{total} 已选")
        self._dedup_scope_lbl.setText(f"已勾选 {checked} 张表参与")
        self._btn_pack_xlsx.setEnabled(total > 0)

    def _refresh_column_and_table_selectors(self) -> None:
        tables = self.checked_tables()

        header_counts: dict[str, int] = {}
        ordered_headers: list[str] = []
        for t in tables:
            seen_in_t: set[str] = set()
            for h in t.headers:
                if h not in seen_in_t:
                    seen_in_t.add(h)
                    header_counts[h] = header_counts.get(h, 0) + 1
                    if h not in ordered_headers:
                        ordered_headers.append(h)

        n_tables = len(tables)
        common_headers = [h for h in ordered_headers if n_tables > 0 and header_counts.get(h, 0) == n_tables]
        other_headers = [h for h in ordered_headers if h not in common_headers]

        for combo in (self._dedup_col_combo, self._merge_col_combo):
            prev_val = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for h in common_headers:
                label = f"{h} (全表共有)" if n_tables > 1 else h
                combo.addItem(label, h)
            for h in other_headers:
                combo.addItem(f"{h} ({header_counts[h]}/{n_tables}表)", h)
            if combo.count() == 0:
                combo.addItem("（无可用列）", "")
            idx = combo.findData(prev_val) if prev_val else -1
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

        prev_primary = self._merge_primary_combo.currentData()
        self._merge_primary_combo.blockSignals(True)
        self._merge_primary_combo.clear()
        for t in tables:
            self._merge_primary_combo.addItem(f"{t.name} ({t.row_count:,}行)", t.table_id)
        if self._merge_primary_combo.count() == 0:
            self._merge_primary_combo.addItem("（请勾选表格）", "")
        p_idx = self._merge_primary_combo.findData(prev_primary) if prev_primary else -1
        self._merge_primary_combo.setCurrentIndex(p_idx if p_idx >= 0 else 0)
        self._merge_primary_combo.blockSignals(False)

        valid_ids = {t.table_id for t in self._tables}
        self._dedup_custom_keys = {k: v for k, v in self._dedup_custom_keys.items() if k in valid_ids}
        self._merge_custom_keys = {k: v for k, v in self._merge_custom_keys.items() if k in valid_ids}
        self._update_custom_key_button_labels()

    def _update_custom_key_button_labels(self) -> None:
        checked_ids = {t.table_id for t in self.checked_tables()}
        d_cnt = sum(1 for tid in checked_ids if self._dedup_custom_keys.get(tid))
        m_cnt = sum(1 for tid in checked_ids if self._merge_custom_keys.get(tid))
        self._btn_dedup_per_table.setText(f"按表已配({d_cnt})" if d_cnt else "按表指定")
        self._btn_merge_per_table.setText(f"按表已配({m_cnt})" if m_cnt else "按表指定")

    def _sync_controls_state(self) -> None:
        has_checked = len(self.checked_tables()) > 0
        self._btn_run_dedup.setEnabled(has_checked)
        self._btn_run_merge.setEnabled(has_checked)
        self._on_dedup_mode_changed()
        self._on_merge_mode_changed()
