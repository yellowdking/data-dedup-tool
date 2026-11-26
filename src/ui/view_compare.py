"""View 3: 快速查重 — Non-mutating DB comparison & A-vs-B cross comparison."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.config import normalize_extension
from src.core.dedup import compare_with_db
from src.core.models import CompareResult, DataRecord, ImportBatch, ScannedItem
from src.core.scanner import scan_document_file
from src.ui.app_context import AppContext
from src.ui.table_models import CompareTableModel
from src.ui.view_compare_ab import (
    _COMPARE_FILE_FILTER,
    CompareABMixin,
    _ABIntersectionTableModel,
    _DroppablePlainTextEdit,
    _stop_worker_safely,
)
from src.ui.widgets import ScrollableDetailDialog, WorkerThread

_OFFICE_EXTENSIONS: frozenset[str] = frozenset({".docx", ".xlsx", ".xlsm"})
_MAX_PREVIEW_LINES: int = 300
_MAX_PREVIEW_CHARS: int = 120_000


class CompareView(CompareABMixin, QWidget):
    """Quick comparison view supporting (a) DB non-mutating check and (b) A-vs-B cross-check."""

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx

        theme = self.ctx.config.theme

        # Mode A state
        self._db_compare_result: Optional[CompareResult] = None
        self._db_result_db_path: str = ""
        self._db_result_stale: bool = False
        self._db_loaded_file: str = ""
        self._self_importing: bool = False
        self._db_worker: Optional[WorkerThread] = None
        self._import_worker: Optional[WorkerThread] = None
        self._db_gen: int = 0
        self._db_cancel_handled_gen: int = -1

        self._model_db_matched = self._create_cmp_model(True, theme)
        self._model_db_new = self._create_cmp_model(False, theme)
        self._model_db_unrec = self._create_cmp_model(False, theme)

        # Mode B state
        self._ab_result: Optional[CompareResult] = None
        self._file_a_path: str = ""
        self._file_b_path: str = ""
        self._ab_worker: Optional[WorkerThread] = None
        self._ab_gen: int = 0
        self._ab_cancel_handled_gen: int = -1

        self._model_ab_inter = _ABIntersectionTableModel(parent=self, theme=theme)
        self._model_ab_only_a = self._create_cmp_model(False, theme)
        self._model_ab_only_b = self._create_cmp_model(False, theme)
        self._model_ab_unrec = self._create_cmp_model(False, theme)

        # Shared export worker
        self._export_worker: Optional[WorkerThread] = None

        self._setup_ui()
        self.ctx.db_switched.connect(self._on_db_switched)
        self.ctx.data_changed.connect(self._on_data_changed)
        self.ctx.settings_changed.connect(self._sync_settings)
        self.ctx.theme_changed.connect(self.apply_theme)

    def _create_cmp_model(self, with_matches: bool, theme: str) -> CompareTableModel:
        try:
            return CompareTableModel(with_matches=with_matches, parent=self, theme=theme)
        except TypeError:
            return CompareTableModel(with_matches=with_matches, parent=self)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(10)

        self._mode_tabs = QTabWidget()
        self._mode_tabs.addTab(self._build_db_compare_tab(), "与当前库比对（不入库）")
        self._mode_tabs.addTab(self._build_ab_compare_tab(), "双文件交叉比对（A vs B）")
        root.addWidget(self._mode_tabs, 1)

    # ------------------------------------------------------------------
    # Lifecycle, theme & context hooks
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self._db_gen += 1
        self._ab_gen += 1
        _stop_worker_safely(self._db_worker)
        self._db_worker = None
        _stop_worker_safely(self._ab_worker)
        self._ab_worker = None
        _stop_worker_safely(self._import_worker)
        self._import_worker = None
        _stop_worker_safely(self._export_worker)
        self._export_worker = None

    def apply_theme(self, theme: str) -> None:
        for model in (
            self._model_db_matched,
            self._model_db_new,
            self._model_db_unrec,
            self._model_ab_inter,
            self._model_ab_only_a,
            self._model_ab_only_b,
            self._model_ab_unrec,
        ):
            if hasattr(model, "set_theme"):
                model.set_theme(theme)

    def _sync_settings(self) -> None:
        checked = self.ctx.config.ignore_companion_files
        self._chk_db_ignore_comp.blockSignals(True)
        self._chk_db_ignore_comp.setChecked(checked)
        self._chk_db_ignore_comp.blockSignals(False)

        self._chk_ab_ignore_comp.blockSignals(True)
        self._chk_ab_ignore_comp.setChecked(checked)
        self._chk_ab_ignore_comp.blockSignals(False)

    def _get_companion_exts(self, enabled: bool) -> set[str]:
        if not enabled:
            return set()
        try:
            return self.ctx.get_companion_extensions(respect_global_toggle=False)
        except TypeError:
            return {
                normalize_extension(ext)
                for ext in self.ctx.config.companion_extensions
                if normalize_extension(ext)
            }

    def _is_worker_running(self, worker: Optional[WorkerThread]) -> bool:
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            return False

    def _on_db_switched(self, _db_path: str) -> None:
        if self._is_worker_running(self._db_worker):
            self._cancel_db_worker()
        self._db_compare_result = None
        self._db_result_db_path = ""
        self._db_result_stale = True
        self._model_db_matched.set_rows([])
        self._model_db_new.set_rows([])
        self._model_db_unrec.set_rows([])
        self._db_result_tabs.setTabText(0, "已存在 (0)")
        self._db_result_tabs.setTabText(1, "未找到 · 新数据 (0)")
        self._db_result_tabs.setTabText(2, "未识别 (0)")
        self._btn_import_new.setEnabled(False)
        self._btn_copy_new.setEnabled(False)
        self._btn_export_db_cmp.setEnabled(False)
        self._db_summary.setText("数据库已切换，比对结果已清空 — 请重新点击「开始快速查重」")

    def _on_data_changed(self) -> None:
        if self._self_importing:
            return
        if self._db_compare_result is not None:
            self._db_result_stale = True
            self._btn_import_new.setEnabled(False)
            self._db_summary.setText(
                f"当前数据库已发生变更（原比对结果：已存在 {len(self._db_compare_result.matched_in_db):,} · "
                f"新数据 {len(self._db_compare_result.not_in_db):,}）— 入库前请重新查重"
            )

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
        header_row.setSpacing(8)
        lbl = QLabel("待比对清单（支持粘贴或拖入文档）")
        lbl.setProperty("role", "section")
        header_row.addWidget(lbl)
        header_row.addStretch()

        self._chk_db_ignore_comp = QCheckBox("忽略伴随文件")
        self._chk_db_ignore_comp.setChecked(self.ctx.config.ignore_companion_files)
        header_row.addWidget(self._chk_db_ignore_comp)

        self._btn_load_txt = QPushButton("载入文件 (Word/Excel/TXT)")
        self._btn_load_txt.setToolTip("支持载入 .txt / .docx / .xlsx / .xlsm / .csv / .tsv / .log 文件")
        self._btn_load_txt.clicked.connect(self._load_file_to_db_input)
        header_row.addWidget(self._btn_load_txt)

        self._btn_clear_input = QPushButton("清空")
        self._btn_clear_input.setProperty("variant", "ghost")
        self._btn_clear_input.clicked.connect(self._clear_db_input)
        header_row.addWidget(self._btn_clear_input)

        self._btn_run_db = QPushButton("开始快速查重")
        self._btn_run_db.setProperty("variant", "primary")
        self._btn_run_db.clicked.connect(lambda: self.run_db_compare())
        header_row.addWidget(self._btn_run_db)
        top_layout.addLayout(header_row)

        self._db_file_badge = QLabel("")
        self._db_file_badge.setProperty("badge", "accent")
        self._db_file_badge.setVisible(False)
        top_layout.addWidget(self._db_file_badge)

        self._db_input = _DroppablePlainTextEdit()
        self._db_input.file_dropped.connect(self._load_specific_file_to_db_input)
        self._db_input.textChanged.connect(self._on_db_input_edited)
        self._db_input.setPlaceholderText(
            "在此粘贴待比对清单（每行一条），或直接拖拽 TXT / Word (.docx) / Excel (.xlsx) / CSV 文件到此区域，例如：\n"
            "ABC-001.mp4\n"
            "PRJ-0042_4K_CHS.mkv\n"
            "D:\\Archive\\DEV-108\\1.mp4\n"
            "支持直接粘贴 Windows tree /f、Everything 清单、或拖入 Word/Excel 文档提取并查重"
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

        self._db_progress = QProgressBar()
        self._db_progress.setFixedWidth(160)
        self._db_progress.setVisible(False)
        action_row.addWidget(self._db_progress)

        self._btn_cancel_db = QPushButton("取消")
        self._btn_cancel_db.setVisible(False)
        self._btn_cancel_db.clicked.connect(self._cancel_db_worker)
        action_row.addWidget(self._btn_cancel_db)

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
        self._db_result_tabs.setProperty("subtabs", True)
        self._db_result_tabs.addTab(self._make_table(self._model_db_matched), "已存在 (0)")
        self._db_result_tabs.addTab(self._make_table(self._model_db_new), "未找到 · 新数据 (0)")
        self._db_result_tabs.addTab(self._make_table(self._model_db_unrec), "未识别 (0)")
        bottom_layout.addWidget(self._db_result_tabs, 1)

        splitter.addWidget(bottom_widget)
        splitter.setSizes([180, 420])
        layout.addWidget(splitter, 1)
        return widget

    def _set_db_busy(self, busy: bool, cancellable: bool = False) -> None:
        self._db_progress.setVisible(busy)
        if busy:
            self._db_progress.setRange(0, 0)
        self._btn_cancel_db.setVisible(busy and cancellable)
        self._btn_cancel_db.setEnabled(busy and cancellable)
        self._btn_run_db.setEnabled(not busy)
        self._btn_load_txt.setEnabled(not busy)
        self._btn_clear_input.setEnabled(not busy)
        if busy:
            self._btn_import_new.setEnabled(False)
            self._btn_export_db_cmp.setEnabled(False)

    def _clear_db_input(self) -> None:
        self._db_loaded_file = ""
        self._db_file_badge.setVisible(False)
        self._db_input.blockSignals(True)
        self._db_input.clear()
        self._db_input.blockSignals(False)

    def _on_db_input_edited(self) -> None:
        if self._db_loaded_file:
            self._db_loaded_file = ""
            self._db_file_badge.setVisible(False)

    def _load_file_to_db_input(self) -> None:
        start_dir = self.ctx.config.default_import_dir or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "选择待比对文件", start_dir, _COMPARE_FILE_FILTER
        )
        if not path:
            return
        try:
            self.ctx.config.default_import_dir = str(Path(path).parent)
            self.ctx.config_manager.save()
        except OSError:
            pass
        self._load_specific_file_to_db_input(path)

    @staticmethod
    def _build_preview_text(scanned: list[ScannedItem], is_office: bool) -> str:
        lines: list[str] = []
        total_chars = 0
        truncated = False
        for idx, item in enumerate(scanned):
            if idx >= _MAX_PREVIEW_LINES:
                truncated = True
                break
            line = (
                f"[{item.source_name}] {item.raw_text}"
                if (is_office and item.source_name)
                else item.raw_text
            )
            if total_chars + len(line) > _MAX_PREVIEW_CHARS:
                truncated = True
                break
            lines.append(line)
            total_chars += len(line) + 1
        if truncated or len(scanned) > _MAX_PREVIEW_LINES:
            lines.append(
                f"... 共提取 {len(scanned):,} 条内容（仅预览前 {len(lines)} 条，比对时将解析完整文件）"
            )
        return "\n".join(lines)

    def _load_specific_file_to_db_input(self, path: str) -> None:
        if self._is_worker_running(self._db_worker) or self._is_worker_running(self._import_worker):
            self.ctx.notify("正在执行查重/入库任务，请稍候或先取消", "warning")
            return

        p = Path(path)
        ignore_comp = self._chk_db_ignore_comp.isChecked()
        comp_exts = self._get_companion_exts(ignore_comp)
        expected_db_path = str(self.ctx.db.db_path)
        is_office = p.suffix.lower() in _OFFICE_EXTENSIONS

        self._db_gen += 1
        gen = self._db_gen
        self._set_db_busy(True, cancellable=True)
        self._db_summary.setText(f"正在后台读取并比对 {p.name}...")

        worker_box: list[Optional[WorkerThread]] = [None]

        def _is_cancelled() -> bool:
            w = worker_box[0]
            return bool(w is not None and w.is_cancelled)

        def _task() -> Optional[dict[str, Any]]:
            # Single read for ALL file types (zero double-read!)
            scanned = scan_document_file(p, companion_extensions=comp_exts)
            if _is_cancelled():
                return None
            preview_text = self._build_preview_text(scanned, is_office=is_office)
            parsed = self.ctx.parser.parse_items(
                scanned,
                ignore_companions=ignore_comp,
                companion_extensions=comp_exts,
                cancel_check=_is_cancelled,
            )
            if _is_cancelled():
                return None
            result = compare_with_db(parsed, self.ctx.db, source_name=p.name)
            return {
                "file_path": str(p),
                "file_name": p.name,
                "scanned_count": len(scanned),
                "preview_text": preview_text,
                "result": result,
            }

        worker = WorkerThread(_task, parent=self)
        worker_box[0] = worker
        self._db_worker = worker
        worker.succeeded.connect(
            lambda payload, g=gen, dbp=expected_db_path: self._on_db_file_compare_succeeded(
                g, payload, dbp
            )
        )
        worker.failed.connect(lambda detail, g=gen: self._on_db_compare_failed(g, detail))
        if hasattr(worker, "cancelled"):
            worker.cancelled.connect(lambda g=gen: self._on_db_compare_cancelled(g))
        worker.finished.connect(lambda w=worker, g=gen: self._on_db_worker_finished(w, g))
        if not hasattr(worker, "safe_stop"):
            worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_db_file_compare_succeeded(
        self, gen: int, payload: Optional[dict[str, Any]], expected_db_path: str
    ) -> None:
        if gen != self._db_gen:
            return
        self._set_db_busy(False)
        if payload is None:
            self._on_db_compare_cancelled(gen)
            return
        if str(self.ctx.db.db_path) != expected_db_path:
            self._on_db_switched(str(self.ctx.db.db_path))
            return

        self._db_input.blockSignals(True)
        self._db_input.setPlainText(payload["preview_text"])
        self._db_input.blockSignals(False)
        self._db_loaded_file = payload["file_path"]
        self._db_file_badge.setText(
            f"📄 已载入文件：{payload['file_name']}（共提取 {payload['scanned_count']:,} 条）"
        )
        self._db_file_badge.setVisible(True)
        self._apply_db_compare_result(payload["result"], db_path=expected_db_path)

    def _apply_db_compare_result(self, result: CompareResult, db_path: str = "") -> None:
        self._db_compare_result = result
        self._db_result_db_path = db_path or str(self.ctx.db.db_path)
        self._db_result_stale = False

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

    def run_db_compare(self, source_name: str = "快速粘贴") -> None:
        if self._is_worker_running(self._db_worker) or self._is_worker_running(self._import_worker):
            self.ctx.notify("正在执行查重/入库任务，请稍候或先取消", "warning")
            return

        ignore_comp = self._chk_db_ignore_comp.isChecked()
        comp_exts = self._get_companion_exts(ignore_comp)
        loaded_file = self._db_loaded_file
        expected_db_path = str(self.ctx.db.db_path)

        text = ""
        if not (loaded_file and Path(loaded_file).is_file()):
            loaded_file = ""
            text = self._db_input.toPlainText().strip()
            if not text:
                self.ctx.notify("请先粘贴或载入待比对内容", "warning")
                return

        self._db_gen += 1
        gen = self._db_gen
        self._set_db_busy(True, cancellable=True)
        self._db_summary.setText("正在后台执行快速查重...")

        worker_box: list[Optional[WorkerThread]] = [None]

        def _is_cancelled() -> bool:
            w = worker_box[0]
            return bool(w is not None and w.is_cancelled)

        def _task() -> Optional[CompareResult]:
            if loaded_file:
                p = Path(loaded_file)
                scanned = scan_document_file(p, companion_extensions=comp_exts)
                if _is_cancelled():
                    return None
                parsed = self.ctx.parser.parse_items(
                    scanned,
                    ignore_companions=ignore_comp,
                    companion_extensions=comp_exts,
                    cancel_check=_is_cancelled,
                )
                if _is_cancelled():
                    return None
                return compare_with_db(parsed, self.ctx.db, source_name=p.name)

            parsed = self.ctx.parser.parse_text(
                text,
                source_name=source_name,
                ignore_companions=ignore_comp,
                companion_extensions=comp_exts,
            )
            if _is_cancelled():
                return None
            return compare_with_db(parsed, self.ctx.db, source_name=source_name)

        worker = WorkerThread(_task, parent=self)
        worker_box[0] = worker
        self._db_worker = worker
        worker.succeeded.connect(
            lambda res, g=gen, dbp=expected_db_path: self._on_db_text_compare_succeeded(g, res, dbp)
        )
        worker.failed.connect(lambda detail, g=gen: self._on_db_compare_failed(g, detail))
        if hasattr(worker, "cancelled"):
            worker.cancelled.connect(lambda g=gen: self._on_db_compare_cancelled(g))
        worker.finished.connect(lambda w=worker, g=gen: self._on_db_worker_finished(w, g))
        if not hasattr(worker, "safe_stop"):
            worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_db_text_compare_succeeded(
        self, gen: int, result: Optional[CompareResult], expected_db_path: str
    ) -> None:
        if gen != self._db_gen:
            return
        self._set_db_busy(False)
        if result is None:
            self._on_db_compare_cancelled(gen)
            return
        if str(self.ctx.db.db_path) != expected_db_path:
            self._on_db_switched(str(self.ctx.db.db_path))
            return
        self._apply_db_compare_result(result, db_path=expected_db_path)

    def _on_db_compare_cancelled(self, gen: int) -> None:
        if gen != self._db_gen or self._db_cancel_handled_gen == gen:
            return
        self._db_cancel_handled_gen = gen
        self._set_db_busy(False)
        self._db_summary.setText("已取消快速查重")
        if self._db_compare_result is not None:
            self._btn_export_db_cmp.setEnabled(True)
            self._btn_import_new.setEnabled(
                (not self._db_result_stale) and len(self._db_compare_result.not_in_db) > 0
            )
        self.ctx.notify("已取消快速查重", "info")

    def _on_db_compare_failed(self, gen: int, detail: str) -> None:
        if gen != self._db_gen:
            return
        self._set_db_busy(False)
        self._db_summary.setText("查重失败，请查看详情")
        ScrollableDetailDialog("快速查重失败", "执行查重时发生错误：", detail, parent=self).exec()

    def _on_db_worker_finished(self, worker: WorkerThread, gen: int) -> None:
        was_cancelled = False
        try:
            was_cancelled = worker.is_cancelled
        except RuntimeError:
            pass
        if self._db_worker is worker:
            self._db_worker = None
        if was_cancelled and gen == self._db_gen and self._db_cancel_handled_gen != gen:
            self._on_db_compare_cancelled(gen)

    def _cancel_db_worker(self) -> None:
        if self._is_worker_running(self._db_worker):
            assert self._db_worker is not None
            self._db_worker.cancel()
            self._btn_cancel_db.setEnabled(False)
            self._db_summary.setText("正在取消快速查重...")

    def _copy_not_in_db_codes(self) -> None:
        if not self._db_compare_result:
            return
        codes = sorted({i.canonical_code for i in self._db_compare_result.not_in_db if i.canonical_code})
        QApplication.clipboard().setText("\n".join(codes))
        self.ctx.notify(f"已复制 {len(codes)} 个新数据编码", "success")

    def _import_not_in_db(self) -> None:
        if not self._db_compare_result or not self._db_compare_result.not_in_db:
            return
        if self._is_worker_running(self._db_worker) or self._is_worker_running(self._import_worker):
            self.ctx.notify("后台任务执行中，请稍候...", "warning")
            return
        if self._db_result_stale or self._db_result_db_path != str(self.ctx.db.db_path):
            self.ctx.notify("数据库已切换或发生变更，请先重新执行快速查重后再入库", "warning")
            self._btn_import_new.setEnabled(False)
            return

        items = list(self._db_compare_result.not_in_db)
        source_a_name = self._db_compare_result.source_a_name or "快速查重入库"
        loaded_file = self._db_loaded_file

        source_type = "paste"
        if loaded_file:
            ext = Path(loaded_file).suffix.lower()
            if ext == ".docx":
                source_type = "word"
            elif ext in {".xlsx", ".xlsm"}:
                source_type = "excel"
            elif ext in {".csv", ".tsv", ".efu"}:
                source_type = "csv"
            else:
                source_type = "txt"

        self._set_db_busy(True, cancellable=False)
        self._db_summary.setText(f"正在后台将 {len(items):,} 条新数据写入当前库...")

        def _task() -> int:
            records = [DataRecord.from_parse_result(item) for item in items]
            batch = ImportBatch(
                source_name=source_a_name,
                source_type=source_type,
                total_count=len(items),
                added_count=len(records),
            )
            saved, _ = self.ctx.db.add_records(records, batch=batch)
            return len(saved)

        worker = WorkerThread(_task, parent=self)
        self._import_worker = worker
        worker.succeeded.connect(
            lambda cnt, sname=source_a_name: self._on_import_new_succeeded(cnt, sname)
        )
        worker.failed.connect(self._on_import_new_failed)
        worker.finished.connect(lambda w=worker: self._on_import_worker_finished(w))
        if not hasattr(worker, "safe_stop"):
            worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_import_new_succeeded(self, saved_count: int, source_name: str) -> None:
        self._set_db_busy(False)
        self.ctx.notify(f"已将 {saved_count:,} 条新数据写入当前库", "success")
        self._self_importing = True
        try:
            self.ctx.data_changed.emit()
        finally:
            self._self_importing = False
        self.run_db_compare(source_name=source_name or "快速粘贴")

    def _on_import_new_failed(self, detail: str) -> None:
        self._set_db_busy(False)
        self._db_summary.setText("新数据入库失败，请查看详情")
        ScrollableDetailDialog("写入数据库失败", "将新数据入库时发生错误：", detail, parent=self).exec()

    def _on_import_worker_finished(self, worker: WorkerThread) -> None:
        if self._import_worker is worker:
            self._import_worker = None
        self._set_db_busy(False)
