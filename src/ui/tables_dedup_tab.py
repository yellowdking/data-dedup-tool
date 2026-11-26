"""Multi-Table Deduplication sub-tab mixin for TablesView (多表查重)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.exporter import ExportError
from src.core.table_data import (
    DataTable,
    TableDedupConfig,
    TableDedupReport,
    generate_table_id,
)
from src.core.table_engine import run_multi_table_dedup
from src.core.table_exporter import (
    export_dedup_report_excel,
    export_dedup_report_word,
    export_table_csv,
)
from src.ui.table_preview_model import DynamicTableModel
from src.ui.tables_widgets import (
    _CompactMetricCard,
    _PerTableKeyDialog,
    _format_row_summary,
)
from src.ui.widgets import ScrollableDetailDialog, WorkerThread


class TablesDedupTabMixin:
    """Mixin implementing Sub-Tab 2 (多表查重) for TablesView."""

    def _build_dedup_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Config card
        cfg_card = QFrame()
        cfg_card.setProperty("card", True)
        cfg_layout = QVBoxLayout(cfg_card)
        cfg_layout.setContentsMargins(12, 10, 12, 10)
        cfg_layout.setSpacing(8)

        # Row 1: Mode toggle + primary button
        row1 = QHBoxLayout()
        row1.setSpacing(8)

        mode_lbl = QLabel("模式：")
        mode_lbl.setProperty("role", "section")
        row1.addWidget(mode_lbl)

        self._dedup_mode_group = QButtonGroup(self)
        self._rb_smart_code = QRadioButton("智能编码模式")
        self._rb_smart_code.setChecked(True)
        self._rb_column_value = QRadioButton("通用列值模式")
        self._dedup_mode_group.addButton(self._rb_smart_code, 0)
        self._dedup_mode_group.addButton(self._rb_column_value, 1)
        self._rb_smart_code.toggled.connect(self._on_dedup_mode_changed)
        row1.addWidget(self._rb_smart_code)
        row1.addWidget(self._rb_column_value)

        self._chk_dedup_scan_all = QCheckBox("扫描全部列")
        self._chk_dedup_scan_all.setToolTip("自动扫描每张表格的所有列以提取标准编码")
        self._chk_dedup_scan_all.setChecked(True)
        self._chk_dedup_scan_all.toggled.connect(self._on_dedup_mode_changed)
        row1.addWidget(self._chk_dedup_scan_all)

        row1.addStretch()

        self._dedup_progress = QProgressBar()
        self._dedup_progress.setFixedWidth(72)
        self._dedup_progress.setFixedHeight(14)
        self._dedup_progress.setTextVisible(False)
        self._dedup_progress.setVisible(False)
        row1.addWidget(self._dedup_progress)

        self._btn_run_dedup = QPushButton("▶ 开始多表查重")
        self._btn_run_dedup.setToolTip("执行多表跨表/表内查重分析")
        self._btn_run_dedup.setProperty("variant", "primary")
        self._btn_run_dedup.setStyleSheet("padding: 5px 12px;")
        self._btn_run_dedup.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_run_dedup.clicked.connect(self.run_dedup)
        row1.addWidget(self._btn_run_dedup)
        cfg_layout.addLayout(row1)

        # Optional column-selector row (visible in column_value mode or targeted smart_code mode)
        self._dedup_col_row_widget = QWidget()
        col_row = QHBoxLayout(self._dedup_col_row_widget)
        col_row.setContentsMargins(0, 0, 0, 0)
        col_row.setSpacing(8)

        self._dedup_col_lbl = QLabel("关键列：")
        self._dedup_col_lbl.setProperty("role", "subtitle")
        col_row.addWidget(self._dedup_col_lbl)

        self._dedup_col_combo = QComboBox()
        self._dedup_col_combo.setMinimumWidth(140)
        self._dedup_col_combo.setMaximumWidth(240)
        col_row.addWidget(self._dedup_col_combo)

        self._btn_dedup_per_table = QPushButton("按表指定")
        self._btn_dedup_per_table.setStyleSheet("padding: 4px 10px; font-size: 12px;")
        self._btn_dedup_per_table.setToolTip("为不同表分别选择关键列或组合多列键")
        self._btn_dedup_per_table.clicked.connect(self._open_dedup_key_dialog)
        col_row.addWidget(self._btn_dedup_per_table)
        col_row.addStretch()
        cfg_layout.addWidget(self._dedup_col_row_widget)

        # Row 2: Matching options + participating count
        row2 = QHBoxLayout()
        row2.setSpacing(14)

        self._chk_dedup_ignore_case = QCheckBox("忽略大小写")
        self._chk_dedup_ignore_case.setChecked(True)
        row2.addWidget(self._chk_dedup_ignore_case)

        self._chk_dedup_strip = QCheckBox("忽略首尾空格")
        self._chk_dedup_strip.setChecked(True)
        row2.addWidget(self._chk_dedup_strip)

        self._chk_dedup_ignore_punct = QCheckBox("忽略标点符号")
        self._chk_dedup_ignore_punct.setChecked(False)
        row2.addWidget(self._chk_dedup_ignore_punct)

        self._chk_dedup_intra = QCheckBox("包含表内自查")
        self._chk_dedup_intra.setChecked(True)
        row2.addWidget(self._chk_dedup_intra)

        row2.addStretch()

        self._dedup_scope_lbl = QLabel("已勾选 0 张表参与")
        self._dedup_scope_lbl.setProperty("role", "subtitle")
        row2.addWidget(self._dedup_scope_lbl)

        cfg_layout.addLayout(row2)
        layout.addWidget(cfg_card)

        # Summary metric cards (6 cards)
        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(8)
        self._card_d_tables = _CompactMetricCard("参与表数", "0", "#2563EB")
        self._card_d_rows = _CompactMetricCard("总行数", "0", "#64748B")
        self._card_d_unique = _CompactMetricCard("唯一键数", "0", "#059669")
        self._card_d_cross = _CompactMetricCard("跨表重复组", "0", "#D97706")
        self._card_d_intra = _CompactMetricCard("表内重复组", "0", "#7C3AED")
        self._card_d_inter = _CompactMetricCard("全表交集数", "0", "#0891B2")

        for card in (
            self._card_d_tables,
            self._card_d_rows,
            self._card_d_unique,
            self._card_d_cross,
            self._card_d_intra,
            self._card_d_inter,
        ):
            metrics_row.addWidget(card, 1)
        layout.addLayout(metrics_row)

        # Search & Report export bar
        action_row = QHBoxLayout()
        action_row.setSpacing(6)

        self._dedup_search = QLineEdit()
        self._dedup_search.setPlaceholderText("🔍 筛选当前查重结果...")
        self._dedup_search.setClearButtonEnabled(True)
        self._dedup_search.setMinimumWidth(130)
        self._dedup_search.textChanged.connect(lambda _: self._dedup_search_timer.start())
        action_row.addWidget(self._dedup_search, 1)

        self._btn_dedup_export_xlsx = QPushButton("Excel 报告")
        self._btn_dedup_export_xlsx.setToolTip("导出完整多 Sheet Excel 查重报告 (.xlsx)")
        self._btn_dedup_export_xlsx.setProperty("variant", "primary")
        self._btn_dedup_export_xlsx.setStyleSheet("padding: 5px 10px; font-size: 12px;")
        self._btn_dedup_export_xlsx.setEnabled(False)
        self._btn_dedup_export_xlsx.clicked.connect(self._export_dedup_excel)
        action_row.addWidget(self._btn_dedup_export_xlsx)

        self._btn_dedup_export_docx = QPushButton("Word 报告")
        self._btn_dedup_export_docx.setToolTip("导出格式化 Word 查重报告 (.docx)")
        self._btn_dedup_export_docx.setStyleSheet("padding: 5px 10px; font-size: 12px;")
        self._btn_dedup_export_docx.setEnabled(False)
        self._btn_dedup_export_docx.clicked.connect(self._export_dedup_word)
        action_row.addWidget(self._btn_dedup_export_docx)

        self._btn_dedup_export_csv = QPushButton("视图 CSV")
        self._btn_dedup_export_csv.setToolTip("导出当前标签页筛选后的 CSV 数据")
        self._btn_dedup_export_csv.setStyleSheet("padding: 5px 10px; font-size: 12px;")
        self._btn_dedup_export_csv.setEnabled(False)
        self._btn_dedup_export_csv.clicked.connect(self._export_dedup_current_csv)
        action_row.addWidget(self._btn_dedup_export_csv)

        layout.addLayout(action_row)

        # 5 Result sub-tabs
        self._dedup_result_tabs = QTabWidget()
        self._tv_dedup_cross = self._make_table_view(self._dedup_cross_model)
        self._tv_dedup_intra = self._make_table_view(self._dedup_intra_model)
        self._tv_dedup_inter = self._make_table_view(self._dedup_inter_model)
        self._tv_dedup_unique = self._make_table_view(self._dedup_unique_model)
        self._tv_dedup_unrec = self._make_table_view(self._dedup_unrec_model)

        self._dedup_result_tabs.addTab(self._tv_dedup_cross, "跨表重复明细 (0)")
        self._dedup_result_tabs.addTab(self._tv_dedup_intra, "表内重复明细 (0)")
        self._dedup_result_tabs.addTab(self._tv_dedup_inter, "全表共有交集 (0)")
        self._dedup_result_tabs.addTab(self._tv_dedup_unique, "各表独有差集 (0)")
        self._dedup_result_tabs.addTab(self._tv_dedup_unrec, "未识别行 (0)")
        self._dedup_result_tabs.currentChanged.connect(lambda _: self._apply_dedup_filter())

        layout.addWidget(self._dedup_result_tabs, 1)
        return tab

    def _on_dedup_mode_changed(self) -> None:
        is_smart = self._rb_smart_code.isChecked()
        self._chk_dedup_scan_all.setVisible(is_smart)
        show_col = (not is_smart) or (not self._chk_dedup_scan_all.isChecked())
        self._dedup_col_row_widget.setVisible(show_col)
        self._chk_dedup_ignore_punct.setVisible(not is_smart)

    def _open_dedup_key_dialog(self) -> None:
        tables = self.checked_tables()
        if not tables:
            self.ctx.notify("请先在左侧勾选至少 1 张表格", "warning")
            return
        dlg = _PerTableKeyDialog(tables, self._dedup_custom_keys, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._dedup_custom_keys.update(dlg.get_key_mapping())
            self._update_custom_key_button_labels()
            self.ctx.notify("已更新各表查重关键列配置", "success")

    def _build_dedup_config(self, tables: list[DataTable]) -> TableDedupConfig:
        mode = "smart_code" if self._rb_smart_code.isChecked() else "column_value"
        scan_all = self._chk_dedup_scan_all.isChecked()
        key_columns: dict[str, list[str]] = {}

        if mode == "column_value" or not scan_all:
            selected_col = self._dedup_col_combo.currentData() or ""
            for tbl in tables:
                custom = [c for c in self._dedup_custom_keys.get(tbl.table_id, []) if c in tbl.headers]
                if custom:
                    key_columns[tbl.table_id] = custom
                elif selected_col and selected_col in tbl.headers:
                    key_columns[tbl.table_id] = [selected_col]
                elif tbl.headers:
                    key_columns[tbl.table_id] = [tbl.headers[0]]

        return TableDedupConfig(
            mode=mode,
            key_columns=key_columns,
            ignore_case=self._chk_dedup_ignore_case.isChecked(),
            strip_whitespace=self._chk_dedup_strip.isChecked(),
            ignore_punctuation=self._chk_dedup_ignore_punct.isChecked(),
            include_intra_table=self._chk_dedup_intra.isChecked(),
            include_cross_table=True,
            scan_all_columns_for_code=scan_all if mode == "smart_code" else False,
        )

    def run_dedup(self) -> None:
        """Run multi-table deduplication in a background WorkerThread."""
        tables = self.checked_tables()
        if not tables:
            self.ctx.notify("请先在左侧勾选至少 1 张表格再执行查重", "warning")
            return
        if self._dedup_worker is not None and self._dedup_worker.isRunning():
            self.ctx.notify("多表查重正在运行中，请稍候...", "warning")
            return

        config = self._build_dedup_config(tables)
        parser_engine = self.ctx.parser

        self._btn_run_dedup.setEnabled(False)
        self._dedup_progress.setVisible(True)
        self._dedup_progress.setRange(0, 0)

        def _task() -> TableDedupReport:
            return run_multi_table_dedup(tables, config, parser_engine=parser_engine)

        self._dedup_worker = WorkerThread(_task, parent=self)
        self._dedup_worker.succeeded.connect(self._on_dedup_finished)
        self._dedup_worker.failed.connect(self._on_dedup_failed)
        self._dedup_worker.start()

    def _on_dedup_finished(self, report: TableDedupReport) -> None:
        self._btn_run_dedup.setEnabled(True)
        self._dedup_progress.setVisible(False)
        self.load_dedup_report(report)
        self.ctx.notify(
            f"多表查重完成：跨表重复 {len(report.cross_table_groups)} 组，表内重复 {len(report.intra_table_groups)} 组",
            "success",
        )

    def _on_dedup_failed(self, detail: str) -> None:
        self._btn_run_dedup.setEnabled(True)
        self._dedup_progress.setVisible(False)
        ScrollableDetailDialog("多表查重出错", "执行多表查重时发生错误：", detail, parent=self).exec()

    def load_dedup_report(self, report: TableDedupReport) -> None:
        """Populate metric cards and all 5 result sub-tabs from a TableDedupReport."""
        self._last_dedup_report = report

        # Metric cards
        self._card_d_tables.set_value(f"{len(report.table_names):,}")
        self._card_d_rows.set_value(f"{report.total_rows:,}")
        self._card_d_unique.set_value(f"{report.unique_key_count:,}")
        self._card_d_cross.set_value(f"{len(report.cross_table_groups):,}")
        self._card_d_intra.set_value(f"{len(report.intra_table_groups):,}")
        self._card_d_inter.set_value(f"{len(report.intersection_keys):,}")

        # 1. Cross-table duplicate rows
        cross_headers = ["关键值 / 编码", "重复表数", "出现次数", "来源表", "原行号", "版本标签", "分集", "行内容摘要"]
        cross_rows: list[list[str]] = []
        cross_tints: dict[int, str] = {}
        for g_idx, grp in enumerate(report.cross_table_groups):
            tone = "cross_dup" if g_idx % 2 == 0 else "info"
            for ref in grp.rows:
                row_idx = len(cross_rows)
                cross_rows.append(
                    [
                        grp.key_value,
                        str(grp.table_count),
                        str(grp.total_rows),
                        ref.table_name,
                        str(ref.row_index),
                        ref.tags or "-",
                        ref.part or "-",
                        _format_row_summary(ref),
                    ]
                )
                cross_tints[row_idx] = tone
        self._dedup_cross_model.set_data(
            cross_headers,
            cross_rows,
            row_tints=cross_tints,
            highlight_columns={0},
            mono_columns={0},
            center_columns={1, 2, 4, 6},
        )
        self._auto_size_columns(self._tv_dedup_cross, self._dedup_cross_model)

        # 2. Intra-table duplicate rows
        intra_headers = ["关键值 / 编码", "来源表", "表内重复数", "原行号", "版本标签", "分集", "行内容摘要"]
        intra_rows: list[list[str]] = []
        intra_tints: dict[int, str] = {}
        for grp in report.intra_table_groups:
            for ref in grp.rows:
                r_idx = len(intra_rows)
                intra_rows.append(
                    [
                        grp.key_value,
                        ref.table_name,
                        str(grp.total_rows),
                        str(ref.row_index),
                        ref.tags or "-",
                        ref.part or "-",
                        _format_row_summary(ref),
                    ]
                )
                intra_tints[r_idx] = "intra_dup"
        self._dedup_intra_model.set_data(
            intra_headers,
            intra_rows,
            row_tints=intra_tints,
            highlight_columns={0},
            mono_columns={0},
            center_columns={2, 3, 5},
        )
        self._auto_size_columns(self._tv_dedup_intra, self._dedup_intra_model)

        # 3. Intersection rows
        inter_headers = ["关键值 / 编码", "来源表", "原行号", "版本标签", "分集", "行内容摘要"]
        inter_rows: list[list[str]] = []
        inter_tints: dict[int, str] = {}
        for ref in report.intersection_rows:
            r_idx = len(inter_rows)
            inter_rows.append(
                [
                    ref.key_value,
                    ref.table_name,
                    str(ref.row_index),
                    ref.tags or "-",
                    ref.part or "-",
                    _format_row_summary(ref),
                ]
            )
            inter_tints[r_idx] = "intersection"
        self._dedup_inter_model.set_data(
            inter_headers,
            inter_rows,
            row_tints=inter_tints,
            highlight_columns={0},
            mono_columns={0},
            center_columns={2, 4},
        )
        self._auto_size_columns(self._tv_dedup_inter, self._dedup_inter_model)

        # 4. Per-table exclusive rows
        uniq_headers = ["来源表", "关键值 / 编码", "原行号", "版本标签", "分集", "行内容摘要"]
        uniq_rows: list[list[str]] = []
        for _tid, refs in report.unique_by_table.items():
            for ref in refs:
                uniq_rows.append(
                    [
                        ref.table_name,
                        ref.key_value,
                        str(ref.row_index),
                        ref.tags or "-",
                        ref.part or "-",
                        _format_row_summary(ref),
                    ]
                )
        self._dedup_unique_model.set_data(
            uniq_headers,
            uniq_rows,
            highlight_columns={1},
            mono_columns={1},
            center_columns={2, 4},
        )
        self._auto_size_columns(self._tv_dedup_unique, self._dedup_unique_model)

        # 5. Unrecognized rows
        unrec_headers = ["来源表", "原行号", "行内容摘要"]
        unrec_rows = [
            [ref.table_name, str(ref.row_index), _format_row_summary(ref, max_chars=240)]
            for ref in report.unrecognized_rows
        ]
        self._dedup_unrec_model.set_data(
            unrec_headers,
            unrec_rows,
            center_columns={1},
        )
        self._auto_size_columns(self._tv_dedup_unrec, self._dedup_unrec_model)

        # Tab labels
        self._dedup_result_tabs.setTabText(0, f"跨表重复明细 ({len(report.cross_table_groups)}组/{len(cross_rows)}行)")
        self._dedup_result_tabs.setTabText(1, f"表内重复明细 ({len(report.intra_table_groups)}组/{len(intra_rows)}行)")
        self._dedup_result_tabs.setTabText(2, f"全表共有交集 ({len(report.intersection_keys)}键)")
        self._dedup_result_tabs.setTabText(3, f"各表独有差集 ({len(uniq_rows)}行)")
        self._dedup_result_tabs.setTabText(4, f"未识别行 ({len(unrec_rows)}行)")

        self._btn_dedup_export_xlsx.setEnabled(True)
        self._btn_dedup_export_docx.setEnabled(True)
        self._btn_dedup_export_csv.setEnabled(True)

        self._apply_dedup_filter()

        if cross_rows:
            self._dedup_result_tabs.setCurrentIndex(0)
        elif intra_rows:
            self._dedup_result_tabs.setCurrentIndex(1)

    def _active_dedup_model(self) -> tuple[DynamicTableModel, str]:
        idx = self._dedup_result_tabs.currentIndex()
        mapping = [
            (self._dedup_cross_model, "跨表重复明细"),
            (self._dedup_intra_model, "表内重复明细"),
            (self._dedup_inter_model, "全表共有交集"),
            (self._dedup_unique_model, "各表独有差集"),
            (self._dedup_unrec_model, "未识别行"),
        ]
        if 0 <= idx < len(mapping):
            return mapping[idx]
        return mapping[0]

    def _apply_dedup_filter(self) -> None:
        query = self._dedup_search.text()
        model, _ = self._active_dedup_model()
        model.set_filter(query)

    def _reset_dedup_ui(self) -> None:
        for card in (
            self._card_d_tables,
            self._card_d_rows,
            self._card_d_unique,
            self._card_d_cross,
            self._card_d_intra,
            self._card_d_inter,
        ):
            card.set_value("0")
        self._dedup_result_tabs.setTabText(0, "跨表重复明细 (0)")
        self._dedup_result_tabs.setTabText(1, "表内重复明细 (0)")
        self._dedup_result_tabs.setTabText(2, "全表共有交集 (0)")
        self._dedup_result_tabs.setTabText(3, "各表独有差集 (0)")
        self._dedup_result_tabs.setTabText(4, "未识别行 (0)")
        self._btn_dedup_export_xlsx.setEnabled(False)
        self._btn_dedup_export_docx.setEnabled(False)
        self._btn_dedup_export_csv.setEnabled(False)

    def _export_dedup_excel(self) -> None:
        if not self._last_dedup_report:
            return
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出完整多 Sheet Excel 查重报告",
            str(self._default_export_dir() / "多表查重完整报告.xlsx"),
            "Excel 工作簿 (*.xlsx)",
        )
        if not save_path:
            return
        self._remember_export_dir(save_path)
        try:
            merged_tbl = self._last_merge_result.merged_table if self._last_merge_result else None
            export_dedup_report_excel(self._last_dedup_report, save_path, merged_table=merged_tbl)
            self.ctx.notify(f"已导出多 Sheet Excel 查重报告：{Path(save_path).name}", "success")
        except ExportError as exc:
            ScrollableDetailDialog("导出报告失败", str(exc), parent=self).exec()
        except Exception as exc:
            ScrollableDetailDialog("导出报告异常", "导出 Excel 查重报告时发生错误：", str(exc), parent=self).exec()

    def _export_dedup_word(self) -> None:
        if not self._last_dedup_report:
            return
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出 Word 查重报告",
            str(self._default_export_dir() / "多表查重报告.docx"),
            "Word 文档 (*.docx)",
        )
        if not save_path:
            return
        self._remember_export_dir(save_path)
        try:
            export_dedup_report_word(self._last_dedup_report, save_path)
            self.ctx.notify(f"已导出 Word 查重报告：{Path(save_path).name}", "success")
        except ExportError as exc:
            ScrollableDetailDialog("导出 Word 报告失败", str(exc), parent=self).exec()
        except Exception as exc:
            ScrollableDetailDialog("导出 Word 报告异常", "导出 Word 查重报告时发生错误：", str(exc), parent=self).exec()

    def _export_dedup_current_csv(self) -> None:
        model, tab_name = self._active_dedup_model()
        if model.columnCount() == 0:
            self.ctx.notify("当前查重视图暂无数据可导出", "warning")
            return
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出当前查重视图为 CSV",
            str(self._default_export_dir() / f"{tab_name}.csv"),
            "CSV 表格 (*.csv)",
        )
        if not save_path:
            return
        self._remember_export_dir(save_path)
        try:
            view_table = DataTable(
                table_id=generate_table_id(),
                name=tab_name,
                headers=model.headers(),
                rows=model.visible_rows(),
                source_type="merged",
            )
            export_table_csv(view_table, save_path)
            self.ctx.notify(f"已导出当前视图 CSV：{Path(save_path).name}", "success")
        except ExportError as exc:
            ScrollableDetailDialog("导出 CSV 失败", str(exc), parent=self).exec()
        except Exception as exc:
            ScrollableDetailDialog("导出 CSV 异常", "导出当前视图 CSV 时发生错误：", str(exc), parent=self).exec()
