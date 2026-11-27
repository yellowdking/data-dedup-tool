"""View 1: 数据总库 — Searchable, customizable, rapid-copy database table & record editor."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMenu, QPlainTextEdit, QPushButton,
    QStackedWidget, QTableView, QVBoxLayout, QWidget,
)

from src.core.database import DatabaseError
from src.core.exporter import export_codes_txt, export_records_csv, export_records_excel
from src.core.models import DataRecord
from src.ui.app_context import AppContext
from src.ui.delegates import apply_slim_scrollbars, enhance_table_view
from src.ui.icons import get_icon
from src.ui.table_copy import TableCopyHelper
from src.ui.table_header import TableHeaderManager
from src.ui.table_models import RecordTableModel
from src.ui.theme import get_palette
from src.ui.widgets import (
    EmptyStateWidget, PageHeaderBar, ScrollableDetailDialog, WorkerThread, format_file_size,
)


def _stop_worker_safely(worker: Optional[WorkerThread], timeout_ms: int = 3000) -> None:
    if worker is not None:
        try:
            worker.safe_stop(timeout_ms)
        except RuntimeError:
            pass


def _split_code_prefix_number(code: str) -> tuple[str, str]:
    normalized = code.strip().upper()
    if not normalized:
        return "", ""
    if "-" in normalized:
        left, right = normalized.rsplit("-", 1)
        if left and right:
            return left, right
    m = re.match(r"^([A-Z]+)(\d+)$", normalized)
    return (m.group(1), m.group(2)) if m else (normalized, "")


def _parse_tags_input(raw: str) -> list[str]:
    tokens = re.split(r"[,，;/／|\s]+", raw.strip())
    return list(dict.fromkeys(t for tok in tokens if (t := tok.strip())))


def open_local_folder_safely(full_path: str, ctx: AppContext) -> None:
    """Safely reveal a local file or directory, rejecting UNC/network paths and non-dir targets."""
    raw = (full_path or "").strip()
    if not raw:
        ctx.notify("该记录未关联本地路径", "warning")
        return

    # Security: reject UNC network paths (prevents SMB NTLMv2 leak) and URI schemes.
    if raw.startswith("\\\\") or raw.startswith("//") or "://" in raw:
        ctx.notify("出于安全考虑，不支持直接打开网络共享 (UNC) 或远程链接路径", "warning")
        return

    try:
        p = Path(raw)
        if str(p).startswith(("\\\\", "//")):
            ctx.notify("出于安全考虑，不支持直接打开网络共享 (UNC) 路径", "warning")
            return
        if p.is_file():
            if os.name == "nt":
                subprocess.Popen(["explorer", f"/select,{os.path.normpath(str(p))}"])
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent)))
        elif p.is_dir():
            if os.name == "nt":
                os.startfile(os.path.normpath(str(p)))
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
        elif p.parent.is_dir():
            if os.name == "nt":
                os.startfile(os.path.normpath(str(p.parent)))
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent)))
        else:
            ctx.notify("未在本地磁盘找到该文件或目录（可能来源于文本清单导入）", "warning")
    except OSError as exc:
        ctx.notify(f"无法打开目录：{exc}", "error")


def _find_main_window(widget: QWidget) -> Optional[QWidget]:
    win = widget.window()
    return win if (win is not None and callable(getattr(win, "switch_view", None))) else None


def _detect_theme(parent: Optional[QWidget]) -> str:
    cur = parent
    while cur is not None:
        theme = getattr(getattr(getattr(cur, "ctx", None), "config", None), "theme", None)
        if theme in ("light", "dark"):
            return str(theme)
        cur = cur.parentWidget()
    return "light"


def _repolish(widget: QWidget) -> None:
    st = widget.style()
    if st is not None:
        st.unpolish(widget)
        st.polish(widget)


class RecordEditDialog(QDialog):
    """Modal dialog for inspecting record details and editing mutable metadata."""

    def __init__(
        self,
        record: DataRecord,
        parent: Optional[QWidget] = None,
        *,
        show_advanced_fields: bool = False,
    ) -> None:
        super().__init__(parent)
        self._orig = record
        self._theme = _detect_theme(parent)
        self._advanced_expanded = bool(show_advanced_fields or record.tags or record.part)
        self.setWindowTitle(f"记录详情与编辑  #{record.id or '-'}")
        self.resize(560, 440)
        p = get_palette(self._theme)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        meta_card = QFrame(self)
        meta_card.setProperty("card", True)
        meta_form = QFormLayout(meta_card)
        meta_form.setContentsMargins(14, 12, 14, 12)
        meta_form.setSpacing(6)

        def _ro(text: str) -> QLabel:
            lbl = QLabel(text or "-")
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lbl.setProperty("role", "subtitle")
            return lbl

        meta_form.addRow("原始文件名：", _ro(record.file_name or record.raw_text))
        if record.raw_text and record.raw_text != record.file_name:
            meta_form.addRow("原始文本：", _ro(record.raw_text))
        if record.full_path:
            meta_form.addRow("完整路径：", _ro(record.full_path))
        src_info = f"{record.source_name or '-'} ({record.source_type or 'txt'})"
        meta_form.addRow("来源 / 大小：", _ro(f"{src_info}  ·  {format_file_size(record.file_size)}"))
        if show_advanced_fields:
            meta_form.addRow("匹配规则：", _ro(f"{record.matched_rule or '-'} (置信度 {record.confidence:.2f})"))
        time_info = f"入库 {record.created_at[:19] if record.created_at else '-'}"
        if record.updated_at and record.updated_at != record.created_at:
            time_info += f"  ·  更新 {record.updated_at[:19]}"
        meta_form.addRow("时间信息：", _ro(time_info))
        root.addWidget(meta_card)

        edit_form = QFormLayout()
        edit_form.setSpacing(10)
        self._code_edit = QLineEdit(record.canonical_code)
        self._code_edit.setProperty("mono", True)
        self._code_edit.setPlaceholderText("必填，例如：ABC-123 或 PRJ-0042")
        edit_form.addRow("标准编码*：", self._code_edit)

        self._note_edit = QPlainTextEdit(record.note or "")
        self._note_edit.setPlaceholderText("可选备注信息...")
        self._note_edit.setMaximumHeight(82)
        apply_slim_scrollbars(self._note_edit, theme=self._theme)
        edit_form.addRow("备注：", self._note_edit)
        root.addLayout(edit_form)

        # Collapsible "更多属性" section (tucks 版本标签 & 分集标记 cleanly)
        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(0, 0, 0, 0)
        self._more_toggle_btn = QPushButton("更多属性（版本标签 / 分集标记）", self)
        self._more_toggle_btn.setProperty("variant", "ghost")
        self._more_toggle_btn.setProperty("compact", True)
        self._more_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._more_toggle_btn.setIconSize(QSize(14, 14))
        self._more_toggle_btn.clicked.connect(self._toggle_advanced_section)
        toggle_row.addWidget(self._more_toggle_btn)
        toggle_row.addStretch(1)
        root.addLayout(toggle_row)

        self._advanced_box = QFrame(self)
        self._advanced_box.setProperty("summaryStrip", True)
        adv_form = QFormLayout(self._advanced_box)
        adv_form.setContentsMargins(12, 10, 12, 10)
        adv_form.setSpacing(8)
        self._tags_edit = QLineEdit(", ".join(record.tags))
        self._tags_edit.setPlaceholderText("可选，用逗号分隔，例如：4K, 字幕, 高码率")
        adv_form.addRow("版本标签：", self._tags_edit)
        self._part_edit = QLineEdit(record.part)
        self._part_edit.setPlaceholderText("可选，例如：CD1 / CD2 / Part1")
        adv_form.addRow("分集标记：", self._part_edit)
        root.addWidget(self._advanced_box)
        self._sync_advanced_section()

        self._err_label = QLabel("")
        self._err_label.setStyleSheet(f"color: {p['danger']}; font-size: 12px; font-weight: 500;")
        self._err_label.setVisible(False)
        root.addWidget(self._err_label)
        root.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        btn_cancel = QPushButton("取消")
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.clicked.connect(self.reject)
        btn_save = QPushButton("保存修改")
        btn_save.setProperty("variant", "primary")
        btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save.clicked.connect(self._on_accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_save)
        root.addLayout(btn_row)

    def _toggle_advanced_section(self) -> None:
        self._advanced_expanded = not self._advanced_expanded
        self._sync_advanced_section()

    def _sync_advanced_section(self) -> None:
        self._advanced_box.setVisible(self._advanced_expanded)
        icon_name = "chevron-down" if self._advanced_expanded else "chevron-right"
        self._more_toggle_btn.setIcon(get_icon(icon_name, 14, theme=self._theme))

    def _on_accept(self) -> None:
        if not self._code_edit.text().strip():
            self._err_label.setText("标准编码不能为空")
            self._err_label.setVisible(True)
            self._code_edit.setFocus()
            return
        self.accept()

    def build_updated_record(self) -> DataRecord:
        code = self._code_edit.text().strip().upper()
        tags = _parse_tags_input(self._tags_edit.text())
        part = self._part_edit.text().strip().upper()
        note = self._note_edit.toPlainText().strip()
        if code != self._orig.canonical_code.strip().upper():
            prefix, number = _split_code_prefix_number(code)
            matched_rule = "手动编辑"
        else:
            prefix, number, matched_rule = self._orig.prefix, self._orig.number, self._orig.matched_rule
        return replace(
            self._orig, canonical_code=code, prefix=prefix, number=number,
            tags=tags, part=part, matched_rule=matched_rule, note=note,
        )


class LibraryView(QWidget):
    """Main database browsing view with instant search, customizable columns, rapid copy & export."""

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._model = RecordTableModel(self, theme=self.ctx.config.theme)
        self._dirty: bool = False
        self._initial_fit_done: bool = False
        self._total_db_records: int = 0
        self._empty_mode: str = ""
        self._delete_btn_active: bool = False
        self._export_worker: Optional[WorkerThread] = None
        self._delete_worker: Optional[WorkerThread] = None

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self.refresh_table)

        self._setup_ui()
        self.apply_theme(self.ctx.config.theme)
        self.ctx.data_changed.connect(self._on_data_changed)
        self.ctx.db_switched.connect(self._on_db_switched)
        self.ctx.theme_changed.connect(self.apply_theme)
        self.ctx.settings_changed.connect(self._on_settings_changed)
        self.refresh_all()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 1. Slim 48px PageHeaderBar
        self._header = PageHeaderBar("数据总库", parent=self)
        self._dup_pill_btn = QPushButton("", self._header)
        self._dup_pill_btn.setProperty("metricPill", "warning")
        self._dup_pill_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dup_pill_btn.setVisible(False)
        self._dup_pill_btn.setToolTip("点击前往重复项中心处理重复记录")
        self._dup_pill_btn.clicked.connect(self._go_to_duplicates)
        self._header.add_meta_widget(self._dup_pill_btn)

        self._refresh_btn = QPushButton("刷新", self._header)
        self._refresh_btn.setProperty("variant", "ghost")
        self._refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_btn.setIconSize(QSize(15, 15))
        self._refresh_btn.setToolTip("刷新当前数据库列表 (F5)")
        self._refresh_btn.clicked.connect(self.refresh_all)
        self._header.add_action_widget(self._refresh_btn)

        self._export_btn = QPushButton("导出", self._header)
        self._export_btn.setProperty("variant", "primary")
        self._export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._export_btn.setIconSize(QSize(15, 15))
        export_menu = QMenu(self._export_btn)
        export_menu.addAction("导出为 Excel (.xlsx)", lambda: self._export("xlsx"))
        export_menu.addAction("导出为 CSV (.csv)", lambda: self._export("csv"))
        export_menu.addAction("导出编码清单 (.txt)", lambda: self._export("txt"))
        self._export_btn.setMenu(export_menu)
        self._header.add_action_widget(self._export_btn)
        root.addWidget(self._header)

        body = QWidget(self)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(20, 12, 20, 12)
        body_layout.setSpacing(10)

        # 2. Virtualized TableView + Craft + TableHeaderManager + TableCopyHelper
        self._table = QTableView(body)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)
        self._table.doubleClicked.connect(self._on_row_double_clicked)

        self._delegate = enhance_table_view(
            self._table, theme=self.ctx.config.theme, on_code_copied=self._on_inline_code_copied
        )
        self._header_manager = TableHeaderManager.attach(
            self._table, "library_main", RecordTableModel.COLUMN_SPECS, self.ctx
        )
        self._copy_helper = TableCopyHelper(
            self._table, self.ctx, header_manager=self._header_manager,
            code_column=0, name_column=4,
            code_extractor=self._extract_row_code,
            name_extractor=self._extract_row_name,
            path_extractor=self._extract_row_path,
            default_ctrl_c_mode="code", install_shortcuts=True,
        )
        self._table.selectionModel().selectionChanged.connect(self._on_selection_changed)

        self._sc_delete = QShortcut(QKeySequence(Qt.Key.Key_Delete), self._table)
        self._sc_delete.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._sc_delete.activated.connect(self._delete_current_selection)

        # 3. Single clean borderless toolbar
        toolbar = QFrame(body)
        toolbar.setProperty("toolbar", True)
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(0, 0, 0, 0)
        tb_layout.setSpacing(8)

        self._search_input = QLineEdit(toolbar)
        self._search_input.setClearButtonEnabled(True)
        self._search_action = self._search_input.addAction(
            get_icon("search", 15, theme=self.ctx.config.theme),
            QLineEdit.ActionPosition.LeadingPosition,
        )
        self._sync_search_placeholder()
        self._search_input.textChanged.connect(lambda _: self._search_timer.start())
        tb_layout.addWidget(self._search_input, 1)

        self._filter_combo = QComboBox(toolbar)
        self._filter_combo.setMinimumWidth(108)
        self._populate_filter_combo()
        self._filter_combo.currentIndexChanged.connect(lambda _: self.refresh_table())
        tb_layout.addWidget(self._filter_combo)

        self._prefix_combo = QComboBox(toolbar)
        self._prefix_combo.setMinimumWidth(112)
        self._prefix_combo.addItem("全部前缀", "")
        self._prefix_combo.currentIndexChanged.connect(lambda _: self.refresh_table())
        tb_layout.addWidget(self._prefix_combo)

        self._copy_btn = self._copy_helper.create_copy_split_button(
            parent=toolbar, primary_variant=False,
            all_label="复制全部编码", selected_label="复制选中编码",
        )
        tb_layout.addWidget(self._copy_btn)

        self._delete_btn = QPushButton("删除选中", toolbar)
        self._delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._delete_btn.setIconSize(QSize(15, 15))
        self._delete_btn.setEnabled(False)
        self._delete_btn.setToolTip("安全删除选中的记录 (Delete，执行前自动备份)")
        self._delete_btn.clicked.connect(self._delete_current_selection)
        tb_layout.addWidget(self._delete_btn)

        self._col_btn = QPushButton(toolbar)
        self._col_btn.setProperty("variant", "ghost")
        self._col_btn.setProperty("iconOnly", True)
        self._col_btn.setFixedSize(32, 32)
        self._col_btn.setIconSize(QSize(16, 16))
        self._col_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._col_btn.setToolTip("列设置（显示/隐藏列、调整列宽、恢复默认）")
        self._col_btn.clicked.connect(self._open_column_menu)
        tb_layout.addWidget(self._col_btn)
        body_layout.addWidget(toolbar)

        # 4. Stacked content area (TableView / EmptyStateWidget)
        self._body_stack = QStackedWidget(body)
        self._body_stack.addWidget(self._table)
        self._empty_state = EmptyStateWidget(
            icon_name="database", title="当前数据库为空",
            subtitle="尚未入库任何记录，您可以批量导入文件清单，或打开已有本地数据库。",
            icon_tone="default", theme=self.ctx.config.theme, parent=body,
        )
        self._body_stack.addWidget(self._empty_state)
        body_layout.addWidget(self._body_stack, 1)

        # 5. Slim footer row
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 2, 0, 0)
        self._footer_label = QLabel("当前显示 0 条记录")
        self._footer_label.setProperty("role", "subtitle")
        footer.addWidget(self._footer_label)
        footer.addStretch()
        hint_label = QLabel("双击编辑详情 · Ctrl+C 复制编码 · Delete 删除 · 右键表头自定义列")
        hint_label.setProperty("role", "muted")
        footer.addWidget(hint_label)
        body_layout.addLayout(footer)
        root.addWidget(body, 1)

    # -- Lifecycle, lazy refresh & theme --

    def shutdown(self) -> None:
        self._search_timer.stop()
        self._header_manager.flush()
        _stop_worker_safely(self._export_worker)
        self._export_worker = None
        _stop_worker_safely(self._delete_worker)
        self._delete_worker = None

    def focus_search(self) -> None:
        self._search_input.setFocus()
        self._search_input.selectAll()

    def mark_dirty(self) -> None:
        self._dirty = True
        if self.isVisible():
            self.refresh_all()

    def on_activated(self) -> None:
        if self._dirty:
            self.refresh_all()
        self._schedule_initial_column_fit()

    def refresh_if_dirty(self) -> None:
        self.on_activated()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dirty:
            self.refresh_all()
        self._schedule_initial_column_fit()

    def _schedule_initial_column_fit(self) -> None:
        if not self._initial_fit_done:
            QTimer.singleShot(0, self._fit_default_columns_to_viewport)

    def _fit_default_columns_to_viewport(self) -> None:
        if self._initial_fit_done or "library_main" in self.ctx.config.table_layouts:
            self._initial_fit_done = True
            return
        if self._table.viewport().width() <= 200:
            return
        self._initial_fit_done = True
        self._header_manager.reset_to_defaults(save_config=False)

    def _sync_delete_button_style(self, active: bool) -> None:
        self._delete_btn_active = bool(active)
        self._delete_btn.setProperty("variant", "danger-outline" if active else "")
        _repolish(self._delete_btn)
        norm = "dark" if self.ctx.config.theme == "dark" else "light"
        p = get_palette(norm)
        self._delete_btn.setIcon(
            get_icon("trash", 15, p["danger"] if active else p["btn_disabled_text"],
                     theme=norm, disabled_color=p["btn_disabled_text"])
        )

    def apply_theme(self, theme: str) -> None:
        norm = "dark" if theme == "dark" else "light"
        p = get_palette(norm)
        if hasattr(self._model, "set_theme"):
            self._model.set_theme(norm)
        self._delegate.set_theme(norm)
        self._empty_state.apply_theme(norm)
        self._search_action.setIcon(get_icon("search", 15, p["icon_muted"], theme=norm))
        self._refresh_btn.setIcon(get_icon("refresh-cw", 15, p["icon_default"], theme=norm))
        self._export_btn.setIcon(
            get_icon("download", 15, p["cta_text"], theme=norm, disabled_color=p["btn_disabled_text"])
        )
        self._col_btn.setIcon(get_icon("columns", 16, p["icon_default"], theme=norm))
        self._sync_delete_button_style(self._delete_btn_active)

    def _on_data_changed(self) -> None:
        if not self.isVisible():
            self._dirty = True
            return
        self.refresh_all()

    def _clear_filters_ui(self) -> None:
        self._search_timer.stop()
        for w, fn in (
            (self._search_input, self._search_input.clear),
            (self._filter_combo, lambda: self._filter_combo.setCurrentIndex(0)),
            (self._prefix_combo, lambda: self._prefix_combo.setCurrentIndex(0)),
        ):
            w.blockSignals(True)
            fn()
            w.blockSignals(False)

    def _on_db_switched(self, _db_path: str) -> None:
        self._clear_filters_ui()
        if not self.isVisible():
            self._dirty = True
            return
        self.refresh_all()

    def _on_settings_changed(self) -> None:
        self._sync_search_placeholder()
        prev_mode = self._filter_combo.currentData() or "all"
        self._filter_combo.blockSignals(True)
        self._populate_filter_combo()
        idx = self._filter_combo.findData(prev_mode)
        self._filter_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._filter_combo.blockSignals(False)
        if idx < 0:
            self.refresh_table()

    def _sync_search_placeholder(self) -> None:
        adv = bool(getattr(self.ctx.config, "show_advanced_fields", False))
        self._search_input.setPlaceholderText(
            "搜索编码、文件名、路径、来源或标签..." if adv else "搜索编码、文件名、路径或来源..."
        )

    def _populate_filter_combo(self) -> None:
        self._filter_combo.clear()
        self._filter_combo.addItem("全部记录", "all")
        self._filter_combo.addItem("仅重复项", "duplicates")
        self._filter_combo.addItem("仅唯一项", "unique")
        if getattr(self.ctx.config, "show_advanced_fields", False):
            self._filter_combo.addItem("疑似分集", "multipart")

    # -- Data loading, metrics & empty states --

    def refresh_all(self) -> None:
        self._dirty = False
        self._reload_prefixes()
        self.refresh_table()

    def _reload_prefixes(self) -> None:
        current_prefix = self._prefix_combo.currentData() or ""
        try:
            prefixes = self.ctx.db.get_distinct_prefixes()
        except (DatabaseError, OSError, Exception) as exc:
            self.ctx.notify(f"加载前缀列表失败：{exc}", "error")
            return
        self._prefix_combo.blockSignals(True)
        self._prefix_combo.clear()
        self._prefix_combo.addItem("全部前缀", "")
        for prefix in prefixes:
            self._prefix_combo.addItem(prefix, prefix)
        idx = self._prefix_combo.findData(current_prefix)
        self._prefix_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._prefix_combo.blockSignals(False)

    def refresh_table(self) -> None:
        try:
            records = self.ctx.db.query_records(
                search=self._search_input.text().strip(),
                filter_mode=self._filter_combo.currentData() or "all",
                prefix=self._prefix_combo.currentData() or "",
                sort_by="id",
                sort_desc=True,
            )
            code_counts = self.ctx.db.get_code_counts()
            stats = self.ctx.db.get_stats()
        except (DatabaseError, OSError, Exception) as exc:
            ScrollableDetailDialog("读取数据总库失败", "查询记录时发生错误：", str(exc), parent=self).exec()
            return

        self._total_db_records = stats.total_records
        dup_codes = {k for k, v in code_counts.items() if v > 1}
        self._model.set_records(records, duplicate_codes=dup_codes)
        self._header_manager.reapply_sort()
        self._copy_helper.update_split_buttons()

        self._header.set_metrics([f"{stats.total_records:,} 条记录", f"{stats.unique_codes:,} 个编码"])
        if stats.duplicate_groups > 0:
            self._dup_pill_btn.setText(f"{stats.duplicate_groups:,} 组重复")
            self._dup_pill_btn.setVisible(True)
        else:
            self._dup_pill_btn.setVisible(False)

        self._update_empty_state()
        self._update_footer()

    def _update_empty_state(self) -> None:
        if self._total_db_records == 0:
            if self._empty_mode != "empty_db":
                self._empty_mode = "empty_db"
                self._empty_state.set_content(
                    title="当前数据库为空",
                    subtitle="尚未入库任何记录，您可以批量导入文件清单，或打开已有本地数据库。",
                    icon_name="database", icon_tone="default",
                )
                self._empty_state.clear_actions()
                self._empty_state.add_action("前往批量导入", self._go_to_import, variant="primary", icon_name="upload")
                self._empty_state.add_action("打开已有数据库", self._open_existing_db, variant="", icon_name="folder-open")
            self._body_stack.setCurrentIndex(1)
            return

        if self._model.rowCount() == 0:
            if self._empty_mode != "no_match":
                self._empty_mode = "no_match"
                self._empty_state.set_content(
                    title="没有匹配的记录",
                    subtitle="当前搜索或筛选条件未命中任何记录，请尝试重置条件。",
                    icon_name="search", icon_tone="default",
                )
                self._empty_state.clear_actions()
                self._empty_state.add_action("重置筛选", self._reset_filters, variant="primary", icon_name="x")
            self._body_stack.setCurrentIndex(1)
            return

        self._empty_mode = ""
        self._body_stack.setCurrentIndex(0)

    def _on_selection_changed(self, *_args) -> None:
        self._update_footer()

    def _update_footer(self) -> None:
        total = self._model.rowCount()
        sel_model = self._table.selectionModel()
        selected = len(sel_model.selectedRows()) if sel_model is not None else 0
        can_delete = selected > 0 and not self._is_worker_running(self._delete_worker)
        self._delete_btn.setEnabled(can_delete)
        if can_delete != self._delete_btn_active:
            self._sync_delete_button_style(can_delete)
        self._footer_label.setText(
            f"当前显示 {total:,} 条记录 · 已选中 {selected} 条" if selected > 0 else f"当前显示 {total:,} 条记录"
        )

    def _selected_records(self) -> list[DataRecord]:
        sel_model = self._table.selectionModel()
        if sel_model is None:
            return []
        return [rec for idx in sel_model.selectedRows() if (rec := self._model.record_at(idx.row())) is not None]

    # -- Navigation, column menu & copy extractors --

    def _go_to_duplicates(self) -> None:
        win = _find_main_window(self)
        if win is not None:
            win.switch_view(4)
            return
        idx = self._filter_combo.findData("duplicates")
        if idx >= 0:
            self._filter_combo.setCurrentIndex(idx)

    def _go_to_import(self) -> None:
        win = _find_main_window(self)
        if win is not None:
            win.switch_view(1)

    def _open_existing_db(self) -> None:
        win = _find_main_window(self)
        if win is not None and callable(getattr(win, "open_existing_db", None)):
            win.open_existing_db()
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "打开已有数据库文件", str(Path(self.ctx.db.db_path).parent),
            "SQLite 数据库 (*.db *.sqlite *.sqlite3);;所有文件 (*.*)",
        )
        if path:
            try:
                self.ctx.switch_database(path)
                self.ctx.notify(f"已打开数据库：{Path(path).stem}", "success")
            except Exception as exc:
                ScrollableDetailDialog("打开数据库失败", str(exc), parent=self).exec()

    def _reset_filters(self) -> None:
        self._clear_filters_ui()
        self.refresh_table()

    def _open_column_menu(self) -> None:
        self._header_manager.open_column_menu(self._col_btn.mapToGlobal(QPoint(0, self._col_btn.height() + 4)))

    def _extract_row_code(self, row: int) -> str:
        rec = self._model.record_at(row)
        return rec.canonical_code if rec is not None else ""

    def _extract_row_name(self, row: int) -> str:
        rec = self._model.record_at(row)
        return (rec.file_name or rec.raw_text) if rec is not None else ""

    def _extract_row_path(self, row: int) -> str:
        rec = self._model.record_at(row)
        return rec.full_path if rec is not None else ""

    def _on_inline_code_copied(self, code: str) -> None:
        self.ctx.notify(f"已复制编码：{code}", "success")

    # -- Record edit & context menu --

    def _on_row_double_clicked(self, index) -> None:
        if index.isValid() and (rec := self._model.record_at(index.row())) is not None:
            self._open_edit_dialog(rec)

    def _open_edit_dialog(self, record: DataRecord) -> None:
        dlg = RecordEditDialog(
            record, parent=self,
            show_advanced_fields=bool(getattr(self.ctx.config, "show_advanced_fields", False)),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dlg.build_updated_record()
        try:
            if self.ctx.db.update_record(updated):
                self.ctx.notify(f"已更新记录 #{updated.id}（{updated.canonical_code}）", "success")
                self.ctx.data_changed.emit()
            else:
                self.ctx.notify("未找到该记录，可能已被其他操作删除", "warning")
        except (DatabaseError, ValueError, OSError, Exception) as exc:
            ScrollableDetailDialog("更新记录失败", "保存记录修改时发生错误：", str(exc), parent=self).exec()

    def _delete_current_selection(self) -> None:
        records = self._selected_records()
        if records:
            self._delete_selected(records)

    def _show_context_menu(self, pos: QPoint) -> None:
        index = self._table.indexAt(pos)
        if index.isValid() and index.row() not in self._copy_helper.selected_rows():
            self._table.selectRow(index.row())

        records = self._selected_records()
        if not records:
            return
        selected_rows = self._copy_helper.selected_rows()

        menu = QMenu(self)
        if len(records) == 1:
            menu.addAction("查看 / 编辑记录详情", lambda: self._open_edit_dialog(records[0]))
            menu.addSeparator()

        self._copy_helper.populate_context_menu(menu, rows=selected_rows)

        if len(records) == 1 and records[0].full_path:
            menu.addSeparator()
            menu.addAction(
                "打开所在目录",
                lambda _=False, fp=records[0].full_path: self._open_containing_folder(fp),
            )

        menu.addSeparator()
        menu.addAction(f"删除选中记录 ({len(records)} 条)", lambda: self._delete_selected(records))
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _open_containing_folder(self, full_path: str) -> None:
        open_local_folder_safely(full_path, self.ctx)

    # -- Threaded delete & export --

    def _is_worker_running(self, worker: Optional[WorkerThread]) -> bool:
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            return False

    def _delete_selected(self, records: list[DataRecord]) -> None:
        if self._is_worker_running(self._delete_worker):
            self.ctx.notify("正在执行删除任务，请稍候...", "warning")
            return
        ids = [r.id for r in records if r.id is not None]
        if not ids:
            return
        preview = [f"[{r.canonical_code}] {r.file_name or r.raw_text}" for r in records[:100]]
        if len(records) > 100:
            preview.append(f"... 共 {len(records)} 条记录")
        dlg = ScrollableDetailDialog(
            title="确认删除记录",
            summary=f"即将从当前数据库永久删除 {len(ids)} 条记录（删除前将自动创建数据库备份）：",
            detail_text="\n".join(preview),
            confirm_mode=True, confirm_label="确认删除", danger=True, parent=self,
        )
        if dlg.exec() != ScrollableDetailDialog.DialogCode.Accepted:
            return

        self._table.setEnabled(False)
        self._delete_btn.setEnabled(False)
        self._sync_delete_button_style(False)
        self._footer_label.setText(f"正在后台安全删除 {len(ids)} 条记录（含自动备份）...")

        worker = WorkerThread(lambda: self.ctx.db.delete_records(ids), parent=self)
        self._delete_worker = worker
        worker.succeeded.connect(self._on_delete_succeeded)
        worker.failed.connect(self._on_delete_failed)
        worker.finished.connect(lambda w=worker: self._on_delete_finished(w))
        worker.start()

    def _on_delete_succeeded(self, deleted: int) -> None:
        self._table.setEnabled(True)
        self.ctx.notify(f"已删除 {deleted} 条记录（已自动备份）", "success")
        self.ctx.data_changed.emit()

    def _on_delete_failed(self, detail: str) -> None:
        self._table.setEnabled(True)
        self._update_footer()
        ScrollableDetailDialog("删除记录失败", "删除过程中发生数据库错误：", detail, parent=self).exec()

    def _on_delete_finished(self, worker: WorkerThread) -> None:
        if self._delete_worker is worker:
            self._delete_worker = None
        self._table.setEnabled(True)
        self._update_footer()

    def _export(self, fmt: str) -> None:
        if self._is_worker_running(self._export_worker):
            self.ctx.notify("正在导出文件，请稍候...", "warning")
            return
        records = self._model.all_records()
        if not records:
            self.ctx.notify("当前列表没有可导出的记录", "warning")
            return

        default_dir = self.ctx.config.default_export_dir or str(Path.home() / "Desktop")
        ext_map = {
            "xlsx": ("Excel 工作簿 (*.xlsx)", "数据总库导出.xlsx"),
            "csv": ("CSV 表格 (*.csv)", "数据总库导出.csv"),
            "txt": ("编码清单 (*.txt)", "标准编码清单.txt"),
        }
        file_filter, default_name = ext_map[fmt]
        save_path, _ = QFileDialog.getSaveFileName(
            self, "选择导出保存位置", str(Path(default_dir) / default_name), file_filter
        )
        if not save_path:
            return

        try:
            self.ctx.config.default_export_dir = str(Path(save_path).parent)
            self.ctx.config_manager.save()
        except OSError:
            pass

        self._export_btn.setEnabled(False)
        self._footer_label.setText(f"正在后台导出 {len(records):,} 条记录至 {Path(save_path).name}...")

        def _task() -> str:
            if fmt == "xlsx":
                export_records_excel(
                    records, save_path,
                    code_counts=self.ctx.db.get_code_counts(), stats=self.ctx.db.get_stats(),
                )
            elif fmt == "csv":
                export_records_csv(records, save_path, code_counts=self.ctx.db.get_code_counts())
            else:
                export_codes_txt(records, save_path, unique=True, sort_codes=True)
            return save_path

        worker = WorkerThread(_task, parent=self)
        self._export_worker = worker
        worker.succeeded.connect(self._on_export_succeeded)
        worker.failed.connect(self._on_export_failed)
        worker.finished.connect(lambda w=worker: self._on_export_finished(w))
        worker.start()

    def _on_export_succeeded(self, save_path: str) -> None:
        self._export_btn.setEnabled(True)
        self._update_footer()
        self.ctx.notify(f"导出成功：{Path(save_path).name}", "success")

    def _on_export_failed(self, detail: str) -> None:
        self._export_btn.setEnabled(True)
        self._update_footer()
        ScrollableDetailDialog("导出失败", "导出过程中发生错误：", detail, parent=self).exec()

    def _on_export_finished(self, worker: WorkerThread) -> None:
        if self._export_worker is worker:
            self._export_worker = None
        self._export_btn.setEnabled(True)
