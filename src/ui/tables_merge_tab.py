"""Table Merge & Export sub-tab mixin for TablesView (表格合并与导出)."""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QModelIndex, QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.table_data import (
    DataTable,
    TableMergeConfig,
    TableMergeResult,
    TableRowRef,
)
from src.core.table_engine import (
    import_table_to_database,
    merge_tables,
)
from src.ui.table_header import ColumnSpec
from src.ui.tables_widgets import (
    _PerTableKeyDialog,
    _format_row_summary,
    _set_leading_search_icon,
)
from src.ui.widgets import EmptyStateWidget, ScrollableDetailDialog

_MERGE_DROPPED_SPECS: tuple[ColumnSpec, ...] = (
    ColumnSpec("key", "去重键 / 编码", 148, True, 90, True, True, 0.0, "basic"),
    ColumnSpec("source_table", "来源表", 148, True, 84, True, False, 0.0, "basic"),
    ColumnSpec("row_index", "原行号", 70, True, 56, True, False, 0.0, "basic"),
    ColumnSpec("parsed_code", "提取编码", 136, True, 80, True, False, 0.0, "basic"),
    ColumnSpec("tags", "版本标签", 110, False, 70, True, False, 0.0, "advanced"),
    ColumnSpec("part", "分集", 70, False, 54, True, False, 0.0, "advanced"),
    ColumnSpec("summary", "原行内容摘要", 280, True, 120, True, False, 1.0, "basic"),
)


