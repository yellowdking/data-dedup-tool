"""View 2: 批量导入 — Two-state drag-and-drop scanner, threaded preview, inline fix & commit."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMenu,
    QProgressBar, QPushButton, QSizePolicy, QTableView, QTabWidget, QVBoxLayout, QWidget,
)

from src.core.database import DatabaseError, normalize_part_key
from src.core.dedup import commit_preview, preview_import
from src.core.models import DedupPreview, ParseResult, ScannedItem
from src.core.scanner import SUPPORTED_DOCUMENT_EXTENSIONS, scan_document_file, scan_folder
from src.ui.app_context import AppContext
from src.ui.delegates import enhance_table_view, sync_widget_craft_theme
from src.ui.icons import get_icon, render_icon_pixmap
from src.ui.table_copy import TableCopyHelper
from src.ui.table_header import TableHeaderManager
from src.ui.table_models import PreviewTableModel
from src.ui.theme import get_palette
from src.ui.widgets import DropZoneWidget, PageHeaderBar, ScrollableDetailDialog, WorkerThread

_IMPORT_FILE_FILTER = (
    "支持的文档与清单 (*.txt *.docx *.xlsx *.xlsm *.csv *.tsv *.efu *.log);;"
    "Word 文档 (*.docx);;Excel 表格 (*.xlsx *.xlsm);;"
    "文本与 CSV (*.txt *.csv *.tsv *.efu *.log);;所有文件 (*.*)"
)


def _repolish(w: QWidget) -> None:
    if (st := w.style()) is not None:
        st.unpolish(w)
        st.polish(w)


def _btn(
    text: str = "", *, variant: str = "", compact: bool = False,
    icon_only: bool = False, tooltip: str = "", slot: Optional[Callable[..., Any]] = None,
) -> QPushButton:
    b = QPushButton(text)
    if variant:
        b.setProperty("variant", variant)
    if compact:
        b.setProperty("compact", True)
    if icon_only:
        b.setProperty("iconOnly", True)
    if tooltip:
        b.setToolTip(tooltip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if slot is not None:
        b.clicked.connect(slot)
    return b


class ImportView(QWidget):
    """Batch import workflow: State 0 empty canvas <-> State 1 compact bar + 4-tab preview."""

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
        self._is_empty_state: bool = True

        theme = self.ctx.config.theme
        self._new_model = PreviewTableModel("new", self, theme=theme)
        self._db_dup_model = PreviewTableModel("db_dup", self, theme=theme)
        self._batch_dup_model = PreviewTableModel("batch_dup", self, theme=theme)
        self._unrec_model = PreviewTableModel("unrecognized", self, theme=theme)
        self._unrec_model.code_manually_fixed.connect(self._on_manual_fix)

        self._header_managers: list[TableHeaderManager] = []
        self._copy_helpers: dict[QTableView, TableCopyHelper] = {}

        self._setup_ui()
        self._apply_view_state(empty=True)
        self._sync_settings()

        self.ctx.db_switched.connect(self._on_db_switched)
        self.ctx.settings_changed.connect(self._sync_settings)
        self.ctx.theme_changed.connect(self.apply_theme)
        self.ctx.table_layouts_reset.connect(self._schedule_column_fit)

    def _setup_ui(self) -> None:
        theme = self.ctx.config.theme
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._header = PageHeaderBar("批量导入")
        self._btn_options = _btn("选项", variant="ghost", compact=True)
        self._options_menu = QMenu(self._btn_options)
        self._options_menu.aboutToShow.connect(self._rebuild_options_menu)
        self._btn_options.setMenu(self._options_menu)
        self._header.add_action_widget(self._btn_options)
        self._btn_columns = _btn(variant="ghost", icon_only=True, tooltip="自定义当前表显示列与顺序", slot=self._open_active_column_menu)
        self._header.add_action_widget(self._btn_columns)
        root.addWidget(self._header)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(20, 14, 20, 14)
        body_layout.setSpacing(10)

        self._empty_top_spacer = QWidget()
        self._empty_top_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        body_layout.addWidget(self._empty_top_spacer, 1)

        self._drop_zone = DropZoneWidget(
            title="拖入 TXT、Word (.docx)、Excel (.xlsx)、CSV 或文件夹开始批量导入",
            subtitle="支持多文件与目录递归扫描，先预览去重分类，确认后再写入本地数据库",
            allow_dirs=True, allowed_exts=set(SUPPORTED_DOCUMENT_EXTENSIONS), icon_name="upload", theme=theme,
        )
        self._drop_zone.paths_dropped.connect(self._on_paths_added)

        self._btn_files = _btn("选择文件 (TXT/Word/Excel/CSV)", variant="primary", slot=self._pick_files)
        self._btn_folder = _btn("扫描文件夹", slot=self._pick_folder)
        self._btn_rescan = _btn("重新扫描", compact=True, slot=self._start_scan_worker)
        self._btn_rescan.setEnabled(False)
        self._btn_clear = _btn("清空", variant="ghost", compact=True, slot=self._clear_all)
        for b in (self._btn_files, self._btn_folder, self._btn_rescan, self._btn_clear):
            self._drop_zone.add_action_button(b)
        body_layout.addWidget(self._drop_zone)

        self._empty_opts_widget = QWidget()
        opts_layout = QHBoxLayout(self._empty_opts_widget)
        opts_layout.setContentsMargins(0, 2, 0, 0)
        opts_layout.setSpacing(18)
        opts_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._chk_ignore_companion = QCheckBox("忽略伴随扩展名 (.jpg / .nfo / .srt / .txt 等)")
        self._chk_ignore_companion.setChecked(self.ctx.config.ignore_companion_files)
        self._chk_ignore_companion.toggled.connect(self._on_toggle_companion)
        opts_layout.addWidget(self._chk_ignore_companion)
        self._chk_include_multipart = QCheckBox("疑似分集随新增一并入库")
        self._chk_include_multipart.setChecked(True)
        self._chk_include_multipart.setVisible(bool(self.ctx.config.show_advanced_fields))
        self._chk_include_multipart.toggled.connect(lambda _: self._refresh_commit_buttons())
        opts_layout.addWidget(self._chk_include_multipart)
        body_layout.addWidget(self._empty_opts_widget)

        self._empty_bottom_spacer = QWidget()
        self._empty_bottom_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        body_layout.addWidget(self._empty_bottom_spacer, 1)

        self._workspace_widget = QWidget()
        ws_layout = QVBoxLayout(self._workspace_widget)
        ws_layout.setContentsMargins(0, 0, 0, 0)
        ws_layout.setSpacing(8)

        self._progress_strip = QFrame()
        self._progress_strip.setProperty("summaryStrip", True)
        self._progress_strip.setVisible(False)
        pl = QHBoxLayout(self._progress_strip)
        pl.setContentsMargins(12, 6, 12, 6)
        pl.setSpacing(10)
        self._progress_status = QLabel("正在后台扫描并解析...")
        self._progress_status.setProperty("role", "subtitle")
        pl.addWidget(self._progress_status, 1)
        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedSize(220, 10)
        self._progress_bar.setVisible(False)
        pl.addWidget(self._progress_bar)
        self._btn_cancel = _btn("取消", variant="ghost", compact=True, slot=self._cancel_worker)
        self._btn_cancel.setVisible(False)
        pl.addWidget(self._btn_cancel)
        ws_layout.addWidget(self._progress_strip)

        self._tabs = QTabWidget()
        self._tabs.setProperty("subtabs", True)
        self._table_new = self._make_table(self._new_model, "import_new", False)
        self._table_db_dup = self._make_table(self._db_dup_model, "import_db_dup", False)
        self._table_batch_dup = self._make_table(self._batch_dup_model, "import_batch_dup", False)
        self._table_unrec = self._make_table(self._unrec_model, "import_unrec", True)

        unrec_box = QWidget()
        ul = QVBoxLayout(unrec_box)
        ul.setContentsMargins(8, 8, 8, 8)
        ul.setSpacing(6)
        hr = QHBoxLayout()
        hr.setContentsMargins(2, 0, 2, 0)
        hr.setSpacing(6)
        self._unrec_hint_icon = QLabel()
        self._unrec_hint_icon.setFixedSize(16, 16)
        hr.addWidget(self._unrec_hint_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        hl = QLabel("双击第一列可直接手动修正标准编码（例如 PRJ-0042），提交前自动复核重复项。")
        hl.setProperty("role", "subtitle")
        hr.addWidget(hl, 1)
        ul.addLayout(hr)
        ul.addWidget(self._table_unrec, 1)

        for tab_w, title in ((self._table_new, "新增待入库 (0)"), (self._table_db_dup, "库内重复 (0)"), (self._table_batch_dup, "批内重复 (0)"), (unrec_box, "未识别与待确认 (0)")):
            self._tabs.addTab(tab_w, title)
        self._tabs.currentChanged.connect(lambda _i: self._schedule_column_fit())
        ws_layout.addWidget(self._tabs, 1)

        ab = QHBoxLayout()
        ab.setContentsMargins(0, 2, 0, 0)
        ab.setSpacing(10)
        self._summary_label = QLabel("")
        self._summary_label.setProperty("role", "subtitle")
        ab.addWidget(self._summary_label, 1)
        self._fixed_count_label = QLabel("")
        self._fixed_count_label.setProperty("badge", "accent")
        self._fixed_count_label.setVisible(False)
        ab.addWidget(self._fixed_count_label)
        self._btn_commit_all = _btn("全部入库（含重复项）", variant="outline", slot=lambda: self._commit(True))
        self._btn_commit_all.setEnabled(False)
        ab.addWidget(self._btn_commit_all)
        self._btn_commit_new = _btn("仅入库新增", variant="primary", slot=lambda: self._commit(False))
        self._btn_commit_new.setEnabled(False)
        ab.addWidget(self._btn_commit_new)
        ws_layout.addLayout(ab)
        body_layout.addWidget(self._workspace_widget, 1)
        root.addWidget(body, 1)

    def _make_table(self, model: PreviewTableModel, table_id: str, editable: bool) -> QTableView:
        tv = QTableView()
        tv.setModel(model)
        tv.setAlternatingRowColors(False)
        tv.setWordWrap(False)
        tv.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tv.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tv.setEditTriggers(
            (QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
            if editable else QAbstractItemView.EditTrigger.NoEditTriggers
        )
        tv.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tv.customContextMenuRequested.connect(lambda pos, t=tv: self._show_table_menu(t, pos))

        enhance_table_view(tv, theme=self.ctx.config.theme, on_code_copied=self._on_inline_code_copied, row_height=34, header_height=34)
        mgr = TableHeaderManager.attach(tv, table_id, model.column_specs(), self.ctx)
        self._header_managers.append(mgr)

        def _item_at(row: int, m: PreviewTableModel = model) -> Optional[ParseResult]:
            raw = m.row_at(row)
            return None if raw is None else (raw[0] if isinstance(raw, tuple) else raw)

        self._copy_helpers[tv] = TableCopyHelper(
            tv, self.ctx, header_manager=mgr, code_column=0,
            name_extractor=lambda r, fn=_item_at: (it.file_name or it.raw_text) if (it := fn(r)) else "",
            path_extractor=lambda r, fn=_item_at: (it.full_path or "") if (it := fn(r)) else "",
            install_shortcuts=True,
        )
        return tv

    def _apply_view_state(self, *, empty: bool) -> None:
        self._is_empty_state = bool(empty)
        for w, vis in (
            (self._empty_top_spacer, empty), (self._empty_bottom_spacer, empty), (self._empty_opts_widget, empty),
            (self._workspace_widget, not empty), (self._btn_options, not empty), (self._btn_columns, not empty),
            (self._btn_rescan, not empty), (self._btn_clear, not empty),
        ):
            w.setVisible(vis)

        if empty:
            self._drop_zone.set_compact(False)
            self._btn_files.setText("选择文件 (TXT/Word/Excel/CSV)")
            self._btn_files.setProperty("variant", "primary")
            self._btn_files.setProperty("compact", False)
            self._btn_folder.setProperty("compact", False)
            self._header.set_subtitle("")
        else:
            self._drop_zone.set_compact(True, summary_text=self._build_compact_queue_text())
            self._btn_files.setText("继续添加文件")
            self._btn_files.setProperty("variant", "")
            self._btn_files.setProperty("compact", True)
            self._btn_folder.setProperty("compact", True)
            self._drop_zone.setToolTip("\n".join(self._queued_paths))
        _repolish(self._btn_files)
        _repolish(self._btn_folder)
        self._refresh_icons(self.ctx.config.theme)
        if not empty:
            self._schedule_column_fit()

    def _schedule_column_fit(self) -> None:
        self._fit_active_table_to_viewport()
        QTimer.singleShot(0, self._fit_active_table_to_viewport)

    def _fit_active_table_to_viewport(self) -> None:
        if self._is_empty_state or (tv := self._active_table()) is None:
            return
        mgr = getattr(tv, "_table_header_manager", None)
        if not isinstance(mgr, TableHeaderManager) or not mgr._table_id or mgr._table_id in self.ctx.config.table_layouts or tv.viewport().width() <= 200:
            return
        hdr = tv.horizontalHeader()
        hdr.blockSignals(True)
        try:
            for c, sp in enumerate(mgr._columns):
                if not hdr.isSectionHidden(c):
                    tv.setColumnWidth(c, sp.default_width)
            mgr._distribute_spare_width(len(mgr._columns))
        finally:
            hdr.blockSignals(False)

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        if not self._is_empty_state:
            self._schedule_column_fit()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if not self._is_empty_state:
            self._fit_active_table_to_viewport()

    def _build_compact_queue_text(self) -> str:
        count = len(self._queued_paths)
        if not count:
            return "拖入文件或文件夹添加到导入队列"
        names = [Path(p).name or p for p in self._queued_paths]
        preview = "、".join(names[:3]) + (f" 等共 {count} 个" if count > 3 else "")
        return f"已选 {count} 个源 · {preview}"

    def shutdown(self) -> None:
        self._scan_gen += 1
        for attr in ("_worker", "_commit_worker"):
            if (worker := getattr(self, attr, None)) is not None:
                worker.safe_stop()
                setattr(self, attr, None)
        for mgr in self._header_managers:
            mgr.flush()

    def on_activated(self) -> None:
        self._sync_settings()

    def focus_search(self) -> None:
        if self._is_empty_state:
            self._btn_files.setFocus()
        elif (tv := self._active_table()) is not None:
            tv.setFocus()

    def _active_table(self) -> Optional[QTableView]:
        tables = (self._table_new, self._table_db_dup, self._table_batch_dup, self._table_unrec)
        idx = self._tabs.currentIndex()
        return tables[idx] if 0 <= idx < len(tables) else None

    def _open_active_column_menu(self) -> None:
        tv = self._active_table()
        if tv is not None and isinstance(mgr := getattr(tv, "_table_header_manager", None), TableHeaderManager):
            mgr.open_column_menu(self._btn_columns.mapToGlobal(QPoint(0, self._btn_columns.height() + 4)))

    def _rebuild_options_menu(self) -> None:
        menu = self._options_menu
        menu.clear()
        for text, chk, show in (
            ("忽略伴随扩展名 (.jpg / .nfo / .srt / .txt 等)", self._chk_ignore_companion, True),
            ("疑似分集随新增一并入库", self._chk_include_multipart, bool(self.ctx.config.show_advanced_fields)),
        ):
            if show:
                act = QAction(text, menu)
                act.setCheckable(True)
                act.setChecked(chk.isChecked())
                act.toggled.connect(chk.setChecked)
                menu.addAction(act)

    def _refresh_icons(self, theme: str) -> None:
        p = get_palette(theme)
        for btn, name, sz, color in (
            (self._btn_files, "plus", 15, p["cta_text"] if self._is_empty_state else p["icon_default"]),
            (self._btn_folder, "folder-open", 15, p["icon_default"]),
            (self._btn_rescan, "refresh-cw", 14, p["icon_default"]),
            (self._btn_columns, "columns", 15, p["icon_default"]),
            (self._btn_commit_new, "check", 15, p["cta_text"]),
        ):
            btn.setIcon(get_icon(name, sz, color, theme=theme))
            btn.setIconSize(QSize(sz, sz))
        self._unrec_hint_icon.setPixmap(render_icon_pixmap("info", 15, p["accent"], stroke_width=1.6))

    def apply_theme(self, theme: str) -> None:
        for m in (self._new_model, self._db_dup_model, self._batch_dup_model, self._unrec_model):
            m.set_theme(theme)
        self._drop_zone.apply_theme(theme)
        self._refresh_icons(theme)
        sync_widget_craft_theme(self, theme)

    def _sync_settings(self) -> None:
        self._chk_ignore_companion.blockSignals(True)
        self._chk_ignore_companion.setChecked(self.ctx.config.ignore_companion_files)
        self._chk_ignore_companion.blockSignals(False)
        show_adv = bool(self.ctx.config.show_advanced_fields)
        self._chk_include_multipart.setVisible(show_adv)
        if not show_adv and not self._chk_include_multipart.isChecked():
            self._chk_include_multipart.setChecked(True)
        if self._current_preview is not None:
            self._update_preview_summary(self._current_preview)

    def _on_inline_code_copied(self, code: str) -> None:
        if code:
            self.ctx.notify(f"已复制编码：{code}", "success")

    def _on_db_switched(self, _db_path: str) -> None:
        if self._is_worker_running(self._worker):
            self._cancel_worker(silent=True)
        self._invalidate_preview("数据库已切换，当前预演结果已失效 — 请点击「重新扫描」针对新库重新比对" if self._queued_paths else "")

    def _invalidate_preview(self, reason: str = "") -> None:
        self._current_preview = None
        self._preview_db_path = ""
        for m in (self._new_model, self._db_dup_model, self._batch_dup_model, self._unrec_model):
            m.set_rows([])
        for idx, title in enumerate(("新增待入库 (0)", "库内重复 (0)", "批内重复 (0)", "未识别与待确认 (0)")):
            self._tabs.setTabText(idx, title)
        self._fixed_count_label.setText("")
        self._fixed_count_label.setVisible(False)
        self._btn_commit_new.setEnabled(False)
        self._btn_commit_all.setEnabled(False)
        self._summary_label.setText(reason)
        if not self._is_empty_state:
            self._header.set_subtitle(f"已排队 {len(self._queued_paths)} 个源" if self._queued_paths else "")

    def _is_worker_running(self, worker: Optional[WorkerThread]) -> bool:
        try:
            return bool(worker is not None and worker.isRunning())
        except RuntimeError:
            return False

    def _set_busy(self, busy: bool, cancellable: bool = False) -> None:
        self._progress_strip.setVisible(busy)
        self._progress_bar.setVisible(busy)
        self._btn_cancel.setVisible(busy and cancellable)
        self._btn_cancel.setEnabled(busy and cancellable)
        for b in (self._btn_files, self._btn_folder, self._btn_clear):
            b.setEnabled(not busy)
        self._btn_rescan.setEnabled((not busy) and bool(self._queued_paths))
        if busy:
            self._btn_commit_new.setEnabled(False)
            self._btn_commit_all.setEnabled(False)
        else:
            self._refresh_commit_buttons()

    def _update_queue_summary(self) -> None:
        if not self._queued_paths:
            self._drop_zone.setToolTip("")
            return
        self._drop_zone.set_compact_text(self._build_compact_queue_text())
        self._drop_zone.setToolTip("\n".join(self._queued_paths))

    def _on_toggle_companion(self, checked: bool) -> None:
        self.ctx.config.ignore_companion_files = checked
        try:
            self.ctx.config_manager.save()
        except OSError as exc:
            self.ctx.notify(f"保存配置失败：{exc}", "warning")
        self.ctx.settings_changed.emit()

    def _save_import_dir(self, dir_path: str) -> None:
        try:
            self.ctx.config.default_import_dir = dir_path
            self.ctx.config_manager.save()
        except OSError:
            pass

    def _pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要导入的文件（支持多选）", self.ctx.config.default_import_dir or str(Path.home()), _IMPORT_FILE_FILTER)
        if paths:
            self._save_import_dir(str(Path(paths[0]).parent))
            self._on_paths_added(paths)

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择要递归扫描的本地文件夹", self.ctx.config.default_import_dir or str(Path.home()))
        if folder:
            self._save_import_dir(folder)
            self._on_paths_added([folder])

    def _on_paths_added(self, paths: list[str]) -> None:
        if self._is_worker_running(self._worker) or self._is_worker_running(self._commit_worker):
            self.ctx.notify("后台任务运行中，请稍候或先取消当前扫描", "warning")
            return
        seen = {os.path.normcase(os.path.normpath(p)) for p in self._queued_paths}
        added = 0
        for p in paths:
            norm = os.path.normcase(os.path.normpath(p))
            if norm not in seen:
                seen.add(norm)
                self._queued_paths.append(p)
                added += 1
        if self._queued_paths:
            self._apply_view_state(empty=False)
            self._update_queue_summary()
        self._btn_rescan.setEnabled(bool(self._queued_paths))
        if added == 0 and self._current_preview is not None:
            self.ctx.notify("所选文件已在导入队列中", "info")
            return
        self._start_scan_worker()

    def _start_scan_worker(self) -> None:
        if not self._queued_paths:
            return
        if self._is_worker_running(self._worker) or self._is_worker_running(self._commit_worker):
            self.ctx.notify("正在处理中，请稍候或先取消当前任务", "warning")
            return
        if self._is_empty_state:
            self._apply_view_state(empty=False)
        self._update_queue_summary()

        ignore_comp = self._chk_ignore_companion.isChecked()
        comp_exts = self.ctx.get_companion_extensions(respect_global_toggle=False) if ignore_comp else set()
        paths_copy, expected_db = list(self._queued_paths), str(self.ctx.db.db_path)

        self._scan_gen += 1
        gen = self._scan_gen
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setValue(0)
        self._set_busy(True, cancellable=True)
        msg = f"正在后台扫描并解析 {len(paths_copy)} 个来源，请稍候..."
        self._progress_status.setText(msg)
        self._summary_label.setText(msg)

        wbox: list[Optional[WorkerThread]] = [None]
        _cancelled = lambda: bool(wbox[0] is not None and wbox[0].is_cancelled)
        _prog = lambda c, t, s: wbox[0].emit_progress(c, t, s) if (wbox[0] is not None and not wbox[0].is_cancelled) else None

        def _task() -> Optional[DedupPreview]:
            scanned: list[ScannedItem] = []
            tot = len(paths_copy)
            for idx, p_str in enumerate(paths_copy, start=1):
                if _cancelled():
                    return None
                p = Path(p_str)
                if p.is_dir():
                    _prog(0, 0, f"扫描目录 ({idx}/{tot}): {p.name}...")
                    scanned.extend(scan_folder(
                        p, recursive=True, companion_extensions=comp_exts, cancel_check=_cancelled,
                        progress_callback=lambda cnt, fn, i=idx, ts=tot: _prog(0, 0, f"扫描目录 ({i}/{ts})：已发现 {cnt:,} ({fn})"),
                    ))
                elif p.is_file():
                    _prog(idx - 1, tot, f"读取文件 ({idx}/{tot}): {p.name}...")
                    scanned.extend(scan_document_file(p, companion_extensions=comp_exts))
                    _prog(idx, tot, f"已读取 ({idx}/{tot}): {p.name}")
            if _cancelled():
                return None
            total_scanned = len(scanned)
            skipped = sum(1 for s in scanned if s.is_companion) if ignore_comp else 0
            if total_scanned:
                _prog(0, total_scanned, f"正在解析 {total_scanned:,} 条编码...")
            parsed = self.ctx.parser.parse_items(
                scanned, ignore_companions=ignore_comp, companion_extensions=comp_exts,
                progress_callback=lambda c, t: _prog(c, t, f"解析编码 ({c:,}/{t:,})..."), cancel_check=_cancelled,
            )
            if _cancelled():
                return None
            _prog(0, 0, f"比对数据库 ({len(parsed):,} 条)...")
            return preview_import(parsed, self.ctx.db, skipped_companions=skipped, total_scanned=total_scanned)

        w = WorkerThread(_task, parent=self)
        wbox[0] = self._worker = w
        w.progress.connect(lambda c, t, m, g=gen: self._on_scan_progress(g, c, t, m))
        w.succeeded.connect(lambda prev, g=gen, dbp=expected_db: self._on_scan_finished(g, prev, dbp))
        w.failed.connect(lambda detail, g=gen: self._on_scan_failed(g, detail))
        w.cancelled.connect(lambda g=gen: self._on_scan_cancelled(g))
        w.finished.connect(lambda wk=w, g=gen: self._on_scan_thread_finished(wk, g))
        w.start()

    def _cancel_worker(self, silent: bool = False) -> None:
        if self._is_worker_running(self._worker):
            assert self._worker is not None
            self._worker.cancel()
            self._btn_cancel.setEnabled(False)
            if not silent:
                self._progress_status.setText("正在取消后台扫描...")
                self._summary_label.setText("正在取消后台扫描...")

    def _on_scan_progress(self, gen: int, current: int, total: int, message: str) -> None:
        if gen != self._scan_gen:
            return
        self._progress_bar.setRange(0, total if total > 0 else 0)
        if total > 0:
            self._progress_bar.setValue(min(current, total))
        if message:
            self._progress_status.setText(message)
            self._summary_label.setText(message)

    def _on_scan_cancelled(self, gen: int) -> None:
        if gen != self._scan_gen or self._cancel_handled_gen == gen:
            return
        self._cancel_handled_gen = gen
        self._set_busy(False)
        self._summary_label.setText("已取消后台扫描 — 可点击「重新扫描」继续或清空队列")
        self.ctx.notify("已取消扫描解析", "info")

    def _on_scan_finished(self, gen: int, preview: Optional[DedupPreview], expected_db: str) -> None:
        if gen != self._scan_gen:
            return
        self._set_busy(False)
        if preview is None:
            self._on_scan_cancelled(gen)
        elif str(self.ctx.db.db_path) != expected_db:
            self._invalidate_preview("扫描期间数据库已切换，预演结果已丢弃 — 请点击「重新扫描」")
            self.ctx.notify("数据库已切换，请重新扫描预演", "warning")
        else:
            self._preview_db_path = expected_db
            self.load_preview(preview)

    def _on_scan_failed(self, gen: int, detail: str) -> None:
        if gen != self._scan_gen:
            return
        self._set_busy(False)
        self._summary_label.setText("解析失败，请查看详情")
        ScrollableDetailDialog("扫描解析出错", "后台解析任务遇到错误：", detail, parent=self).exec()

    def _on_scan_thread_finished(self, worker: WorkerThread, gen: int) -> None:
        try:
            was_cancelled = worker.is_cancelled
        except RuntimeError:
            was_cancelled = False
        if self._worker is worker:
            self._worker = None
        if was_cancelled and gen == self._scan_gen and self._cancel_handled_gen != gen:
            self._on_scan_cancelled(gen)

    def _update_preview_summary(self, preview: DedupPreview) -> None:
        cnt = len(preview.new_items) + len(preview.possible_multiparts)
        for idx, txt in enumerate((f"新增待入库 ({cnt})", f"库内重复 ({len(preview.db_duplicates)})", f"批内重复 ({len(preview.batch_duplicates)})", f"未识别与待确认 ({len(preview.unrecognized)})")):
            self._tabs.setTabText(idx, txt)
        parts = [f"共扫描 {preview.total_scanned:,} 条"]
        if preview.skipped_companions:
            parts.append(f"跳过伴随 {preview.skipped_companions:,}")
        parts.append(f"新增 {cnt:,}")
        if self.ctx.config.show_advanced_fields and preview.possible_multiparts:
            parts.append(f"含分集 {len(preview.possible_multiparts):,}")
        parts.extend((f"库内重复 {len(preview.db_duplicates):,}", f"批内重复 {len(preview.batch_duplicates):,}", f"未识别 {len(preview.unrecognized):,}"))
        self._summary_label.setText(" · ".join(parts))
        self._header.set_metrics([f"新增 {cnt:,}", f"重复 {len(preview.db_duplicates) + len(preview.batch_duplicates):,}"])

    def load_preview(self, preview: DedupPreview) -> None:
        self._current_preview = preview
        self._preview_db_path = self._preview_db_path or str(self.ctx.db.db_path)
        if self._is_empty_state:
            self._apply_view_state(empty=False)
        self._new_model.set_rows(list(preview.new_items) + list(preview.possible_multiparts))
        self._db_dup_model.set_rows(preview.db_duplicates)
        self._batch_dup_model.set_rows(preview.batch_duplicates)
        self._unrec_model.set_rows(preview.unrecognized)
        self._update_preview_summary(preview)
        self._refresh_commit_buttons()
        self._schedule_column_fit()
        self.ctx.notify("批量导入预演完成，请确认后点击入库", "success")

    def _on_manual_fix(self, _row: int, _code: str) -> None:
        self._refresh_commit_buttons()

    def _manually_fixed_items(self) -> list[ParseResult]:
        return [it for it in self._current_preview.unrecognized if it.recognized and it.canonical_code.strip()] if self._current_preview else []

    def _include_multiparts_enabled(self) -> bool:
        return self._chk_include_multipart.isChecked() if self.ctx.config.show_advanced_fields else True

    def _refresh_commit_buttons(self) -> None:
        if not self._current_preview or self._is_worker_running(self._worker) or self._is_worker_running(self._commit_worker):
            self._btn_commit_new.setEnabled(False)
            self._btn_commit_all.setEnabled(False)
            if not self._current_preview:
                self._fixed_count_label.setText("")
                self._fixed_count_label.setVisible(False)
            return
        fixed = self._manually_fixed_items()
        self._fixed_count_label.setText(f"已手动修正 {len(fixed)} 条" if fixed else "")
        self._fixed_count_label.setVisible(bool(fixed))
        mp_cnt = len(self._current_preview.possible_multiparts) if self._include_multiparts_enabled() else 0
        new_count = len(self._current_preview.new_items) + mp_cnt + len(fixed)
        all_count = new_count + len(self._current_preview.db_duplicates) + len(self._current_preview.batch_duplicates)
        self._btn_commit_new.setText(f"仅入库新增 ({new_count:,})" if new_count > 0 else "仅入库新增")
        self._btn_commit_new.setEnabled(new_count > 0)
        self._btn_commit_all.setEnabled(all_count > 0)

    def _show_table_menu(self, tv: QTableView, pos: Any) -> None:
        helper = self._copy_helpers.get(tv)
        if helper is None or not helper.selected_rows():
            return
        menu = QMenu(self)
        helper.populate_context_menu(menu)
        if not menu.isEmpty():
            menu.exec(tv.viewport().mapToGlobal(pos))

    # --- Commit with stale-DB guard & manual-fix dedup recheck ---

    @staticmethod
    def _infer_source_type(paths: list[str]) -> str:
        if any(Path(p).is_dir() for p in paths):
            return "folder"
        ext_map = {".docx": "word", ".xlsx": "excel", ".xlsm": "excel", ".csv": "csv", ".tsv": "csv", ".efu": "csv"}
        kinds = {ext_map.get(Path(p).suffix.lower(), "txt") for p in paths}
        return next(iter(kinds)) if len(kinds) == 1 else ("mixed" if kinds else "txt")

    def _filter_duplicate_manual_fixes(self, preview: DedupPreview, fixed: list[ParseResult], include_multiparts: bool) -> tuple[list[ParseResult], list[str]]:
        if not fixed:
            return [], []
        existing = self.ctx.db.find_by_codes([it.canonical_code.strip().upper() for it in fixed if it.canonical_code.strip()])
        seen: dict[str, set[str]] = {code: {normalize_part_key(r.part) for r in recs} for code, recs in existing.items()}
        for it in list(preview.new_items) + (list(preview.possible_multiparts) if include_multiparts else []):
            if (c := it.canonical_code.strip().upper()):
                seen.setdefault(c, set()).add(normalize_part_key(it.part))
        clean, skipped = [], []
        for it in fixed:
            c, pk = it.canonical_code.strip().upper(), normalize_part_key(it.part)
            if pk in seen.get(c, set()):
                skipped.append(f"[{c}] {it.file_name or it.raw_text}  ({'库内已存在' if c in existing else '本批已包含'})")
            else:
                clean.append(it)
                seen.setdefault(c, set()).add(pk)
        return clean, skipped

    def _commit(self, include_duplicates: bool) -> None:
        if not self._current_preview:
            return
        if self._is_worker_running(self._worker) or self._is_worker_running(self._commit_worker):
            self.ctx.notify("后台任务执行中，请稍候...", "warning")
            return
        if self._preview_db_path and self._preview_db_path != str(self.ctx.db.db_path):
            self._invalidate_preview("数据库已切换，请先点击「重新扫描」再入库")
            self.ctx.notify("当前预演不属于已打开的数据库，请重新扫描预演", "warning")
            return

        preview, include_mp, fixed = self._current_preview, self._include_multiparts_enabled(), self._manually_fixed_items()
        if not include_duplicates and fixed:
            try:
                clean, skipped = self._filter_duplicate_manual_fixes(preview, fixed, include_mp)
            except (DatabaseError, OSError, Exception) as exc:
                ScrollableDetailDialog("查重复核失败", "复核手动修正编码时发生数据库错误：", str(exc), parent=self).exec()
                return
            if skipped:
                dlg = ScrollableDetailDialog(
                    title="手动修正项包含重复编码",
                    summary=f"检测到 {len(skipped)} 条手动修正记录已存在。点击「跳过重复并入库」仅入库其余新增项（有效修正 {len(clean)} 条）：",
                    detail_text="\n".join(skipped), confirm_mode=True, confirm_label="跳过重复并入库", danger=False, parent=self,
                )
                if dlg.exec() != ScrollableDetailDialog.DialogCode.Accepted:
                    return
                fixed = clean

        names = [Path(p).name for p in self._queued_paths]
        source_label = ", ".join(names[:3]) + (" 等" if len(names) > 3 else "")
        source_type = self._infer_source_type(self._queued_paths)
        self._progress_bar.setRange(0, 0)
        self._set_busy(True, cancellable=False)
        self._progress_status.setText("正在后台写入数据库，请稍候...")
        self._summary_label.setText("正在后台写入数据库，请稍候...")

        def _task() -> int:
            saved, _ = commit_preview(
                preview, self.ctx.db, include_multiparts=include_mp,
                include_db_duplicates=include_duplicates, include_batch_duplicates=include_duplicates,
                extra_items=fixed, source_name=source_label or "批量导入", source_type=source_type,
            )
            return len(saved)

        w = WorkerThread(_task, parent=self)
        self._commit_worker = w
        w.succeeded.connect(self._on_commit_succeeded)
        w.failed.connect(self._on_commit_failed)
        w.finished.connect(lambda wk=w: self._on_commit_finished(wk))
        w.start()

    def _on_commit_succeeded(self, saved_count: int) -> None:
        self._set_busy(False)
        self.ctx.notify(f"成功入库 {saved_count:,} 条记录", "success")
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
        self._invalidate_preview("")
        self._btn_rescan.setEnabled(False)
        self._apply_view_state(empty=True)
