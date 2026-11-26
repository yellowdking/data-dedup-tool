"""View 3: 快速查重 — Non-mutating DB comparison & A-vs-B cross comparison."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.dedup import compare_two_sources, compare_with_db
from src.core.exporter import (
    ExportError,
    export_codes_txt,
    export_compare_result_csv,
    export_compare_result_excel,
)
from src.core.models import CompareResult, DataRecord, ImportBatch
from src.core.scanner import scan_file
from src.ui.app_context import AppContext
from src.ui.table_models import CompareTableModel
from src.ui.widgets import DropZoneWidget, ScrollableDetailDialog


class _DroppablePlainTextEdit(QPlainTextEdit):
    """QPlainTextEdit that also accepts dropped TXT/CSV files and emits the path."""

    file_dropped = Signal(str)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and Path(url.toLocalFile()).is_file():
                    event.acceptProposedAction()
                    return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and Path(url.toLocalFile()).is_file():
                    event.acceptProposedAction()
                    self.file_dropped.emit(url.toLocalFile())
                    return
        super().dropEvent(event)


class CompareView(QWidget):
    """Quick comparison view supporting (a) DB non-mutating check and (b) A-vs-B cross-check."""

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx

        # Mode A state
        self._db_compare_result: Optional[CompareResult] = None
        self._model_db_matched = CompareTableModel(with_matches=True, parent=self)
        self._model_db_new = CompareTableModel(with_matches=False, parent=self)
        self._model_db_unrec = CompareTableModel(with_matches=False, parent=self)

        # Mode B state
        self._ab_result: Optional[CompareResult] = None
        self._file_a_path: str = ""
        self._file_b_path: str = ""
        self._model_ab_inter = CompareTableModel(with_matches=False, parent=self)
        self._model_ab_only_a = CompareTableModel(with_matches=False, parent=self)
        self._model_ab_only_b = CompareTableModel(with_matches=False, parent=self)
        self._model_ab_unrec = CompareTableModel(with_matches=False, parent=self)

        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(10)

        self._mode_tabs = QTabWidget()
        self._mode_tabs.addTab(self._build_db_compare_tab(), "与当前库比对（不入库）")
        self._mode_tabs.addTab(self._build_ab_compare_tab(), "双文件交叉比对（A vs B）")
        root.addWidget(self._mode_tabs, 1)

    # ------------------------------------------------------------------
    # Mode A: Compare against current database without mutating
    # ------------------------------------------------------------------

    def _build_db_compare_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # Top input panel
        top_card = QFrame()
        top_card.setProperty("card", True)
        top_layout = QVBoxLayout(top_card)
        top_layout.setContentsMargins(14, 12, 14, 12)
        top_layout.setSpacing(8)

        header_row = QHBoxLayout()
        lbl = QLabel("粘贴多行文件名 / 路径 / 目录树，或直接加载 TXT 文件")
        lbl.setProperty("role", "section")
        header_row.addWidget(lbl)
        header_row.addStretch()

        self._chk_db_ignore_comp = QCheckBox("忽略伴随文件")
        self._chk_db_ignore_comp.setChecked(self.ctx.config.ignore_companion_files)
        header_row.addWidget(self._chk_db_ignore_comp)

        btn_load_txt = QPushButton("从 TXT/CSV 载入")
        btn_load_txt.clicked.connect(self._load_file_to_db_input)
        header_row.addWidget(btn_load_txt)

        btn_clear_input = QPushButton("清空")
        btn_clear_input.setProperty("variant", "ghost")
        btn_clear_input.clicked.connect(lambda: self._db_input.clear())
        header_row.addWidget(btn_clear_input)

        btn_run = QPushButton("开始快速查重")
        btn_run.setProperty("variant", "primary")
        btn_run.clicked.connect(self.run_db_compare)
        header_row.addWidget(btn_run)
        top_layout.addLayout(header_row)

        self._db_input = _DroppablePlainTextEdit()
        self._db_input.file_dropped.connect(self._load_specific_file_to_db_input)
        self._db_input.setPlaceholderText(
            "在此粘贴待比对清单（每行一条），或直接拖拽 TXT/CSV 文件到此文本框，例如：\n"
            "ABC-001.mp4\n"
            "PRJ-0042_4K_CHS.mkv\n"
            "D:\\Archive\\DEV-108\\1.mp4\n"
            "支持直接粘贴 Windows tree /f、Everything 清单或多编码混排文本"
        )
        self._db_input.setMinimumHeight(115)
        top_layout.addWidget(self._db_input)

        splitter.addWidget(top_card)

        # Bottom results panel
        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(0, 4, 0, 0)
        bottom_layout.setSpacing(8)

        action_row = QHBoxLayout()
        self._db_summary = QLabel("等待比对 — 不会修改当前数据库")
        self._db_summary.setProperty("role", "subtitle")
        action_row.addWidget(self._db_summary)
        action_row.addStretch()

        self._btn_copy_new = QPushButton("复制新数据编码")
        self._btn_copy_new.setEnabled(False)
        self._btn_copy_new.clicked.connect(self._copy_not_in_db_codes)
        action_row.addWidget(self._btn_copy_new)

        self._btn_export_db_cmp = QPushButton("导出清单 (.xlsx)")
        self._btn_export_db_cmp.setEnabled(False)
        self._btn_export_db_cmp.clicked.connect(lambda: self._export_compare(self._db_compare_result))
        action_row.addWidget(self._btn_export_db_cmp)

        self._btn_import_new = QPushButton("一键将新数据入库")
        self._btn_import_new.setProperty("variant", "success")
        self._btn_import_new.setEnabled(False)
        self._btn_import_new.clicked.connect(self._import_not_in_db)
        action_row.addWidget(self._btn_import_new)
        bottom_layout.addLayout(action_row)

        self._db_result_tabs = QTabWidget()
        self._db_result_tabs.addTab(self._make_table(self._model_db_matched), "已存在 (0)")
        self._db_result_tabs.addTab(self._make_table(self._model_db_new), "未找到 · 新数据 (0)")
        self._db_result_tabs.addTab(self._make_table(self._model_db_unrec), "未识别 (0)")
        bottom_layout.addWidget(self._db_result_tabs, 1)

        splitter.addWidget(bottom_widget)
        splitter.setSizes([180, 420])
        layout.addWidget(splitter, 1)
        return widget

    def _load_file_to_db_input(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择待比对文件", "", "文本清单 (*.txt *.csv *.efu *.log);;所有文件 (*.*)"
        )
        if not path:
            return
        self._load_specific_file_to_db_input(path)

    def _load_specific_file_to_db_input(self, path: str) -> None:
        from src.core.scanner import read_text_auto_encoding

        content, _ = read_text_auto_encoding(path)
        self._db_input.setPlainText(content)
        self.run_db_compare(source_name=Path(path).name)

    def run_db_compare(self, source_name: str = "快速粘贴") -> None:
        text = self._db_input.toPlainText().strip()
        if not text:
            self.ctx.notify("请先粘贴或载入待比对内容", "warning")
            return
        ignore_comp = self._chk_db_ignore_comp.isChecked()
        comp_exts = self.ctx.get_companion_extensions() if ignore_comp else set()

        parsed = self.ctx.parser.parse_text(
            text,
            source_name=source_name,
            ignore_companions=ignore_comp,
            companion_extensions=comp_exts,
        )
        result = compare_with_db(parsed, self.ctx.db, source_name=source_name)
        self._db_compare_result = result

        self._model_db_matched.set_rows(result.matched_in_db)
        self._model_db_new.set_rows(result.not_in_db)
        self._model_db_unrec.set_rows(result.unrecognized_a)

        self._db_result_tabs.setTabText(0, f"已存在 ({len(result.matched_in_db)})")
        self._db_result_tabs.setTabText(1, f"未找到 · 新数据 ({len(result.not_in_db)})")
        self._db_result_tabs.setTabText(2, f"未识别 ({len(result.unrecognized_a)})")

        self._db_summary.setText(
            f"比对完成：已存在 {len(result.matched_in_db):,} 条 · "
            f"未找到(新数据) {len(result.not_in_db):,} 条 · 未识别 {len(result.unrecognized_a):,} 条"
        )
        has_new = len(result.not_in_db) > 0
        self._btn_import_new.setEnabled(has_new)
        self._btn_copy_new.setEnabled(has_new)
        self._btn_export_db_cmp.setEnabled(True)
        if has_new:
            self._db_result_tabs.setCurrentIndex(1)
        self.ctx.notify("快速查重完成（未修改当前数据库）", "success")

    def _copy_not_in_db_codes(self) -> None:
        if not self._db_compare_result:
            return
        codes = sorted({i.canonical_code for i in self._db_compare_result.not_in_db if i.canonical_code})
        QApplication.clipboard().setText("\n".join(codes))
        self.ctx.notify(f"已复制 {len(codes)} 个新数据编码", "success")

    def _import_not_in_db(self) -> None:
        if not self._db_compare_result or not self._db_compare_result.not_in_db:
            return
        items = self._db_compare_result.not_in_db
        records = [DataRecord.from_parse_result(item) for item in items]
        batch = ImportBatch(
            source_name=self._db_compare_result.source_a_name or "快速查重入库",
            source_type="paste",
            total_count=len(items),
            added_count=len(records),
        )
        saved, _ = self.ctx.db.add_records(records, batch=batch)
        self.ctx.notify(f"已将 {len(saved)} 条新数据写入当前库", "success")
        self.ctx.data_changed.emit()
        self.run_db_compare(source_name=self._db_compare_result.source_a_name or "快速粘贴")

    # ------------------------------------------------------------------
    # Mode B: A-vs-B Cross Comparison
    # ------------------------------------------------------------------

    def _build_ab_compare_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(12)

        self._drop_a = DropZoneWidget(
            title="文件 A（拖拽或选择 TXT/CSV）",
            subtitle="尚未选择文件 A",
            allow_dirs=False,
        )
        self._drop_a.paths_dropped.connect(lambda ps: self._set_ab_file("A", ps[0]))
        btn_a = QPushButton("选择文件 A")
        btn_a.clicked.connect(lambda: self._pick_ab_file("A"))
        self._drop_a.add_action_button(btn_a)
        top_row.addWidget(self._drop_a, 1)

        self._drop_b = DropZoneWidget(
            title="文件 B（拖拽或选择 TXT/CSV）",
            subtitle="尚未选择文件 B",
            allow_dirs=False,
        )
        self._drop_b.paths_dropped.connect(lambda ps: self._set_ab_file("B", ps[0]))
        btn_b = QPushButton("选择文件 B")
        btn_b.clicked.connect(lambda: self._pick_ab_file("B"))
        self._drop_b.add_action_button(btn_b)
        top_row.addWidget(self._drop_b, 1)

        layout.addLayout(top_row)

        ctrl_row = QHBoxLayout()
        self._ab_summary = QLabel("请选择或拖入文件 A 与文件 B，点击「执行交叉比对」")
        self._ab_summary.setProperty("role", "subtitle")
        ctrl_row.addWidget(self._ab_summary)
        ctrl_row.addStretch()

        self._btn_run_ab = QPushButton("执行交叉比对")
        self._btn_run_ab.setProperty("variant", "primary")
        self._btn_run_ab.setEnabled(False)
        self._btn_run_ab.clicked.connect(self.run_ab_compare)
        ctrl_row.addWidget(self._btn_run_ab)

        self._btn_export_ab = QPushButton("导出比对报告 (.xlsx)")
        self._btn_export_ab.setEnabled(False)
        self._btn_export_ab.clicked.connect(lambda: self._export_compare(self._ab_result))
        ctrl_row.addWidget(self._btn_export_ab)

        layout.addLayout(ctrl_row)

        self._ab_result_tabs = QTabWidget()
        self._ab_result_tabs.addTab(self._make_table(self._model_ab_inter), "交集 · 重复 (0)")
        self._ab_result_tabs.addTab(self._make_table(self._model_ab_only_a), "仅 A 有 (0)")
        self._ab_result_tabs.addTab(self._make_table(self._model_ab_only_b), "仅 B 有 (0)")
        self._ab_result_tabs.addTab(self._make_table(self._model_ab_unrec), "未识别 (0)")
        layout.addWidget(self._ab_result_tabs, 1)

        return widget

    def _pick_ab_file(self, side: str) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, f"选择文件 {side}", "", "清单文件 (*.txt *.csv *.efu *.log);;所有文件 (*.*)"
        )
        if path:
            self._set_ab_file(side, path)

    def _set_ab_file(self, side: str, path: str) -> None:
        p = Path(path)
        if side == "A":
            self._file_a_path = str(p)
            self._drop_a._sub_label.setText(f"已选：{p.name}")
        else:
            self._file_b_path = str(p)
            self._drop_b._sub_label.setText(f"已选：{p.name}")
        self._btn_run_ab.setEnabled(bool(self._file_a_path and self._file_b_path))

    def run_ab_compare(self) -> None:
        if not self._file_a_path or not self._file_b_path:
            return
        comp_exts = self.ctx.get_companion_extensions()
        ignore_comp = self.ctx.config.ignore_companion_files

        scanned_a = scan_file(self._file_a_path, companion_extensions=comp_exts)
        scanned_b = scan_file(self._file_b_path, companion_extensions=comp_exts)
        parsed_a = self.ctx.parser.parse_items(scanned_a, ignore_companions=ignore_comp, companion_extensions=comp_exts)
        parsed_b = self.ctx.parser.parse_items(scanned_b, ignore_companions=ignore_comp, companion_extensions=comp_exts)

        result = compare_two_sources(
            parsed_a,
            parsed_b,
            source_a_name=Path(self._file_a_path).name,
            source_b_name=Path(self._file_b_path).name,
        )
        self._ab_result = result

        self._model_ab_inter.set_rows(result.intersection_a)
        self._model_ab_only_a.set_rows(result.only_in_a)
        self._model_ab_only_b.set_rows(result.only_in_b)
        self._model_ab_unrec.set_rows(result.unrecognized_a + result.unrecognized_b)

        self._ab_result_tabs.setTabText(0, f"交集 · 重复 ({len(result.intersection_codes)} 编码)")
        self._ab_result_tabs.setTabText(1, f"仅 A 有 ({len(result.only_in_a)})")
        self._ab_result_tabs.setTabText(2, f"仅 B 有 ({len(result.only_in_b)})")
        self._ab_result_tabs.setTabText(
            3, f"未识别 ({len(result.unrecognized_a) + len(result.unrecognized_b)})"
        )
        self._ab_summary.setText(
            f"交叉比对完成：交集 {len(result.intersection_codes)} 个编码 · "
            f"仅 A 有 {len(result.only_in_a)} 条 · 仅 B 有 {len(result.only_in_b)} 条"
        )
        self._btn_export_ab.setEnabled(True)
        self.ctx.notify("双文件交叉比对完成", "success")

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _make_table(self, model: CompareTableModel) -> QTableView:
        tv = QTableView()
        tv.setModel(model)
        tv.setAlternatingRowColors(True)
        tv.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tv.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tv.verticalHeader().setDefaultSectionSize(30)
        tv.verticalHeader().setVisible(False)
        tv.horizontalHeader().setStretchLastSection(True)
        tv.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        if model._with_matches:
            widths = [135, 240, 280, 110, 75]
        else:
            widths = [135, 85, 110, 75, 320, 160]
        for idx, w in enumerate(widths):
            if idx < model.columnCount():
                tv.setColumnWidth(idx, w)
        return tv

    def _export_compare(self, result: Optional[CompareResult]) -> None:
        if result is None:
            return
        default_name = "交叉比对报告.xlsx" if result.mode == "ab" else "快速查重结果.xlsx"
        save_path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "导出比对结果",
            str(Path(self.ctx.config.default_export_dir or Path.home()) / default_name),
            "Excel 工作簿 (*.xlsx);;CSV 表格 (*.csv);;新数据编码清单 (*.txt)",
        )
        if not save_path:
            return
        try:
            low = save_path.lower()
            if low.endswith(".csv") or "CSV" in selected_filter:
                export_compare_result_csv(result, save_path)
            elif low.endswith(".txt") or "TXT" in selected_filter.upper():
                target_items = result.not_in_db if result.mode == "db" else result.intersection_a
                export_codes_txt(target_items, save_path, unique=True, sort_codes=True)
            else:
                export_compare_result_excel(result, save_path)
            self.ctx.notify(f"导出成功：{Path(save_path).name}", "success")
        except ExportError as exc:
            ScrollableDetailDialog("导出失败", str(exc), parent=self).exec()
