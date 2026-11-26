"""View 2: 批量导入 — Drag-and-drop scanner, threaded preview, inline fix & commit."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.config import normalize_extension
from src.core.database import DatabaseError, normalize_part_key
from src.core.dedup import commit_preview, preview_import
from src.core.models import DedupPreview, ParseResult, ScannedItem
from src.core.scanner import (
    SUPPORTED_DOCUMENT_EXTENSIONS,
    scan_document_file,
    scan_folder,
)
from src.ui.app_context import AppContext
from src.ui.table_models import PreviewTableModel
from src.ui.widgets import DropZoneWidget, ScrollableDetailDialog, WorkerThread


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


class ImportView(QWidget):
    """Batch import workflow: source queue -> threaded scan & parse -> 4-tab preview -> commit."""

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._queued_paths: list[str] = []
        self._current_preview: Optional[DedupPreview] = None
        self._preview_db_path: str = ""
        self._worker: Optional[WorkerThread] = None
        self._commit_worker: Optional[WorkerThread] = None
        self._scan_gen: int = 0
        self._cancel_handled_gen: int = -1

        theme = self.ctx.config.theme
        self._new_model = self._create_preview_model("new", theme)
        self._db_dup_model = self._create_preview_model("db_dup", theme)
        self._batch_dup_model = self._create_preview_model("batch_dup", theme)
        self._unrec_model = self._create_preview_model("unrecognized", theme)
        self._unrec_model.code_manually_fixed.connect(self._on_manual_fix)

        self._setup_ui()
        self.ctx.db_switched.connect(self._on_db_switched)
        self.ctx.settings_changed.connect(self._sync_settings)
        self.ctx.theme_changed.connect(self.apply_theme)

    def _create_preview_model(self, mode: str, theme: str) -> PreviewTableModel:
        try:
            return PreviewTableModel(mode, self, theme=theme)
        except TypeError:
            return PreviewTableModel(mode, self)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # Drag-and-drop source zone
        self._drop_zone = DropZoneWidget(
            title="拖入 TXT、Word (.docx)、Excel (.xlsx)、CSV 或文件夹（多次拖入将自动追加到队列）",
            subtitle="支持 Word 段落/表格、多 Sheet Excel、CSV/TSV 及目录树清单，自动识别编码并先预演再入库",
            allow_dirs=True,
            allowed_exts=set(SUPPORTED_DOCUMENT_EXTENSIONS),
        )
        self._drop_zone.paths_dropped.connect(self._on_paths_added)

        self._btn_files = QPushButton("添加文件 (TXT / Word / Excel / CSV)")
        self._btn_files.setProperty("variant", "primary")
        self._btn_files.clicked.connect(self._pick_files)
        self._drop_zone.add_action_button(self._btn_files)

        self._btn_folder = QPushButton("添加本地文件夹")
        self._btn_folder.clicked.connect(self._pick_folder)
        self._drop_zone.add_action_button(self._btn_folder)

        self._btn_clear = QPushButton("清空队列")
        self._btn_clear.setProperty("variant", "ghost")
        self._btn_clear.clicked.connect(self._clear_all)
        self._drop_zone.add_action_button(self._btn_clear)

        root.addWidget(self._drop_zone)

        # Queue summary strip
        self._queue_label = QLabel("")
        self._queue_label.setProperty("badge", "accent")
        self._queue_label.setVisible(False)
        root.addWidget(self._queue_label)

        # Controls & Progress card
        ctrl_card = QFrame()
        ctrl_card.setProperty("card", True)
        ctrl_layout = QHBoxLayout(ctrl_card)
        ctrl_layout.setContentsMargins(14, 10, 14, 10)
        ctrl_layout.setSpacing(14)

        self._chk_ignore_companion = QCheckBox("忽略伴随扩展名 (.jpg / .nfo / .srt / .txt 等)")
        self._chk_ignore_companion.setChecked(self.ctx.config.ignore_companion_files)
        self._chk_ignore_companion.toggled.connect(self._on_toggle_companion)
        ctrl_layout.addWidget(self._chk_ignore_companion)

        self._chk_include_multipart = QCheckBox("疑似分集 (CD1/CD2) 随新增一并入库")
        self._chk_include_multipart.setChecked(True)
        self._chk_include_multipart.toggled.connect(lambda _: self._refresh_commit_buttons())
        ctrl_layout.addWidget(self._chk_include_multipart)

        ctrl_layout.addStretch()

        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedWidth(220)
        self._progress_bar.setVisible(False)
        ctrl_layout.addWidget(self._progress_bar)

        self._btn_cancel = QPushButton("取消")
        self._btn_cancel.setVisible(False)
        self._btn_cancel.clicked.connect(self._cancel_worker)
        ctrl_layout.addWidget(self._btn_cancel)

        self._btn_rescan = QPushButton("重新解析预演")
        self._btn_rescan.setEnabled(False)
        self._btn_rescan.clicked.connect(self._start_scan_worker)
        ctrl_layout.addWidget(self._btn_rescan)

        root.addWidget(ctrl_card)

        # Summary strip
        self._summary_label = QLabel("尚未导入文件 — 请拖入或选择 TXT、Word (.docx)、Excel (.xlsx)、CSV 文件或本地文件夹")
        self._summary_label.setProperty("role", "subtitle")
        root.addWidget(self._summary_label)

        # 4-Tab Preview
        self._tabs = QTabWidget()
        self._table_new = self._make_table(self._new_model, editable=False)
        self._table_db_dup = self._make_table(self._db_dup_model, editable=False)
        self._table_batch_dup = self._make_table(self._batch_dup_model, editable=False)
        self._table_unrec = self._make_table(self._unrec_model, editable=True)

        # Unrecognized tab wrapped with inline-fix hint banner
        unrec_container = QWidget()
        unrec_layout = QVBoxLayout(unrec_container)
        unrec_layout.setContentsMargins(8, 8, 8, 8)
        unrec_layout.setSpacing(6)
        hint = QLabel("💡 双击第一列可直接手动修正标准编码（例如输入 PRJ-0042），提交前将自动复核重复项并随本批入库。")
        hint.setProperty("role", "subtitle")
        unrec_layout.addWidget(hint)
        unrec_layout.addWidget(self._table_unrec, 1)

        self._tabs.addTab(self._table_new, "新增待入库 (0)")
        self._tabs.addTab(self._table_db_dup, "库内重复 (0)")
        self._tabs.addTab(self._table_batch_dup, "批内重复 (0)")
        self._tabs.addTab(unrec_container, "未识别与待确认 (0)")
        root.addWidget(self._tabs, 1)

        # Bottom Commit Action Bar
        action_bar = QHBoxLayout()
        action_bar.setSpacing(10)

        self._fixed_count_label = QLabel("")
        self._fixed_count_label.setProperty("role", "subtitle")
        action_bar.addWidget(self._fixed_count_label)
        action_bar.addStretch()

        self._btn_commit_new = QPushButton("仅入库新增")
        self._btn_commit_new.setProperty("variant", "success")
        self._btn_commit_new.setEnabled(False)
        self._btn_commit_new.clicked.connect(lambda: self._commit(include_duplicates=False))
        action_bar.addWidget(self._btn_commit_new)

        self._btn_commit_all = QPushButton("全部入库（含重复项）")
        self._btn_commit_all.setProperty("variant", "primary")
        self._btn_commit_all.setEnabled(False)
        self._btn_commit_all.clicked.connect(lambda: self._commit(include_duplicates=True))
        action_bar.addWidget(self._btn_commit_all)

        root.addLayout(action_bar)

    def _make_table(self, model: PreviewTableModel, editable: bool) -> QTableView:
        tv = QTableView()
        tv.setModel(model)
        tv.setAlternatingRowColors(True)
        tv.setWordWrap(False)
        tv.setSortingEnabled(True)
        tv.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        tv.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tv.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tv.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tv.customContextMenuRequested.connect(lambda pos, t=tv, m=model: self._show_table_menu(t, m, pos))
        tv.verticalHeader().setDefaultSectionSize(30)
        tv.verticalHeader().setVisible(False)
        tv.horizontalHeader().setStretchLastSection(True)
        tv.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        if not editable:
            tv.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            if model._mode == "db_dup":
                widths = [118, 175, 195, 70, 52, 200]
            else:
                widths = [118, 62, 70, 52, 205, 112, 200]
            for idx, w in enumerate(widths):
                if idx < model.columnCount():
                    tv.setColumnWidth(idx, w)
        else:
            tv.setEditTriggers(
                QAbstractItemView.EditTrigger.DoubleClicked
                | QAbstractItemView.EditTrigger.EditKeyPressed
            )
            widths = [155, 225, 110, 195, 150]
            for idx, w in enumerate(widths):
                if idx < model.columnCount():
                    tv.setColumnWidth(idx, w)
        return tv

    # ------------------------------------------------------------------
    # Lifecycle, theme & context signal handlers
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self._scan_gen += 1
        _stop_worker_safely(self._worker)
        self._worker = None
        _stop_worker_safely(self._commit_worker)
        self._commit_worker = None

    def apply_theme(self, theme: str) -> None:
        for model in (self._new_model, self._db_dup_model, self._batch_dup_model, self._unrec_model):
            if hasattr(model, "set_theme"):
                model.set_theme(theme)

    def _sync_settings(self) -> None:
        self._chk_ignore_companion.blockSignals(True)
        self._chk_ignore_companion.setChecked(self.ctx.config.ignore_companion_files)
        self._chk_ignore_companion.blockSignals(False)

    def _on_db_switched(self, _db_path: str) -> None:
        if self._is_worker_running(self._worker):
            self._cancel_worker(silent=True)
        self._invalidate_preview(
            reason="数据库已切换，当前预演结果已失效 — 请点击「重新解析预演」针对新库重新比对"
            if self._queued_paths
            else ""
        )

    def _invalidate_preview(self, reason: str = "") -> None:
        self._current_preview = None
        self._preview_db_path = ""
        self._new_model.set_rows([])
        self._db_dup_model.set_rows([])
        self._batch_dup_model.set_rows([])
        self._unrec_model.set_rows([])
        self._tabs.setTabText(0, "新增待入库 (0)")
        self._tabs.setTabText(1, "库内重复 (0)")
        self._tabs.setTabText(2, "批内重复 (0)")
        self._tabs.setTabText(3, "未识别与待确认 (0)")
        self._fixed_count_label.setText("")
        self._btn_commit_new.setEnabled(False)
        self._btn_commit_all.setEnabled(False)
        if reason:
            self._summary_label.setText(reason)

    def _is_worker_running(self, worker: Optional[WorkerThread]) -> bool:
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            return False

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

    def _set_busy(self, busy: bool, cancellable: bool = False) -> None:
        self._progress_bar.setVisible(busy)
        self._btn_cancel.setVisible(busy and cancellable)
        self._btn_cancel.setEnabled(busy and cancellable)
        self._btn_files.setEnabled(not busy)
        self._btn_folder.setEnabled(not busy)
        self._btn_clear.setEnabled(not busy)
        self._btn_rescan.setEnabled((not busy) and bool(self._queued_paths))
        if busy:
            self._btn_commit_new.setEnabled(False)
            self._btn_commit_all.setEnabled(False)
        else:
            self._refresh_commit_buttons()

    def _update_queue_badge(self) -> None:
        if not self._queued_paths:
            self._queue_label.setVisible(False)
            self._queue_label.setText("")
            self._queue_label.setToolTip("")
            return
        names = [Path(p).name or p for p in self._queued_paths]
        preview = "、".join(names[:4])
        if len(names) > 4:
            preview += f" 等共 {len(names)} 个来源"
        self._queue_label.setText(f"📦 已排队来源 ({len(self._queued_paths)})：{preview}")
        self._queue_label.setToolTip("\n".join(self._queued_paths))
        self._queue_label.setVisible(True)

    # ------------------------------------------------------------------
    # Source selection & queue management
    # ------------------------------------------------------------------

    def _on_toggle_companion(self, checked: bool) -> None:
        self.ctx.config.ignore_companion_files = checked
        try:
            self.ctx.config_manager.save()
        except OSError as exc:
            self.ctx.notify(f"保存配置失败：{exc}", "warning")
        self.ctx.settings_changed.emit()

    def _pick_files(self) -> None:
        start_dir = self.ctx.config.default_import_dir or str(Path.home())
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择要导入的文件（支持多选）",
            start_dir,
            "支持的文档与清单 (*.txt *.docx *.xlsx *.xlsm *.csv *.tsv *.efu *.log);;"
            "Word 文档 (*.docx);;Excel 表格 (*.xlsx *.xlsm);;"
            "文本与 CSV (*.txt *.csv *.tsv *.efu *.log);;所有文件 (*.*)",
        )
        if paths:
            try:
                self.ctx.config.default_import_dir = str(Path(paths[0]).parent)
                self.ctx.config_manager.save()
            except OSError:
                pass
            self._on_paths_added(paths)

    def _pick_folder(self) -> None:
        start_dir = self.ctx.config.default_import_dir or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "选择要递归扫描的本地文件夹", start_dir)
        if folder:
            try:
                self.ctx.config.default_import_dir = folder
                self.ctx.config_manager.save()
            except OSError:
                pass
            self._on_paths_added([folder])

    def _on_paths_added(self, paths: list[str]) -> None:
        if self._is_worker_running(self._worker) or self._is_worker_running(self._commit_worker):
            self.ctx.notify("后台任务运行中，请稍候或先取消当前扫描", "warning")
            return

        existing_norm = {os.path.normcase(os.path.normpath(p)) for p in self._queued_paths}
        added_count = 0
        for p in paths:
            norm = os.path.normcase(os.path.normpath(p))
            if norm not in existing_norm:
                existing_norm.add(norm)
                self._queued_paths.append(p)
                added_count += 1

        self._update_queue_badge()
        self._btn_rescan.setEnabled(bool(self._queued_paths))
        if added_count == 0 and self._current_preview is not None:
            self.ctx.notify("所选文件已在导入队列中", "info")
            return
        self._start_scan_worker()

    # ------------------------------------------------------------------
    # Threaded scan & preview with real progress + safe cancellation
    # ------------------------------------------------------------------

    def _start_scan_worker(self) -> None:
        if not self._queued_paths:
            return
        if self._is_worker_running(self._worker) or self._is_worker_running(self._commit_worker):
            self.ctx.notify("正在处理中，请稍候或先取消当前任务", "warning")
            return

        ignore_companions = self._chk_ignore_companion.isChecked()
        comp_exts = self._get_companion_exts(ignore_companions)
        paths_copy = list(self._queued_paths)
        expected_db_path = str(self.ctx.db.db_path)

        self._scan_gen += 1
        gen = self._scan_gen

        self._progress_bar.setRange(0, 0)
        self._progress_bar.setValue(0)
        self._set_busy(True, cancellable=True)
        self._summary_label.setText(f"正在后台扫描并解析 {len(paths_copy)} 个来源，请稍候...")

        worker_box: list[Optional[WorkerThread]] = [None]

        def _is_cancelled() -> bool:
            w = worker_box[0]
            return bool(w is not None and w.is_cancelled)

        def _emit_prog(cur: int, tot: int, msg: str) -> None:
            w = worker_box[0]
            if w is not None and not w.is_cancelled:
                w.emit_progress(cur, tot, msg)

        def _task() -> Optional[DedupPreview]:
            scanned: list[ScannedItem] = []
            total_sources = len(paths_copy)
            for idx, p_str in enumerate(paths_copy, start=1):
                if _is_cancelled():
                    return None
                p = Path(p_str)
                if p.is_dir():
                    _emit_prog(0, 0, f"正在扫描目录 ({idx}/{total_sources}): {p.name}...")
                    scanned.extend(
                        scan_folder(
                            p,
                            recursive=True,
                            companion_extensions=comp_exts,
                            progress_callback=lambda cnt, fname, i=idx, ts=total_sources: _emit_prog(
                                0, 0, f"正在扫描目录 ({i}/{ts})：已发现 {cnt:,} 个文件 ({fname})"
                            ),
                            cancel_check=_is_cancelled,
                        )
                    )
                elif p.is_file():
                    _emit_prog(idx - 1, total_sources, f"正在读取文件 ({idx}/{total_sources}): {p.name}...")
                    scanned.extend(scan_document_file(p, companion_extensions=comp_exts))
                    _emit_prog(idx, total_sources, f"已读取文件 ({idx}/{total_sources}): {p.name}")

            if _is_cancelled():
                return None

            total_scanned = len(scanned)
            skipped = sum(1 for s in scanned if s.is_companion) if ignore_companions else 0

            if total_scanned > 0:
                _emit_prog(0, total_scanned, f"正在解析 {total_scanned:,} 条记录的编码...")

            parsed = self.ctx.parser.parse_items(
                scanned,
                ignore_companions=ignore_companions,
                companion_extensions=comp_exts,
                progress_callback=lambda cur, tot: _emit_prog(
                    cur, tot, f"正在解析编码 ({cur:,}/{tot:,})..."
                ),
                cancel_check=_is_cancelled,
            )

            if _is_cancelled():
                return None

            _emit_prog(0, 0, f"正在与数据库比对 {len(parsed):,} 条解析结果...")
            return preview_import(
                parsed,
                self.ctx.db,
                skipped_companions=skipped,
                total_scanned=total_scanned,
            )

        worker = WorkerThread(_task, parent=self)
        worker_box[0] = worker
        self._worker = worker
        worker.progress.connect(lambda c, t, m, g=gen: self._on_scan_progress(g, c, t, m))
        worker.succeeded.connect(
            lambda prev, g=gen, dbp=expected_db_path: self._on_scan_finished(g, prev, dbp)
        )
        worker.failed.connect(lambda detail, g=gen: self._on_scan_failed(g, detail))
        if hasattr(worker, "cancelled"):
            worker.cancelled.connect(lambda g=gen: self._on_scan_cancelled(g))
        worker.finished.connect(lambda w=worker, g=gen: self._on_scan_thread_finished(w, g))
        if not hasattr(worker, "safe_stop"):
            worker.finished.connect(worker.deleteLater)
        worker.start()

    def _cancel_worker(self, silent: bool = False) -> None:
        if self._is_worker_running(self._worker):
            assert self._worker is not None
            self._worker.cancel()
            self._btn_cancel.setEnabled(False)
            if not silent:
                self._summary_label.setText("正在取消后台扫描...")

    def _on_scan_progress(self, gen: int, current: int, total: int, message: str) -> None:
        if gen != self._scan_gen:
            return
        if total > 0:
            self._progress_bar.setRange(0, total)
            self._progress_bar.setValue(min(current, total))
        else:
            self._progress_bar.setRange(0, 0)
        if message:
            self._summary_label.setText(message)

    def _on_scan_cancelled(self, gen: int) -> None:
        if gen != self._scan_gen or self._cancel_handled_gen == gen:
            return
        self._cancel_handled_gen = gen
        self._set_busy(False)
        self._summary_label.setText("已取消后台扫描 — 可点击「重新解析预演」继续或清空队列")
        self.ctx.notify("已取消扫描解析", "info")

    def _on_scan_finished(
        self, gen: int, preview: Optional[DedupPreview], expected_db_path: str
    ) -> None:
        if gen != self._scan_gen:
            return
        self._set_busy(False)
        if preview is None:
            self._on_scan_cancelled(gen)
            return
        if str(self.ctx.db.db_path) != expected_db_path:
            self._invalidate_preview(
                "扫描期间数据库已切换，预演结果已丢弃 — 请点击「重新解析预演」"
            )
            self.ctx.notify("数据库已切换，请重新解析预演", "warning")
            return
        self._preview_db_path = expected_db_path
        self.load_preview(preview)

    def _on_scan_failed(self, gen: int, detail: str) -> None:
        if gen != self._scan_gen:
            return
        self._set_busy(False)
        self._summary_label.setText("解析失败，请查看详情")
        dlg = ScrollableDetailDialog("扫描解析出错", "后台解析任务遇到错误：", detail, parent=self)
        dlg.exec()

    def _on_scan_thread_finished(self, worker: WorkerThread, gen: int) -> None:
        was_cancelled = False
        try:
            was_cancelled = worker.is_cancelled
        except RuntimeError:
            pass
        if self._worker is worker:
            self._worker = None
        if was_cancelled and gen == self._scan_gen and self._cancel_handled_gen != gen:
            self._on_scan_cancelled(gen)

    # ------------------------------------------------------------------
    # Preview rendering & inline manual fix
    # ------------------------------------------------------------------

    def load_preview(self, preview: DedupPreview) -> None:
        """Populate the 4 preview tables from a DedupPreview result."""
        self._current_preview = preview
        if not self._preview_db_path:
            self._preview_db_path = str(self.ctx.db.db_path)
        combined_new = list(preview.new_items) + list(preview.possible_multiparts)
        self._new_model.set_rows(combined_new)
        self._db_dup_model.set_rows(preview.db_duplicates)
        self._batch_dup_model.set_rows(preview.batch_duplicates)
        self._unrec_model.set_rows(preview.unrecognized)

        self._tabs.setTabText(
            0, f"新增待入库 ({len(preview.new_items)} + 分集 {len(preview.possible_multiparts)})"
        )
        self._tabs.setTabText(1, f"库内重复 ({len(preview.db_duplicates)})")
        self._tabs.setTabText(2, f"批内重复 ({len(preview.batch_duplicates)})")
        self._tabs.setTabText(3, f"未识别与待确认 ({len(preview.unrecognized)})")

        self._summary_label.setText(
            f"预演完成 · 共扫描 {preview.total_scanned:,} 条（已跳过伴随文件 {preview.skipped_companions:,} 条）"
            f" | 新增 {len(preview.new_items):,} · 疑似分集 {len(preview.possible_multiparts):,}"
            f" · 库内重复 {len(preview.db_duplicates):,} · 批内重复 {len(preview.batch_duplicates):,}"
            f" · 未识别 {len(preview.unrecognized):,}"
        )
        self._refresh_commit_buttons()
        self.ctx.notify("批量导入预演完成，请确认后点击入库", "success")

    def _on_manual_fix(self, _row: int, _code: str) -> None:
        self._refresh_commit_buttons()

    def _manually_fixed_items(self) -> list[ParseResult]:
        if not self._current_preview:
            return []
        return [
            item
            for item in self._current_preview.unrecognized
            if item.recognized and item.canonical_code.strip()
        ]

    def _refresh_commit_buttons(self) -> None:
        if not self._current_preview or self._is_worker_running(self._worker) or self._is_worker_running(self._commit_worker):
            self._btn_commit_new.setEnabled(False)
            self._btn_commit_all.setEnabled(False)
            if not self._current_preview:
                self._fixed_count_label.setText("")
            return

        fixed = self._manually_fixed_items()
        if fixed:
            self._fixed_count_label.setText(f"已手动修正 {len(fixed)} 条未识别记录，提交前将自动查重复核")
        else:
            self._fixed_count_label.setText("")

        new_count = (
            len(self._current_preview.new_items)
            + (len(self._current_preview.possible_multiparts) if self._chk_include_multipart.isChecked() else 0)
            + len(fixed)
        )
        all_count = (
            new_count
            + len(self._current_preview.db_duplicates)
            + len(self._current_preview.batch_duplicates)
        )
        self._btn_commit_new.setEnabled(new_count > 0)
        self._btn_commit_all.setEnabled(all_count > 0)

    def _show_table_menu(self, tv: QTableView, model: PreviewTableModel, pos) -> None:
        sel = tv.selectionModel()
        if sel is None:
            return
        rows = [model.row_at(idx.row()) for idx in sel.selectedRows()]
        rows = [r for r in rows if r is not None]
        if not rows:
            return
        items: list[ParseResult] = [r[0] if isinstance(r, tuple) else r for r in rows]

        menu = QMenu(self)
        act_code = QAction(f"复制标准编码 ({len(items)} 条)", self)
        act_code.triggered.connect(lambda: self._copy_items(items, "code"))
        menu.addAction(act_code)

        act_name = QAction("复制原始文件名 / 文本", self)
        act_name.triggered.connect(lambda: self._copy_items(items, "name"))
        menu.addAction(act_name)
        menu.exec(tv.viewport().mapToGlobal(pos))

    def _copy_items(self, items: list[ParseResult], field: str) -> None:
        if field == "code":
            lines = [it.canonical_code for it in items if it.canonical_code]
        else:
            lines = [it.file_name or it.raw_text for it in items]
        QApplication.clipboard().setText("\n".join(lines))
        self.ctx.notify(f"已复制 {len(lines)} 条到剪贴板", "success")

    # ------------------------------------------------------------------
    # Commit with stale-DB guard, manual-fix dedup recheck & threading
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_source_type(paths: list[str]) -> str:
        if any(Path(p).is_dir() for p in paths):
            return "folder"
        kinds: set[str] = set()
        for p in paths:
            ext = Path(p).suffix.lower()
            if ext == ".docx":
                kinds.add("word")
            elif ext in {".xlsx", ".xlsm"}:
                kinds.add("excel")
            elif ext in {".csv", ".tsv", ".efu"}:
                kinds.add("csv")
            else:
                kinds.add("txt")
        if len(kinds) == 1:
            return next(iter(kinds))
        return "mixed" if kinds else "txt"

    def _filter_duplicate_manual_fixes(
        self,
        preview: DedupPreview,
        fixed: list[ParseResult],
        include_multiparts: bool,
    ) -> tuple[list[ParseResult], list[str]]:
        """Re-check manually fixed items against DB & current batch to avoid silent duplicate inserts."""
        if not fixed:
            return [], []

        codes = [it.canonical_code.strip().upper() for it in fixed if it.canonical_code.strip()]
        existing_by_code = self.ctx.db.find_by_codes(codes)

        seen_parts: dict[str, set[str]] = {}
        for code, recs in existing_by_code.items():
            seen_parts[code] = {normalize_part_key(r.part) for r in recs}

        batch_pool = list(preview.new_items)
        if include_multiparts:
            batch_pool.extend(preview.possible_multiparts)
        for it in batch_pool:
            c = it.canonical_code.strip().upper()
            if c:
                seen_parts.setdefault(c, set()).add(normalize_part_key(it.part))

        clean_fixed: list[ParseResult] = []
        skipped_notes: list[str] = []
        for it in fixed:
            c = it.canonical_code.strip().upper()
            pk = normalize_part_key(it.part)
            existing_parts = seen_parts.get(c)
            if existing_parts is not None and pk in existing_parts:
                reason = "库内已存在" if c in existing_by_code else "本批已包含"
                skipped_notes.append(f"[{c}] {it.file_name or it.raw_text}  ({reason})")
            else:
                clean_fixed.append(it)
                seen_parts.setdefault(c, set()).add(pk)

        return clean_fixed, skipped_notes

    def _commit(self, include_duplicates: bool) -> None:
        if not self._current_preview:
            return
        if self._is_worker_running(self._worker) or self._is_worker_running(self._commit_worker):
            self.ctx.notify("后台任务执行中，请稍候...", "warning")
            return

        current_db_path = str(self.ctx.db.db_path)
        if self._preview_db_path and self._preview_db_path != current_db_path:
            self._invalidate_preview("数据库已切换，请先点击「重新解析预演」再入库")
            self.ctx.notify("当前预演不属于已打开的数据库，请重新解析预演", "warning")
            return

        preview = self._current_preview
        include_mp = self._chk_include_multipart.isChecked()
        fixed = self._manually_fixed_items()

        if not include_duplicates and fixed:
            try:
                clean_fixed, skipped_notes = self._filter_duplicate_manual_fixes(
                    preview, fixed, include_mp
                )
            except (DatabaseError, OSError, Exception) as exc:
                ScrollableDetailDialog(
                    "查重复核失败", "复核手动修正编码时发生数据库错误：", str(exc), parent=self
                ).exec()
                return

            if skipped_notes:
                dlg = ScrollableDetailDialog(
                    title="手动修正项包含重复编码",
                    summary=(
                        f"检测到 {len(skipped_notes)} 条手动修正的记录在当前库或本批中已存在。"
                        f"点击「跳过重复并入库」将仅入库其余新增项（有效修正 {len(clean_fixed)} 条）："
                    ),
                    detail_text="\n".join(skipped_notes),
                    confirm_mode=True,
                    confirm_label="跳过重复并入库",
                    danger=False,
                    parent=self,
                )
                if dlg.exec() != ScrollableDetailDialog.DialogCode.Accepted:
                    return
                fixed = clean_fixed

        source_names = [Path(p).name for p in self._queued_paths]
        source_label = ", ".join(source_names[:3]) + (" 等" if len(source_names) > 3 else "")
        source_type = self._infer_source_type(self._queued_paths)

        self._progress_bar.setRange(0, 0)
        self._set_busy(True, cancellable=False)
        self._summary_label.setText("正在后台写入数据库，请稍候...")

        def _task() -> int:
            saved, _batch = commit_preview(
                preview,
                self.ctx.db,
                include_multiparts=include_mp,
                include_db_duplicates=include_duplicates,
                include_batch_duplicates=include_duplicates,
                extra_items=fixed,
                source_name=source_label or "批量导入",
                source_type=source_type,
            )
            return len(saved)

        worker = WorkerThread(_task, parent=self)
        self._commit_worker = worker
        worker.succeeded.connect(self._on_commit_succeeded)
        worker.failed.connect(self._on_commit_failed)
        worker.finished.connect(lambda w=worker: self._on_commit_finished(w))
        if not hasattr(worker, "safe_stop"):
            worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_commit_succeeded(self, saved_count: int) -> None:
        self._set_busy(False)
        self.ctx.notify(f"成功入库 {saved_count:,} 条记录！", "success")
        self._clear_all()
        self.ctx.data_changed.emit()

    def _on_commit_failed(self, detail: str) -> None:
        self._set_busy(False)
        self._summary_label.setText("写入数据库失败，请查看详情")
        ScrollableDetailDialog("入库失败", "写入数据库时发生错误：", detail, parent=self).exec()

    def _on_commit_finished(self, worker: WorkerThread) -> None:
        if self._commit_worker is worker:
            self._commit_worker = None
        self._set_busy(False)

    def _clear_all(self) -> None:
        if self._is_worker_running(self._worker):
            self._cancel_worker(silent=True)
        self._queued_paths.clear()
        self._update_queue_badge()
        self._invalidate_preview(
            "尚未导入文件 — 请拖入或选择 TXT、Word (.docx)、Excel (.xlsx)、CSV 文件或本地文件夹"
        )
        self._btn_rescan.setEnabled(False)
