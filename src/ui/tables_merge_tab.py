"""Table Merge & Export sub-tab mixin for TablesView (表格合并与导出)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.table_data import (
    DataTable,
    TableMergeConfig,
    TableMergeResult,
)
from src.core.table_engine import (
    import_table_to_database,
    merge_tables,
)
from src.ui.tables_widgets import _PerTableKeyDialog, _format_row_summary
from src.ui.widgets import ScrollableDetailDialog, WorkerThread


class TablesMergeTabMixin:
    """Mixin implementing Sub-Tab 3 (表格合并与导出) for TablesView."""

    def _build_merge_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Merge configuration card
        cfg_card = QFrame()
        cfg_card.setProperty("card", True)
        cfg_layout = QVBoxLayout(cfg_card)
        cfg_layout.setContentsMargins(12, 10, 12, 10)
        cfg_layout.setSpacing(8)

        # Row 1: Merge mode + alignment / join controls + run CTA
        r1 = QHBoxLayout()
        r1.setSpacing(7)

        lbl_mode = QLabel("模式：")
        lbl_mode.setProperty("role", "section")
        r1.addWidget(lbl_mode)

        self._merge_mode_combo = QComboBox()
        self._merge_mode_combo.addItem("纵向合并去重", "union_dedup")
        self._merge_mode_combo.addItem("纵向追加合并", "union_all")
        self._merge_mode_combo.addItem("横向拼接", "join")
        self._merge_mode_combo.setMinimumWidth(108)
        self._merge_mode_combo.currentIndexChanged.connect(self._on_merge_mode_changed)
        r1.addWidget(self._merge_mode_combo)

        # Alignment controls (for union modes)
        self._merge_align_lbl = QLabel("对齐：")
        self._merge_align_lbl.setProperty("role", "subtitle")
        r1.addWidget(self._merge_align_lbl)

        self._merge_align_combo = QComboBox()
        self._merge_align_combo.addItem("按列名智能对齐", "by_header")
        self._merge_align_combo.addItem("按列位置对齐", "by_position")
        self._merge_align_combo.setMinimumWidth(118)
        r1.addWidget(self._merge_align_combo)

        # Join controls (for join mode)
        self._merge_join_lbl = QLabel("连接：")
        self._merge_join_lbl.setProperty("role", "subtitle")
        r1.addWidget(self._merge_join_lbl)

        self._merge_join_combo = QComboBox()
        self._merge_join_combo.addItem("左连接", "left")
        self._merge_join_combo.addItem("内连接", "inner")
        self._merge_join_combo.addItem("全外连", "outer")
        self._merge_join_combo.setMinimumWidth(78)
        r1.addWidget(self._merge_join_combo)

        self._merge_primary_lbl = QLabel("主表：")
        self._merge_primary_lbl.setProperty("role", "subtitle")
        r1.addWidget(self._merge_primary_lbl)

        self._merge_primary_combo = QComboBox()
        self._merge_primary_combo.setMinimumWidth(96)
        self._merge_primary_combo.setMaximumWidth(155)
        r1.addWidget(self._merge_primary_combo)

        r1.addStretch()

        self._merge_progress = QProgressBar()
        self._merge_progress.setFixedWidth(64)
        self._merge_progress.setFixedHeight(14)
        self._merge_progress.setTextVisible(False)
        self._merge_progress.setVisible(False)
        r1.addWidget(self._merge_progress)

        self._btn_run_merge = QPushButton("▶ 合并预览")
        self._btn_run_merge.setToolTip("执行表格合并并生成预览")
        self._btn_run_merge.setProperty("variant", "primary")
        self._btn_run_merge.setStyleSheet("padding: 5px 10px;")
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
        self._btn_merge_per_table.setStyleSheet("padding: 5px 8px; font-size: 12px;")
        self._btn_merge_per_table.clicked.connect(self._open_merge_key_dialog)
        r2.addWidget(self._btn_merge_per_table)

        self._merge_keep_lbl = QLabel("保留策略：")
        self._merge_keep_lbl.setProperty("role", "subtitle")
        r2.addWidget(self._merge_keep_lbl)

        self._merge_keep_combo = QComboBox()
        self._merge_keep_combo.addItem("保留首条", "first")
        self._merge_keep_combo.addItem("保留末条", "last")
        self._merge_keep_combo.addItem("保留字段最全", "most_complete")
        self._merge_keep_combo.addItem("保留最高标签质量", "highest_tag_score")
        self._merge_keep_combo.addItem("字段互补合并 (Coalesce)", "coalesce")
        self._merge_keep_combo.setMinimumWidth(150)
        r2.addWidget(self._merge_keep_combo)

        r2.addStretch()
        cfg_layout.addWidget(self._merge_row2_widget)

        # Row 3: Additional output column & normalization checkboxes
        r3 = QHBoxLayout()
        r3.setSpacing(10)

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

        r3.addStretch()
        cfg_layout.addLayout(r3)
        layout.addWidget(cfg_card)

        # Preview stats & search bar
        mid_row = QHBoxLayout()
        mid_row.setSpacing(10)

        self._merge_summary_lbl = QLabel("请勾选左侧 2 张及以上表格后，点击「执行合并预览」")
        self._merge_summary_lbl.setProperty("role", "subtitle")
        mid_row.addWidget(self._merge_summary_lbl, 1)

        self._merge_search = QLineEdit()
        self._merge_search.setPlaceholderText("🔍 筛选合并结果或被去重明细...")
        self._merge_search.setClearButtonEnabled(True)
        self._merge_search.setMaximumWidth(280)
        self._merge_search.textChanged.connect(lambda _: self._merge_search_timer.start())
        mid_row.addWidget(self._merge_search)
        layout.addLayout(mid_row)

        # Preview Sub-Tabs: Merged Table + Dropped Rows
        self._merge_preview_tabs = QTabWidget()
        self._tv_merge_result = self._make_table_view(self._merge_result_model)
        self._tv_merge_dropped = self._make_table_view(self._merge_dropped_model)
        self._merge_preview_tabs.addTab(self._tv_merge_result, "合并结果表 (0 行 × 0 列)")
        self._merge_preview_tabs.addTab(self._tv_merge_dropped, "被去重明细 (0 条)")
        self._merge_preview_tabs.currentChanged.connect(lambda _: self._apply_merge_filter())
        layout.addWidget(self._merge_preview_tabs, 1)

        # Bottom Export & Write-to-DB bar
        export_bar = QHBoxLayout()
        export_bar.setSpacing(6)

        lbl_m_exp = QLabel("导出：")
        lbl_m_exp.setProperty("role", "subtitle")
        export_bar.addWidget(lbl_m_exp)

        compact_btn_qss = "padding: 5px 9px; font-size: 12px;"

        self._btn_merge_xlsx = QPushButton("Excel")
        self._btn_merge_xlsx.setToolTip("导出合并结果为 Excel 工作簿 (.xlsx)")
        self._btn_merge_xlsx.setStyleSheet(compact_btn_qss)
        self._btn_merge_xlsx.setEnabled(False)
        self._btn_merge_xlsx.clicked.connect(lambda: self._export_merged("xlsx"))
        export_bar.addWidget(self._btn_merge_xlsx)

        self._btn_merge_docx = QPushButton("Word")
        self._btn_merge_docx.setToolTip("导出合并结果为 Word 文档 (.docx)")
        self._btn_merge_docx.setStyleSheet(compact_btn_qss)
        self._btn_merge_docx.setEnabled(False)
        self._btn_merge_docx.clicked.connect(lambda: self._export_merged("docx"))
        export_bar.addWidget(self._btn_merge_docx)

        self._btn_merge_csv = QPushButton("CSV")
        self._btn_merge_csv.setStyleSheet(compact_btn_qss)
        self._btn_merge_csv.setEnabled(False)
        self._btn_merge_csv.clicked.connect(lambda: self._export_merged("csv"))
        export_bar.addWidget(self._btn_merge_csv)

        self._btn_merge_txt = QPushButton("TXT")
        self._btn_merge_txt.setStyleSheet(compact_btn_qss)
        self._btn_merge_txt.setEnabled(False)
        self._btn_merge_txt.clicked.connect(lambda: self._export_merged("txt"))
        export_bar.addWidget(self._btn_merge_txt)

        self._btn_merge_add_workspace = QPushButton("加入工作台")
        self._btn_merge_add_workspace.setToolTip("将合并结果作为新表加入左侧数据源列表")
        self._btn_merge_add_workspace.setStyleSheet(compact_btn_qss)
        self._btn_merge_add_workspace.setEnabled(False)
        self._btn_merge_add_workspace.clicked.connect(self._add_merged_to_workspace)
        export_bar.addWidget(self._btn_merge_add_workspace)

        export_bar.addStretch()

        self._btn_merge_write_db = QPushButton("写入数据总库")
        self._btn_merge_write_db.setToolTip("一键解析标准编码并写入当前 SQLite 数据总库")
        self._btn_merge_write_db.setProperty("variant", "success")
        self._btn_merge_write_db.setStyleSheet("padding: 5px 10px; font-size: 12px;")
        self._btn_merge_write_db.setEnabled(False)
        self._btn_merge_write_db.clicked.connect(self._write_merged_to_db)
        export_bar.addWidget(self._btn_merge_write_db)

        layout.addLayout(export_bar)
        return tab

    def _on_merge_mode_changed(self) -> None:
        mode = self._merge_mode_combo.currentData() or "union_dedup"
        is_join = mode == "join"
        is_union_all = mode == "union_all"
        is_union_dedup = mode == "union_dedup"

        self._merge_align_lbl.setVisible(not is_join)
        self._merge_align_combo.setVisible(not is_join)
        self._merge_join_lbl.setVisible(is_join)
        self._merge_join_combo.setVisible(is_join)
        self._merge_primary_lbl.setVisible(is_join)
        self._merge_primary_combo.setVisible(is_join)

        self._merge_row2_widget.setVisible(not is_union_all)
        dedup_mode = self._merge_dedup_mode_combo.currentData() or "smart_code"
        show_col = dedup_mode == "column_value"
        self._merge_col_lbl.setVisible(show_col)
        self._merge_col_combo.setVisible(show_col)
        self._btn_merge_per_table.setVisible(show_col)

        self._merge_keep_lbl.setVisible(is_union_dedup)
        self._merge_keep_combo.setVisible(is_union_dedup)

    def _open_merge_key_dialog(self) -> None:
        tables = self.checked_tables()
        if not tables:
            self.ctx.notify("请先在左侧勾选至少 1 张表格", "warning")
            return
        dlg = _PerTableKeyDialog(tables, self._merge_custom_keys, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._merge_custom_keys.update(dlg.get_key_mapping())
            self._update_custom_key_button_labels()
            self.ctx.notify("已更新各表合并关键列配置", "success")

    def _build_merge_config(self, tables: list[DataTable]) -> TableMergeConfig:
        mode = self._merge_mode_combo.currentData() or "union_dedup"
        dedup_mode = self._merge_dedup_mode_combo.currentData() or "smart_code"
        key_columns: dict[str, list[str]] = {}

        if dedup_mode == "column_value" and mode != "union_all":
            selected_col = self._merge_col_combo.currentData() or ""
            for tbl in tables:
                custom = [c for c in self._merge_custom_keys.get(tbl.table_id, []) if c in tbl.headers]
                if custom:
                    key_columns[tbl.table_id] = custom
                elif selected_col and selected_col in tbl.headers:
                    key_columns[tbl.table_id] = [selected_col]
                elif tbl.headers:
                    key_columns[tbl.table_id] = [tbl.headers[0]]

        primary_id = self._merge_primary_combo.currentData() or (tables[0].table_id if tables else "")
        return TableMergeConfig(
            mode=mode,
            align_mode=self._merge_align_combo.currentData() or "by_header",
            dedup_mode=dedup_mode,
            key_columns=key_columns,
            keep_strategy=self._merge_keep_combo.currentData() or "first",
            join_type=self._merge_join_combo.currentData() or "left",
            primary_table_id=primary_id,
            add_source_column=self._chk_merge_add_source.isChecked(),
            add_row_index_column=self._chk_merge_add_row_idx.isChecked(),
            add_extracted_code_column=self._chk_merge_add_code.isChecked(),
            ignore_case=self._chk_merge_ignore_case.isChecked(),
            strip_whitespace=self._chk_merge_strip.isChecked(),
        )

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
        if self._merge_worker is not None and self._merge_worker.isRunning():
            self.ctx.notify("正在执行表格合并，请稍候...", "warning")
            return

        config = self._build_merge_config(tables)
        parser_engine = self.ctx.parser

        self._btn_run_merge.setEnabled(False)
        self._merge_progress.setVisible(True)
        self._merge_progress.setRange(0, 0)

        def _task() -> TableMergeResult:
            return merge_tables(tables, config, parser_engine=parser_engine)

        self._merge_worker = WorkerThread(_task, parent=self)
        self._merge_worker.succeeded.connect(self._on_merge_finished)
        self._merge_worker.failed.connect(self._on_merge_failed)
        self._merge_worker.start()

    def _on_merge_finished(self, result: TableMergeResult) -> None:
        self._btn_run_merge.setEnabled(True)
        self._merge_progress.setVisible(False)
        self.load_merge_result(result)
        self.ctx.notify(
            f"合并预览完成：共 {result.merged_table.row_count:,} 行 × {result.merged_table.col_count} 列"
            + (f"（去重移除 {len(result.dropped_rows):,} 行）" if result.dropped_rows else ""),
            "success",
        )

    def _on_merge_failed(self, detail: str) -> None:
        self._btn_run_merge.setEnabled(True)
        self._merge_progress.setVisible(False)
        ScrollableDetailDialog("表格合并出错", "执行表格合并时发生错误：", detail, parent=self).exec()

    def load_merge_result(self, result: TableMergeResult) -> None:
        """Display merged table and dropped rows audit trail."""
        self._last_merge_result = result
        merged = result.merged_table

        mono_cols = {i for i, h in enumerate(merged.headers) if h in ("标准编码", "_标准编码")}
        self._merge_result_model.set_table(merged, highlight_columns=mono_cols, mono_columns=mono_cols)
        self._auto_size_columns(self._tv_merge_result, self._merge_result_model)

        dropped_headers = ["去重键 / 编码", "来源表", "原行号", "提取编码", "版本标签", "分集", "原行内容摘要"]
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
            center_columns={2, 5},
        )
        self._auto_size_columns(self._tv_merge_dropped, self._merge_dropped_model)

        self._merge_preview_tabs.setTabText(0, f"合并结果表 ({merged.row_count:,} 行 × {merged.col_count} 列)")
        self._merge_preview_tabs.setTabText(1, f"被去重明细 ({len(dropped_rows):,} 条)")

        stats_parts = [
            f"合并结果：{merged.row_count:,} 行 × {merged.col_count} 列",
            f"去重移除：{len(dropped_rows):,} 行",
        ]
        if result.stats.get("duplicate_groups_merged"):
            stats_parts.append(f"合并重复组：{result.stats['duplicate_groups_merged']:,} 组")
        if result.stats.get("input_rows"):
            stats_parts.insert(0, f"原始输入：{result.stats['input_rows']:,} 行")
        self._merge_summary_lbl.setText(" · ".join(stats_parts))

        has_data = merged.row_count > 0 or merged.col_count > 0
        for btn in (
            self._btn_merge_xlsx,
            self._btn_merge_docx,
            self._btn_merge_csv,
            self._btn_merge_txt,
            self._btn_merge_add_workspace,
            self._btn_merge_write_db,
        ):
            btn.setEnabled(has_data)

        self._apply_merge_filter()

    def _apply_merge_filter(self) -> None:
        query = self._merge_search.text()
        if self._merge_preview_tabs.currentIndex() == 0:
            self._merge_result_model.set_filter(query)
        else:
            self._merge_dropped_model.set_filter(query)

    def _reset_merge_ui(self) -> None:
        self._merge_preview_tabs.setTabText(0, "合并结果表 (0 行 × 0 列)")
        self._merge_preview_tabs.setTabText(1, "被去重明细 (0 条)")
        self._merge_summary_lbl.setText("请勾选左侧 2 张及以上表格后，点击「执行合并预览」")
        for btn in (
            self._btn_merge_xlsx,
            self._btn_merge_docx,
            self._btn_merge_csv,
            self._btn_merge_txt,
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
        self.ctx.notify(f"已将「{cloned.name}」加入左侧工作台列表", "success")

    def _write_merged_to_db(self) -> None:
        if not self._last_merge_result:
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
            detail_text="系统会自动提取每行的标准编码、版本标签与分集信息并创建导入批次记录。",
            confirm_mode=True,
            confirm_label="确认写入总库",
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            code_col: Optional[str] = None
            for cand in ("_标准编码", "标准编码"):
                if cand in merged.headers:
                    code_col = cand
                    break

            res = import_table_to_database(
                merged,
                self.ctx.db,
                self.ctx.parser,
                code_column=code_col,
                source_name=merged.name or "多表合并入库",
            )
            added = int(res.get("added", 0))
            skipped = int(res.get("skipped", 0))
            unrec = int(res.get("unrecognized", 0))
            self.ctx.data_changed.emit()
            self.ctx.notify(
                f"写入数据总库完成：新增 {added:,} 条，跳过重复 {skipped:,} 条，未识别 {unrec:,} 条",
                "success",
            )
        except Exception as exc:
            ScrollableDetailDialog("写入数据总库失败", str(exc), parent=self).exec()

    def _export_merged(self, fmt: str) -> None:
        if not self._last_merge_result:
            return
        self._export_single_table(self._last_merge_result.merged_table, fmt)
