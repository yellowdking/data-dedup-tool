"""Mode B (A-vs-B cross-comparison) mixin and table model for CompareView."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.dedup import compare_two_sources
from src.core.exporter import (
    export_codes_txt,
    export_compare_result_csv,
    export_compare_result_excel,
)
from src.core.models import CompareResult, ParseResult
from src.core.scanner import SUPPORTED_DOCUMENT_EXTENSIONS, scan_document_file
from src.ui.table_models import CompareTableModel
from src.ui.widgets import DropZoneWidget, ScrollableDetailDialog, WorkerThread

_COMPARE_FILE_FILTER: str = (
    "支持的文档与清单 (*.txt *.docx *.xlsx *.xlsm *.csv *.tsv *.efu *.log);;"
    "Word 文档 (*.docx);;Excel 表格 (*.xlsx *.xlsm);;"
    "文本与 CSV (*.txt *.csv *.tsv *.efu *.log);;所有文件 (*.*)"
)


def _stop_worker_safely(worker: Optional[WorkerThread], timeout_ms: int = 3000) -> None:
    if worker is None:
        return
    try:
        if hasattr(worker, "safe_stop"):
            worker.safe_stop(timeout_ms)
            return
        worker.cancel()
        for sig_name in ("progress", "succeeded", "failed", "cancelled"):
            sig = getattr(worker, sig_name, None)
            if sig is not None:
                try:
                    sig.disconnect()
                except (RuntimeError, TypeError):
                    pass
        if worker.isRunning():
            worker.wait(timeout_ms)
    except RuntimeError:
        pass


class _DroppablePlainTextEdit(QPlainTextEdit):
    """QPlainTextEdit that also accepts dropped TXT/Word/Excel/CSV files and emits the path."""

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


class _ABIntersectionTableModel(CompareTableModel):
    """CompareTableModel variant displaying matched A-vs-B pairs with accurate headers."""

    _AB_HEADERS = ["标准编码", "文件 A 记录", "文件 B 匹配记录", "标签", "分集"]

    def __init__(self, parent: Optional[Any] = None, theme: str = "light") -> None:
        try:
            super().__init__(with_matches=True, parent=parent, theme=theme)
        except TypeError:
            super().__init__(with_matches=True, parent=parent)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self._AB_HEADERS)
        ):
            return self._AB_HEADERS[section]
        return super().headerData(section, orientation, role)


class CompareABMixin:
    """Mixin implementing Mode B (A-vs-B two-file cross-comparison) for CompareView."""

    def _build_ab_compare_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(12)

        col_a = QVBoxLayout()
        col_a.setSpacing(6)
        self._drop_a = DropZoneWidget(
            title="文件 A（拖拽或选择 TXT / Word / Excel / CSV）",
            subtitle="尚未选择文件 A",
            allow_dirs=False,
            allowed_exts=set(SUPPORTED_DOCUMENT_EXTENSIONS),
        )
        self._drop_a.paths_dropped.connect(lambda ps: self._set_ab_file("A", ps[0]))
        self._btn_pick_a = QPushButton("选择文件 A")
        self._btn_pick_a.clicked.connect(lambda: self._pick_ab_file("A"))
        self._drop_a.add_action_button(self._btn_pick_a)
        col_a.addWidget(self._drop_a)

        self._file_a_badge = QLabel("未选择文件 A")
        self._file_a_badge.setProperty("badge", "muted")
        col_a.addWidget(self._file_a_badge)
        top_row.addLayout(col_a, 1)

        col_b = QVBoxLayout()
        col_b.setSpacing(6)
        self._drop_b = DropZoneWidget(
            title="文件 B（拖拽或选择 TXT / Word / Excel / CSV）",
            subtitle="尚未选择文件 B",
            allow_dirs=False,
            allowed_exts=set(SUPPORTED_DOCUMENT_EXTENSIONS),
        )
        self._drop_b.paths_dropped.connect(lambda ps: self._set_ab_file("B", ps[0]))
        self._btn_pick_b = QPushButton("选择文件 B")
        self._btn_pick_b.clicked.connect(lambda: self._pick_ab_file("B"))
        self._drop_b.add_action_button(self._btn_pick_b)
        col_b.addWidget(self._drop_b)

        self._file_b_badge = QLabel("未选择文件 B")
        self._file_b_badge.setProperty("badge", "muted")
        col_b.addWidget(self._file_b_badge)
        top_row.addLayout(col_b, 1)

        layout.addLayout(top_row)

        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(10)
        self._ab_summary = QLabel("请选择或拖入文件 A 与文件 B，点击「执行交叉比对」")
        self._ab_summary.setProperty("role", "subtitle")
        ctrl_row.addWidget(self._ab_summary)
        ctrl_row.addStretch()

        self._chk_ab_ignore_comp = QCheckBox("忽略伴随文件")
        self._chk_ab_ignore_comp.setChecked(self.ctx.config.ignore_companion_files)
        ctrl_row.addWidget(self._chk_ab_ignore_comp)

        self._ab_progress = QProgressBar()
        self._ab_progress.setFixedWidth(140)
        self._ab_progress.setVisible(False)
        ctrl_row.addWidget(self._ab_progress)

        self._btn_cancel_ab = QPushButton("取消")
        self._btn_cancel_ab.setVisible(False)
        self._btn_cancel_ab.clicked.connect(self._cancel_ab_worker)
        ctrl_row.addWidget(self._btn_cancel_ab)

        self._btn_copy_ab_inter = QPushButton("复制交集编码")
        self._btn_copy_ab_inter.setEnabled(False)
        self._btn_copy_ab_inter.clicked.connect(self._copy_ab_intersection_codes)
        ctrl_row.addWidget(self._btn_copy_ab_inter)

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
        self._ab_result_tabs.setProperty("subtabs", True)
        self._ab_result_tabs.addTab(self._make_table(self._model_ab_inter), "交集 · 重复 (0)")
        self._ab_result_tabs.addTab(self._make_table(self._model_ab_only_a), "仅 A 有 (0)")
        self._ab_result_tabs.addTab(self._make_table(self._model_ab_only_b), "仅 B 有 (0)")
        self._ab_result_tabs.addTab(self._make_table(self._model_ab_unrec), "未识别 (0)")
        layout.addWidget(self._ab_result_tabs, 1)

        return widget

    def _set_ab_busy(self, busy: bool) -> None:
        self._ab_progress.setVisible(busy)
        if busy:
            self._ab_progress.setRange(0, 0)
        self._btn_cancel_ab.setVisible(busy)
        self._btn_cancel_ab.setEnabled(busy)
        self._btn_pick_a.setEnabled(not busy)
        self._btn_pick_b.setEnabled(not busy)
        self._btn_run_ab.setEnabled((not busy) and bool(self._file_a_path and self._file_b_path))
        if busy:
            self._btn_export_ab.setEnabled(False)
            self._btn_copy_ab_inter.setEnabled(False)

    def _pick_ab_file(self, side: str) -> None:
        start_dir = self.ctx.config.default_import_dir or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, f"选择文件 {side}", start_dir, _COMPARE_FILE_FILTER
        )
        if path:
            try:
                self.ctx.config.default_import_dir = str(Path(path).parent)
                self.ctx.config_manager.save()
            except OSError:
                pass
            self._set_ab_file(side, path)

    def _set_ab_file(self, side: str, path: str) -> None:
        p = Path(path)
        if side == "A":
            self._file_a_path = str(p)
            if hasattr(self._drop_a, "set_subtitle"):
                self._drop_a.set_subtitle(f"已选：{p.name}")
            self._file_a_badge.setText(f"📄 文件 A：{p.name}")
            self._file_a_badge.setToolTip(str(p))
            self._file_a_badge.setProperty("badge", "accent")
            self._file_a_badge.style().unpolish(self._file_a_badge)
            self._file_a_badge.style().polish(self._file_a_badge)
        else:
            self._file_b_path = str(p)
            if hasattr(self._drop_b, "set_subtitle"):
                self._drop_b.set_subtitle(f"已选：{p.name}")
            self._file_b_badge.setText(f"📄 文件 B：{p.name}")
            self._file_b_badge.setToolTip(str(p))
            self._file_b_badge.setProperty("badge", "accent")
            self._file_b_badge.style().unpolish(self._file_b_badge)
            self._file_b_badge.style().polish(self._file_b_badge)
        self._btn_run_ab.setEnabled(
            (not self._is_worker_running(self._ab_worker))
            and bool(self._file_a_path and self._file_b_path)
        )

    def run_ab_compare(self) -> None:
        if not self._file_a_path or not self._file_b_path:
            return
        if self._is_worker_running(self._ab_worker):
            self.ctx.notify("正在执行交叉比对，请稍候或先取消", "warning")
            return

        ignore_comp = self._chk_ab_ignore_comp.isChecked()
        comp_exts = self._get_companion_exts(ignore_comp)
        path_a = self._file_a_path
        path_b = self._file_b_path

        self._ab_gen += 1
        gen = self._ab_gen
        self._set_ab_busy(True)
        self._ab_summary.setText("正在后台读取并交叉比对文件 A 与文件 B...")

        worker_box: list[Optional[WorkerThread]] = [None]

        def _is_cancelled() -> bool:
            w = worker_box[0]
            return bool(w is not None and w.is_cancelled)

        def _task() -> Optional[CompareResult]:
            scanned_a = scan_document_file(path_a, companion_extensions=comp_exts)
            if _is_cancelled():
                return None
            scanned_b = scan_document_file(path_b, companion_extensions=comp_exts)
            if _is_cancelled():
                return None
            parsed_a = self.ctx.parser.parse_items(
                scanned_a,
                ignore_companions=ignore_comp,
                companion_extensions=comp_exts,
                cancel_check=_is_cancelled,
            )
            if _is_cancelled():
                return None
            parsed_b = self.ctx.parser.parse_items(
                scanned_b,
                ignore_companions=ignore_comp,
                companion_extensions=comp_exts,
                cancel_check=_is_cancelled,
            )
            if _is_cancelled():
                return None
            return compare_two_sources(
                parsed_a,
                parsed_b,
                source_a_name=Path(path_a).name,
                source_b_name=Path(path_b).name,
            )

        worker = WorkerThread(_task, parent=self)
        worker_box[0] = worker
        self._ab_worker = worker
        worker.succeeded.connect(lambda res, g=gen: self._on_ab_compare_succeeded(g, res))
        worker.failed.connect(lambda detail, g=gen: self._on_ab_compare_failed(g, detail))
        if hasattr(worker, "cancelled"):
            worker.cancelled.connect(lambda g=gen: self._on_ab_compare_cancelled(g))
        worker.finished.connect(lambda w=worker, g=gen: self._on_ab_worker_finished(w, g))
        if not hasattr(worker, "safe_stop"):
            worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_ab_compare_succeeded(self, gen: int, result: Optional[CompareResult]) -> None:
        if gen != self._ab_gen:
            return
        self._set_ab_busy(False)
        if result is None:
            self._on_ab_compare_cancelled(gen)
            return

        self._ab_result = result

        # Pair each File A intersection item with matching File B items by canonical code
        b_by_code: dict[str, list[ParseResult]] = {}
        for item_b in result.intersection_b:
            c = item_b.canonical_code.strip().upper()
            b_by_code.setdefault(c, []).append(item_b)
        inter_pairs = [
            (item_a, b_by_code.get(item_a.canonical_code.strip().upper(), []))
            for item_a in result.intersection_a
        ]

        self._model_ab_inter.set_rows(inter_pairs)
        self._model_ab_only_a.set_rows(result.only_in_a)
        self._model_ab_only_b.set_rows(result.only_in_b)
        self._model_ab_unrec.set_rows(result.unrecognized_a + result.unrecognized_b)

        self._ab_result_tabs.setTabText(
            0, f"交集 · 重复 ({len(inter_pairs)} 条 / {len(result.intersection_codes)} 编码)"
        )
        self._ab_result_tabs.setTabText(1, f"仅 A 有 ({len(result.only_in_a)})")
        self._ab_result_tabs.setTabText(2, f"仅 B 有 ({len(result.only_in_b)})")
        self._ab_result_tabs.setTabText(
            3, f"未识别 ({len(result.unrecognized_a) + len(result.unrecognized_b)})"
        )
        self._ab_summary.setText(
            f"交叉比对完成：交集 {len(result.intersection_codes)} 个编码（A {len(result.intersection_a)} 条 / "
            f"B {len(result.intersection_b)} 条）· 仅 A 有 {len(result.only_in_a)} 条 · 仅 B 有 {len(result.only_in_b)} 条"
        )
        self._btn_export_ab.setEnabled(True)
        self._btn_copy_ab_inter.setEnabled(bool(result.intersection_codes))
        self.ctx.notify("双文件交叉比对完成", "success")

    def _on_ab_compare_cancelled(self, gen: int) -> None:
        if gen != self._ab_gen or self._ab_cancel_handled_gen == gen:
            return
        self._ab_cancel_handled_gen = gen
        self._set_ab_busy(False)
        self._ab_summary.setText("已取消交叉比对")
        if self._ab_result is not None:
            self._btn_export_ab.setEnabled(True)
            self._btn_copy_ab_inter.setEnabled(bool(self._ab_result.intersection_codes))
        self.ctx.notify("已取消双文件交叉比对", "info")

    def _on_ab_compare_failed(self, gen: int, detail: str) -> None:
        if gen != self._ab_gen:
            return
        self._set_ab_busy(False)
        self._ab_summary.setText("交叉比对失败，请查看详情")
        ScrollableDetailDialog("读取或比对文件失败", "执行交叉比对时发生错误：", detail, parent=self).exec()

    def _on_ab_worker_finished(self, worker: WorkerThread, gen: int) -> None:
        was_cancelled = False
        try:
            was_cancelled = worker.is_cancelled
        except RuntimeError:
            pass
        if self._ab_worker is worker:
            self._ab_worker = None
        if was_cancelled and gen == self._ab_gen and self._ab_cancel_handled_gen != gen:
            self._on_ab_compare_cancelled(gen)

    def _cancel_ab_worker(self) -> None:
        if self._is_worker_running(self._ab_worker):
            assert self._ab_worker is not None
            self._ab_worker.cancel()
            self._btn_cancel_ab.setEnabled(False)
            self._ab_summary.setText("正在取消交叉比对...")

    def _copy_ab_intersection_codes(self) -> None:
        if not self._ab_result or not self._ab_result.intersection_codes:
            return
        codes = list(self._ab_result.intersection_codes)
        QApplication.clipboard().setText("\n".join(codes))
        self.ctx.notify(f"已复制 {len(codes)} 个交集编码", "success")

    # ------------------------------------------------------------------
    # Shared table & export helpers
    # ------------------------------------------------------------------

    def _make_table(self, model: CompareTableModel) -> QTableView:
        tv = QTableView()
        tv.setModel(model)
        tv.setAlternatingRowColors(True)
        tv.setWordWrap(False)
        tv.setSortingEnabled(True)
        tv.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        tv.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tv.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tv.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tv.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tv.customContextMenuRequested.connect(lambda pos, t=tv, m=model: self._show_table_menu(t, m, pos))
        tv.verticalHeader().setDefaultSectionSize(30)
        tv.verticalHeader().setVisible(False)
        tv.horizontalHeader().setStretchLastSection(True)
        tv.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        if model._with_matches:
            widths = [118, 215, 240, 90, 62]
        else:
            widths = [118, 62, 72, 54, 245, 220]
        for idx, w in enumerate(widths):
            if idx < model.columnCount():
                tv.setColumnWidth(idx, w)
        return tv

    def _show_table_menu(self, tv: QTableView, model: CompareTableModel, pos) -> None:
        sel = tv.selectionModel()
        if sel is None:
            return
        all_rows = model.rows()
        selected_rows = [
            all_rows[idx.row()] for idx in sel.selectedRows() if 0 <= idx.row() < len(all_rows)
        ]
        if not selected_rows:
            return
        items: list[ParseResult] = [r[0] if isinstance(r, tuple) else r for r in selected_rows]

        menu = QMenu(self)
        act_code = QAction(f"复制标准编码 ({len(items)} 条)", self)
        act_code.triggered.connect(lambda: self._copy_selected_items(items, "code"))
        menu.addAction(act_code)

        act_name = QAction("复制文件名 / 原始文本", self)
        act_name.triggered.connect(lambda: self._copy_selected_items(items, "name"))
        menu.addAction(act_name)
        menu.exec(tv.viewport().mapToGlobal(pos))

    def _copy_selected_items(self, items: list[ParseResult], field: str) -> None:
        if field == "code":
            lines = [it.canonical_code for it in items if it.canonical_code]
        else:
            lines = [it.file_name or it.raw_text for it in items]
        QApplication.clipboard().setText("\n".join(lines))
        self.ctx.notify(f"已复制 {len(lines)} 条到剪贴板", "success")

    def _export_compare(self, result: Optional[CompareResult]) -> None:
        if result is None:
            return
        if self._is_worker_running(self._export_worker):
            self.ctx.notify("正在导出文件，请稍候...", "warning")
            return

        default_name = "交叉比对报告.xlsx" if result.mode == "ab" else "快速查重结果.xlsx"
        txt_label = "交集编码清单 (*.txt)" if result.mode == "ab" else "新数据编码清单 (*.txt)"
        file_filter = f"Excel 工作簿 (*.xlsx);;CSV 表格 (*.csv);;{txt_label}"

        save_path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "导出比对结果",
            str(Path(self.ctx.config.default_export_dir or Path.home()) / default_name),
            file_filter,
        )
        if not save_path:
            return

        try:
            self.ctx.config.default_export_dir = str(Path(save_path).parent)
            self.ctx.config_manager.save()
        except OSError:
            pass

        self._btn_export_db_cmp.setEnabled(False)
        self._btn_export_ab.setEnabled(False)
        self.ctx.notify(f"正在后台导出 {Path(save_path).name}...", "info")

        def _task() -> str:
            low = save_path.lower()
            if low.endswith(".csv") or "CSV" in selected_filter:
                export_compare_result_csv(result, save_path)
            elif low.endswith(".txt") or "TXT" in selected_filter.upper():
                target_items = result.not_in_db if result.mode == "db" else result.intersection_a
                export_codes_txt(target_items, save_path, unique=True, sort_codes=True)
            else:
                export_compare_result_excel(result, save_path)
            return save_path

        worker = WorkerThread(_task, parent=self)
        self._export_worker = worker
        worker.succeeded.connect(self._on_export_succeeded)
        worker.failed.connect(self._on_export_failed)
        worker.finished.connect(lambda w=worker: self._on_export_finished(w))
        if not hasattr(worker, "safe_stop"):
            worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_export_succeeded(self, save_path: str) -> None:
        self._restore_export_buttons()
        self.ctx.notify(f"导出成功：{Path(save_path).name}", "success")

    def _on_export_failed(self, detail: str) -> None:
        self._restore_export_buttons()
        ScrollableDetailDialog("导出失败", "导出比对结果时发生错误：", detail, parent=self).exec()

    def _on_export_finished(self, worker: WorkerThread) -> None:
        if self._export_worker is worker:
            self._export_worker = None
        self._restore_export_buttons()

    def _restore_export_buttons(self) -> None:
        self._btn_export_db_cmp.setEnabled(self._db_compare_result is not None)
        self._btn_export_ab.setEnabled(self._ab_result is not None)
