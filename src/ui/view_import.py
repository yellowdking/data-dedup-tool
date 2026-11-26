"""View 2: 批量导入 — Drag-and-drop scanner, threaded preview, inline fix & commit."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

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


class ImportView(QWidget):
    """Batch import workflow: source selection -> threaded scan & parse -> 4-tab preview -> commit."""

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._queued_paths: list[str] = []
        self._current_preview: Optional[DedupPreview] = None
        self._worker: Optional[WorkerThread] = None

        self._new_model = PreviewTableModel("new", self)
        self._db_dup_model = PreviewTableModel("db_dup", self)
        self._batch_dup_model = PreviewTableModel("batch_dup", self)
        self._unrec_model = PreviewTableModel("unrecognized", self)
        self._unrec_model.code_manually_fixed.connect(self._on_manual_fix)

        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # Drag-and-drop source zone
        self._drop_zone = DropZoneWidget(
            title="拖入 TXT、Word (.docx)、Excel (.xlsx)、CSV 或文件夹",
            subtitle="支持 Word 段落/表格、多 Sheet Excel、CSV/TSV 及目录树清单，自动识别编码并先预演再入库",
            allow_dirs=True,
            allowed_exts=set(SUPPORTED_DOCUMENT_EXTENSIONS),
        )
        self._drop_zone.paths_dropped.connect(self._on_paths_added)

        btn_files = QPushButton("选择文件 (TXT / Word / Excel / CSV)")
        btn_files.setProperty("variant", "primary")
        btn_files.clicked.connect(self._pick_files)
        self._drop_zone.add_action_button(btn_files)

        btn_folder = QPushButton("扫描本地文件夹")
        btn_folder.clicked.connect(self._pick_folder)
        self._drop_zone.add_action_button(btn_folder)

        btn_clear = QPushButton("清空预演")
        btn_clear.setProperty("variant", "ghost")
        btn_clear.clicked.connect(self._clear_all)
        self._drop_zone.add_action_button(btn_clear)

        root.addWidget(self._drop_zone)

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
        hint = QLabel("💡 双击第一列可直接手动修正标准编码（例如输入 PRJ-0042），已修正条目会在提交时自动随本批入库。")
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
        tv.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tv.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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

    def _on_toggle_companion(self, checked: bool) -> None:
        self.ctx.config.ignore_companion_files = checked
        self.ctx.config_manager.save()

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
            self.ctx.config.default_import_dir = str(Path(paths[0]).parent)
            self.ctx.config_manager.save()
            self._on_paths_added(paths)

    def _pick_folder(self) -> None:
        start_dir = self.ctx.config.default_import_dir or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "选择要递归扫描的本地文件夹", start_dir)
        if folder:
            self.ctx.config.default_import_dir = folder
            self.ctx.config_manager.save()
            self._on_paths_added([folder])

    def _on_paths_added(self, paths: list[str]) -> None:
        self._queued_paths = list(paths)
        self._btn_rescan.setEnabled(bool(self._queued_paths))
        self._start_scan_worker()

    def _start_scan_worker(self) -> None:
        if not self._queued_paths:
            return
        if self._worker is not None and self._worker.isRunning():
            self.ctx.notify("正在解析中，请稍候或先取消当前任务", "warning")
            return

        ignore_companions = self._chk_ignore_companion.isChecked()
        comp_exts = self.ctx.config_manager.get_companion_extension_set() if ignore_companions else set()
        paths_copy = list(self._queued_paths)

        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, 0)
        self._btn_cancel.setVisible(True)
        self._btn_commit_new.setEnabled(False)
        self._btn_commit_all.setEnabled(False)
        self._summary_label.setText(f"正在后台扫描并解析 {len(paths_copy)} 个来源，请稍候...")

        def _task() -> DedupPreview:
            scanned: list[ScannedItem] = []
            for idx, p_str in enumerate(paths_copy, start=1):
                if self._worker and self._worker.is_cancelled:
                    break
                p = Path(p_str)
                if p.is_dir():
                    scanned.extend(
                        scan_folder(
                            p,
                            recursive=True,
                            companion_extensions=comp_exts,
                            cancel_check=lambda: bool(self._worker and self._worker.is_cancelled),
                        )
                    )
                elif p.is_file():
                    scanned.extend(scan_document_file(p, companion_extensions=comp_exts))

            total_scanned = len(scanned)
            skipped = 0
            if ignore_companions:
                skipped = sum(1 for s in scanned if s.is_companion)

            parsed = self.ctx.parser.parse_items(
                scanned,
                ignore_companions=ignore_companions,
                companion_extensions=comp_exts,
                cancel_check=lambda: bool(self._worker and self._worker.is_cancelled),
            )
            return preview_import(
                parsed,
                self.ctx.db,
                skipped_companions=skipped,
                total_scanned=total_scanned,
            )

        self._worker = WorkerThread(_task, parent=self)
        self._worker.succeeded.connect(self._on_scan_finished)
        self._worker.failed.connect(self._on_scan_failed)
        self._worker.start()

    def _cancel_worker(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._summary_label.setText("正在取消后台扫描...")

    def _on_scan_finished(self, preview: DedupPreview) -> None:
        self._progress_bar.setVisible(False)
        self._btn_cancel.setVisible(False)
        self.load_preview(preview)

    def _on_scan_failed(self, detail: str) -> None:
        self._progress_bar.setVisible(False)
        self._btn_cancel.setVisible(False)
        self._summary_label.setText("解析失败，请查看详情")
        dlg = ScrollableDetailDialog("扫描解析出错", "后台解析任务遇到错误：", detail, parent=self)
        dlg.exec()

    def load_preview(self, preview: DedupPreview) -> None:
        """Populate the 4 preview tables from a DedupPreview result."""
        self._current_preview = preview
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
        if not self._current_preview:
            self._btn_commit_new.setEnabled(False)
            self._btn_commit_all.setEnabled(False)
            self._fixed_count_label.setText("")
            return

        fixed = self._manually_fixed_items()
        if fixed:
            self._fixed_count_label.setText(f"已手动修正 {len(fixed)} 条未识别记录，提交时将一并入库")
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

    def _commit(self, include_duplicates: bool) -> None:
        if not self._current_preview:
            return
        fixed = self._manually_fixed_items()
        source_names = [Path(p).name for p in self._queued_paths]
        source_label = ", ".join(source_names[:3]) + (" 等" if len(source_names) > 3 else "")
        source_type = self._infer_source_type(self._queued_paths)

        saved, _batch = commit_preview(
            self._current_preview,
            self.ctx.db,
            include_multiparts=self._chk_include_multipart.isChecked(),
            include_db_duplicates=include_duplicates,
            include_batch_duplicates=include_duplicates,
            extra_items=fixed,
            source_name=source_label or "批量导入",
            source_type=source_type,
        )
        self.ctx.notify(f"成功入库 {len(saved):,} 条记录！", "success")
        self._clear_all()
        self.ctx.data_changed.emit()

    def _clear_all(self) -> None:
        self._queued_paths.clear()
        self._current_preview = None
        self._new_model.set_rows([])
        self._db_dup_model.set_rows([])
        self._batch_dup_model.set_rows([])
        self._unrec_model.set_rows([])
        self._tabs.setTabText(0, "新增待入库 (0)")
        self._tabs.setTabText(1, "库内重复 (0)")
        self._tabs.setTabText(2, "批内重复 (0)")
        self._tabs.setTabText(3, "未识别与待确认 (0)")
        self._summary_label.setText("尚未导入文件 — 请拖入或选择 TXT、Word (.docx)、Excel (.xlsx)、CSV 文件或本地文件夹")
        self._fixed_count_label.setText("")
        self._btn_rescan.setEnabled(False)
        self._btn_commit_new.setEnabled(False)
        self._btn_commit_all.setEnabled(False)