class TablesMergeTabMixin:
    """Mixin implementing Sub-Tab 3 (表格合并与导出) for TablesView."""

    def _build_merge_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        theme = self.ctx.config.theme

        # Single clean merge configuration card
        cfg_card = QFrame()
        cfg_card.setProperty("card", True)
        cfg_layout = QVBoxLayout(cfg_card)
        cfg_layout.setContentsMargins(12, 9, 12, 9)
        cfg_layout.setSpacing(7)

        # Row 1: Merge mode + alignment / join controls + cancel + primary CTA
        r1 = QHBoxLayout()
        r1.setSpacing(7)

        lbl_mode = QLabel("模式：")
        lbl_mode.setProperty("role", "section")
        r1.addWidget(lbl_mode)

        self._merge_mode_combo = QComboBox()
        self._merge_mode_combo.addItem("纵向合并去重", "union_dedup")
        self._merge_mode_combo.addItem("纵向追加合并", "union_all")
        self._merge_mode_combo.addItem("横向拼接", "join")
        self._merge_mode_combo.setMinimumWidth(112)
        self._merge_mode_combo.currentIndexChanged.connect(self._on_merge_mode_changed)
        r1.addWidget(self._merge_mode_combo)

        self._merge_align_lbl = QLabel("对齐：")
        self._merge_align_lbl.setProperty("role", "subtitle")
        r1.addWidget(self._merge_align_lbl)

        self._merge_align_combo = QComboBox()
        self._merge_align_combo.addItem("按列名智能对齐", "by_header")
        self._merge_align_combo.addItem("按列位置对齐", "by_position")
        self._merge_align_combo.setMinimumWidth(118)
        r1.addWidget(self._merge_align_combo)

        self._merge_join_lbl = QLabel("连接：")
        self._merge_join_lbl.setProperty("role", "subtitle")
        r1.addWidget(self._merge_join_lbl)

        self._merge_join_combo = QComboBox()
        self._merge_join_combo.addItem("左连接", "left")
        self._merge_join_combo.addItem("内连接", "inner")
        self._merge_join_combo.addItem("全外连", "outer")
        self._merge_join_combo.setMinimumWidth(78)
        r1.addWidget(self._merge_join_combo)

        self._merge_join_match_combo = QComboBox()
        self._merge_join_match_combo.addItem("全部匹配", "all")
        self._merge_join_match_combo.addItem("仅首条匹配", "first")
        self._merge_join_match_combo.setToolTip("当右表存在重复键时，选择保留全部匹配或仅取首条匹配")
        self._merge_join_match_combo.setMinimumWidth(92)
        r1.addWidget(self._merge_join_match_combo)

        self._merge_primary_lbl = QLabel("主表：")
        self._merge_primary_lbl.setProperty("role", "subtitle")
        r1.addWidget(self._merge_primary_lbl)

        self._merge_primary_combo = QComboBox()
        self._merge_primary_combo.setMinimumWidth(96)
        self._merge_primary_combo.setMaximumWidth(155)
        r1.addWidget(self._merge_primary_combo)

        self._btn_merge_cols = QPushButton("列管理")
        self._btn_merge_cols.setProperty("compact", True)
        self._btn_merge_cols.setToolTip("管理当前选中表的列名与列筛选，合并前快速对齐不一致列名")
        self._btn_merge_cols.clicked.connect(lambda: self._open_column_manager(self.active_table()))
        r1.addWidget(self._btn_merge_cols)

        r1.addStretch()

        self._merge_progress = QProgressBar()
        self._merge_progress.setFixedWidth(64)
        self._merge_progress.setFixedHeight(12)
        self._merge_progress.setTextVisible(False)
        self._merge_progress.setVisible(False)
        r1.addWidget(self._merge_progress)

        self._btn_cancel_merge = QPushButton("取消")
        self._btn_cancel_merge.setProperty("compact", True)
        self._btn_cancel_merge.setProperty("variant", "danger-outline")
        self._btn_cancel_merge.setVisible(False)
        self._btn_cancel_merge.clicked.connect(self._cancel_merge)
        r1.addWidget(self._btn_cancel_merge)

        self._btn_run_merge = QPushButton("合并预览")
        self._btn_run_merge.setToolTip("执行表格合并并生成预览")
        self._btn_run_merge.setProperty("variant", "primary")
        self._btn_run_merge.setProperty("compact", True)
        self._btn_run_merge.setIconSize(QSize(14, 14))
        self._btn_run_merge.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_run_merge.clicked.connect(self.run_merge)
        r1.addWidget(self._btn_run_merge)
        cfg_layout.addLayout(r1)

        # Row 2: Dedup/Join key mode + key column + keep strategy
        self._merge_row2_widget = QWidget()
        r2 = QHBoxLayout(self._merge_row2_widget)
        r2.setContentsMargins(0, 0, 0, 0)
        r2.setSpacing(7)

        lbl_dm = QLabel("依据：")
        lbl_dm.setProperty("role", "subtitle")
        r2.addWidget(lbl_dm)

        self._merge_dedup_mode_combo = QComboBox()
        self._merge_dedup_mode_combo.addItem("智能编码模式", "smart_code")
        self._merge_dedup_mode_combo.addItem("通用列值模式", "column_value")
        self._merge_dedup_mode_combo.setMinimumWidth(112)
        self._merge_dedup_mode_combo.currentIndexChanged.connect(self._on_merge_mode_changed)
        r2.addWidget(self._merge_dedup_mode_combo)

        self._merge_col_lbl = QLabel("关键列：")
        self._merge_col_lbl.setProperty("role", "subtitle")
        r2.addWidget(self._merge_col_lbl)

        self._merge_col_combo = QComboBox()
        self._merge_col_combo.setMinimumWidth(105)
        self._merge_col_combo.setMaximumWidth(155)
        r2.addWidget(self._merge_col_combo)

        self._btn_merge_per_table = QPushButton("按表指定")
        self._btn_merge_per_table.setProperty("compact", True)
        self._btn_merge_per_table.setToolTip("为不同表分别配置有序关键列或组合多列键")
        self._btn_merge_per_table.clicked.connect(self._open_merge_key_dialog)
        r2.addWidget(self._btn_merge_per_table)

        self._merge_custom_key_badge = QLabel("已覆盖下拉框")
        self._merge_custom_key_badge.setProperty("badge", "warning")
        self._merge_custom_key_badge.setToolTip("已启用按表自定义关键列配置，将优先于左侧统一关键列下拉框")
        self._merge_custom_key_badge.setVisible(False)
        r2.addWidget(self._merge_custom_key_badge)

        self._btn_merge_clear_keys = QPushButton("恢复默认")
        self._btn_merge_clear_keys.setProperty("compact", True)
        self._btn_merge_clear_keys.setProperty("variant", "ghost")
        self._btn_merge_clear_keys.setToolTip("清除按表自定义关键列配置")
        self._btn_merge_clear_keys.setVisible(False)
        self._btn_merge_clear_keys.clicked.connect(self._clear_merge_custom_keys)
        r2.addWidget(self._btn_merge_clear_keys)

        self._merge_keep_lbl = QLabel("保留策略：")
        self._merge_keep_lbl.setProperty("role", "subtitle")
        r2.addWidget(self._merge_keep_lbl)

        self._merge_keep_combo = QComboBox()
        self._merge_keep_combo.setMinimumWidth(150)
        self._refresh_merge_keep_strategies()
        r2.addWidget(self._merge_keep_combo)

        r2.addStretch()
        cfg_layout.addWidget(self._merge_row2_widget)

        # Row 3: Additional output column & normalization checkboxes
        r3 = QHBoxLayout()
        r3.setSpacing(12)

        self._chk_merge_add_source = QCheckBox("来源表列")
        self._chk_merge_add_source.setToolTip("在合并结果中附加 _来源表 列")
        self._chk_merge_add_source.setChecked(True)
        r3.addWidget(self._chk_merge_add_source)

        self._chk_merge_add_row_idx = QCheckBox("原行号列")
        self._chk_merge_add_row_idx.setToolTip("在合并结果中附加 _原行号 列")
        self._chk_merge_add_row_idx.setChecked(False)
        r3.addWidget(self._chk_merge_add_row_idx)

        self._chk_merge_add_code = QCheckBox("提取编码列")
        self._chk_merge_add_code.setToolTip("在合并结果中附加 _标准编码 列")
        self._chk_merge_add_code.setChecked(False)
        r3.addWidget(self._chk_merge_add_code)

        self._chk_merge_ignore_case = QCheckBox("忽略大小写")
        self._chk_merge_ignore_case.setChecked(True)
        r3.addWidget(self._chk_merge_ignore_case)

        self._chk_merge_strip = QCheckBox("忽略首尾空格")
        self._chk_merge_strip.setChecked(True)
        r3.addWidget(self._chk_merge_strip)

        self._chk_merge_ignore_punct = QCheckBox("忽略标点符号")
        self._chk_merge_ignore_punct.setChecked(False)
        r3.addWidget(self._chk_merge_ignore_punct)

        r3.addStretch()
        cfg_layout.addLayout(r3)
        layout.addWidget(cfg_card)

        # Preview stats & search bar
        mid_row = QHBoxLayout()
        mid_row.setContentsMargins(2, 0, 2, 0)
        mid_row.setSpacing(8)

        self._merge_summary_lbl = QLabel("勾选左侧表格后点击「合并预览」（支持单表去重或多表合并）")
        self._merge_summary_lbl.setProperty("role", "subtitle")
        mid_row.addWidget(self._merge_summary_lbl, 1)

        self._merge_filter_lbl = QLabel("0 行")
        self._merge_filter_lbl.setProperty("role", "muted")
        mid_row.addWidget(self._merge_filter_lbl)

        self._merge_search = QLineEdit()
        self._merge_search.setPlaceholderText("筛选合并结果或被去重明细...")
        self._merge_search.setClearButtonEnabled(True)
        self._merge_search.setMaximumWidth(240)
        _set_leading_search_icon(self._merge_search, theme)
        self._merge_search.textChanged.connect(lambda _: self._merge_search_timer.start())
        mid_row.addWidget(self._merge_search)

        self._btn_merge_col_menu = QPushButton()
        self._btn_merge_col_menu.setProperty("iconOnly", True)
        self._btn_merge_col_menu.setIconSize(QSize(15, 15))
        self._btn_merge_col_menu.setToolTip("自定义当前结果表显示列或恢复默认顺序")
        self._btn_merge_col_menu.clicked.connect(self._open_active_merge_col_menu)
        mid_row.addWidget(self._btn_merge_col_menu)

        layout.addLayout(mid_row)

        # Stacked EmptyState vs Merge Sub-Tabs
        self._merge_stack = QStackedWidget()
        self._merge_empty = EmptyStateWidget(
            "layers",
            "准备执行表格合并",
            "在左侧勾选表格并选择合并方式，点击上方「合并预览」查看合并表与去重明细",
            theme=theme,
            parent=self,
        )
        self._merge_stack.addWidget(self._merge_empty)

        self._merge_preview_tabs = QTabWidget()
        self._merge_preview_tabs.setProperty("subtabs", True)

        self._tv_merge_result, self._merge_result_header_mgr = self._make_table_view(
            self._merge_result_model, dynamic=True
        )
        self._tv_merge_result.doubleClicked.connect(self._on_merge_result_double_clicked)

        self._tv_merge_dropped, self._merge_dropped_header_mgr = self._make_table_view(
            self._merge_dropped_model,
            fixed_table_id="tables_merge_dropped",
            fixed_columns=_MERGE_DROPPED_SPECS,
        )
        self._tv_merge_dropped.doubleClicked.connect(self._on_merge_dropped_double_clicked)

        self._merge_preview_tabs.addTab(self._tv_merge_result, "合并结果表 (0 行)")
        self._merge_preview_tabs.addTab(self._tv_merge_dropped, "被去重明细 (0)")
        self._merge_preview_tabs.currentChanged.connect(lambda _: self._apply_merge_filter())

        self._merge_stack.addWidget(self._merge_preview_tabs)
        self._merge_stack.setCurrentIndex(0)
        layout.addWidget(self._merge_stack, 1)

        # Streamlined bottom bar: consolidated Export dropdown + Add to Workspace + Write to DB
        export_bar = QHBoxLayout()
        export_bar.setContentsMargins(2, 0, 2, 0)
        export_bar.setSpacing(8)

        self._btn_merge_export = QPushButton("导出  ")
        self._btn_merge_export.setToolTip("导出合并结果表为 Excel / Word / CSV / TXT")
        self._btn_merge_export.setProperty("compact", True)
        self._btn_merge_export.setIconSize(QSize(14, 14))
        self._btn_merge_export.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_merge_export.setEnabled(False)
        merge_exp_menu = QMenu(self._btn_merge_export)
        for label, fmt in (
            ("Excel 工作簿 (.xlsx)", "xlsx"),
            ("Word 文档 (.docx)", "docx"),
            ("CSV 表格 (.csv)", "csv"),
            ("纯文本 (.txt)", "txt"),
        ):
            merge_exp_menu.addAction(label, lambda _=False, f=fmt: self._export_merged(f))
        self._btn_merge_export.setMenu(merge_exp_menu)
        export_bar.addWidget(self._btn_merge_export)

        self._btn_merge_add_workspace = QPushButton("加入工作台")
        self._btn_merge_add_workspace.setToolTip("将合并结果作为新表加入左侧数据源列表")
        self._btn_merge_add_workspace.setProperty("compact", True)
        self._btn_merge_add_workspace.setIconSize(QSize(14, 14))
        self._btn_merge_add_workspace.setEnabled(False)
        self._btn_merge_add_workspace.clicked.connect(self._add_merged_to_workspace)
        export_bar.addWidget(self._btn_merge_add_workspace)

        export_bar.addStretch()

        self._db_import_progress = QProgressBar()
        self._db_import_progress.setFixedWidth(56)
        self._db_import_progress.setFixedHeight(12)
        self._db_import_progress.setTextVisible(False)
        self._db_import_progress.setVisible(False)
        export_bar.addWidget(self._db_import_progress)

        self._btn_merge_write_db = QPushButton("写入数据总库")
        self._btn_merge_write_db.setToolTip("后台解析标准编码并写入当前 SQLite 数据总库")
        self._btn_merge_write_db.setProperty("variant", "outline")
        self._btn_merge_write_db.setProperty("compact", True)
        self._btn_merge_write_db.setIconSize(QSize(14, 14))
        self._btn_merge_write_db.setEnabled(False)
        self._btn_merge_write_db.clicked.connect(self._write_merged_to_db)
        export_bar.addWidget(self._btn_merge_write_db)

        layout.addLayout(export_bar)
        return tab

    def _open_active_merge_col_menu(self) -> None:
        mgr = (
            self._merge_result_header_mgr
            if self._merge_preview_tabs.currentIndex() == 0
            else self._merge_dropped_header_mgr
        )
        self._open_header_menu_below(self._btn_merge_col_menu, mgr)

    def _clear_merge_dropped_model(self) -> None:
        self._clear_fixed_model(self._merge_dropped_model, _MERGE_DROPPED_SPECS)

    def _refresh_merge_keep_strategies(self) -> None:
        """Populate keep-strategy selector; hide 最高标签质量 unless show_advanced_fields is True."""
        prev_val = self._merge_keep_combo.currentData()
        show_adv = bool(getattr(self.ctx.config, "show_advanced_fields", False))

        self._merge_keep_combo.blockSignals(True)
        self._merge_keep_combo.clear()
        self._merge_keep_combo.addItem("保留首条", "first")
        self._merge_keep_combo.addItem("保留末条", "last")
        self._merge_keep_combo.addItem("保留字段最全", "most_complete")
        self._merge_keep_combo.addItem("字段互补合并 (Coalesce)", "coalesce")
        if show_adv:
            self._merge_keep_combo.addItem("保留最高标签质量", "highest_tag_score")

        idx = self._merge_keep_combo.findData(prev_val) if prev_val else -1
        self._merge_keep_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._merge_keep_combo.blockSignals(False)

    def _on_merge_mode_changed(self) -> None:
        mode = self._merge_mode_combo.currentData() or "union_dedup"
        is_join = mode == "join"
        is_union_all = mode == "union_all"
        is_union_dedup = mode == "union_dedup"

        self._merge_align_lbl.setVisible(not is_join)
        self._merge_align_combo.setVisible(not is_join)
        self._merge_join_lbl.setVisible(is_join)
        self._merge_join_combo.setVisible(is_join)
        self._merge_join_match_combo.setVisible(is_join)
        self._merge_primary_lbl.setVisible(is_join)
        self._merge_primary_combo.setVisible(is_join)

        self._merge_row2_widget.setVisible(not is_union_all)
        dedup_mode = self._merge_dedup_mode_combo.currentData() or "smart_code"
        show_col = (not is_union_all) and (dedup_mode == "column_value")
        self._merge_col_lbl.setVisible(show_col)
        self._merge_col_combo.setVisible(show_col)
        self._btn_merge_per_table.setVisible(show_col)

        checked_ids = {t.table_id for t in self.checked_tables()}
        has_custom = any(self._merge_custom_keys.get(tid) for tid in checked_ids)
        self._merge_custom_key_badge.setVisible(show_col and has_custom)
        self._btn_merge_clear_keys.setVisible(show_col and has_custom)

        self._merge_keep_lbl.setVisible(is_union_dedup)
        self._merge_keep_combo.setVisible(is_union_dedup)

        self._chk_merge_ignore_case.setVisible(show_col)
        self._chk_merge_strip.setVisible(show_col)
        cfg_fields = {f.name for f in dataclasses.fields(TableMergeConfig)}
        self._chk_merge_ignore_punct.setVisible(show_col and ("ignore_punctuation" in cfg_fields))

    def _open_merge_key_dialog(self) -> None:
        tables = self.checked_tables()
        if not tables:
            self.ctx.notify("请先在左侧勾选至少 1 张表格", "warning")
            return
        default_col = str(self._merge_col_combo.currentData() or "")
        dlg = _PerTableKeyDialog(
            tables,
            self._merge_custom_keys,
            parent=self,
            default_column=default_col,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.was_cleared:
                self._merge_custom_keys.clear()
                self._update_custom_key_button_labels()
                self._on_merge_mode_changed()
                self.ctx.notify("已清除合并按表关键列配置，恢复使用统一关键列", "info")
            else:
                self._merge_custom_keys.update(dlg.get_key_mapping())
                self._update_custom_key_button_labels()
                self._on_merge_mode_changed()
                self.ctx.notify("已更新各表合并关键列配置", "success")

    def _clear_merge_custom_keys(self) -> None:
        self._merge_custom_keys.clear()
        self._update_custom_key_button_labels()
        self._on_merge_mode_changed()
        self.ctx.notify("已恢复默认合并关键列", "info")

    def _validate_merge_key_columns(self, tables: list[DataTable]) -> bool:
        mode = self._merge_mode_combo.currentData() or "union_dedup"
        dedup_mode = self._merge_dedup_mode_combo.currentData() or "smart_code"
        if mode == "union_all" or dedup_mode != "column_value":
            return True

        selected_col = str(self._merge_col_combo.currentData() or "")
        missing_tables: list[str] = []
        for tbl in tables:
            custom = [c for c in self._merge_custom_keys.get(tbl.table_id, []) if c in tbl.headers]
            if custom:
                continue
            if selected_col and selected_col not in tbl.headers:
                missing_tables.append(tbl.name)

        if not missing_tables:
            return True

        detail_lines = [
            f"当前选择的关键列「{selected_col}」在以下表格中不存在：",
            *[f"- {name}" for name in missing_tables],
            "",
            "若继续执行，缺失该列的表格将默认回退到第 1 列进行匹配。",
            "建议先通过「列管理」重命名列名，或点击「按表指定」为各表分别映射关键列。",
        ]
        dlg = ScrollableDetailDialog(
            title="部分表格缺少所选关键列",
            summary=f"有 {len(missing_tables)} 张参与表不包含列「{selected_col}」，是否继续执行合并？",
            detail_text="\n".join(detail_lines),
            confirm_mode=True,
            confirm_label="仍要继续",
            parent=self,
        )
        return dlg.exec() == QDialog.DialogCode.Accepted

    def _build_merge_config(self, tables: list[DataTable]) -> TableMergeConfig:
        mode = self._merge_mode_combo.currentData() or "union_dedup"
        dedup_mode = self._merge_dedup_mode_combo.currentData() or "smart_code"
        key_columns: dict[str, list[str]] = {}

        if dedup_mode == "column_value" and mode != "union_all":
            selected_col = str(self._merge_col_combo.currentData() or "")
            for tbl in tables:
                custom = [c for c in self._merge_custom_keys.get(tbl.table_id, []) if c in tbl.headers]
                if custom:
                    key_columns[tbl.table_id] = custom
                elif selected_col and selected_col in tbl.headers:
                    key_columns[tbl.table_id] = [selected_col]
                elif tbl.headers:
                    key_columns[tbl.table_id] = [tbl.headers[0]]

        primary_id = self._merge_primary_combo.currentData() or (tables[0].table_id if tables else "")
        kwargs: dict[str, Any] = {
            "mode": mode,
            "align_mode": self._merge_align_combo.currentData() or "by_header",
            "dedup_mode": dedup_mode,
            "key_columns": key_columns,
            "keep_strategy": self._merge_keep_combo.currentData() or "first",
            "join_type": self._merge_join_combo.currentData() or "left",
            "primary_table_id": primary_id,
            "add_source_column": self._chk_merge_add_source.isChecked(),
            "add_row_index_column": self._chk_merge_add_row_idx.isChecked(),
            "add_extracted_code_column": self._chk_merge_add_code.isChecked(),
            "ignore_case": self._chk_merge_ignore_case.isChecked(),
            "strip_whitespace": self._chk_merge_strip.isChecked(),
        }
        valid_fields = {f.name for f in dataclasses.fields(TableMergeConfig)}
        if "ignore_punctuation" in valid_fields:
            kwargs["ignore_punctuation"] = self._chk_merge_ignore_punct.isChecked()
        if "join_match_mode" in valid_fields:
            kwargs["join_match_mode"] = self._merge_join_match_combo.currentData() or "all"

        return TableMergeConfig(**kwargs)

    def run_merge(self) -> None:
        """Execute table merge in a background WorkerThread."""
        tables = self.checked_tables()
        if not tables:
            self.ctx.notify("请先在左侧勾选至少 1 张表格", "warning")
            return
        mode = self._merge_mode_combo.currentData() or "union_dedup"
        if mode == "join" and len(tables) < 2:
            self.ctx.notify("横向拼接 (Join) 至少需要勾选 2 张表格", "warning")
            return
        if self._is_worker_running(self._merge_worker):
            self.ctx.notify("正在执行表格合并，请稍候...", "warning")
            return
        if not self._validate_merge_key_columns(tables):
            return

        config = self._build_merge_config(tables)
        snapshots = [t.clone(new_id=t.table_id) for t in tables]
        snapshot_sig = self._compute_tables_snapshot(tables)
        parser_engine = self.ctx.parser

        self._merge_gen += 1
        gen = self._merge_gen

        self._merge_progress.setVisible(True)
        self._merge_progress.setRange(0, 0)
        self._btn_cancel_merge.setVisible(True)

        def _task() -> TableMergeResult:
            return merge_tables(snapshots, config, parser_engine=parser_engine)

        worker = self._make_worker(_task)
        self._merge_worker = worker
        worker.succeeded.connect(
            lambda res, g=gen, sig=snapshot_sig: self._on_merge_finished(g, sig, res)
        )
        worker.failed.connect(lambda detail, g=gen: self._on_merge_failed(g, detail))
        if hasattr(worker, "cancelled"):
            worker.cancelled.connect(lambda g=gen: self._on_merge_cancelled(g))
        worker.finished.connect(lambda w=worker: self._clear_worker_ref("_merge_worker", w))
        self._sync_controls_state()
        worker.start()

    def _cancel_merge(self) -> None:
        self._merge_gen += 1
        worker = self._merge_worker
        if worker is not None:
            try:
                worker.cancel()
            except RuntimeError:
                pass
        self._merge_progress.setVisible(False)
        self._btn_cancel_merge.setVisible(False)
        self._sync_controls_state()
        self.ctx.notify("已请求取消表格合并", "info")

    def _on_merge_finished(
        self,
        gen: int,
        snapshot_sig: Any,
        result: TableMergeResult,
    ) -> None:
        if gen != self._merge_gen:
            return
        self._merge_progress.setVisible(False)
        self._btn_cancel_merge.setVisible(False)
        self.load_merge_result(result, snapshot=snapshot_sig)
        self._sync_controls_state()
        self.ctx.notify(
            f"合并预览完成：共 {result.merged_table.row_count:,} 行 × {result.merged_table.col_count} 列"
            + (f"（去重移除 {len(result.dropped_rows):,} 行）" if result.dropped_rows else ""),
            "success",
        )

    def _on_merge_failed(self, gen: int, detail: str) -> None:
        if gen != self._merge_gen:
            return
        self._merge_progress.setVisible(False)
        self._btn_cancel_merge.setVisible(False)
        self._sync_controls_state()
        ScrollableDetailDialog("表格合并出错", "执行表格合并时发生错误：", detail, parent=self).exec()

    def _on_merge_cancelled(self, gen: int) -> None:
        if gen != self._merge_gen:
            return
        self._merge_progress.setVisible(False)
        self._btn_cancel_merge.setVisible(False)
        self._sync_controls_state()

    def load_merge_result(
        self,
        result: TableMergeResult,
        snapshot: Optional[Any] = None,
    ) -> None:
        """Display merged table (dynamic header sync) and dropped rows audit trail."""
        self._last_merge_result = result
        self._last_merge_snapshot = snapshot
        self._merge_dropped_refs = list(result.dropped_rows)
        merged = result.merged_table

        mono_cols = {i for i, h in enumerate(merged.headers) if h in ("标准编码", "_标准编码")}
        self._merge_result_model.set_table(
            merged,
            highlight_columns=mono_cols,
            mono_columns=mono_cols,
            copy_columns=mono_cols,
            reset_filter=True,
        )
        if self._merge_result_header_mgr is not None:
            self._merge_result_header_mgr.sync_dynamic_columns(auto_fit=True)
        self._reset_table_view_sort_indicator(self._tv_merge_result)

        dropped_headers = [s.label for s in _MERGE_DROPPED_SPECS]
        dropped_rows: list[list[str]] = []
        dropped_tints: dict[int, str] = {}
        for idx, ref in enumerate(result.dropped_rows):
            dropped_rows.append(
                [
                    ref.key_value or "-",
                    ref.table_name,
                    str(ref.row_index),
                    ref.parsed_code or "-",
                    ref.tags or "-",
                    ref.part or "-",
                    _format_row_summary(ref),
                ]
            )
            dropped_tints[idx] = "dropped"

        self._merge_dropped_model.set_data(
            dropped_headers,
            dropped_rows,
            row_tints=dropped_tints,
            highlight_columns={0},
            mono_columns={0, 3},
            copy_columns={0, 3},
            center_columns={2, 5},
            reset_filter=True,
        )
        if self._merge_dropped_header_mgr is not None:
            self._merge_dropped_header_mgr.reapply_sort()

        self._merge_preview_tabs.setTabText(0, f"合并结果表 ({merged.row_count:,} 行 × {merged.col_count} 列)")
        self._merge_preview_tabs.setTabText(1, f"被去重明细 ({len(dropped_rows):,})")

        stats_parts = [
            f"合并结果：{merged.row_count:,} 行 × {merged.col_count} 列",
            f"去重移除：{len(dropped_rows):,} 行",
        ]
        if result.stats.get("duplicate_groups_merged"):
            stats_parts.append(f"合并重复组：{result.stats['duplicate_groups_merged']:,} 组")
        if result.stats.get("input_rows"):
            stats_parts.insert(0, f"原始输入：{result.stats['input_rows']:,} 行")
        self._merge_summary_lbl.setText(" · ".join(stats_parts))

        self._merge_stack.setCurrentIndex(1)
        self._apply_merge_filter()
        self._sync_controls_state()

    def _on_merge_result_double_clicked(self, index: QModelIndex) -> None:
        if index.isValid():
            self._inspect_table_view_row(self._tv_merge_result, index.row())

    def _on_merge_dropped_double_clicked(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        orig_row = self._merge_dropped_model.original_row_index(index.row())
        if 0 <= orig_row < len(self._merge_dropped_refs):
            ref: TableRowRef = self._merge_dropped_refs[orig_row]
            self._jump_to_table_row(ref.table_id, ref.row_index, fallback_ref=ref)

    def _apply_merge_filter(self) -> None:
        query = self._merge_search.text()
        if self._merge_preview_tabs.currentIndex() == 0:
            self._merge_result_model.set_filter(query)
        else:
            self._merge_dropped_model.set_filter(query)
        self._update_merge_filter_label()

    def _update_merge_filter_label(self) -> None:
        model = (
            self._merge_result_model
            if self._merge_preview_tabs.currentIndex() == 0
            else self._merge_dropped_model
        )
        shown = model.filtered_row_count()
        total = model.total_row_count()
        if model.filter_query():
            self._merge_filter_lbl.setText(f"筛选命中 {shown:,} / {total:,} 行")
        else:
            self._merge_filter_lbl.setText(f"共 {total:,} 行")

    def _reset_merge_ui(self) -> None:
        self._merge_search_timer.stop()
        self._merge_search.blockSignals(True)
        self._merge_search.clear()
        self._merge_search.blockSignals(False)
        self._merge_dropped_refs.clear()

        if self._merge_result_header_mgr is not None:
            self._merge_result_header_mgr.sync_dynamic_columns(auto_fit=False)
        self._reset_table_view_sort_indicator(self._tv_merge_result)

        self._merge_preview_tabs.setTabText(0, "合并结果表 (0 行)")
        self._merge_preview_tabs.setTabText(1, "被去重明细 (0)")
        self._merge_summary_lbl.setText("勾选左侧表格后点击「合并预览」（支持单表去重或多表合并）")
        self._merge_filter_lbl.setText("0 行")
        self._merge_stack.setCurrentIndex(0)
        for btn in (
            self._btn_merge_export,
            self._btn_merge_add_workspace,
            self._btn_merge_write_db,
        ):
            btn.setEnabled(False)

    def _add_merged_to_workspace(self) -> None:
        if not self._last_merge_result:
            return
        cloned = self._last_merge_result.merged_table.clone()
        cloned.source_type = "merged"
        self.add_tables([cloned], select_first=True)
        self.ctx.notify(f"已将「{cloned.name}」加入左侧数据源列表", "success")

    def _write_merged_to_db(self) -> None:
        if not self._last_merge_result:
            return
        if self._is_worker_running(self._db_import_worker):
            self.ctx.notify("正在写入数据总库，请稍候...", "warning")
            return
        merged = self._last_merge_result.merged_table
        if merged.row_count == 0:
            self.ctx.notify("合并结果为空，无需写入数据总库", "warning")
            return

        dlg = ScrollableDetailDialog(
            title="确认写入当前数据总库",
            summary=(
                f"即将把合并结果「{merged.name}」（共 {merged.row_count:,} 行）"
                f"解析标准编码并写入当前数据库「{Path(self.ctx.db.db_path).name}」："
            ),
            detail_text="系统将在后台线程自动提取每行的标准编码并创建导入批次记录。",
            confirm_mode=True,
            confirm_label="确认写入总库",
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        snapshot = merged.clone(new_id=merged.table_id)
        db_repo = self.ctx.db
        parser_engine = self.ctx.parser
        source_name = snapshot.name or "多表合并入库"

        preferred_code_col: Optional[str] = None
        for cand in ("_标准编码", "标准编码"):
            if cand in snapshot.headers:
                preferred_code_col = cand
                break
        sig_params = inspect.signature(import_table_to_database).parameters

        self._db_import_gen += 1
        gen = self._db_import_gen
        self._db_import_progress.setVisible(True)
        self._db_import_progress.setRange(0, 0)

        def _task() -> dict[str, Any]:
            if "fallback_scan_all" in sig_params:
                return import_table_to_database(
                    snapshot,
                    db_repo,
                    parser_engine,
                    code_column=preferred_code_col,
                    source_name=source_name,
                    fallback_scan_all=True,
                )
            return import_table_to_database(
                snapshot,
                db_repo,
                parser_engine,
                code_column=None,
                source_name=source_name,
            )

        worker = self._make_worker(_task)
        self._db_import_worker = worker
        worker.succeeded.connect(lambda res, g=gen: self._on_db_import_finished(g, res))
        worker.failed.connect(lambda detail, g=gen: self._on_db_import_failed(g, detail))
        if hasattr(worker, "cancelled"):
            worker.cancelled.connect(lambda g=gen: self._on_db_import_cancelled(g))
        worker.finished.connect(lambda w=worker: self._clear_worker_ref("_db_import_worker", w))
        self._sync_controls_state()
        worker.start()

    def _on_db_import_finished(self, gen: int, res: dict[str, Any]) -> None:
        if gen != self._db_import_gen:
            return
        self._db_import_progress.setVisible(False)
        self._sync_controls_state()
        added = int(res.get("added", 0))
        skipped = int(res.get("skipped", 0))
        unrec = int(res.get("unrecognized", 0))
        self.ctx.data_changed.emit()
        self.ctx.notify(
            f"写入数据总库完成：新增 {added:,} 条，跳过重复 {skipped:,} 条，未识别 {unrec:,} 条",
            "success",
        )

    def _on_db_import_failed(self, gen: int, detail: str) -> None:
        if gen != self._db_import_gen:
            return
        self._db_import_progress.setVisible(False)
        self._sync_controls_state()
        ScrollableDetailDialog("写入数据总库失败", "写入数据总库时发生错误：", detail, parent=self).exec()

    def _on_db_import_cancelled(self, gen: int) -> None:
        if gen != self._db_import_gen:
            return
        self._db_import_progress.setVisible(False)
        self._sync_controls_state()

    def _export_merged(self, fmt: str) -> None:
        if not self._last_merge_result:
            return
        self._export_single_table(self._last_merge_result.merged_table, fmt)
