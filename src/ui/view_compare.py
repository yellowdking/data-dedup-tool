"""View 3: 查重与对比 — Non-mutating DB comparison & Dual-List Coverage Comparison."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QMenu, QProgressBar, QPushButton, QSplitter, QStackedWidget, QTableView, QTabWidget,
    QVBoxLayout, QWidget,
)

from src.core.dedup import compare_with_db
from src.core.exporter import export_codes_txt, export_compare_result_csv, export_compare_result_excel
from src.core.models import CompareResult, DataRecord, ImportBatch, ParseResult, ScannedItem
from src.core.scanner import scan_document_file
from src.ui.app_context import AppContext
from src.ui.compare_ab_models import _COMPARE_FILE_FILTER, _DroppablePlainTextEdit, _stop_worker_safely
from src.ui.delegates import apply_slim_scrollbars, enhance_table_view, sync_widget_craft_theme
from src.ui.icons import get_icon
from src.ui.table_copy import TableCopyHelper, format_codes_for_clipboard
from src.ui.table_header import TableHeaderManager
from src.ui.table_models import CompareTableModel
from src.ui.theme import get_palette
from src.ui.view_compare_ab import CompareABMixin
from src.ui.widgets import EmptyStateWidget, PageHeaderBar, ScrollableDetailDialog, WorkerThread

_OFFICE_EXTENSIONS: frozenset[str] = frozenset({".docx", ".xlsx", ".xlsm"})
_MAX_PREVIEW_LINES: int = 300
_MAX_PREVIEW_CHARS: int = 120_000


def _make_btn(
    text: str = "", *, variant: str = "", compact: bool = False,
    icon_only: bool = False, tooltip: str = "", slot: Optional[Callable[..., Any]] = None,
) -> QPushButton:
    btn = QPushButton(text)
    if variant:
        btn.setProperty("variant", variant)
    if compact:
        btn.setProperty("compact", True)
    if icon_only:
        btn.setProperty("iconOnly", True)
    if tooltip:
        btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if slot is not None:
        btn.clicked.connect(slot)
    return btn


class _ModeTabsCompat(QObject):
    """QTabWidget-compatible shim syncing PageHeaderBar segmented switcher and QStackedWidget."""

    currentChanged = Signal(int)

    def __init__(
        self,
        stack: QStackedWidget,
        button_group: QButtonGroup,
        labels: list[str],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._stack = stack
        self._group = button_group
        self._labels = list(labels)
        self._stack.currentChanged.connect(self._on_stack_changed)

    def _on_stack_changed(self, idx: int) -> None:
        if (btn := self._group.button(idx)) is not None and not btn.isChecked():
            btn.setChecked(True)
        self.currentChanged.emit(idx)

    def setCurrentIndex(self, index: int) -> None:
        if 0 <= index < self._stack.count():
            self._stack.setCurrentIndex(index)
            if (btn := self._group.button(index)) is not None:
                btn.setChecked(True)

    def currentIndex(self) -> int:
        return self._stack.currentIndex()

    def count(self) -> int:
        return self._stack.count()

    def tabText(self, index: int) -> str:
        return self._labels[index] if 0 <= index < len(self._labels) else ""


class CompareView(CompareABMixin, QWidget):
    """Quick comparison view supporting (A) non-mutating DB check and (B) Dual-List Coverage Audit."""

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

        self._model_db_matched = CompareTableModel(with_matches=True, parent=self, theme=theme)
        self._model_db_new = CompareTableModel(with_matches=False, parent=self, theme=theme)
        self._model_db_unrec = CompareTableModel(with_matches=False, parent=self, theme=theme)

        # Shared table managers & copy helpers across all 7 Mode A + Mode B tables
        self._header_managers: list[TableHeaderManager] = []
        self._copy_helpers: dict[QTableView, TableCopyHelper] = {}

        self._init_ab_state(theme)
        self._export_worker: Optional[WorkerThread] = None

        self._setup_ui()
        self._refresh_compare_icons(theme)
        self.ctx.db_switched.connect(self._on_db_switched)
        self.ctx.data_changed.connect(self._on_data_changed)
        self.ctx.settings_changed.connect(self._sync_settings)
        self.ctx.theme_changed.connect(self.apply_theme)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 48px PageHeaderBar with integrated Mode A / Mode B segmented switcher
        self._header = PageHeaderBar("查重与对比")

        seg_frame = QFrame()
        seg_frame.setProperty("segmented", True)
        seg_layout = QHBoxLayout(seg_frame)
        seg_layout.setContentsMargins(2, 2, 2, 2)
        seg_layout.setSpacing(2)

        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        labels = ["模式 A：查当前总库", "模式 B：双列表覆盖率对比"]
        for idx, text in enumerate(labels):
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setChecked(idx == 0)
            btn.setProperty("segment", True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._mode_group.addButton(btn, idx)
            seg_layout.addWidget(btn)
        self._header.add_meta_widget(seg_frame)

        self._btn_columns = _make_btn(
            variant="ghost",
            icon_only=True,
            tooltip="自定义当前结果表显示列与顺序",
            slot=self._open_active_column_menu,
        )
        self._header.add_action_widget(self._btn_columns)
        root.addWidget(self._header)

        self._mode_stack = QStackedWidget()
        self._mode_stack.addWidget(self._build_db_compare_tab())
        self._mode_stack.addWidget(self._build_ab_compare_tab())
        self._mode_group.idClicked.connect(self._mode_stack.setCurrentIndex)
        self._mode_tabs = _ModeTabsCompat(self._mode_stack, self._mode_group, labels, parent=self)
        for sig in (
            self._mode_stack.currentChanged, self._db_result_tabs.currentChanged,
            self._db_result_stack.currentChanged, self._ab_result_tabs.currentChanged,
            self._ab_result_stack.currentChanged,
        ):
            sig.connect(lambda _i: self._schedule_column_fit())
        self.ctx.table_layouts_reset.connect(self._schedule_column_fit)
        root.addWidget(self._mode_stack, 1)

    # --- Lifecycle, theme, shortcut & context hooks ---

    def shutdown(self) -> None:
        self._db_gen += 1
        self._ab_gen += 1
        for attr in ("_db_worker", "_ab_worker", "_import_worker", "_export_worker"):
            _stop_worker_safely(getattr(self, attr, None))
            setattr(self, attr, None)
        for mgr in self._header_managers:
            mgr.flush()

    def on_activated(self) -> None:
        self._sync_settings()
        self._schedule_column_fit()

    def _schedule_column_fit(self) -> None:
        self._fit_active_table_to_viewport()
        QTimer.singleShot(0, self._fit_active_table_to_viewport)

    def _fit_active_table_to_viewport(self) -> None:
        tv = self._active_result_table()
        mgr = getattr(tv, "_table_header_manager", None) if tv is not None else None
        if tv is None or not isinstance(mgr, TableHeaderManager) or not mgr._table_id or mgr._table_id in self.ctx.config.table_layouts or tv.viewport().width() <= 200:
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
        self._schedule_column_fit()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._fit_active_table_to_viewport()

    def _active_result_table(self) -> Optional[QTableView]:
        if self._mode_tabs.currentIndex() == 1:
            tables = (self._tbl_ab_checklist, self._tbl_ab_missing, self._tbl_ab_matched, self._tbl_ab_only_a)
            idx = self._ab_result_tabs.currentIndex()
            return tables[idx] if 0 <= idx < len(tables) else None
        tables_a = (self._tbl_db_matched, self._tbl_db_new, self._tbl_db_unrec)
        idx = self._db_result_tabs.currentIndex()
        return tables_a[idx] if 0 <= idx < len(tables_a) else None

    def _open_active_column_menu(self) -> None:
        tv = self._active_result_table()
        mgr = getattr(tv, "_table_header_manager", None) if tv is not None else None
        if isinstance(mgr, TableHeaderManager):
            mgr.open_column_menu(self._btn_columns.mapToGlobal(QPoint(0, self._btn_columns.height() + 4)))

    def _on_inline_code_copied(self, code: str) -> None:
        if code:
            self.ctx.notify(f"已复制编码：{code}", "success")

    def _refresh_compare_icons(self, theme: str) -> None:
        p = get_palette(theme)
        for btn, name, sz, color in (
            (self._btn_columns, "columns", 15, p["icon_default"]),
            (self._btn_load_txt, "folder-open", 14, p["icon_default"]),
            (self._btn_run_db, "play", 14, p["cta_text"]),
            (self._btn_export_db_cmp, "download", 14, p["icon_default"]),
            (self._btn_import_new, "plus", 14, p["icon_default"]),
        ):
            btn.setIcon(get_icon(name, sz, color, theme=theme))
            btn.setIconSize(QSize(sz, sz))
        self._refresh_ab_icons(theme)

    def apply_theme(self, theme: str) -> None:
        for model in (self._model_db_matched, self._model_db_new, self._model_db_unrec):
            model.set_theme(theme)
        self._db_empty_state.apply_theme(theme)
        self._apply_ab_theme(theme)
        self._refresh_compare_icons(theme)
        sync_widget_craft_theme(self, theme)

    def focus_search(self) -> None:
        """Handle Ctrl+F shortcut: focus Mode B checklist search or Mode A input."""
        if self._mode_tabs.currentIndex() == 1:
            if self._ab_result is not None:
                self._ab_result_stack.setCurrentIndex(1)
            self._ab_result_tabs.setCurrentIndex(0)
            self._ab_checklist_search.setFocus()
            self._ab_checklist_search.selectAll()
        else:
            self._db_input.setFocus()

    def _sync_settings(self) -> None:
        checked = self.ctx.config.ignore_companion_files
        for chk in (self._chk_db_ignore_comp, self._chk_ab_ignore_comp):
            chk.blockSignals(True)
            chk.setChecked(checked)
            chk.blockSignals(False)

    def _get_companion_exts(self, enabled: bool) -> set[str]:
        return self.ctx.get_companion_extensions(respect_global_toggle=False) if enabled else set()

    def _is_worker_running(self, worker: Optional[WorkerThread]) -> bool:
        try:
            return bool(worker is not None and worker.isRunning())
        except RuntimeError:
            return False

    def _on_db_switched(self, db_path: str) -> None:
        if self._is_worker_running(self._db_worker):
            self._cancel_db_worker()
        self._db_compare_result = None
        self._db_result_db_path = ""
        self._db_result_stale = True
        for m in (self._model_db_matched, self._model_db_new, self._model_db_unrec):
            m.set_rows([])
        for idx, lbl in enumerate(("已存在 (0)", "未找到 · 新数据 (0)", "未识别 (0)")):
            self._db_result_tabs.setTabText(idx, lbl)
        self._db_result_stack.setCurrentIndex(0)
        self._btn_import_new.setEnabled(False)
        self._btn_copy_new.setEnabled(False)
        self._btn_export_db_cmp.setEnabled(False)
        self._db_summary.setText("数据库已切换，比对结果已清空 — 请重新点击「开始快速查重」")
        self._on_ab_db_switched(db_path)

    def _on_data_changed(self) -> None:
        self._on_ab_data_changed()
        if self._self_importing or self._db_compare_result is None:
            return
        self._db_result_stale = True
        self._btn_import_new.setEnabled(False)
        self._db_summary.setText(
            f"当前数据库已变更（原结果：已存在 {len(self._db_compare_result.matched_in_db):,} · "
            f"新数据 {len(self._db_compare_result.not_in_db):,}）— 入库前请重新查重"
        )

    # --- Mode A: Compare against current database without mutating ---

    def _build_db_compare_tab(self) -> QWidget:
        theme = self.ctx.config.theme
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(8)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)

        top_panel = QWidget()
        top_layout = QVBoxLayout(top_panel)
        top_layout.setContentsMargins(0, 0, 0, 4)
        top_layout.setSpacing(6)

        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        lbl = QLabel("待比对清单")
        lbl.setProperty("role", "section")
        header_row.addWidget(lbl)

        self._db_file_badge = QLabel("")
        self._db_file_badge.setProperty("badge", "accent")
        self._db_file_badge.setVisible(False)
        header_row.addWidget(self._db_file_badge)
        header_row.addStretch()

        self._chk_db_ignore_comp = QCheckBox("忽略伴随文件")
        self._chk_db_ignore_comp.setChecked(self.ctx.config.ignore_companion_files)
        header_row.addWidget(self._chk_db_ignore_comp)

        self._btn_load_txt = _make_btn(
            "载入文件",
            compact=True,
            tooltip="支持 .txt / .docx / .xlsx / .xlsm / .csv / .tsv / .log 文件",
            slot=self._load_file_to_db_input,
        )
        self._btn_clear_input = _make_btn("清空", variant="ghost", compact=True, slot=self._clear_db_input)
        self._btn_run_db = _make_btn("开始快速查重", variant="primary", slot=lambda: self.run_db_compare())
        for btn in (self._btn_load_txt, self._btn_clear_input, self._btn_run_db):
            header_row.addWidget(btn)
        top_layout.addLayout(header_row)

        self._db_input = _DroppablePlainTextEdit()
        self._db_input.file_dropped.connect(self._load_specific_file_to_db_input)
        self._db_input.textChanged.connect(self._on_db_input_edited)
        self._db_input.setPlaceholderText(
            "在此粘贴待比对清单（每行一条），或拖入 TXT / Word (.docx) / Excel (.xlsx) / CSV 文件：\n"
            "ABC-001.mp4\nPRJ-0042_4K.mkv\n仅与本地总库比对，不会自动修改数据库"
        )
        self._db_input.setMinimumHeight(88)
        apply_slim_scrollbars(self._db_input, theme)
        top_layout.addWidget(self._db_input, 1)
        splitter.addWidget(top_panel)

        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(0, 6, 0, 0)
        bottom_layout.setSpacing(8)

        self._db_result_tabs = QTabWidget()
        self._db_result_tabs.setProperty("subtabs", True)
        self._tbl_db_matched = self._make_table(self._model_db_matched, "compare_db_matched")
        self._tbl_db_new = self._make_table(self._model_db_new, "compare_db_new")
        self._tbl_db_unrec = self._make_table(self._model_db_unrec, "compare_db_unrec")
        self._db_result_tabs.addTab(self._tbl_db_matched, "已存在 (0)")
        self._db_result_tabs.addTab(self._tbl_db_new, "未找到 · 新数据 (0)")
        self._db_result_tabs.addTab(self._tbl_db_unrec, "未识别 (0)")

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self._db_summary = QLabel("等待比对 — 不会修改当前数据库")
        self._db_summary.setProperty("role", "subtitle")
        action_row.addWidget(self._db_summary, 1)

        self._db_progress = QProgressBar()
        self._db_progress.setFixedSize(150, 10)
        self._db_progress.setVisible(False)
        action_row.addWidget(self._db_progress)

        self._btn_cancel_db = _make_btn("取消", variant="ghost", compact=True, slot=self._cancel_db_worker)
        self._btn_cancel_db.setVisible(False)
        action_row.addWidget(self._btn_cancel_db)

        self._btn_copy_new = self._copy_helpers[self._tbl_db_new].create_copy_split_button(
            parent=bottom_widget,
            primary_variant=False,
            all_label="复制新数据编码",
            selected_label="复制选中编码",
        )
        self._btn_copy_new.setEnabled(False)
        action_row.addWidget(self._btn_copy_new)

        self._btn_export_db_cmp = _make_btn("导出")
        self._btn_export_db_cmp.setEnabled(False)
        export_menu = QMenu(self._btn_export_db_cmp)
        for lbl_txt, fmt in (
            ("完整比对报告 (.xlsx)", "xlsx"),
            ("比对明细表格 (.csv)", "csv"),
            ("新数据编码清单 (.txt)", "txt"),
        ):
            export_menu.addAction(lbl_txt).triggered.connect(
                lambda _=False, f=fmt: self._export_compare(self._db_compare_result, f)
            )
        self._btn_export_db_cmp.setMenu(export_menu)
        action_row.addWidget(self._btn_export_db_cmp)

        self._btn_import_new = _make_btn("新数据入库", variant="outline", slot=self._import_not_in_db)
        self._btn_import_new.setEnabled(False)
        action_row.addWidget(self._btn_import_new)
        bottom_layout.addLayout(action_row)

        self._db_result_stack = QStackedWidget()
        self._db_empty_state = EmptyStateWidget(
            icon_name="git-compare",
            title="载入或粘贴清单后开始快速查重",
            subtitle="即时核验哪些条目已存在于当前本地总库、哪些为新数据，整个过程不修改数据库",
            theme=theme,
        )
        self._db_result_stack.addWidget(self._db_empty_state)
        self._db_result_stack.addWidget(self._db_result_tabs)
        self._db_result_stack.setCurrentIndex(0)
        bottom_layout.addWidget(self._db_result_stack, 1)

        splitter.addWidget(bottom_widget)
        splitter.setSizes([150, 480])
        layout.addWidget(splitter, 1)
        return widget

    def _set_db_busy(self, busy: bool, cancellable: bool = False) -> None:
        self._db_progress.setVisible(busy)
        if busy:
            self._db_progress.setRange(0, 0)
        self._btn_cancel_db.setVisible(busy and cancellable)
        self._btn_cancel_db.setEnabled(busy and cancellable)
        for b in (self._btn_run_db, self._btn_load_txt, self._btn_clear_input):
            b.setEnabled(not busy)
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
        path, _ = QFileDialog.getOpenFileName(self, "选择待比对文件", self.ctx.config.default_import_dir or str(Path.home()), _COMPARE_FILE_FILTER)
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
        lines, total_chars, truncated = [], 0, False
        for idx, item in enumerate(scanned):
            if idx >= _MAX_PREVIEW_LINES:
                truncated = True
                break
            line = f"[{item.source_name}] {item.raw_text}" if (is_office and item.source_name) else item.raw_text
            if total_chars + len(line) > _MAX_PREVIEW_CHARS:
                truncated = True
                break
            lines.append(line)
            total_chars += len(line) + 1
        if truncated or len(scanned) > _MAX_PREVIEW_LINES:
            lines.append(f"... 共提取 {len(scanned):,} 条内容（仅预览前 {len(lines)} 条，比对时将解析完整文件）")
        return "\n".join(lines)

    def _load_specific_file_to_db_input(self, path: str) -> None:
        if self._is_worker_running(self._db_worker) or self._is_worker_running(self._import_worker):
            self.ctx.notify("正在执行查重/入库任务，请稍候或先取消", "warning")
            return
        p = Path(path)
        ignore_comp = self._chk_db_ignore_comp.isChecked()
        comp_exts, expected_db = self._get_companion_exts(ignore_comp), str(self.ctx.db.db_path)
        is_office = p.suffix.lower() in _OFFICE_EXTENSIONS

        self._db_gen += 1
        gen = self._db_gen
        self._set_db_busy(True, cancellable=True)
        self._db_summary.setText(f"正在后台读取并比对 {p.name}...")

        wbox: list[Optional[WorkerThread]] = [None]
        _cancelled = lambda: bool(wbox[0] is not None and wbox[0].is_cancelled)

        def _task() -> Optional[dict[str, Any]]:
            scanned = scan_document_file(p, companion_extensions=comp_exts)
            if _cancelled():
                return None
            parsed = self.ctx.parser.parse_items(scanned, ignore_companions=ignore_comp, companion_extensions=comp_exts, cancel_check=_cancelled)
            if _cancelled():
                return None
            return {
                "file_path": str(p), "file_name": p.name, "scanned_count": len(scanned),
                "preview_text": self._build_preview_text(scanned, is_office=is_office),
                "result": compare_with_db(parsed, self.ctx.db, source_name=p.name),
            }

        worker = WorkerThread(_task, parent=self)
        wbox[0] = self._db_worker = worker
        worker.succeeded.connect(lambda pl, g=gen, dbp=expected_db: self._on_db_file_compare_succeeded(g, pl, dbp))
        worker.failed.connect(lambda detail, g=gen: self._on_db_compare_failed(g, detail))
        worker.cancelled.connect(lambda g=gen: self._on_db_compare_cancelled(g))
        worker.finished.connect(lambda w=worker, g=gen: self._on_db_worker_finished(w, g))
        worker.start()

    def _on_db_file_compare_succeeded(self, gen: int, payload: Optional[dict[str, Any]], expected_db: str) -> None:
        if gen != self._db_gen:
            return
        self._set_db_busy(False)
        if payload is None:
            self._on_db_compare_cancelled(gen)
            return
        if str(self.ctx.db.db_path) != expected_db:
            self._on_db_switched(str(self.ctx.db.db_path))
            return
        self._db_input.blockSignals(True)
        self._db_input.setPlainText(payload["preview_text"])
        self._db_input.blockSignals(False)
        self._db_loaded_file = payload["file_path"]
        self._db_file_badge.setText(f"已载入：{payload['file_name']}（{payload['scanned_count']:,} 条）")
        self._db_file_badge.setVisible(True)
        self._apply_db_compare_result(payload["result"], db_path=expected_db)

    def _apply_db_compare_result(self, result: CompareResult, db_path: str = "") -> None:
        self._db_compare_result = result
        self._db_result_db_path = db_path or str(self.ctx.db.db_path)
        self._db_result_stale = False
        self._model_db_matched.set_rows(result.matched_in_db)
        self._model_db_new.set_rows(result.not_in_db)
        self._model_db_unrec.set_rows(result.unrecognized_a)
        for idx, txt in enumerate((f"已存在 ({len(result.matched_in_db)})", f"未找到 · 新数据 ({len(result.not_in_db)})", f"未识别 ({len(result.unrecognized_a)})")):
            self._db_result_tabs.setTabText(idx, txt)
        self._db_result_stack.setCurrentIndex(1)
        self._db_summary.setText(
            f"比对完成：已存在 {len(result.matched_in_db):,} 条 · 未找到(新数据) {len(result.not_in_db):,} 条 · 未识别 {len(result.unrecognized_a):,} 条"
        )
        has_new = len(result.not_in_db) > 0
        self._btn_import_new.setEnabled(has_new)
        self._copy_helpers[self._tbl_db_new].update_split_buttons()
        self._btn_export_db_cmp.setEnabled(True)
        if has_new:
            self._db_result_tabs.setCurrentIndex(1)
        self._schedule_column_fit()
        self.ctx.notify("快速查重完成（未修改当前数据库）", "success")

    def run_db_compare(self, source_name: str = "快速粘贴") -> None:
        if self._is_worker_running(self._db_worker) or self._is_worker_running(self._import_worker):
            self.ctx.notify("正在执行查重/入库任务，请稍候或先取消", "warning")
            return
        ignore_comp = self._chk_db_ignore_comp.isChecked()
        comp_exts, expected_db = self._get_companion_exts(ignore_comp), str(self.ctx.db.db_path)
        loaded_file = self._db_loaded_file if (self._db_loaded_file and Path(self._db_loaded_file).is_file()) else ""
        text = "" if loaded_file else self._db_input.toPlainText().strip()
        if not loaded_file and not text:
            self.ctx.notify("请先粘贴或载入待比对内容", "warning")
            return

        self._db_gen += 1
        gen = self._db_gen
        self._set_db_busy(True, cancellable=True)
        self._db_summary.setText("正在后台执行快速查重...")
        wbox: list[Optional[WorkerThread]] = [None]
        _cancelled = lambda: bool(wbox[0] is not None and wbox[0].is_cancelled)

        def _task() -> Optional[CompareResult]:
            if loaded_file:
                p = Path(loaded_file)
                scanned = scan_document_file(p, companion_extensions=comp_exts)
                if _cancelled():
                    return None
                parsed = self.ctx.parser.parse_items(scanned, ignore_companions=ignore_comp, companion_extensions=comp_exts, cancel_check=_cancelled)
                return None if _cancelled() else compare_with_db(parsed, self.ctx.db, source_name=p.name)
            parsed = self.ctx.parser.parse_text(text, source_name=source_name, ignore_companions=ignore_comp, companion_extensions=comp_exts)
            return None if _cancelled() else compare_with_db(parsed, self.ctx.db, source_name=source_name)

        w = WorkerThread(_task, parent=self)
        wbox[0] = self._db_worker = w
        w.succeeded.connect(lambda res, g=gen, dbp=expected_db: self._on_db_text_compare_succeeded(g, res, dbp))
        w.failed.connect(lambda detail, g=gen: self._on_db_compare_failed(g, detail))
        w.cancelled.connect(lambda g=gen: self._on_db_compare_cancelled(g))
        w.finished.connect(lambda wk=w, g=gen: self._on_db_worker_finished(wk, g))
        w.start()

    def _on_db_text_compare_succeeded(self, gen: int, result: Optional[CompareResult], expected_db: str) -> None:
        if gen != self._db_gen:
            return
        self._set_db_busy(False)
        if result is None:
            self._on_db_compare_cancelled(gen)
        elif str(self.ctx.db.db_path) != expected_db:
            self._on_db_switched(str(self.ctx.db.db_path))
        else:
            self._apply_db_compare_result(result, db_path=expected_db)

    def _on_db_compare_cancelled(self, gen: int) -> None:
        if gen != self._db_gen or self._db_cancel_handled_gen == gen:
            return
        self._db_cancel_handled_gen = gen
        self._set_db_busy(False)
        self._db_summary.setText("已取消快速查重")
        if self._db_compare_result is not None:
            self._btn_export_db_cmp.setEnabled(True)
            self._btn_import_new.setEnabled((not self._db_result_stale) and bool(self._db_compare_result.not_in_db))
        self.ctx.notify("已取消快速查重", "info")

    def _on_db_compare_failed(self, gen: int, detail: str) -> None:
        if gen != self._db_gen:
            return
        self._set_db_busy(False)
        self._db_summary.setText("查重失败，请查看详情")
        ScrollableDetailDialog("快速查重失败", "执行查重时发生错误：", detail, parent=self).exec()

    def _on_db_worker_finished(self, worker: WorkerThread, gen: int) -> None:
        try:
            was_cancelled = worker.is_cancelled
        except RuntimeError:
            was_cancelled = False
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

    def _copy_not_in_db_codes(self, fmt: str = "newline") -> int:
        if not self._db_compare_result:
            return 0
        codes = [i.canonical_code for i in self._db_compare_result.not_in_db if i.canonical_code]
        if not (payload := format_codes_for_clipboard(codes, fmt=fmt)):
            return 0
        QApplication.clipboard().setText(payload)
        cnt = len(set(codes))
        self.ctx.notify(f"已复制 {cnt} 个新数据编码", "success")
        return cnt

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
        sname = self._db_compare_result.source_a_name or "快速查重入库"
        ext_map = {".docx": "word", ".xlsx": "excel", ".xlsm": "excel", ".csv": "csv", ".tsv": "csv", ".efu": "csv"}
        stype = ext_map.get(Path(self._db_loaded_file).suffix.lower(), "txt") if self._db_loaded_file else "paste"
        self._set_db_busy(True, cancellable=False)
        self._db_summary.setText(f"正在后台将 {len(items):,} 条新数据写入当前库...")

        def _task() -> int:
            recs = [DataRecord.from_parse_result(item) for item in items]
            saved, _ = self.ctx.db.add_records(recs, batch=ImportBatch(source_name=sname, source_type=stype, total_count=len(items), added_count=len(recs)))
            return len(saved)

        w = WorkerThread(_task, parent=self)
        self._import_worker = w
        w.succeeded.connect(lambda cnt, sn=sname: self._on_import_new_succeeded(cnt, sn))
        w.failed.connect(self._on_import_new_failed)
        w.finished.connect(lambda wk=w: self._on_import_worker_finished(wk))
        w.start()

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

    def _make_table(self, model: CompareTableModel, table_id: str) -> QTableView:
        tv = QTableView()
        tv.setModel(model)
        tv.setAlternatingRowColors(False)
        tv.setWordWrap(False)
        tv.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tv.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tv.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tv.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tv.customContextMenuRequested.connect(lambda pos, t=tv: self._show_table_menu(t, pos))
        enhance_table_view(tv, theme=self.ctx.config.theme, on_code_copied=self._on_inline_code_copied, row_height=34, header_height=34)
        mgr = TableHeaderManager.attach(tv, table_id, model.column_specs(), self.ctx)
        self._header_managers.append(mgr)

        def _item_at(row: int, m: CompareTableModel = model) -> Optional[ParseResult]:
            raw = m.row_at(row)
            return None if raw is None else (raw[0] if isinstance(raw, tuple) else raw)

        self._copy_helpers[tv] = TableCopyHelper(
            tv, self.ctx, header_manager=mgr, code_column=0,
            name_extractor=lambda r, fn=_item_at: (it.file_name or it.raw_text) if (it := fn(r)) else "",
            path_extractor=lambda r, fn=_item_at: (it.full_path or "") if (it := fn(r)) else "",
            install_shortcuts=True,
        )
        return tv

    def _show_table_menu(self, tv: QTableView, pos: Any) -> None:
        helper = self._copy_helpers.get(tv)
        if helper is None or not helper.selected_rows():
            return
        menu = QMenu(self)
        helper.populate_context_menu(menu)
        if not menu.isEmpty():
            menu.exec(tv.viewport().mapToGlobal(pos))

    def _pick_export_path(self, title: str, default_name: str, file_filter: str, explicit_path: Optional[str]) -> tuple[str, str]:
        if explicit_path:
            return explicit_path, ""
        save_path, sel_filter = QFileDialog.getSaveFileName(self, title, str(Path(self.ctx.config.default_export_dir or Path.home()) / default_name), file_filter)
        if save_path:
            try:
                self.ctx.config.default_export_dir = str(Path(save_path).parent)
                self.ctx.config_manager.save()
            except OSError:
                pass
        return save_path, sel_filter

    def _export_compare(self, result: Optional[CompareResult], fmt: str = "xlsx") -> None:
        if result is None:
            return
        if result.mode == "ab":
            self.export_ab_full_report()
            return
        if self._is_worker_running(self._export_worker):
            self.ctx.notify("正在导出文件，请稍候...", "warning")
            return
        presets = {
            "csv": ("快速查重结果.csv", "CSV 表格 (*.csv);;Excel 工作簿 (*.xlsx);;新数据编码清单 (*.txt)"),
            "txt": ("新数据编码清单.txt", "新数据编码清单 (*.txt);;Excel 工作簿 (*.xlsx);;CSV 表格 (*.csv)"),
            "xlsx": ("快速查重结果.xlsx", "Excel 工作簿 (*.xlsx);;CSV 表格 (*.csv);;新数据编码清单 (*.txt)"),
        }
        path, sel_filter = self._pick_export_path("导出比对结果", *presets.get(fmt, presets["xlsx"]), None)
        if not path:
            return
        self._disable_export_buttons()
        self.ctx.notify(f"正在后台导出 {Path(path).name}...", "info")

        def _task() -> str:
            low = path.lower()
            if low.endswith(".csv") or (fmt == "csv" and not low.endswith((".xlsx", ".txt"))) or "CSV" in sel_filter:
                export_compare_result_csv(result, path)
            elif low.endswith(".txt") or (fmt == "txt" and not low.endswith((".xlsx", ".csv"))) or "TXT" in sel_filter.upper():
                export_codes_txt(result.not_in_db, path, unique=True, sort_codes=True)
            else:
                export_compare_result_excel(result, path)
            return path

        self._start_export_worker(_task)

    def _disable_export_buttons(self) -> None:
        self._btn_export_db_cmp.setEnabled(False)
        self._btn_export_ab.setEnabled(False)
        self._btn_export_missing.setEnabled(False)
        self._act_export_missing_csv.setEnabled(False)

    def _start_export_worker(self, task_fn: Any) -> None:
        worker = WorkerThread(task_fn, parent=self)
        self._export_worker = worker
        worker.succeeded.connect(self._on_export_succeeded)
        worker.failed.connect(self._on_export_failed)
        worker.finished.connect(lambda w=worker: self._on_export_finished(w))
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
        has_ab = self._ab_result is not None
        has_missing = bool(self._ab_result and self._ab_result.missing_b_codes)
        self._btn_export_ab.setEnabled(has_ab)
        self._btn_export_missing.setEnabled(has_missing)
        self._act_export_missing_csv.setEnabled(has_missing)
