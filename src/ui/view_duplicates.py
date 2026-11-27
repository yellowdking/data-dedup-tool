"""View 4: 重复项中心 — Virtualized flat/grouped duplicate table, rapid copy & safe cleanup."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QModelIndex, QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.core.database import DatabaseError
from src.core.dedup import (
    STRATEGY_KEEP_EARLIEST,
    STRATEGY_KEEP_HIGHEST_TAG_SCORE,
    STRATEGY_KEEP_LATEST,
    STRATEGY_KEEP_LONGEST_NAME,
    execute_duplicate_cleanup,
    plan_duplicate_cleanup,
)
from src.core.exporter import export_duplicate_groups_csv, export_duplicate_groups_excel
from src.core.models import DataRecord, DuplicateGroup
from src.ui.app_context import AppContext
from src.ui.delegates import enhance_table_view
from src.ui.icons import get_icon
from src.ui.table_copy import TableCopyHelper
from src.ui.table_header import TableHeaderManager
from src.ui.table_models import DuplicateTableModel
from src.ui.theme import get_palette
from src.ui.view_library import open_local_folder_safely
from src.ui.widgets import (
    EmptyStateWidget,
    PageHeaderBar,
    ScrollableDetailDialog,
    WorkerThread,
)


def _stop_worker_safely(worker: Optional[WorkerThread], timeout_ms: int = 3000) -> None:
    if worker is not None:
        try:
            worker.safe_stop(timeout_ms)
        except RuntimeError:
            pass


def _find_main_window(widget: QWidget) -> Optional[QWidget]:
    win = widget.window()
    return win if (win is not None and callable(getattr(win, "switch_view", None))) else None


class DuplicatesView(QWidget):
    """Virtualized duplicate management center with flat/grouped toggle, rapid copy & safe cleanup."""

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._groups: list[DuplicateGroup] = []
        self._dirty: bool = False
        self._initial_fit_done: bool = False
        self._cleanup_worker: Optional[WorkerThread] = None
        self._export_worker: Optional[WorkerThread] = None
        self._pressed_check_state: Optional[tuple[int, bool]] = None
        self._empty_mode: str = ""

        initial_mode = (
            "grouped" if getattr(self.ctx.config, "duplicates_view_mode", "flat") == "grouped" else "flat"
        )
        self._model = DuplicateTableModel(self, theme=self.ctx.config.theme, view_mode=initial_mode)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._apply_search_filter)

        self._setup_ui(initial_mode)
        self.apply_theme(self.ctx.config.theme)

        self._model.checked_changed.connect(self._on_checked_changed)
        self.ctx.data_changed.connect(self._on_data_changed)
        self.ctx.db_switched.connect(self._on_db_switched)
        self.ctx.theme_changed.connect(self.apply_theme)
        self.ctx.settings_changed.connect(self._on_settings_changed)
        self.refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self, initial_mode: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 1. Slim 48px PageHeaderBar
        self._header = PageHeaderBar("重复项中心", parent=self)
        self._btn_delete = QPushButton("删除已勾选 (0)")
        self._btn_delete.setProperty("variant", "danger")
        self._btn_delete.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_delete.setIconSize(QSize(15, 15))
        self._btn_delete.setEnabled(False)
        self._btn_delete.setToolTip("预览并安全删除已勾选的重复记录（执行前自动创建 .bak 备份）")
        self._btn_delete.clicked.connect(self._confirm_and_delete)
        self._header.add_action_widget(self._btn_delete)
        root.addWidget(self._header)

        body = QWidget(self)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(20, 12, 20, 12)
        body_layout.setSpacing(10)

        # 2. Virtualized QTableView + ModernItemDelegate + TableHeaderManager + TableCopyHelper
        self._table = QTableView(body)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)
        self._table.pressed.connect(self._on_table_pressed)
        self._table.clicked.connect(self._on_table_clicked)

        self._delegate = enhance_table_view(
            self._table,
            theme=self.ctx.config.theme,
            on_code_copied=self._on_inline_code_copied,
        )
        self._header_manager = TableHeaderManager.attach(
            self._table,
            "duplicates_main",
            DuplicateTableModel.COLUMN_SPECS,
            self.ctx,
        )
        self._copy_helper = TableCopyHelper(
            self._table,
            self.ctx,
            header_manager=self._header_manager,
            code_column=DuplicateTableModel.COL_CODE,
            name_column=DuplicateTableModel.COL_FILE_NAME,
            code_extractor=self._extract_row_code,
            name_extractor=self._extract_row_name,
            path_extractor=self._extract_row_path,
            default_ctrl_c_mode="code",
            install_shortcuts=True,
        )

        self._sc_space = QShortcut(QKeySequence(Qt.Key.Key_Space), self._table)
        self._sc_space.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._sc_space.activated.connect(self._toggle_selected_checks)

        # 3. Single-row borderless toolbar
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

        self._copy_btn = self._copy_helper.create_copy_split_button(
            parent=toolbar,
            primary_variant=True,
            all_label="复制全部编码",
            selected_label="复制选中编码",
        )
        tb_layout.addWidget(self._copy_btn)

        # Smart selection dropdown menu button
        self._smart_btn = QPushButton("智能勾选", toolbar)
        self._smart_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._smart_btn.setIconSize(QSize(15, 15))
        self._smart_menu = QMenu(self._smart_btn)
        for label, strat in (
            ("每组保留最早", STRATEGY_KEEP_EARLIEST),
            ("每组保留最新", STRATEGY_KEEP_LATEST),
            ("每组保留最长文件名", STRATEGY_KEEP_LONGEST_NAME),
            ("每组保留最高标签质量（进阶）", STRATEGY_KEEP_HIGHEST_TAG_SCORE),
        ):
            self._smart_menu.addAction(label, lambda _=False, s=strat: self._apply_smart_plan(s))
        self._smart_menu.addSeparator()
        self._smart_menu.addAction("取消全部勾选", self._clear_checks)
        self._smart_menu.addSeparator()

        self._act_protect_multipart = QAction("保护不同分集不被误删", self._smart_menu)
        self._act_protect_multipart.setCheckable(True)
        self._act_protect_multipart.setChecked(True)
        self._smart_menu.addAction(self._act_protect_multipart)

        self._act_include_multipart = QAction("包含疑似分集组", self._smart_menu)
        self._act_include_multipart.setCheckable(True)
        self._act_include_multipart.setChecked(True)
        self._act_include_multipart.toggled.connect(lambda _: self.refresh())
        self._smart_menu.addAction(self._act_include_multipart)

        self._smart_btn.setMenu(self._smart_menu)
        tb_layout.addWidget(self._smart_btn)

        # Compact [ 平铺 | 分组 ] segmented toggle
        seg_frame = QFrame(toolbar)
        seg_frame.setProperty("segmented", True)
        seg_layout = QHBoxLayout(seg_frame)
        seg_layout.setContentsMargins(2, 2, 2, 2)
        seg_layout.setSpacing(2)

        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._btn_mode_flat = QPushButton("平铺", seg_frame)
        self._btn_mode_grouped = QPushButton("分组", seg_frame)
        for idx, (btn, mode_key, tip) in enumerate((
            (self._btn_mode_flat, "flat", "平铺列表视图（支持全列自由排序与拖拽调序）"),
            (self._btn_mode_grouped, "grouped", "按编码分组折叠视图（点击分组行可展开或折叠）"),
        )):
            btn.setProperty("segment", True)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(tip)
            btn.clicked.connect(lambda _=False, m=mode_key: self._set_view_mode(m))
            self._mode_group.addButton(btn, idx)
            seg_layout.addWidget(btn)
        (self._btn_mode_grouped if initial_mode == "grouped" else self._btn_mode_flat).setChecked(True)
        tb_layout.addWidget(seg_frame)

        # More icon menu button
        self._more_btn = QPushButton(toolbar)
        self._more_btn.setProperty("iconOnly", True)
        self._more_btn.setFixedSize(32, 32)
        self._more_btn.setIconSize(QSize(16, 16))
        self._more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._more_btn.setToolTip("更多操作（列设置、导出、刷新）")
        self._more_btn.clicked.connect(self._open_more_menu)
        tb_layout.addWidget(self._more_btn)

        body_layout.addWidget(toolbar)

        # 4. Stacked content area (Page 0: TableView, Page 1: EmptyStateWidget)
        self._body_stack = QStackedWidget(body)
        self._body_stack.addWidget(self._table)
        self._empty_state = EmptyStateWidget(
            icon_name="check-circle",
            title="当前数据库没有重复记录",
            subtitle="所有已入库标准编码均保持唯一，无需清理。",
            icon_tone="success",
            theme=self.ctx.config.theme,
            parent=body,
        )
        self._body_stack.addWidget(self._empty_state)
        body_layout.addWidget(self._body_stack, 1)

        # 5. Slim contextual footer bar
        footer_row = QHBoxLayout()
        footer_row.setContentsMargins(0, 2, 0, 0)
        footer_row.setSpacing(8)

        self._checked_label = QLabel("右键表头自定义列与顺序 · 空格勾选 · 删除前自动备份 .bak")
        self._checked_label.setProperty("role", "muted")
        footer_row.addWidget(self._checked_label)

        self._whole_group_badge = QLabel("")
        self._whole_group_badge.setProperty("badge", "danger")
        self._whole_group_badge.setVisible(False)
        footer_row.addWidget(self._whole_group_badge)

        self._btn_clear_checks = QPushButton("取消勾选")
        self._btn_clear_checks.setProperty("variant", "ghost")
        self._btn_clear_checks.setProperty("compact", True)
        self._btn_clear_checks.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear_checks.setVisible(False)
        self._btn_clear_checks.clicked.connect(self._clear_checks)
        footer_row.addWidget(self._btn_clear_checks)

        footer_row.addStretch(1)
        self._shortcut_hint = QLabel("Ctrl+C 复制编码 · Ctrl+Shift+C 复制文件名")
        self._shortcut_hint.setProperty("role", "muted")
        footer_row.addWidget(self._shortcut_hint)

        body_layout.addLayout(footer_row)
        root.addWidget(body, 1)

    # ------------------------------------------------------------------
    # Lifecycle, theme, settings & MainWindow hooks
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self._search_timer.stop()
        self._header_manager.flush()
        _stop_worker_safely(self._cleanup_worker)
        self._cleanup_worker = None
        _stop_worker_safely(self._export_worker)
        self._export_worker = None

    def focus_search(self) -> None:
        self._search_input.setFocus()
        self._search_input.selectAll()

    def mark_dirty(self) -> None:
        self._dirty = True
        if self.isVisible():
            self.refresh()

    def on_activated(self) -> None:
        if self._dirty:
            self.refresh()
        self._schedule_initial_column_fit()

    def refresh_if_dirty(self) -> None:
        self.on_activated()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dirty:
            self.refresh()
        self._schedule_initial_column_fit()

    def _schedule_initial_column_fit(self) -> None:
        if not self._initial_fit_done:
            QTimer.singleShot(0, self._fit_default_columns_to_viewport)

    def _fit_default_columns_to_viewport(self) -> None:
        """Fit uncustomized default columns cleanly into the initial viewport without saving."""
        if self._initial_fit_done or "duplicates_main" in self.ctx.config.table_layouts:
            self._initial_fit_done = True
            return
        vp_w = self._table.viewport().width()
        if vp_w <= 200:
            return
        self._initial_fit_done = True
        header = self._table.horizontalHeader()
        vis_cols = self._header_manager.visible_logical_columns_in_visual_order()
        total_w = sum(header.sectionSize(c) for c in vis_cols)
        file_col = DuplicateTableModel.COL_FILE_NAME
        if file_col not in vis_cols:
            return
        delta = (vp_w - 4) - total_w
        if abs(delta) < 6:
            return
        new_w = max(180, min(520, header.sectionSize(file_col) + delta))
        header.blockSignals(True)
        try:
            self._table.setColumnWidth(file_col, new_w)
        finally:
            header.blockSignals(False)

    def _on_data_changed(self) -> None:
        if not self.isVisible():
            self._dirty = True
            return
        self.refresh()

    def _on_db_switched(self, _db_path: str) -> None:
        self._search_timer.stop()
        self._search_input.blockSignals(True)
        self._search_input.clear()
        self._search_input.blockSignals(False)
        self._model.set_search_query("")
        if not self.isVisible():
            self._dirty = True
            return
        self.refresh()

    def _on_settings_changed(self) -> None:
        self._sync_search_placeholder()
        self._update_header_metrics()

    def _sync_search_placeholder(self) -> None:
        if getattr(self.ctx.config, "show_advanced_fields", False):
            self._search_input.setPlaceholderText("筛选重复组（编码、文件名、标签或来源...）")
        else:
            self._search_input.setPlaceholderText("筛选重复组（编码、文件名或来源...）")

    def apply_theme(self, theme: str) -> None:
        norm = "dark" if theme == "dark" else "light"
        p = get_palette(norm)
        self._model.set_theme(norm)
        self._delegate.set_theme(norm)
        self._empty_state.apply_theme(norm)
        self._search_action.setIcon(get_icon("search", 15, p["icon_muted"], theme=norm))
        self._smart_btn.setIcon(get_icon("sparkles", 15, p["icon_default"], theme=norm))
        self._more_btn.setIcon(get_icon("more-horizontal", 16, p["icon_default"], theme=norm))
        self._btn_delete.setIcon(
            get_icon("trash", 15, "#FFFFFF", theme=norm, disabled_color=p["btn_disabled_text"])
        )

    def _is_worker_running(self, worker: Optional[WorkerThread]) -> bool:
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            return False

    # ------------------------------------------------------------------
    # Data refresh, view mode, search & empty states
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        self._dirty = False
        try:
            self._groups = self.ctx.db.get_duplicate_groups(
                include_multipart=self._act_include_multipart.isChecked()
            )
        except (DatabaseError, OSError, Exception) as exc:
            ScrollableDetailDialog("加载重复项失败", "读取重复记录组时发生错误：", str(exc), parent=self).exec()
            return

        self._model.set_groups(self._groups, preserve_checks=False)
        query = self._search_input.text().strip()
        if query:
            self._model.set_search_query(query)
        self._header_manager.reapply_sort()
        self._copy_helper.update_split_buttons()
        self._update_header_metrics()
        self._update_empty_state()
        self._update_checked_summary()

    def _apply_search_filter(self) -> None:
        self._model.set_search_query(self._search_input.text().strip())
        self._copy_helper.update_split_buttons()
        self._update_header_metrics()
        self._update_empty_state()

    def _set_view_mode(self, mode: str) -> None:
        normalized = "grouped" if mode == "grouped" else "flat"
        self._btn_mode_flat.setChecked(normalized == "flat")
        self._btn_mode_grouped.setChecked(normalized == "grouped")
        self._model.set_view_mode(normalized)
        self._copy_helper.update_split_buttons()
        if getattr(self.ctx.config, "duplicates_view_mode", "flat") != normalized:
            self.ctx.config.duplicates_view_mode = normalized
            try:
                self.ctx.config_manager.save()
            except OSError:
                pass

    def _update_header_metrics(self) -> None:
        total_groups = len(self._groups)
        total_recs = self._model.total_record_count()
        query = self._search_input.text().strip()
        if query and total_groups > 0:
            self._header.set_metrics(f"筛选命中 {self._model.visible_group_count():,} / {total_groups:,} 组重复")
            return
        if total_groups == 0:
            self._header.set_metrics("0 组重复")
            return

        parts = [f"{total_groups:,} 组重复", f"涉及 {total_recs:,} 条记录"]
        if getattr(self.ctx.config, "show_advanced_fields", False):
            mp_count = sum(1 for g in self._groups if g.is_possible_multipart)
            if mp_count > 0:
                parts.append(f"疑似分集 {mp_count:,} 组")
        self._header.set_metrics(parts)

    def _update_empty_state(self) -> None:
        if len(self._groups) == 0:
            if self._empty_mode != "no_dups":
                self._empty_mode = "no_dups"
                self._empty_state.set_content(
                    title="当前数据库没有重复记录",
                    subtitle="所有已入库标准编码均保持唯一，无需清理。",
                    icon_name="check-circle",
                    icon_tone="success",
                )
                self._empty_state.clear_actions()
                self._empty_state.add_action("返回数据总库", self._go_to_library, variant="primary", icon_name="database")
            self._body_stack.setCurrentIndex(1)
            return

        if self._model.rowCount() == 0:
            if self._empty_mode != "no_match":
                self._empty_mode = "no_match"
                self._empty_state.set_content(
                    title="没有匹配的重复记录",
                    subtitle="未找到符合当前搜索条件的重复组，请尝试调整关键词。",
                    icon_name="search",
                    icon_tone="default",
                )
                self._empty_state.clear_actions()
                self._empty_state.add_action("清除筛选", self._clear_search, variant="primary", icon_name="x")
            self._body_stack.setCurrentIndex(1)
            return

        self._empty_mode = ""
        self._body_stack.setCurrentIndex(0)

    def _go_to_library(self) -> None:
        win = _find_main_window(self)
        if win is not None:
            win.switch_view(0)

    def _clear_search(self) -> None:
        self._search_input.clear()
        self._apply_search_filter()

    # ------------------------------------------------------------------
    # Copy extractors, inline copy & table interaction
    # ------------------------------------------------------------------

    def _extract_row_code(self, row: int) -> str:
        grp = self._model.group_at(row)
        return grp.canonical_code if grp is not None else ""

    def _extract_row_name(self, row: int) -> str:
        rec = self._model.record_at(row)
        return (rec.file_name or rec.raw_text) if rec is not None else ""

    def _extract_row_path(self, row: int) -> str:
        rec = self._model.record_at(row)
        return rec.full_path if rec is not None else ""

    def _on_inline_code_copied(self, code: str) -> None:
        self.ctx.notify(f"已复制编码：{code}", "success")

    def _on_table_pressed(self, index: QModelIndex) -> None:
        self._pressed_check_state = None
        if (
            index.isValid()
            and index.column() == DuplicateTableModel.COL_CHECK
            and not self._model.is_group_row(index.row())
        ):
            rec = self._model.record_at(index.row())
            if rec is not None and rec.id is not None:
                self._pressed_check_state = (rec.id, self._model.is_record_checked(rec.id))

    def _on_table_clicked(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        row = index.row()
        if self._model.is_group_row(row):
            self._model.toggle_row_group_collapsed(row)
            return
        if index.column() == DuplicateTableModel.COL_CHECK:
            rec = self._model.record_at(row)
            if (
                rec is not None
                and rec.id is not None
                and self._pressed_check_state is not None
                and self._pressed_check_state[0] == rec.id
                and self._model.is_record_checked(rec.id) == self._pressed_check_state[1]
            ):
                self._model.set_record_checked(rec.id, not self._pressed_check_state[1])
            self._pressed_check_state = None

    def _toggle_selected_checks(self) -> None:
        rows = self._copy_helper.selected_rows()
        if not rows:
            return
        if len(rows) == 1 and self._model.is_group_row(rows[0]):
            self._model.toggle_row_group_collapsed(rows[0])
            return
        self._model.toggle_rows_checked(rows)

    # ------------------------------------------------------------------
    # Menus: More menu & Row right-click context menu
    # ------------------------------------------------------------------

    def _open_more_menu(self) -> None:
        menu = QMenu(self)
        if self._model.view_mode() == "grouped":
            act_exp = menu.addAction("全部展开", self._model.expand_all_groups)
            act_exp.setEnabled(bool(self._groups))
            act_col = menu.addAction("全部折叠", self._model.collapse_all_groups)
            act_col.setEnabled(bool(self._groups))
            menu.addSeparator()

        menu.addAction("自定义显示列...", self._open_column_settings)
        menu.addAction("自动调整列宽", self._header_manager.auto_fit_all)
        menu.addAction("恢复默认列", self._header_manager.reset_to_defaults)
        menu.addSeparator()

        can_export = bool(self._groups) and not self._is_worker_running(self._export_worker)
        act_xlsx = menu.addAction("导出重复分组 (Excel)", lambda: self._export_groups("xlsx"))
        act_xlsx.setEnabled(can_export)
        act_csv = menu.addAction("导出重复分组 (CSV)", lambda: self._export_groups("csv"))
        act_csv.setEnabled(can_export)
        menu.addSeparator()
        menu.addAction("刷新", self.refresh)

        menu.exec(self._more_btn.mapToGlobal(QPoint(0, self._more_btn.height() + 4)))

    def _open_column_settings(self) -> None:
        self._header_manager.open_column_menu(
            self._more_btn.mapToGlobal(QPoint(0, self._more_btn.height() + 4))
        )

    def _show_context_menu(self, pos: QPoint) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        clicked_row = index.row()
        selected_rows = self._copy_helper.selected_rows()
        if clicked_row not in selected_rows:
            self._table.selectRow(clicked_row)
            selected_rows = [clicked_row]

        menu = QMenu(self)
        self._copy_helper.populate_context_menu(menu, rows=selected_rows)

        if len(self._copy_helper.extract_codes(selected_rows, deduplicate=True)) == 1:
            act_space = QAction("复制为多词检索（空格分隔）", menu)
            act_space.triggered.connect(
                lambda _=False, r=list(selected_rows): self._copy_helper.copy_codes("space", rows=r)
            )
            acts = menu.actions()
            if len(acts) > 1:
                menu.insertAction(acts[1], act_space)
            else:
                menu.addAction(act_space)

        rec = self._model.record_at(clicked_row)
        grp = self._model.group_at(clicked_row)
        if len(selected_rows) == 1 and rec is not None and rec.id is not None:
            menu.addSeparator()
            menu.addAction("仅保留此条，勾选本组其余副本", lambda _=False, t=rec: self._keep_only_record(t))
            is_checked = self._model.is_record_checked(rec.id)
            menu.addAction(
                "取消勾选此项" if is_checked else "勾选此项待删除",
                lambda _=False, rid=rec.id, chk=not is_checked: self._model.set_record_checked(rid, chk),
            )
            if rec.full_path:
                menu.addAction(
                    "打开所在目录",
                    lambda _=False, fp=rec.full_path: open_local_folder_safely(fp, self.ctx),
                )
        elif len(selected_rows) == 1 and self._model.is_group_row(clicked_row) and grp is not None:
            menu.addSeparator()
            collapsed = self._model.is_row_collapsed(clicked_row)
            menu.addAction(
                "展开该分组" if collapsed else "折叠该分组",
                lambda _=False, code=grp.canonical_code: self._model.toggle_group_collapsed(code),
            )
        else:
            record_rows = [r for r in selected_rows if not self._model.is_group_row(r)]
            if record_rows:
                menu.addSeparator()
                menu.addAction(
                    f"勾选 / 取消勾选已选行 ({len(record_rows)} 条)",
                    lambda _=False, rows=list(record_rows): self._model.toggle_rows_checked(rows),
                )

        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _keep_only_record(self, rec: DataRecord) -> None:
        if rec.id is None:
            return
        checked_siblings = self._model.check_all_except_one(rec.id)
        self.ctx.notify(f"已保留当前记录，勾选本组其余 {checked_siblings} 条待清理副本", "info")

    # ------------------------------------------------------------------
    # Smart selection & check state summary
    # ------------------------------------------------------------------

    def _apply_smart_plan(self, strategy: str) -> None:
        if not self._groups:
            self.ctx.notify("当前没有重复记录组", "info")
            return
        plan = plan_duplicate_cleanup(
            self._groups,
            strategy=strategy,
            protect_multipart=self._act_protect_multipart.isChecked(),
        )
        self._model.set_checked_ids(plan.delete_ids)
        extra = f"，已保护 {len(plan.protected_groups)} 组疑似分集" if plan.protected_groups else ""
        self.ctx.notify(f"智能勾选完成：待删除 {plan.delete_count} 条，保留 {plan.keep_count} 条{extra}", "info")

    def _clear_checks(self) -> None:
        self._model.clear_checks()

    def _on_checked_changed(self, _count: int) -> None:
        self._update_checked_summary()

    def _update_checked_summary(self) -> None:
        count = self._model.checked_count()
        busy = self._is_worker_running(self._cleanup_worker)
        self._btn_delete.setText(f"删除已勾选 ({count})")
        self._btn_delete.setEnabled(count > 0 and not busy)
        self._btn_clear_checks.setVisible(count > 0 and not busy)

        if busy:
            self._whole_group_badge.setVisible(False)
            return

        if count == 0:
            self._checked_label.setText("右键表头自定义列与顺序 · 空格勾选 · 删除前自动备份 .bak")
            self._checked_label.setProperty("role", "muted")
            self._shortcut_hint.setText("Ctrl+C 复制编码 · Ctrl+Shift+C 复制文件名")
            self._whole_group_badge.setVisible(False)
        else:
            self._checked_label.setText(f"已勾选 {count:,} 条待删除记录")
            self._checked_label.setProperty("role", "subtitle")
            self._shortcut_hint.setText("删除前自动创建 .bak 备份")
            fully_checked = self._model.fully_checked_groups()
            if fully_checked:
                self._whole_group_badge.setText(f"注意：{len(fully_checked)} 组将被整组清空")
                self._whole_group_badge.setVisible(True)
            else:
                self._whole_group_badge.setVisible(False)

        st = self._checked_label.style()
        if st is not None:
            st.unpolish(self._checked_label)
            st.polish(self._checked_label)

    # ------------------------------------------------------------------
    # Safe threaded cleanup & export
    # ------------------------------------------------------------------

    def _confirm_and_delete(self) -> None:
        if self._is_worker_running(self._cleanup_worker):
            self.ctx.notify("正在执行清理任务，请稍候...", "warning")
            return
        ids = self._model.checked_ids()
        if not ids:
            return

        fully_checked = self._model.fully_checked_groups()
        lines: list[str] = []
        if fully_checked:
            lines.append("====== 警告：以下编码组的所有副本均已勾选（执行后该编码将在库中彻底消失）======")
            for code in fully_checked[:50]:
                lines.append(f"  [整组清空] {code}")
            if len(fully_checked) > 50:
                lines.append(f"  ... 共 {len(fully_checked)} 组被整组勾选")
            lines.append("")

        for rec_id in ids[:150]:
            rec = self._model.record_by_id(rec_id)
            if rec is not None:
                part = f" [{rec.part}]" if rec.part else ""
                lines.append(f"[{rec.canonical_code}]{part}  {rec.file_name or rec.raw_text}")
        if len(ids) > 150:
            lines.append(f"... 共 {len(ids)} 条记录")

        if fully_checked:
            title = "危险操作警告：确认删除（含整组清空项）"
            summary = (
                f"数据丢失警告：您勾选了 {len(fully_checked)} 个重复组的全部记录"
                f"（{', '.join(fully_checked[:5])}{' 等' if len(fully_checked) > 5 else ''}），"
                f"删除后这些编码将不保留任何副本。\n"
                f"本次共删除 {len(ids)} 条记录（执行前自动创建 .bak 备份，不删除硬盘上的实际文件），确定继续吗？"
            )
        else:
            title = "确认清理重复记录"
            summary = (
                f"即将从当前库删除以下 {len(ids)} 条重复记录"
                f"（执行前自动创建 .bak 备份，不删除硬盘上的实际文件）："
            )

        dlg = ScrollableDetailDialog(
            title=title,
            summary=summary,
            detail_text="\n".join(lines),
            confirm_mode=True,
            confirm_label=f"确认删除 ({len(ids)} 条)",
            danger=True,
            parent=self,
        )
        if dlg.exec() != ScrollableDetailDialog.DialogCode.Accepted:
            return

        self._btn_delete.setEnabled(False)
        self._table.setEnabled(False)
        self._checked_label.setText(f"正在后台安全清理 {len(ids)} 条重复记录（含自动备份）...")
        self._whole_group_badge.setVisible(False)
        self._btn_clear_checks.setVisible(False)

        worker = WorkerThread(lambda: execute_duplicate_cleanup(self.ctx.db, ids, create_backup=True), parent=self)
        self._cleanup_worker = worker
        worker.succeeded.connect(self._on_cleanup_succeeded)
        worker.failed.connect(self._on_cleanup_failed)
        worker.finished.connect(lambda w=worker: self._on_cleanup_finished(w))
        worker.start()

    def _on_cleanup_succeeded(self, deleted: int) -> None:
        self._table.setEnabled(True)
        self.ctx.notify(f"已安全清理 {deleted} 条重复记录（已自动创建备份）", "success")
        self.ctx.data_changed.emit()

    def _on_cleanup_failed(self, detail: str) -> None:
        self._table.setEnabled(True)
        self._update_checked_summary()
        ScrollableDetailDialog("清理重复记录失败", "执行数据库删除时发生错误：", detail, parent=self).exec()

    def _on_cleanup_finished(self, worker: WorkerThread) -> None:
        if self._cleanup_worker is worker:
            self._cleanup_worker = None
        self._table.setEnabled(True)
        self._update_checked_summary()

    def _export_groups(self, fmt: str = "xlsx") -> None:
        if self._is_worker_running(self._export_worker):
            self.ctx.notify("正在导出重复项报告，请稍候...", "warning")
            return
        if not self._groups:
            self.ctx.notify("当前没有可导出的重复项", "warning")
            return

        default_dir = Path(self.ctx.config.default_export_dir or Path.home())
        is_csv = fmt == "csv"
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出重复项报告",
            str(default_dir / ("重复项汇总报告.csv" if is_csv else "重复项汇总报告.xlsx")),
            "CSV 表格 (*.csv)" if is_csv else "Excel 工作簿 (*.xlsx)",
        )
        if not save_path:
            return

        try:
            self.ctx.config.default_export_dir = str(Path(save_path).parent)
            self.ctx.config_manager.save()
        except OSError:
            pass

        groups_snapshot = list(self._groups)
        self.ctx.notify(f"正在后台导出 {Path(save_path).name}...", "info")

        def _task() -> str:
            if is_csv:
                export_duplicate_groups_csv(groups_snapshot, save_path)
            else:
                export_duplicate_groups_excel(groups_snapshot, save_path)
            return save_path

        worker = WorkerThread(_task, parent=self)
        self._export_worker = worker
        worker.succeeded.connect(self._on_export_succeeded)
        worker.failed.connect(self._on_export_failed)
        worker.finished.connect(lambda w=worker: self._on_export_finished(w))
        worker.start()

    def _on_export_succeeded(self, save_path: str) -> None:
        self.ctx.notify(f"已导出重复项报告：{Path(save_path).name}", "success")

    def _on_export_failed(self, detail: str) -> None:
        ScrollableDetailDialog("导出失败", "导出重复项报告时发生错误：", detail, parent=self).exec()

    def _on_export_finished(self, worker: WorkerThread) -> None:
        if self._export_worker is worker:
            self._export_worker = None
