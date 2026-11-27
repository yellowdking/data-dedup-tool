"""Multi-Table Deduplication sub-tab mixin for TablesView (多表查重)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QModelIndex, QSize, Qt
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
    QMenu,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.table_data import (
    DataTable,
    TableDedupConfig,
    TableDedupReport,
    TableRowRef,
    generate_table_id,
)
from src.core.table_engine import run_multi_table_dedup
from src.core.table_exporter import (
    export_dedup_report_excel,
    export_dedup_report_word,
    export_table_csv,
)
from src.ui.table_header import ColumnSpec, TableHeaderManager
from src.ui.table_preview_model import DynamicTableModel
from src.ui.tables_widgets import (
    _InlineMetricBar,
    _PerTableKeyDialog,
    _format_row_summary,
    _set_leading_search_icon,
)
from src.ui.widgets import EmptyStateWidget, ScrollableDetailDialog

def _col(
    key: str,
    label: str,
    width: int = 120,
    vis: bool = True,
    mw: int = 56,
    lock: bool = False,
    sw: float = 0.0,
    cat: str = "basic",
) -> ColumnSpec:
    return ColumnSpec(key, label, width, vis, mw, True, lock, sw, cat)


# Persistent fixed-schema column specifications (hide 版本标签 / 分集 by default)
_DEDUP_CROSS_SPECS: tuple[ColumnSpec, ...] = (
    _col("key", "关键值 / 编码", 132, True, 90, True),
    _col("table_count", "重复表数", 74, True, 60),
    _col("occurrences", "出现次数", 74, True, 60),
    _col("source_table", "来源表", 140, True, 84),
    _col("row_index", "原行号", 66),
    _col("tags", "版本标签", 110, False, 70, False, 0.0, "advanced"),
    _col("part", "分集", 70, False, 54, False, 0.0, "advanced"),
    _col("summary", "行内容摘要", 320, True, 120, False, 1.0),
)

_DEDUP_INTRA_SPECS: tuple[ColumnSpec, ...] = (
    _col("key", "关键值 / 编码", 148, True, 90, True),
    _col("source_table", "来源表", 150, True, 84),
    _col("occurrences", "表内重复数", 86, True, 68),
    _col("row_index", "原行号", 70),
    _col("tags", "版本标签", 110, False, 70, False, 0.0, "advanced"),
    _col("part", "分集", 70, False, 54, False, 0.0, "advanced"),
    _col("summary", "行内容摘要", 290, True, 120, False, 1.0),
)

_DEDUP_INTER_SPECS: tuple[ColumnSpec, ...] = (
    _col("key", "关键值 / 编码", 152, True, 90, True),
    _col("source_table", "来源表", 156, True, 84),
    _col("row_index", "原行号", 72),
    _col("tags", "版本标签", 110, False, 70, False, 0.0, "advanced"),
    _col("part", "分集", 70, False, 54, False, 0.0, "advanced"),
    _col("summary", "行内容摘要", 300, True, 120, False, 1.0),
)

_DEDUP_UNIQUE_SPECS: tuple[ColumnSpec, ...] = (
    _col("source_table", "来源表", 152, True, 84),
    _col("key", "关键值 / 编码", 152, True, 90, True),
    _col("row_index", "原行号", 72),
    _col("tags", "版本标签", 110, False, 70, False, 0.0, "advanced"),
    _col("part", "分集", 70, False, 54, False, 0.0, "advanced"),
    _col("summary", "行内容摘要", 300, True, 120, False, 1.0),
)

_DEDUP_UNREC_SPECS: tuple[ColumnSpec, ...] = (
    _col("source_table", "来源表", 168, True, 90, True),
    _col("row_index", "原行号", 78),
    _col("summary", "行内容摘要", 360, True, 140, False, 1.0),
)

_DEDUP_ALL_SPECS: tuple[tuple[ColumnSpec, ...], ...] = (
    _DEDUP_CROSS_SPECS,
    _DEDUP_INTRA_SPECS,
    _DEDUP_INTER_SPECS,
    _DEDUP_UNIQUE_SPECS,
    _DEDUP_UNREC_SPECS,
)


class TablesDedupTabMixin:
    """Mixin implementing Sub-Tab 2 (多表查重) for TablesView."""

    def _build_dedup_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        theme = self.ctx.config.theme

        # Single compact config card (no nested borders)
        cfg_card = QFrame()
        cfg_card.setProperty("card", True)
        cfg_layout = QVBoxLayout(cfg_card)
        cfg_layout.setContentsMargins(12, 9, 12, 9)
        cfg_layout.setSpacing(7)

        # Row 1: Mode radios + scan-all + progress + cancel + primary CTA
        row1 = QHBoxLayout()
        row1.setSpacing(10)

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
        self._dedup_progress.setFixedHeight(12)
        self._dedup_progress.setTextVisible(False)
        self._dedup_progress.setVisible(False)
        row1.addWidget(self._dedup_progress)

        self._btn_cancel_dedup = QPushButton("取消")
        self._btn_cancel_dedup.setProperty("compact", True)
        self._btn_cancel_dedup.setProperty("variant", "danger-outline")
        self._btn_cancel_dedup.setVisible(False)
        self._btn_cancel_dedup.clicked.connect(self._cancel_dedup)
        row1.addWidget(self._btn_cancel_dedup)

        self._btn_run_dedup = QPushButton("开始多表查重")
        self._btn_run_dedup.setToolTip("执行多表跨表/表内查重分析")
        self._btn_run_dedup.setProperty("variant", "primary")
        self._btn_run_dedup.setProperty("compact", True)
        self._btn_run_dedup.setIconSize(QSize(14, 14))
        self._btn_run_dedup.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_run_dedup.clicked.connect(self.run_dedup)
        row1.addWidget(self._btn_run_dedup)
        cfg_layout.addLayout(row1)

        # Optional column-selector row
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
        self._btn_dedup_per_table.setProperty("compact", True)
        self._btn_dedup_per_table.setToolTip("为不同表分别配置有序关键列或组合多列键")
        self._btn_dedup_per_table.clicked.connect(self._open_dedup_key_dialog)
        col_row.addWidget(self._btn_dedup_per_table)

        self._dedup_custom_key_badge = QLabel("已覆盖下拉框")
        self._dedup_custom_key_badge.setProperty("badge", "warning")
        self._dedup_custom_key_badge.setToolTip("已启用按表自定义关键列配置，将优先于左侧统一关键列下拉框")
        self._dedup_custom_key_badge.setVisible(False)
        col_row.addWidget(self._dedup_custom_key_badge)

        self._btn_dedup_clear_keys = QPushButton("恢复默认")
        self._btn_dedup_clear_keys.setProperty("compact", True)
        self._btn_dedup_clear_keys.setProperty("variant", "ghost")
        self._btn_dedup_clear_keys.setToolTip("清除按表自定义关键列配置")
        self._btn_dedup_clear_keys.setVisible(False)
        self._btn_dedup_clear_keys.clicked.connect(self._clear_dedup_custom_keys)
        col_row.addWidget(self._btn_dedup_clear_keys)

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
        self._dedup_scope_lbl.setProperty("role", "muted")
        row2.addWidget(self._dedup_scope_lbl)

        cfg_layout.addLayout(row2)
        layout.addWidget(cfg_card)

        # Single 32px crisp inline metric bar (replaces 6 heavy KPI cards)
        self._dedup_metric_bar = _InlineMetricBar(self, theme=theme)
        self._dedup_metric_cards: list[Any] = []
        layout.addWidget(self._dedup_metric_bar)

        # Search & Consolidated Report Export bar
        action_row = QHBoxLayout()
        action_row.setContentsMargins(2, 0, 2, 0)
        action_row.setSpacing(8)

        self._dedup_search = QLineEdit()
        self._dedup_search.setPlaceholderText("筛选当前查重结果（双击结果行定位原表）...")
        self._dedup_search.setClearButtonEnabled(True)
        self._dedup_search.setMinimumWidth(160)
        _set_leading_search_icon(self._dedup_search, theme)
        self._dedup_search.textChanged.connect(lambda _: self._dedup_search_timer.start())
        action_row.addWidget(self._dedup_search, 1)

        self._dedup_filter_lbl = QLabel("0 行")
        self._dedup_filter_lbl.setProperty("role", "muted")
        action_row.addWidget(self._dedup_filter_lbl)

        self._btn_dedup_col_menu = QPushButton()
        self._btn_dedup_col_menu.setProperty("iconOnly", True)
        self._btn_dedup_col_menu.setIconSize(QSize(15, 15))
        self._btn_dedup_col_menu.setToolTip("自定义当前结果表显示列或恢复默认顺序")
        self._btn_dedup_col_menu.clicked.connect(self._open_active_dedup_col_menu)
        action_row.addWidget(self._btn_dedup_col_menu)

        self._btn_dedup_export = QPushButton("导出报告  ")
        self._btn_dedup_export.setProperty("compact", True)
        self._btn_dedup_export.setIconSize(QSize(14, 14))
        self._btn_dedup_export.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_dedup_export.setEnabled(False)
        self._btn_dedup_export.setToolTip("导出多表查重报告 (Excel / Word / 当前视图 CSV)")
        dedup_exp_menu = QMenu(self._btn_dedup_export)
        dedup_exp_menu.addAction("Excel 完整查重报告 (.xlsx)", self._export_dedup_excel)
        dedup_exp_menu.addAction("Word 查重报告 (.docx)", self._export_dedup_word)
        dedup_exp_menu.addSeparator()
        dedup_exp_menu.addAction("当前视图 CSV (.csv)", self._export_dedup_current_csv)
        self._btn_dedup_export.setMenu(dedup_exp_menu)
        action_row.addWidget(self._btn_dedup_export)

        layout.addLayout(action_row)

        # Stacked EmptyState vs 5 Result sub-tabs (concise labels preventing '< >' overflow)
        self._dedup_stack = QStackedWidget()
        self._dedup_empty = EmptyStateWidget(
            "git-compare",
            "准备执行多表查重",
            "在左侧勾选 1 张或多张表格，点击上方「开始多表查重」分析跨表重复、表内重复、交集与差集",
            theme=theme,
            parent=self,
        )
        self._dedup_stack.addWidget(self._dedup_empty)

        self._dedup_result_tabs = QTabWidget()
        self._dedup_result_tabs.setProperty("subtabs", True)
        self._dedup_result_tabs.tabBar().setUsesScrollButtons(False)

        self._tv_dedup_cross, mgr_cross = self._make_table_view(
            self._dedup_cross_model,
            fixed_table_id="tables_dedup_cross",
            fixed_columns=_DEDUP_CROSS_SPECS,
        )
        self._tv_dedup_intra, mgr_intra = self._make_table_view(
            self._dedup_intra_model,
            fixed_table_id="tables_dedup_intra",
            fixed_columns=_DEDUP_INTRA_SPECS,
        )
        self._tv_dedup_inter, mgr_inter = self._make_table_view(
            self._dedup_inter_model,
            fixed_table_id="tables_dedup_inter",
            fixed_columns=_DEDUP_INTER_SPECS,
        )
        self._tv_dedup_unique, mgr_unique = self._make_table_view(
            self._dedup_unique_model,
            fixed_table_id="tables_dedup_unique",
            fixed_columns=_DEDUP_UNIQUE_SPECS,
        )
        self._tv_dedup_unrec, mgr_unrec = self._make_table_view(
            self._dedup_unrec_model,
            fixed_table_id="tables_dedup_unrec",
            fixed_columns=_DEDUP_UNREC_SPECS,
        )

        self._dedup_views = [
            self._tv_dedup_cross,
            self._tv_dedup_intra,
            self._tv_dedup_inter,
            self._tv_dedup_unique,
            self._tv_dedup_unrec,
        ]
        self._dedup_header_mgrs: list[Optional[TableHeaderManager]] = [
            mgr_cross,
            mgr_intra,
            mgr_inter,
            mgr_unique,
            mgr_unrec,
        ]
        for tab_idx, tv in enumerate(self._dedup_views):
            tv.doubleClicked.connect(
                lambda index, tidx=tab_idx: self._on_dedup_row_double_clicked(tidx, index)
            )

        self._dedup_result_tabs.addTab(self._tv_dedup_cross, "跨表重复 (0)")
        self._dedup_result_tabs.addTab(self._tv_dedup_intra, "表内重复 (0)")
        self._dedup_result_tabs.addTab(self._tv_dedup_inter, "全表交集 (0)")
        self._dedup_result_tabs.addTab(self._tv_dedup_unique, "独有差集 (0)")
        self._dedup_result_tabs.addTab(self._tv_dedup_unrec, "未识别 (0)")
        self._dedup_result_tabs.currentChanged.connect(self._on_dedup_subtab_changed)

        self._dedup_stack.addWidget(self._dedup_result_tabs)
        self._dedup_stack.setCurrentIndex(0)
        layout.addWidget(self._dedup_stack, 1)
        return tab

    def _open_active_dedup_col_menu(self) -> None:
        idx = self._dedup_result_tabs.currentIndex()
        mgr = self._dedup_header_mgrs[idx] if 0 <= idx < len(self._dedup_header_mgrs) else None
        self._open_header_menu_below(self._btn_dedup_col_menu, mgr)

    def _clear_dedup_fixed_models(self) -> None:
        models = (
            self._dedup_cross_model,
            self._dedup_intra_model,
            self._dedup_inter_model,
            self._dedup_unique_model,
            self._dedup_unrec_model,
        )
        for model, specs in zip(models, _DEDUP_ALL_SPECS):
            self._clear_fixed_model(model, specs)

    def _on_dedup_mode_changed(self) -> None:
        is_smart = self._rb_smart_code.isChecked()
        self._chk_dedup_scan_all.setVisible(is_smart)
        show_col = (not is_smart) or (not self._chk_dedup_scan_all.isChecked())
        self._dedup_col_row_widget.setVisible(show_col)
        self._chk_dedup_ignore_case.setVisible(not is_smart)
        self._chk_dedup_strip.setVisible(not is_smart)
        self._chk_dedup_ignore_punct.setVisible(not is_smart)

    def _open_dedup_key_dialog(self) -> None:
        tables = self.checked_tables()
        if not tables:
            self.ctx.notify("请先在左侧勾选至少 1 张表格", "warning")
            return
        default_col = str(self._dedup_col_combo.currentData() or "")
        dlg = _PerTableKeyDialog(
            tables,
            self._dedup_custom_keys,
            parent=self,
            default_column=default_col,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.was_cleared:
                self._dedup_custom_keys.clear()
                self._update_custom_key_button_labels()
                self.ctx.notify("已清除查重按表关键列配置，恢复使用统一关键列", "info")
            else:
                self._dedup_custom_keys.update(dlg.get_key_mapping())
                self._update_custom_key_button_labels()
                self.ctx.notify("已更新各表查重关键列配置", "success")

    def _clear_dedup_custom_keys(self) -> None:
        self._dedup_custom_keys.clear()
        self._update_custom_key_button_labels()
        self.ctx.notify("已恢复默认查重关键列", "info")

    def _validate_dedup_key_columns(self, tables: list[DataTable]) -> bool:
        """Warn if a participating table lacks the selected key column and has no custom override."""
        mode = "smart_code" if self._rb_smart_code.isChecked() else "column_value"
        scan_all = self._chk_dedup_scan_all.isChecked()
        if mode == "smart_code" and scan_all:
            return True

        selected_col = str(self._dedup_col_combo.currentData() or "")
        missing_tables: list[str] = []
        for tbl in tables:
            custom = [c for c in self._dedup_custom_keys.get(tbl.table_id, []) if c in tbl.headers]
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
            "若继续执行，缺失该列的表格将默认回退到第 1 列进行比对。",
            "建议先通过「列管理」重命名列名，或点击「按表指定」为各表分别映射关键列。",
        ]
        dlg = ScrollableDetailDialog(
            title="部分表格缺少所选关键列",
            summary=f"有 {len(missing_tables)} 张参与表不包含列「{selected_col}」，是否继续执行查重？",
            detail_text="\n".join(detail_lines),
            confirm_mode=True,
            confirm_label="仍要继续",
            parent=self,
        )
        return dlg.exec() == QDialog.DialogCode.Accepted

    def _build_dedup_config(
        self,
        tables: list[DataTable],
        *,
        force_intra: bool = False,
    ) -> TableDedupConfig:
        mode = "smart_code" if self._rb_smart_code.isChecked() else "column_value"
        scan_all = self._chk_dedup_scan_all.isChecked()
        key_columns: dict[str, list[str]] = {}

        if mode == "column_value" or not scan_all:
            selected_col = str(self._dedup_col_combo.currentData() or "")
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
            include_intra_table=True if force_intra else self._chk_dedup_intra.isChecked(),
            include_cross_table=True,
            scan_all_columns_for_code=scan_all if mode == "smart_code" else False,
        )

    def run_dedup(self) -> None:
        """Run multi-table deduplication in a background WorkerThread."""
        tables = self.checked_tables()
        if not tables:
            self.ctx.notify("请先在左侧勾选至少 1 张表格再执行查重", "warning")
            return
        if self._is_worker_running(self._dedup_worker):
            self.ctx.notify("多表查重正在运行中，请稍候...", "warning")
            return
        if not self._validate_dedup_key_columns(tables):
            return

        config = self._build_dedup_config(tables)
        self._start_dedup_run(tables, config)

    def _run_single_table_dedup(self, table: Optional[DataTable] = None) -> None:
        """Quickly deduplicate a single table (intra-table analysis) without altering checkmarks."""
        target = table or self.active_table()
        if target is None:
            self.ctx.notify("请先选择一张表格", "warning")
            return
        if self._is_worker_running(self._dedup_worker):
            self.ctx.notify("多表查重正在运行中，请稍候...", "warning")
            return

        config = self._build_dedup_config([target], force_intra=True)
        self.switch_sub_tab(1)
        self._start_dedup_run([target], config, single_label=target.name)

    def _start_dedup_run(
        self,
        tables: list[DataTable],
        config: TableDedupConfig,
        single_label: str = "",
    ) -> None:
        snapshots = [t.clone(new_id=t.table_id) for t in tables]
        snapshot_sig = self._compute_tables_snapshot(tables)
        parser_engine = self.ctx.parser

        self._dedup_gen += 1
        gen = self._dedup_gen

        self._dedup_progress.setVisible(True)
        self._dedup_progress.setRange(0, 0)
        self._btn_cancel_dedup.setVisible(True)

        def _task() -> TableDedupReport:
            return run_multi_table_dedup(snapshots, config, parser_engine=parser_engine)

        worker = self._make_worker(_task)
        self._dedup_worker = worker
        worker.succeeded.connect(
            lambda report, g=gen, sig=snapshot_sig, lbl=single_label: self._on_dedup_finished(
                g, sig, lbl, report
            )
        )
        worker.failed.connect(lambda detail, g=gen: self._on_dedup_failed(g, detail))
        if hasattr(worker, "cancelled"):
            worker.cancelled.connect(lambda g=gen: self._on_dedup_cancelled(g))
        worker.finished.connect(lambda w=worker: self._clear_worker_ref("_dedup_worker", w))
        self._sync_controls_state()
        worker.start()

    def _cancel_dedup(self) -> None:
        self._dedup_gen += 1
        worker = self._dedup_worker
        if worker is not None:
            try:
                worker.cancel()
            except RuntimeError:
                pass
        self._dedup_progress.setVisible(False)
        self._btn_cancel_dedup.setVisible(False)
        self._sync_controls_state()
        self.ctx.notify("已请求取消多表查重", "info")

    def _on_dedup_finished(
        self,
        gen: int,
        snapshot_sig: Any,
        single_label: str,
        report: TableDedupReport,
    ) -> None:
        if gen != self._dedup_gen:
            return
        self._dedup_progress.setVisible(False)
        self._btn_cancel_dedup.setVisible(False)
        self.load_dedup_report(report, snapshot=snapshot_sig)
        self._sync_controls_state()
        if single_label:
            self.ctx.notify(
                f"「{single_label}」单表查重完成：表内重复 {len(report.intra_table_groups)} 组，唯一键 {report.unique_key_count:,} 个",
                "success",
            )
        else:
            self.ctx.notify(
                f"多表查重完成：跨表重复 {len(report.cross_table_groups)} 组，表内重复 {len(report.intra_table_groups)} 组",
                "success",
            )

    def _on_dedup_failed(self, gen: int, detail: str) -> None:
        if gen != self._dedup_gen:
            return
        self._dedup_progress.setVisible(False)
        self._btn_cancel_dedup.setVisible(False)
        self._sync_controls_state()
        ScrollableDetailDialog("多表查重出错", "执行多表查重时发生错误：", detail, parent=self).exec()

    def _on_dedup_cancelled(self, gen: int) -> None:
        if gen != self._dedup_gen:
            return
        self._dedup_progress.setVisible(False)
        self._btn_cancel_dedup.setVisible(False)
        self._sync_controls_state()

    def load_dedup_report(
        self,
        report: TableDedupReport,
        snapshot: Optional[Any] = None,
    ) -> None:
        """Populate inline metric bar and lazily bind all 5 result sub-tabs from a TableDedupReport."""
        self._last_dedup_report = report
        self._last_dedup_snapshot = snapshot
        self._dedup_tab_populated.clear()
        self._dedup_tab_refs.clear()
        self._clear_dedup_fixed_models()

        self._dedup_metric_bar.set_values(
            tables=len(report.table_names),
            rows=report.total_rows,
            unique=report.unique_key_count,
            cross=len(report.cross_table_groups),
            intra=len(report.intra_table_groups),
            inter=len(report.intersection_keys),
        )

        cross_row_count = sum(len(g.rows) for g in report.cross_table_groups)
        intra_row_count = sum(len(g.rows) for g in report.intra_table_groups)
        uniq_row_count = sum(len(refs) for refs in report.unique_by_table.values())
        unrec_row_count = len(report.unrecognized_rows)

        # Concise labels keep sub-tabs crisp without '< >' scroll overflow
        self._dedup_result_tabs.setTabText(0, f"跨表重复 ({len(report.cross_table_groups)})")
        self._dedup_result_tabs.setTabToolTip(0, f"跨表重复：{len(report.cross_table_groups)} 组 / {cross_row_count} 行")
        self._dedup_result_tabs.setTabText(1, f"表内重复 ({len(report.intra_table_groups)})")
        self._dedup_result_tabs.setTabToolTip(1, f"表内重复：{len(report.intra_table_groups)} 组 / {intra_row_count} 行")
        self._dedup_result_tabs.setTabText(2, f"全表交集 ({len(report.intersection_keys)})")
        self._dedup_result_tabs.setTabToolTip(2, f"全表共有交集：{len(report.intersection_keys)} 个键")
        self._dedup_result_tabs.setTabText(3, f"独有差集 ({uniq_row_count})")
        self._dedup_result_tabs.setTabToolTip(3, f"各表独有差集：{uniq_row_count} 行")
        self._dedup_result_tabs.setTabText(4, f"未识别 ({unrec_row_count})")
        self._dedup_result_tabs.setTabToolTip(4, f"未识别行：{unrec_row_count} 行")

        target_tab = 0
        if cross_row_count > 0:
            target_tab = 0
        elif intra_row_count > 0:
            target_tab = 1

        self._dedup_result_tabs.blockSignals(True)
        self._dedup_result_tabs.setCurrentIndex(target_tab)
        self._dedup_result_tabs.blockSignals(False)

        self._dedup_stack.setCurrentIndex(1)
        self._ensure_dedup_subtab_populated(target_tab)
        self._apply_dedup_filter()
        self._sync_controls_state()

    def _ensure_dedup_subtab_populated(self, tab_idx: int) -> None:
        """Lazily build the DynamicTableModel rows for the requested dedup result sub-tab."""
        if tab_idx in self._dedup_tab_populated or self._last_dedup_report is None:
            return
        report = self._last_dedup_report
        refs_out: list[TableRowRef] = []

        if tab_idx == 0:
            headers = [s.label for s in _DEDUP_CROSS_SPECS]
            rows: list[list[str]] = []
            tints: dict[int, str] = {}
            for g_idx, grp in enumerate(report.cross_table_groups):
                tone = "cross_dup" if g_idx % 2 == 0 else "info"
                for ref in grp.rows:
                    tints[len(rows)] = tone
                    rows.append([
                        grp.key_value, str(grp.table_count), str(grp.total_rows),
                        ref.table_name, str(ref.row_index), ref.tags or "-", ref.part or "-",
                        _format_row_summary(ref),
                    ])
                    refs_out.append(ref)
            self._dedup_cross_model.set_data(
                headers, rows, row_tints=tints, highlight_columns={0}, mono_columns={0},
                copy_columns={0}, center_columns={1, 2, 4, 6}, reset_filter=True,
            )

        elif tab_idx == 1:
            headers = [s.label for s in _DEDUP_INTRA_SPECS]
            rows, tints = [], {}
            for grp in report.intra_table_groups:
                for ref in grp.rows:
                    tints[len(rows)] = "intra_dup"
                    rows.append([
                        grp.key_value, ref.table_name, str(grp.total_rows),
                        str(ref.row_index), ref.tags or "-", ref.part or "-",
                        _format_row_summary(ref),
                    ])
                    refs_out.append(ref)
            self._dedup_intra_model.set_data(
                headers, rows, row_tints=tints, highlight_columns={0}, mono_columns={0},
                copy_columns={0}, center_columns={2, 3, 5}, reset_filter=True,
            )

        elif tab_idx == 2:
            headers = [s.label for s in _DEDUP_INTER_SPECS]
            rows, tints = [], {}
            for ref in report.intersection_rows:
                tints[len(rows)] = "intersection"
                rows.append([
                    ref.key_value, ref.table_name, str(ref.row_index),
                    ref.tags or "-", ref.part or "-", _format_row_summary(ref),
                ])
                refs_out.append(ref)
            self._dedup_inter_model.set_data(
                headers, rows, row_tints=tints, highlight_columns={0}, mono_columns={0},
                copy_columns={0}, center_columns={2, 4}, reset_filter=True,
            )

        elif tab_idx == 3:
            headers = [s.label for s in _DEDUP_UNIQUE_SPECS]
            rows = []
            for _tid, refs in report.unique_by_table.items():
                for ref in refs:
                    rows.append([
                        ref.table_name, ref.key_value, str(ref.row_index),
                        ref.tags or "-", ref.part or "-", _format_row_summary(ref),
                    ])
                    refs_out.append(ref)
            self._dedup_unique_model.set_data(
                headers, rows, highlight_columns={1}, mono_columns={1},
                copy_columns={1}, center_columns={2, 4}, reset_filter=True,
            )

        elif tab_idx == 4:
            headers = [s.label for s in _DEDUP_UNREC_SPECS]
            rows = []
            for ref in report.unrecognized_rows:
                rows.append([ref.table_name, str(ref.row_index), _format_row_summary(ref, max_chars=240)])
                refs_out.append(ref)
            self._dedup_unrec_model.set_data(headers, rows, center_columns={1}, reset_filter=True)

        mgr = self._dedup_header_mgrs[tab_idx] if 0 <= tab_idx < len(self._dedup_header_mgrs) else None
        if mgr is not None:
            mgr.reapply_sort()

        self._dedup_tab_refs[tab_idx] = refs_out
        self._dedup_tab_populated.add(tab_idx)

    def _on_dedup_subtab_changed(self, tab_idx: int) -> None:
        self._ensure_dedup_subtab_populated(tab_idx)
        self._apply_dedup_filter()

    def _on_dedup_row_double_clicked(self, tab_idx: int, index: QModelIndex) -> None:
        if not index.isValid():
            return
        model, _ = self._active_dedup_model()
        orig_row = model.original_row_index(index.row())
        refs = self._dedup_tab_refs.get(tab_idx, [])
        if 0 <= orig_row < len(refs):
            ref = refs[orig_row]
            self._jump_to_table_row(ref.table_id, ref.row_index, fallback_ref=ref)

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
        idx = self._dedup_result_tabs.currentIndex()
        self._ensure_dedup_subtab_populated(idx)
        query = self._dedup_search.text()
        model, _ = self._active_dedup_model()
        model.set_filter(query)
        self._update_dedup_filter_label()

    def _update_dedup_filter_label(self) -> None:
        model, _ = self._active_dedup_model()
        shown = model.filtered_row_count()
        total = model.total_row_count()
        if model.filter_query():
            self._dedup_filter_lbl.setText(f"筛选命中 {shown:,} / {total:,} 行")
        else:
            self._dedup_filter_lbl.setText(f"共 {total:,} 行")

    def _reset_dedup_ui(self) -> None:
        self._dedup_search_timer.stop()
        self._dedup_search.blockSignals(True)
        self._dedup_search.clear()
        self._dedup_search.blockSignals(False)
        self._dedup_tab_populated.clear()
        self._dedup_tab_refs.clear()

        self._dedup_metric_bar.reset()
        self._dedup_result_tabs.setTabText(0, "跨表重复 (0)")
        self._dedup_result_tabs.setTabText(1, "表内重复 (0)")
        self._dedup_result_tabs.setTabText(2, "全表交集 (0)")
        self._dedup_result_tabs.setTabText(3, "独有差集 (0)")
        self._dedup_result_tabs.setTabText(4, "未识别 (0)")
        self._dedup_filter_lbl.setText("0 行")
        self._btn_dedup_export.setEnabled(False)
        self._dedup_stack.setCurrentIndex(0)

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

        report_ref = self._last_dedup_report
        merged_tbl: Optional[DataTable] = None
        if (
            self._last_merge_result is not None
            and self._last_merge_snapshot is not None
            and self._last_dedup_snapshot is not None
            and self._last_merge_snapshot == self._last_dedup_snapshot
        ):
            merged_tbl = self._last_merge_result.merged_table.clone(
                new_id=self._last_merge_result.merged_table.table_id
            )

        self._start_export_worker(
            lambda: export_dedup_report_excel(report_ref, save_path, merged_table=merged_tbl),
            success_msg=f"已导出多 Sheet Excel 查重报告：{Path(save_path).name}",
            error_title="导出报告失败",
        )

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

        report_ref = self._last_dedup_report
        self._start_export_worker(
            lambda: export_dedup_report_word(report_ref, save_path),
            success_msg=f"已导出 Word 查重报告：{Path(save_path).name}",
            error_title="导出 Word 报告失败",
        )

    def _export_dedup_current_csv(self) -> None:
        idx = self._dedup_result_tabs.currentIndex()
        self._ensure_dedup_subtab_populated(idx)
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

        view_table = DataTable(
            table_id=generate_table_id(),
            name=tab_name,
            headers=model.headers(),
            rows=model.visible_rows(),
            source_type="merged",
        )
        self._start_export_worker(
            lambda: export_table_csv(view_table, save_path),
            success_msg=f"已导出当前视图 CSV：{Path(save_path).name}",
            error_title="导出 CSV 失败",
        )
