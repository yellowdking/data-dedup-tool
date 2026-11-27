"""Multi-Table Studio view (多表工作台) for 数据查重程序 v2.0.0.

Provides unified multi-format table management (Excel/Word/CSV/TXT/Database),
interactive multi-sheet / Word-table / header-row import picking, column profiling
and renaming, virtualized preview & search, cross-table/intra-table deduplication,
union/dedup/coalesce/join merging, qBittorrent-style customizable table headers,
and fully asynchronous background exports.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.doc_reader import read_database_as_table
from src.core.table_data import (
    DataTable,
    TableDedupReport,
    TableMergeResult,
    TableRowRef,
)
from src.ui.app_context import AppContext
from src.ui.delegates import sync_widget_craft_theme
from src.ui.icons import get_icon
from src.ui.table_header import TableHeaderManager
from src.ui.table_preview_model import DynamicTableModel
from src.ui.tables_dedup_tab import TablesDedupTabMixin
from src.ui.tables_dialogs import (
    _ColumnManagerDialog,
    _ImportOptionsDialog,
    _PerTableKeyDialog,
    _RowDetailDialog,
    _call_load_tables_from_file,
    _inspect_file_for_import,
)
from src.ui.tables_merge_tab import TablesMergeTabMixin
from src.ui.tables_source_panel import TablesSourcePanelMixin
from src.ui.tables_widgets import (
    TablesPreviewAndExportMixin,
    _BadgeIconCache,
    _CompactMetricCard,
    _CompactTableDropZone,
    _InlineMetricBar,
    _format_row_summary,
    _set_leading_search_icon,
)
from src.ui.theme import get_palette
from src.ui.widgets import PageHeaderBar, ScrollableDetailDialog, WorkerThread

__all__ = [
    "TablesView",
    "_BadgeIconCache",
    "_ColumnManagerDialog",
    "_CompactMetricCard",
    "_CompactTableDropZone",
    "_ImportOptionsDialog",
    "_InlineMetricBar",
    "_PerTableKeyDialog",
    "_RowDetailDialog",
    "_call_load_tables_from_file",
    "_format_row_summary",
    "_inspect_file_for_import",
]


class TablesView(
    TablesSourcePanelMixin,
    TablesPreviewAndExportMixin,
    TablesDedupTabMixin,
    TablesMergeTabMixin,
    QWidget,
):
    """Multi-Table Studio view: manage, preview, deduplicate, merge, and export tabular datasets."""

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx

        # Loaded tables registry (ordered)
        self._tables: list[DataTable] = []
        self._tables_by_id: dict[str, DataTable] = {}
        self._active_table_id: Optional[str] = None
        self._db_snapshot_stale: bool = False

        # Per-table custom key mappings for column_value mode
        self._dedup_custom_keys: dict[str, list[str]] = {}
        self._merge_custom_keys: dict[str, list[str]] = {}

        # Last computed results & input table snapshots
        self._last_dedup_report: Optional[TableDedupReport] = None
        self._last_dedup_snapshot: Optional[tuple[tuple[str, str, int, int, tuple[str, ...]], ...]] = None
        self._last_merge_result: Optional[TableMergeResult] = None
        self._last_merge_snapshot: Optional[tuple[tuple[str, str, int, int, tuple[str, ...]], ...]] = None

        # Lazy dedup sub-tab population state & row references for double-click jump
        self._dedup_tab_populated: set[int] = set()
        self._dedup_tab_refs: dict[int, list[TableRowRef]] = {}
        self._merge_dropped_refs: list[TableRowRef] = []
        self._header_managers: list[TableHeaderManager] = []

        # Background worker threads & monotonic run generation tokens
        self._inspect_worker: Optional[WorkerThread] = None
        self._inspect_gen: int = 0
        self._load_worker: Optional[WorkerThread] = None
        self._load_gen: int = 0
        self._db_load_worker: Optional[WorkerThread] = None
        self._db_load_gen: int = 0
        self._dedup_worker: Optional[WorkerThread] = None
        self._dedup_gen: int = 0
        self._merge_worker: Optional[WorkerThread] = None
        self._merge_gen: int = 0
        self._export_worker: Optional[WorkerThread] = None
        self._export_gen: int = 0
        self._db_import_worker: Optional[WorkerThread] = None
        self._db_import_gen: int = 0

        theme = self.ctx.config.theme

        # Virtualized models
        self._preview_model = DynamicTableModel(theme=theme, parent=self)
        self._dedup_cross_model = DynamicTableModel(theme=theme, parent=self)
        self._dedup_intra_model = DynamicTableModel(theme=theme, parent=self)
        self._dedup_inter_model = DynamicTableModel(theme=theme, parent=self)
        self._dedup_unique_model = DynamicTableModel(theme=theme, parent=self)
        self._dedup_unrec_model = DynamicTableModel(theme=theme, parent=self)
        self._merge_result_model = DynamicTableModel(theme=theme, parent=self)
        self._merge_dropped_model = DynamicTableModel(theme=theme, parent=self)

        self._all_models: list[DynamicTableModel] = [
            self._preview_model,
            self._dedup_cross_model,
            self._dedup_intra_model,
            self._dedup_inter_model,
            self._dedup_unique_model,
            self._dedup_unrec_model,
            self._merge_result_model,
            self._merge_dropped_model,
        ]

        # Debounce timers for instant table search
        self._preview_search_timer = QTimer(self)
        self._preview_search_timer.setSingleShot(True)
        self._preview_search_timer.setInterval(120)
        self._preview_search_timer.timeout.connect(self._apply_preview_filter)

        self._dedup_search_timer = QTimer(self)
        self._dedup_search_timer.setSingleShot(True)
        self._dedup_search_timer.setInterval(120)
        self._dedup_search_timer.timeout.connect(self._apply_dedup_filter)

        self._merge_search_timer = QTimer(self)
        self._merge_search_timer.setSingleShot(True)
        self._merge_search_timer.setInterval(120)
        self._merge_search_timer.timeout.connect(self._apply_merge_filter)

        self._setup_ui()
        self._refresh_view_icons(theme)
        self.ctx.theme_changed.connect(self.apply_theme)
        self.ctx.settings_changed.connect(self._on_settings_changed)
        self.ctx.data_changed.connect(self._on_db_mutated)
        self.ctx.db_switched.connect(self._on_db_mutated)
        self._sync_controls_state()

    # ------------------------------------------------------------------
    # Lifecycle, Worker Management & Shell Hooks
    # ------------------------------------------------------------------

    def _make_worker(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> WorkerThread:
        sig_params = inspect.signature(WorkerThread.__init__).parameters
        if "auto_delete" in sig_params:
            return WorkerThread(fn, *args, parent=self, auto_delete=True, **kwargs)
        worker = WorkerThread(fn, *args, parent=self, **kwargs)
        worker.finished.connect(worker.deleteLater)
        return worker

    def _is_worker_running(self, worker: Optional[WorkerThread]) -> bool:
        if worker is None:
            return False
        try:
            return bool(worker.isRunning())
        except RuntimeError:
            return False

    def _clear_worker_ref(self, attr_name: str, worker: WorkerThread) -> None:
        if getattr(self, attr_name, None) is worker:
            setattr(self, attr_name, None)
        self._sync_controls_state()

    def _safe_stop_worker(self, attr_name: str, timeout_ms: int = 3000) -> None:
        worker = getattr(self, attr_name, None)
        setattr(self, attr_name, None)
        if worker is None:
            return
        try:
            if hasattr(worker, "safe_stop"):
                worker.safe_stop(timeout_ms=timeout_ms)
                return
            worker.cancel()
            for sig_name in ("progress", "succeeded", "failed", "cancelled", "finished"):
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

    def shutdown(self) -> None:
        """Flush header layouts and cleanly cancel/join all background workers."""
        self._inspect_gen += 1
        self._load_gen += 1
        self._db_load_gen += 1
        self._dedup_gen += 1
        self._merge_gen += 1
        self._export_gen += 1
        self._db_import_gen += 1

        for timer in (
            self._preview_search_timer,
            self._dedup_search_timer,
            self._merge_search_timer,
        ):
            timer.stop()

        for mgr in self._header_managers:
            try:
                mgr.flush()
            except RuntimeError:
                pass

        for attr in (
            "_inspect_worker",
            "_load_worker",
            "_db_load_worker",
            "_dedup_worker",
            "_merge_worker",
            "_export_worker",
            "_db_import_worker",
        ):
            self._safe_stop_worker(attr, timeout_ms=3000)

    def focus_search(self) -> None:
        """Focus the active sub-tab's search input (invoked by Ctrl+F from MainWindow)."""
        idx = self._work_tabs.currentIndex()
        target = (
            self._preview_search
            if idx == 0
            else (self._dedup_search if idx == 1 else self._merge_search)
        )
        target.setFocus()
        target.selectAll()

    def on_activated(self) -> None:
        """Hook called by MainWindow when switching to Multi-Table Studio."""
        self._update_db_stale_hint()

    def _on_settings_changed(self) -> None:
        self._refresh_merge_keep_strategies()

    def _start_export_worker(
        self,
        task_fn: Callable[[], Any],
        *,
        success_msg: str,
        error_title: str = "导出失败",
    ) -> None:
        if self._is_worker_running(self._export_worker):
            self.ctx.notify("正在执行导出任务，请稍候...", "warning")
            return

        self._export_gen += 1
        gen = self._export_gen
        self.ctx.notify("正在后台生成导出文件...", "info")

        worker = self._make_worker(task_fn)
        self._export_worker = worker
        worker.succeeded.connect(
            lambda _res, g=gen, msg=success_msg: self._on_export_finished(g, msg)
        )
        worker.failed.connect(
            lambda detail, g=gen, title=error_title: self._on_export_failed(g, title, detail)
        )
        worker.finished.connect(lambda w=worker: self._clear_worker_ref("_export_worker", w))
        self._sync_controls_state()
        worker.start()

    def _on_export_finished(self, gen: int, success_msg: str) -> None:
        if gen != self._export_gen:
            return
        self._sync_controls_state()
        self.ctx.notify(success_msg, "success")

    def _on_export_failed(self, gen: int, error_title: str, detail: str) -> None:
        if gen != self._export_gen:
            return
        self._sync_controls_state()
        ScrollableDetailDialog(error_title, "导出文件时遇到错误：", detail, parent=self).exec()

    # ------------------------------------------------------------------
    # Snapshot & Invalidation Helpers
    # ------------------------------------------------------------------

    def _compute_tables_snapshot(
        self, tables: list[DataTable]
    ) -> tuple[tuple[str, str, int, int, tuple[str, ...]], ...]:
        return tuple(
            (t.table_id, t.name, t.row_count, t.col_count, tuple(t.headers)) for t in tables
        )

    def _clear_all_fixed_report_models(self) -> None:
        self._clear_dedup_fixed_models()
        self._clear_merge_dropped_model()

    def _invalidate_stale_results(self, affected_table_ids: Optional[set[str]] = None) -> None:
        """Invalidate dedup/merge reports whose input table snapshot is no longer valid."""
        invalidate_dedup = False
        if self._last_dedup_report is not None:
            if affected_table_ids is None or self._last_dedup_snapshot is None:
                invalidate_dedup = True
            else:
                snap_ids = {entry[0] for entry in self._last_dedup_snapshot}
                if snap_ids & affected_table_ids:
                    invalidate_dedup = True

        if invalidate_dedup:
            self._last_dedup_report = None
            self._last_dedup_snapshot = None
            self._clear_dedup_fixed_models()
            self._reset_dedup_ui()

        invalidate_merge = False
        if self._last_merge_result is not None:
            if affected_table_ids is None or self._last_merge_snapshot is None:
                invalidate_merge = True
            else:
                snap_ids = {entry[0] for entry in self._last_merge_snapshot}
                if snap_ids & affected_table_ids:
                    invalidate_merge = True

        if invalidate_merge:
            self._last_merge_result = None
            self._last_merge_snapshot = None
            self._merge_result_model.clear()
            self._clear_merge_dropped_model()
            self._reset_merge_ui()

    def _jump_to_table_row(
        self,
        table_id: str,
        row_index_1based: int,
        fallback_ref: Optional[TableRowRef] = None,
    ) -> None:
        """Jump directly to (table_id, 1-based row_index) in Preview, or open full-row inspector."""
        tbl = self._tables_by_id.get(table_id)
        if tbl is not None and 1 <= row_index_1based <= tbl.row_count:
            self._select_table_by_id(table_id)
            self.switch_sub_tab(0)
            if self._preview_model.filter_query():
                self._preview_search.blockSignals(True)
                self._preview_search.clear()
                self._preview_search.blockSignals(False)
                self._preview_model.set_filter("")
                self._update_preview_footer()
            view_row = self._preview_model.find_view_row_for_original(row_index_1based - 1)
            if view_row >= 0:
                idx = self._preview_model.index(view_row, 0)
                self._preview_table.selectRow(view_row)
                self._preview_table.scrollTo(idx, QAbstractItemView.ScrollHint.PositionAtCenter)
                self._preview_table.setFocus()
                self.ctx.notify(f"已定位到「{tbl.name}」第 {row_index_1based:,} 行", "info")
                return

        if fallback_ref is not None:
            dlg = _RowDetailDialog(
                title=f"行内容完整详情 — {fallback_ref.table_name} (第 {fallback_ref.row_index} 行)",
                headers=fallback_ref.headers,
                values=fallback_ref.row_data,
                subtitle="原表已从工作台移除或行号已变更，以下展示报告快照中的完整行内容：",
                can_jump=False,
                parent=self,
            )
            dlg.exec()

    def _open_column_manager(self, table: Optional[DataTable] = None) -> None:
        if self._any_mutation_lock_active():
            self.ctx.notify("后台任务运行中，请稍候再修改表结构", "warning")
            return
        tbl = table or self.active_table()
        if tbl is None:
            self.ctx.notify("请先在左侧选择一张表格", "warning")
            return
        if tbl.col_count == 0:
            self.ctx.notify("当前表格暂无列可管理", "warning")
            return

        old_headers = list(tbl.headers)
        dlg = _ColumnManagerDialog(tbl, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        plan = dlg.get_column_plan()
        if not plan:
            return

        new_headers = [new_name for _orig_idx, new_name in plan]
        col_indices = [orig_idx for orig_idx, _new_name in plan]
        if new_headers == old_headers and col_indices == list(range(len(old_headers))):
            return

        rename_map = {
            old_headers[orig_idx]: new_name
            for orig_idx, new_name in plan
            if 0 <= orig_idx < len(old_headers)
        }
        tbl.headers = new_headers
        tbl.rows = [
            [row[i] if i < len(row) else "" for i in col_indices]
            for row in tbl.rows
        ]

        for mapping in (self._dedup_custom_keys, self._merge_custom_keys):
            if tbl.table_id in mapping:
                updated = [rename_map[c] for c in mapping[tbl.table_id] if c in rename_map]
                if updated:
                    mapping[tbl.table_id] = updated
                else:
                    mapping.pop(tbl.table_id, None)

        self._table_list.blockSignals(True)
        for idx in range(self._table_list.count()):
            item = self._table_list.item(idx)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == tbl.table_id:
                self._format_list_item(item, tbl)
                break
        self._table_list.blockSignals(False)

        self._invalidate_stale_results({tbl.table_id})
        if self._active_table_id == tbl.table_id:
            self._show_preview_for_table(tbl.table_id)
        self._refresh_column_and_table_selectors()
        self._sync_controls_state()
        self.ctx.notify(
            f"已更新「{tbl.name}」列结构（当前 {tbl.col_count} 列）",
            "success",
        )

    # ------------------------------------------------------------------
    # Public API (theme sync, loading & programmatic control)
    # ------------------------------------------------------------------

    def _refresh_view_icons(self, theme: str) -> None:
        p = get_palette(theme)
        cta = p["cta_text"]
        std = p["icon_default"]

        self._btn_add_files.setIcon(get_icon("plus", 15, cta, theme=theme))
        self._update_db_stale_hint()
        self._btn_prev_cols.setIcon(get_icon("edit", 14, std, theme=theme))
        self._btn_prev_col_menu.setIcon(get_icon("columns", 15, std, theme=theme))
        self._btn_prev_export.setIcon(get_icon("download", 14, std, theme=theme))

        self._btn_run_dedup.setIcon(get_icon("play", 14, cta, theme=theme))
        self._btn_dedup_col_menu.setIcon(get_icon("columns", 15, std, theme=theme))
        self._btn_dedup_export.setIcon(get_icon("download", 14, std, theme=theme))

        self._btn_run_merge.setIcon(get_icon("play", 14, cta, theme=theme))
        self._btn_merge_col_menu.setIcon(get_icon("columns", 15, std, theme=theme))
        self._btn_merge_export.setIcon(get_icon("download", 14, std, theme=theme))
        self._btn_merge_add_workspace.setIcon(get_icon("plus", 14, std, theme=theme))
        self._btn_merge_write_db.setIcon(get_icon("database", 14, std, theme=theme))

        _set_leading_search_icon(self._preview_search, theme)
        _set_leading_search_icon(self._dedup_search, theme)
        _set_leading_search_icon(self._merge_search, theme)

    def apply_theme(self, theme: str) -> None:
        """Propagate Light/Dark theme changes across models, pills, icons, and delegates."""
        for model in self._all_models:
            model.set_theme(theme)
        self._dedup_metric_bar.apply_theme(theme)
        self._empty_preview_canvas.apply_theme(theme)
        self._dedup_empty.apply_theme(theme)
        self._merge_empty.apply_theme(theme)
        self._refresh_source_panel_theme(theme)
        self._refresh_view_icons(theme)
        sync_widget_craft_theme(self, theme)

    def switch_sub_tab(self, index: int) -> None:
        if 0 <= index < self._work_tabs.count():
            self._work_tabs.setCurrentIndex(index)

    def add_tables(self, tables: list[DataTable], select_first: bool = True) -> None:
        """Register one or more DataTable objects into the workspace list."""
        if not tables:
            return
        first_new_id: Optional[str] = None
        existing_names = {t.name for t in self._tables}

        self._table_list.blockSignals(True)
        for tbl in tables:
            if tbl.table_id in self._tables_by_id:
                tbl = tbl.clone()
            base_name = tbl.name.strip() or "未命名表格"
            candidate = base_name
            suffix = 2
            while candidate in existing_names:
                candidate = f"{base_name} ({suffix})"
                suffix += 1
            tbl.name = candidate
            existing_names.add(candidate)

            self._tables.append(tbl)
            self._tables_by_id[tbl.table_id] = tbl
            if first_new_id is None:
                first_new_id = tbl.table_id

            item = self._create_list_item(tbl)
            self._table_list.addItem(item)
        self._table_list.blockSignals(False)

        self._update_table_count_ui()
        self._refresh_column_and_table_selectors()
        self._sync_controls_state()

        if select_first and first_new_id is not None:
            self._select_table_by_id(first_new_id)

    def load_files(
        self,
        paths: list[str],
        *,
        interactive: bool = True,
        force_options_dialog: bool = False,
        import_options: Optional[dict[str, dict[str, Any]]] = None,
    ) -> None:
        """Inspect and load one or more files asynchronously, with Import Picker when needed."""
        valid_paths = [str(p) for p in paths if Path(p).is_file()]
        if not valid_paths:
            return
        if self._is_worker_running(self._inspect_worker) or self._is_worker_running(self._load_worker):
            self.ctx.notify("正在加载表格文件，请稍候...", "warning")
            return

        if import_options is not None or not interactive:
            self._start_load_files_worker(valid_paths, import_options or {})
            return

        inspect_candidates = [
            p
            for p in valid_paths
            if force_options_dialog or Path(p).suffix.lower() in {".xlsx", ".xlsm", ".docx"}
        ]
        if not inspect_candidates:
            self._start_load_files_worker(valid_paths, {})
            return

        self._inspect_gen += 1
        gen = self._inspect_gen
        self._left_progress.setVisible(True)
        self._left_progress.setRange(0, 0)
        self._btn_cancel_left.setVisible(True)

        def _inspect_task() -> tuple[list[dict[str, Any]], list[str]]:
            specs: list[dict[str, Any]] = []
            errors: list[str] = []
            for p_str in inspect_candidates:
                try:
                    specs.append(_inspect_file_for_import(p_str))
                except Exception as exc:
                    errors.append(f"{Path(p_str).name}：{exc}")
            return specs, errors

        worker = self._make_worker(_inspect_task)
        self._inspect_worker = worker
        worker.succeeded.connect(
            lambda payload, g=gen, all_p=valid_paths, forced=force_options_dialog: self._on_inspect_finished(
                g, all_p, forced, payload
            )
        )
        worker.failed.connect(lambda detail, g=gen: self._on_load_failed(g, detail, is_inspect=True))
        if hasattr(worker, "cancelled"):
            worker.cancelled.connect(lambda g=gen: self._on_left_cancelled(g, is_inspect=True))
        worker.finished.connect(lambda w=worker: self._clear_worker_ref("_inspect_worker", w))
        self._sync_controls_state()
        worker.start()

    def _on_inspect_finished(
        self,
        gen: int,
        all_paths: list[str],
        force_options_dialog: bool,
        payload: tuple[list[dict[str, Any]], list[str]],
    ) -> None:
        if gen != self._inspect_gen:
            return
        specs, inspect_errors = payload
        self._left_progress.setVisible(False)
        self._btn_cancel_left.setVisible(False)
        self._sync_controls_state()

        if inspect_errors:
            ScrollableDetailDialog(
                "部分文件检查失败",
                f"共有 {len(inspect_errors)} 个文件无法读取结构：",
                "\n".join(inspect_errors),
                parent=self,
            ).exec()

        failed_names = {e.split("：", 1)[0] for e in inspect_errors}
        remaining_paths = [p for p in all_paths if Path(p).name not in failed_names]
        if not remaining_paths:
            return

        picker_specs: list[dict[str, Any]] = []
        for spec in specs:
            kind = spec.get("kind")
            if force_options_dialog:
                picker_specs.append(spec)
            elif kind == "excel" and len(spec.get("sheets") or []) > 1:
                picker_specs.append(spec)
            elif kind == "word" and (spec.get("tables") or spec.get("paragraph_count", 0) > 0):
                picker_specs.append(spec)

        chosen_options: dict[str, dict[str, Any]] = {}
        if picker_specs:
            dlg = _ImportOptionsDialog(picker_specs, parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                self.ctx.notify("已取消导入", "info")
                return
            chosen_options = dlg.get_options()

            filtered_paths: list[str] = []
            for p_str in remaining_paths:
                opts = chosen_options.get(p_str)
                if opts is not None:
                    if "sheet_names" in opts and not opts["sheet_names"]:
                        continue
                    if (
                        "table_indices" in opts
                        and not opts["table_indices"]
                        and not opts.get("include_paragraphs_as_table", False)
                    ):
                        continue
                filtered_paths.append(p_str)
            remaining_paths = filtered_paths
            if not remaining_paths:
                self.ctx.notify("未勾选任何可导入的工作表或表格", "warning")
                return

        self._start_load_files_worker(remaining_paths, chosen_options)

    def _start_load_files_worker(
        self,
        paths: list[str],
        options_by_path: dict[str, dict[str, Any]],
    ) -> None:
        self._load_gen += 1
        gen = self._load_gen
        self._left_progress.setVisible(True)
        self._left_progress.setRange(0, 0)
        self._btn_cancel_left.setVisible(True)
        parser_engine = self.ctx.parser

        def _task() -> tuple[list[DataTable], list[str]]:
            loaded: list[DataTable] = []
            errors: list[str] = []
            for p_str in paths:
                try:
                    opts = options_by_path.get(p_str, {})
                    sub_tables = _call_load_tables_from_file(p_str, opts, parser_engine)
                    if not sub_tables:
                        errors.append(f"{Path(p_str).name}：未检测到有效数据表或非空内容")
                    else:
                        loaded.extend(sub_tables)
                except Exception as exc:
                    errors.append(f"{Path(p_str).name}：{exc}")
            return loaded, errors

        worker = self._make_worker(_task)
        self._load_worker = worker
        worker.succeeded.connect(lambda payload, g=gen: self._on_files_loaded(g, payload))
        worker.failed.connect(lambda detail, g=gen: self._on_load_failed(g, detail))
        if hasattr(worker, "cancelled"):
            worker.cancelled.connect(lambda g=gen: self._on_left_cancelled(g))
        worker.finished.connect(lambda w=worker: self._clear_worker_ref("_load_worker", w))
        self._sync_controls_state()
        worker.start()

    def load_database_table(self) -> None:
        """Load current SQLite Data Library into the workspace asynchronously via WorkerThread."""
        if self._is_worker_running(self._db_load_worker):
            self.ctx.notify("正在载入数据总库，请稍候...", "warning")
            return

        db_repo = self.ctx.db
        db_stem = Path(db_repo.db_path).stem if not db_repo.is_memory else "内存库"
        tbl_name = f"当前数据总库 ({db_stem})"

        self._db_load_gen += 1
        gen = self._db_load_gen
        self._left_progress.setVisible(True)
        self._left_progress.setRange(0, 0)
        self._btn_cancel_left.setVisible(True)

        worker = self._make_worker(lambda: read_database_as_table(db_repo, name=tbl_name))
        self._db_load_worker = worker
        worker.succeeded.connect(lambda tbl, g=gen: self._on_db_table_loaded(g, tbl))
        worker.failed.connect(lambda detail, g=gen: self._on_db_load_failed(g, detail))
        if hasattr(worker, "cancelled"):
            worker.cancelled.connect(lambda g=gen: self._on_left_cancelled(g, is_db=True))
        worker.finished.connect(lambda w=worker: self._clear_worker_ref("_db_load_worker", w))
        self._sync_controls_state()
        worker.start()

    def _on_db_table_loaded(self, gen: int, tbl: DataTable) -> None:
        if gen != self._db_load_gen:
            return
        self._left_progress.setVisible(False)
        self._btn_cancel_left.setVisible(False)
        self._db_snapshot_stale = False
        self._update_db_stale_hint()
        self._sync_controls_state()

        if tbl.row_count == 0:
            self.ctx.notify("当前数据总库为空，暂无记录可载入", "warning")
            return
        self.add_tables([tbl], select_first=True)
        self.ctx.notify(f"已载入数据总库：{tbl.row_count:,} 条记录", "success")

    def _on_db_load_failed(self, gen: int, detail: str) -> None:
        if gen != self._db_load_gen:
            return
        self._left_progress.setVisible(False)
        self._btn_cancel_left.setVisible(False)
        self._sync_controls_state()
        ScrollableDetailDialog("载入数据总库失败", "后台读取数据总库时发生错误：", detail, parent=self).exec()

    def _on_db_mutated(self, *_args: Any) -> None:
        if any(t.source_type == "database" for t in self._tables):
            self._db_snapshot_stale = True
            self._update_db_stale_hint()

    def _update_db_stale_hint(self) -> None:
        theme = self.ctx.config.theme
        has_db_table = any(t.source_type == "database" for t in self._tables)
        if has_db_table and self._db_snapshot_stale:
            self._btn_load_db.setText("重新载入总库")
            self._btn_load_db.setIcon(get_icon("refresh-cw", 14, theme=theme))
            self._btn_load_db.setToolTip("当前数据总库已发生变化，点击载入最新快照")
        else:
            self._btn_load_db.setText("载入总库")
            self._btn_load_db.setIcon(get_icon("database", 14, theme=theme))
            self._btn_load_db.setToolTip("将当前 SQLite 数据总库作为一张表载入工作台")

    def checked_tables(self) -> list[DataTable]:
        result: list[DataTable] = []
        for idx in range(self._table_list.count()):
            item = self._table_list.item(idx)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                tid = item.data(Qt.ItemDataRole.UserRole)
                tbl = self._tables_by_id.get(tid)
                if tbl is not None:
                    result.append(tbl)
        return result

    def active_table(self) -> Optional[DataTable]:
        if self._active_table_id:
            return self._tables_by_id.get(self._active_table_id)
        return None

    # ------------------------------------------------------------------
    # Top-Level Layout & Control State Synchronization
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Sleek 48px PageHeaderBar with primary Add Files & secondary Load DB actions
        self._page_header = PageHeaderBar("多表工作台", "暂无数据源", parent=self)

        self._btn_load_db = QPushButton("载入总库")
        self._btn_load_db.setProperty("compact", True)
        self._btn_load_db.setIconSize(QSize(14, 14))
        self._btn_load_db.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_load_db.clicked.connect(self.load_database_table)
        self._page_header.add_action(self._btn_load_db)

        self._btn_add_files = QPushButton("添加文件")
        self._btn_add_files.setProperty("variant", "primary")
        self._btn_add_files.setProperty("compact", True)
        self._btn_add_files.setIconSize(QSize(15, 15))
        self._btn_add_files.setToolTip("添加 Excel (.xlsx/.xlsm)、Word (.docx)、CSV 或 TXT 文件")
        self._btn_add_files.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_add_files.clicked.connect(lambda: self._pick_files(force_options=False))
        self._page_header.add_action(self._btn_add_files)

        root.addWidget(self._page_header)

        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(16, 12, 16, 12)
        body_lay.setSpacing(0)

        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.setHandleWidth(6)

        self._main_splitter.addWidget(self._build_left_panel())
        self._main_splitter.addWidget(self._build_right_panel())
        self._main_splitter.setStretchFactor(0, 0)
        self._main_splitter.setStretchFactor(1, 1)
        self._main_splitter.setSizes([244, 820])

        body_lay.addWidget(self._main_splitter, 1)
        root.addWidget(body, 1)

    def _build_right_panel(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 0, 0, 0)
        layout.setSpacing(0)

        self._work_tabs = QTabWidget()
        self._work_tabs.setObjectName("TablesWorkTabs")
        self._work_tabs.addTab(self._build_preview_tab(), "数据预览")
        self._work_tabs.addTab(self._build_dedup_tab(), "多表查重")
        self._work_tabs.addTab(self._build_merge_tab(), "合并与导出")

        layout.addWidget(self._work_tabs, 1)
        return container

    def _any_left_worker_running(self) -> bool:
        return (
            self._is_worker_running(self._inspect_worker)
            or self._is_worker_running(self._load_worker)
            or self._is_worker_running(self._db_load_worker)
        )

    def _any_mutation_lock_active(self) -> bool:
        return (
            self._any_left_worker_running()
            or self._is_worker_running(self._dedup_worker)
            or self._is_worker_running(self._merge_worker)
        )

    def _sync_controls_state(self) -> None:
        has_tables = len(self._tables) > 0
        has_checked = len(self.checked_tables()) > 0
        has_active = self.active_table() is not None

        left_busy = self._any_left_worker_running()
        dedup_busy = self._is_worker_running(self._dedup_worker)
        merge_busy = self._is_worker_running(self._merge_worker)
        export_busy = self._is_worker_running(self._export_worker)
        db_import_busy = self._is_worker_running(self._db_import_worker)
        mut_locked = left_busy or dedup_busy or merge_busy

        # Header & Left-panel actions
        self._btn_add_files.setEnabled(not left_busy)
        self._btn_load_db.setEnabled(not left_busy)
        self._btn_sel_all.setEnabled(has_tables and not mut_locked)
        self._btn_move_up.setEnabled(len(self._tables) > 1 and not mut_locked)
        self._btn_move_down.setEnabled(len(self._tables) > 1 and not mut_locked)
        self._act_src_opts.setEnabled(not left_busy)
        self._act_src_invert.setEnabled(has_tables and not mut_locked)
        self._act_src_rename.setEnabled(has_tables and not mut_locked)
        self._act_src_remove.setEnabled(has_tables and not mut_locked)
        self._act_src_pack.setEnabled(has_tables and not export_busy)
        self._act_src_clear.setEnabled(has_tables and not mut_locked)

        # Preview tab controls
        self._btn_prev_cols.setEnabled(has_active and not mut_locked)
        self._btn_prev_single_dedup.setEnabled(has_active and not dedup_busy)
        self._btn_prev_col_menu.setEnabled(has_active)
        self._btn_prev_export.setEnabled(has_active and not export_busy)

        # Dedup tab controls
        self._btn_run_dedup.setEnabled(has_checked and not dedup_busy)
        has_dedup_report = self._last_dedup_report is not None
        self._btn_dedup_export.setEnabled(has_dedup_report and not export_busy)

        # Merge tab controls
        self._btn_merge_cols.setEnabled(has_active and not mut_locked)
        self._btn_run_merge.setEnabled(has_checked and not merge_busy)
        has_merge_data = self._last_merge_result is not None and (
            self._last_merge_result.merged_table.row_count > 0
            or self._last_merge_result.merged_table.col_count > 0
        )
        self._btn_merge_export.setEnabled(has_merge_data and not export_busy)
        self._btn_merge_add_workspace.setEnabled(has_merge_data and not mut_locked)
        self._btn_merge_write_db.setEnabled(
            has_merge_data
            and self._last_merge_result is not None
            and self._last_merge_result.merged_table.row_count > 0
            and not db_import_busy
        )

        self._on_dedup_mode_changed()
        self._on_merge_mode_changed()
