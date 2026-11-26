"""Left-panel source table management mixin for TablesView (数据源表格管理)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QItemSelectionModel, QSize, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.core.table_data import DataTable
from src.ui.tables_widgets import (
    _FILE_PICKER_FILTER,
    _BadgeIconCache,
    _CompactTableDropZone,
    _DropAwareListWidget,
)
from src.ui.widgets import ScrollableDetailDialog


class TablesSourcePanelMixin:
    """Mixin implementing the left-side Data Source Table Manager panel for TablesView."""

    def _build_left_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("TablesSourcePanel")
        panel.setProperty("card", True)
        panel.setMinimumWidth(236)
        panel.setMaximumWidth(360)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(7)

        hdr_row = QHBoxLayout()
        hdr_lbl = QLabel("数据源表格管理")
        hdr_lbl.setProperty("role", "section")
        hdr_row.addWidget(hdr_lbl)
        hdr_row.addStretch()
        self._table_count_lbl = QLabel("0 个表")
        self._table_count_lbl.setProperty("role", "muted")
        hdr_row.addWidget(self._table_count_lbl)
        layout.addLayout(hdr_row)

        self._drop_zone = _CompactTableDropZone(self)
        self._drop_zone.files_dropped.connect(self.load_files)
        self._drop_zone.clicked.connect(self._pick_files)
        layout.addWidget(self._drop_zone)

        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        self._btn_add_files = QPushButton("＋ 添加表格 / 文档")
        self._btn_add_files.setToolTip("支持多选 Excel (.xlsx/.xlsm)、Word (.docx)、CSV、TXT 文件")
        self._btn_add_files.setProperty("variant", "primary")
        self._btn_add_files.setProperty("compact", True)
        self._btn_add_files.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_add_files.clicked.connect(self._pick_files)
        add_row.addWidget(self._btn_add_files, 1)

        self._btn_add_with_opts = QPushButton("⚙")
        self._btn_add_with_opts.setProperty("compact", True)
        self._btn_add_with_opts.setFixedWidth(30)
        self._btn_add_with_opts.setToolTip("带表头行/工作表选项导入文件")
        self._btn_add_with_opts.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_add_with_opts.clicked.connect(lambda: self._pick_files(force_options=True))
        add_row.addWidget(self._btn_add_with_opts)
        layout.addLayout(add_row)

        self._btn_load_db = QPushButton("载入当前数据总库")
        self._btn_load_db.setProperty("compact", True)
        self._btn_load_db.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_load_db.clicked.connect(self.load_database_table)
        layout.addWidget(self._btn_load_db)

        prog_row = QHBoxLayout()
        prog_row.setSpacing(6)
        self._left_progress = QProgressBar()
        self._left_progress.setFixedHeight(12)
        self._left_progress.setTextVisible(False)
        self._left_progress.setVisible(False)
        prog_row.addWidget(self._left_progress, 1)

        self._btn_cancel_left = QPushButton("取消")
        self._btn_cancel_left.setProperty("compact", True)
        self._btn_cancel_left.setProperty("variant", "danger")
        self._btn_cancel_left.setVisible(False)
        self._btn_cancel_left.clicked.connect(self._cancel_left_workers)
        prog_row.addWidget(self._btn_cancel_left)
        layout.addLayout(prog_row)

        row1 = QHBoxLayout()
        row1.setSpacing(5)
        self._btn_sel_all = QPushButton("全选")
        self._btn_sel_all.setProperty("compact", True)
        self._btn_sel_all.clicked.connect(self._select_all_tables)
        self._btn_invert = QPushButton("反选")
        self._btn_invert.setProperty("compact", True)
        self._btn_invert.clicked.connect(self._invert_table_checks)
        self._btn_rename = QPushButton("重命名")
        self._btn_rename.setProperty("compact", True)
        self._btn_rename.clicked.connect(self._rename_selected_table)
        row1.addWidget(self._btn_sel_all)
        row1.addWidget(self._btn_invert)
        row1.addWidget(self._btn_rename)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(5)
        self._btn_move_up = QPushButton("▲")
        self._btn_move_up.setProperty("compact", True)
        self._btn_move_up.setFixedWidth(30)
        self._btn_move_up.setToolTip("上移选中表格（影响合并保留首条/末条与按位置对齐顺序）")
        self._btn_move_up.clicked.connect(lambda: self._move_selected_table(-1))
        self._btn_move_down = QPushButton("▼")
        self._btn_move_down.setProperty("compact", True)
        self._btn_move_down.setFixedWidth(30)
        self._btn_move_down.setToolTip("下移选中表格")
        self._btn_move_down.clicked.connect(lambda: self._move_selected_table(1))
        self._btn_remove = QPushButton("移除选中")
        self._btn_remove.setProperty("compact", True)
        self._btn_remove.setToolTip("支持 Ctrl/Shift 多选后批量移除")
        self._btn_remove.clicked.connect(self._remove_selected_table)
        self._btn_clear = QPushButton("清空")
        self._btn_clear.setProperty("variant", "ghost")
        self._btn_clear.setProperty("compact", True)
        self._btn_clear.clicked.connect(self._clear_all_tables)
        row2.addWidget(self._btn_move_up)
        row2.addWidget(self._btn_move_down)
        row2.addWidget(self._btn_remove, 1)
        row2.addWidget(self._btn_clear)
        layout.addLayout(row2)

        self._table_list = _DropAwareListWidget()
        self._table_list.setObjectName("TablesSourceList")
        self._table_list.setIconSize(QSize(44, 20))
        self._table_list.setSpacing(3)
        self._table_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table_list.files_dropped.connect(self.load_files)
        self._table_list.customContextMenuRequested.connect(self._show_list_context_menu)
        self._table_list.currentItemChanged.connect(self._on_list_selection_changed)
        self._table_list.itemChanged.connect(self._on_list_item_checked)
        layout.addWidget(self._table_list, 1)

        self._checked_hint_lbl = QLabel("勾选参与查重或合并，支持 Ctrl/Shift 多选移除与 ▲▼ 调序")
        self._checked_hint_lbl.setProperty("role", "muted")
        self._checked_hint_lbl.setWordWrap(True)
        layout.addWidget(self._checked_hint_lbl)

        self._btn_pack_xlsx = QPushButton("📦 打包多Sheet导出")
        self._btn_pack_xlsx.setToolTip("将已勾选表格打包导出为一个多 Sheet Excel 工作簿 (.xlsx)")
        self._btn_pack_xlsx.setProperty("compact", True)
        self._btn_pack_xlsx.setEnabled(False)
        self._btn_pack_xlsx.clicked.connect(self._export_multi_sheet_workbook)
        layout.addWidget(self._btn_pack_xlsx)

        return panel

    def _create_list_item(self, tbl: DataTable) -> QListWidgetItem:
        item = QListWidgetItem()
        item.setFlags(
            item.flags()
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsEnabled
        )
        item.setCheckState(Qt.CheckState.Checked)
        item.setData(Qt.ItemDataRole.UserRole, tbl.table_id)
        item.setIcon(_BadgeIconCache.get(tbl.source_type))
        self._format_list_item(item, tbl)
        return item

    def _format_list_item(self, item: QListWidgetItem, tbl: DataTable) -> None:
        item.setText(f"{tbl.name}\n{tbl.row_count:,} 行 × {tbl.col_count} 列")
        tooltip_parts = [
            f"表名：{tbl.name}",
            f"维度：{tbl.row_count:,} 行 × {tbl.col_count} 列",
            f"类型：{tbl.source_type}",
        ]
        if tbl.source_path:
            tooltip_parts.append(f"来源路径：{tbl.source_path}")
        if tbl.sheet_name:
            tooltip_parts.append(f"工作表/子表：{tbl.sheet_name}")
        item.setToolTip("\n".join(tooltip_parts))

    def _pick_files(self, force_options: bool = False) -> None:
        start_dir = self.ctx.config.default_import_dir or str(Path.home())
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "添加表格或文档文件（支持多选）",
            start_dir,
            _FILE_PICKER_FILTER,
        )
        if paths:
            self.ctx.config.default_import_dir = str(Path(paths[0]).parent)
            self.ctx.config_manager.save()
            self.load_files(paths, interactive=True, force_options_dialog=force_options)

    def _cancel_left_workers(self) -> None:
        self._inspect_gen += 1
        self._load_gen += 1
        self._db_load_gen += 1
        for attr in ("_inspect_worker", "_load_worker", "_db_load_worker"):
            worker = getattr(self, attr, None)
            if worker is not None:
                try:
                    worker.cancel()
                except RuntimeError:
                    pass
        self._left_progress.setVisible(False)
        self._btn_cancel_left.setVisible(False)
        self._sync_controls_state()
        self.ctx.notify("已请求取消加载任务", "info")

    def _on_left_cancelled(self, gen: int, *, is_inspect: bool = False, is_db: bool = False) -> None:
        expected = self._inspect_gen if is_inspect else (self._db_load_gen if is_db else self._load_gen)
        if gen != expected:
            return
        self._left_progress.setVisible(False)
        self._btn_cancel_left.setVisible(False)
        self._sync_controls_state()

    def _on_files_loaded(self, gen: int, payload: tuple[list[DataTable], list[str]]) -> None:
        if gen != self._load_gen:
            return
        loaded, errors = payload
        self._left_progress.setVisible(False)
        self._btn_cancel_left.setVisible(False)

        if loaded:
            self.add_tables(loaded, select_first=True)
            total_rows = sum(t.row_count for t in loaded)
            uncached_total = sum(int(t.metadata.get("uncached_formula_count", 0) or 0) for t in loaded)
            msg = f"成功载入 {len(loaded)} 张表（共 {total_rows:,} 行）"
            if uncached_total > 0:
                msg += f"，注意：检测到 {uncached_total} 个未缓存公式单元"
            self.ctx.notify(msg, "warning" if uncached_total > 0 else "success")
        if errors:
            ScrollableDetailDialog(
                "部分文件未能加载",
                f"共有 {len(errors)} 个文件加载失败或为空：",
                "\n".join(errors),
                parent=self,
            ).exec()
        self._sync_controls_state()

    def _on_load_failed(self, gen: int, detail: str, *, is_inspect: bool = False) -> None:
        expected = self._inspect_gen if is_inspect else self._load_gen
        if gen != expected:
            return
        self._left_progress.setVisible(False)
        self._btn_cancel_left.setVisible(False)
        self._sync_controls_state()
        ScrollableDetailDialog("加载文件出错", "后台读取表格文件时发生异常：", detail, parent=self).exec()

    def _select_table_by_id(self, table_id: str) -> None:
        """Select a table in the left list without triggering duplicate preview model builds."""
        for idx in range(self._table_list.count()):
            item = self._table_list.item(idx)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == table_id:
                if self._table_list.currentRow() == idx:
                    self._table_list.setCurrentRow(idx, QItemSelectionModel.SelectionFlag.ClearAndSelect)
                    if self._active_table_id != table_id:
                        self._show_preview_for_table(table_id)
                else:
                    # setCurrentRow emits currentItemChanged -> _on_list_selection_changed -> _show_preview_for_table
                    self._table_list.setCurrentRow(idx, QItemSelectionModel.SelectionFlag.ClearAndSelect)
                return

    def _on_list_selection_changed(
        self,
        current: Optional[QListWidgetItem],
        _previous: Optional[QListWidgetItem],
    ) -> None:
        if current is None:
            self._active_table_id = None
            self._show_preview_for_table(None)
            return
        tid = current.data(Qt.ItemDataRole.UserRole)
        self._show_preview_for_table(tid)

    def _on_list_item_checked(self, _item: QListWidgetItem) -> None:
        self._update_table_count_ui()
        self._refresh_column_and_table_selectors()
        self._sync_controls_state()

    def _select_all_tables(self) -> None:
        self._table_list.blockSignals(True)
        for idx in range(self._table_list.count()):
            item = self._table_list.item(idx)
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked)
        self._table_list.blockSignals(False)
        self._update_table_count_ui()
        self._refresh_column_and_table_selectors()
        self._sync_controls_state()

    def _invert_table_checks(self) -> None:
        self._table_list.blockSignals(True)
        for idx in range(self._table_list.count()):
            item = self._table_list.item(idx)
            if item is not None:
                nxt = (
                    Qt.CheckState.Unchecked
                    if item.checkState() == Qt.CheckState.Checked
                    else Qt.CheckState.Checked
                )
                item.setCheckState(nxt)
        self._table_list.blockSignals(False)
        self._update_table_count_ui()
        self._refresh_column_and_table_selectors()
        self._sync_controls_state()

    def _move_selected_table(self, delta: int) -> None:
        """Move currently selected table up (delta=-1) or down (delta=+1)."""
        if self._any_mutation_lock_active():
            return
        row = self._table_list.currentRow()
        count = self._table_list.count()
        if row < 0 or count < 2:
            return
        target_row = row + delta
        if not (0 <= target_row < count):
            return

        self._table_list.blockSignals(True)
        item = self._table_list.takeItem(row)
        if item is None:
            self._table_list.blockSignals(False)
            return
        self._table_list.insertItem(target_row, item)
        self._table_list.setCurrentRow(target_row, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self._table_list.blockSignals(False)

        # Keep self._tables in exact sync with QListWidget order
        ordered: list[DataTable] = []
        for idx in range(self._table_list.count()):
            it = self._table_list.item(idx)
            if it is not None:
                tid = it.data(Qt.ItemDataRole.UserRole)
                tbl = self._tables_by_id.get(tid)
                if tbl is not None:
                    ordered.append(tbl)
        self._tables = ordered
        self._refresh_column_and_table_selectors()
        self._sync_controls_state()

    def _rename_selected_table(self) -> None:
        if self._any_mutation_lock_active():
            self.ctx.notify("后台任务运行中，请稍候再重命名", "warning")
            return
        item = self._table_list.currentItem()
        if item is None:
            self.ctx.notify("请先在左侧点击选择一张表格", "warning")
            return
        tid = item.data(Qt.ItemDataRole.UserRole)
        tbl = self._tables_by_id.get(tid)
        if tbl is None:
            return
        new_name, ok = QInputDialog.getText(self, "重命名表格", "请输入新的表格名称：", text=tbl.name)
        cleaned = new_name.strip()
        if not ok or not cleaned or cleaned == tbl.name:
            return

        # Enforce unique table names across workspace (matching add_tables)
        other_names = {t.name for t in self._tables if t.table_id != tid}
        candidate = cleaned
        suffix = 2
        while candidate in other_names:
            candidate = f"{cleaned} ({suffix})"
            suffix += 1

        tbl.rename(candidate)
        self._table_list.blockSignals(True)
        self._format_list_item(item, tbl)
        self._table_list.blockSignals(False)
        if self._active_table_id == tid:
            self._preview_title.setText(self._format_preview_title(tbl))
        self._invalidate_stale_results({tid})
        self._refresh_column_and_table_selectors()
        self._sync_controls_state()
        self.ctx.notify(f"已重命名为「{candidate}」", "success")

    def _remove_selected_table(self) -> None:
        if self._any_mutation_lock_active():
            self.ctx.notify("后台任务运行中，请先取消后再移除表格", "warning")
            return
        selected_items = self._table_list.selectedItems()
        if not selected_items:
            cur = self._table_list.currentItem()
            if cur is not None:
                selected_items = [cur]
        if not selected_items:
            self.ctx.notify("请先选择要移除的表格", "warning")
            return

        rows_to_remove = sorted(
            (self._table_list.row(it) for it in selected_items),
            reverse=True,
        )
        removed_ids: set[str] = set()
        self._table_list.blockSignals(True)
        for row in rows_to_remove:
            item = self._table_list.takeItem(row)
            if item is None:
                continue
            tid = item.data(Qt.ItemDataRole.UserRole)
            removed_ids.add(tid)
            self._tables_by_id.pop(tid, None)
            self._dedup_custom_keys.pop(tid, None)
            self._merge_custom_keys.pop(tid, None)
        self._table_list.blockSignals(False)

        self._tables = [t for t in self._tables if t.table_id not in removed_ids]
        self._invalidate_stale_results(removed_ids)

        if self._active_table_id in removed_ids or self._active_table_id is None:
            self._active_table_id = None
            nxt = self._table_list.currentItem()
            if nxt is None and self._table_list.count() > 0:
                self._table_list.blockSignals(True)
                self._table_list.setCurrentRow(0, QItemSelectionModel.SelectionFlag.ClearAndSelect)
                self._table_list.blockSignals(False)
                nxt = self._table_list.item(0)
            self._show_preview_for_table(nxt.data(Qt.ItemDataRole.UserRole) if nxt else None)

        self._update_db_stale_hint()
        self._update_table_count_ui()
        self._refresh_column_and_table_selectors()
        self._sync_controls_state()
        if len(removed_ids) > 1:
            self.ctx.notify(f"已批量移除 {len(removed_ids)} 张表格", "info")

    def _clear_all_tables(self) -> None:
        if not self._tables:
            return
        if self._any_mutation_lock_active():
            self.ctx.notify("后台任务运行中，请先取消后再清空工作台", "warning")
            return

        # Bump run generation tokens to reject any stray late completions
        self._dedup_gen += 1
        self._merge_gen += 1

        self._table_list.blockSignals(True)
        self._table_list.clear()
        self._table_list.blockSignals(False)
        self._tables.clear()
        self._tables_by_id.clear()
        self._dedup_custom_keys.clear()
        self._merge_custom_keys.clear()
        self._active_table_id = None
        self._db_snapshot_stale = False

        self._invalidate_stale_results(None)
        for model in self._all_models:
            model.clear()

        self._show_preview_for_table(None)
        self._update_db_stale_hint()
        self._update_table_count_ui()
        self._refresh_column_and_table_selectors()
        self._sync_controls_state()
        self.ctx.notify("已清空工作台全部表格", "info")

    def _show_list_context_menu(self, pos: Any) -> None:
        item = self._table_list.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():
            self._table_list.setCurrentItem(item)
        tid = item.data(Qt.ItemDataRole.UserRole)
        tbl = self._tables_by_id.get(tid)
        if tbl is None:
            return

        locked = self._any_mutation_lock_active()
        sel_count = len(self._table_list.selectedItems())

        menu = QMenu(self)
        menu.addAction("预览此表", lambda: (self._select_table_by_id(tid), self.switch_sub_tab(0)))

        act_single = menu.addAction("单表去重分析", lambda: self._run_single_table_dedup(tbl))
        act_single.setEnabled(not self._is_worker_running(self._dedup_worker))

        act_cols = menu.addAction("列管理（重命名/删列/统计）...", lambda: self._open_column_manager(tbl))
        act_cols.setEnabled(not locked)

        act_ren = menu.addAction("重命名...", self._rename_selected_table)
        act_ren.setEnabled(not locked)

        menu.addSeparator()
        act_up = menu.addAction("上移", lambda: self._move_selected_table(-1))
        act_up.setEnabled(not locked)
        act_dn = menu.addAction("下移", lambda: self._move_selected_table(1))
        act_dn.setEnabled(not locked)

        menu.addSeparator()
        exp_locked = self._is_worker_running(self._export_worker)
        for label, fmt in (
            ("导出为 Excel (.xlsx)", "xlsx"),
            ("导出为 Word (.docx)", "docx"),
            ("导出为 CSV", "csv"),
        ):
            act = menu.addAction(label, lambda f=fmt, t=tbl: self._export_single_table(t, f))
            act.setEnabled(not exp_locked)

        menu.addSeparator()
        rm_label = f"移除选中 ({sel_count} 张表)" if sel_count > 1 else "移除此表"
        act_rm = menu.addAction(rm_label, self._remove_selected_table)
        act_rm.setEnabled(not locked)

        menu.exec(self._table_list.viewport().mapToGlobal(pos))

    def _update_table_count_ui(self) -> None:
        total = len(self._tables)
        checked = len(self.checked_tables())
        self._table_count_lbl.setText(f"{checked}/{total} 已选")
        self._dedup_scope_lbl.setText(f"已勾选 {checked} 张表参与")

    def _refresh_column_and_table_selectors(self) -> None:
        tables = self.checked_tables()

        header_counts: dict[str, int] = {}
        ordered_headers: list[str] = []
        for t in tables:
            seen_in_t: set[str] = set()
            for h in t.headers:
                if h not in seen_in_t:
                    seen_in_t.add(h)
                    header_counts[h] = header_counts.get(h, 0) + 1
                    if h not in ordered_headers:
                        ordered_headers.append(h)

        n_tables = len(tables)
        common_headers = [h for h in ordered_headers if n_tables > 0 and header_counts.get(h, 0) == n_tables]
        other_headers = [h for h in ordered_headers if h not in common_headers]

        for combo in (self._dedup_col_combo, self._merge_col_combo):
            prev_val = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for h in common_headers:
                label = f"{h} (全表共有)" if n_tables > 1 else h
                combo.addItem(label, h)
            for h in other_headers:
                combo.addItem(f"{h} ({header_counts[h]}/{n_tables}表)", h)
            if combo.count() == 0:
                combo.addItem("（无可用列）", "")
            idx = combo.findData(prev_val) if prev_val else -1
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

        prev_primary = self._merge_primary_combo.currentData()
        self._merge_primary_combo.blockSignals(True)
        self._merge_primary_combo.clear()
        for t in tables:
            self._merge_primary_combo.addItem(f"{t.name} ({t.row_count:,}行)", t.table_id)
        if self._merge_primary_combo.count() == 0:
            self._merge_primary_combo.addItem("（请勾选表格）", "")
        p_idx = self._merge_primary_combo.findData(prev_primary) if prev_primary else -1
        self._merge_primary_combo.setCurrentIndex(p_idx if p_idx >= 0 else 0)
        self._merge_primary_combo.blockSignals(False)

        valid_ids = {t.table_id for t in self._tables}
        self._dedup_custom_keys = {k: v for k, v in self._dedup_custom_keys.items() if k in valid_ids}
        self._merge_custom_keys = {k: v for k, v in self._merge_custom_keys.items() if k in valid_ids}
        self._update_custom_key_button_labels()

    def _update_custom_key_button_labels(self) -> None:
        checked_ids = {t.table_id for t in self.checked_tables()}
        d_cnt = sum(1 for tid in checked_ids if self._dedup_custom_keys.get(tid))
        m_cnt = sum(1 for tid in checked_ids if self._merge_custom_keys.get(tid))

        self._btn_dedup_per_table.setText(f"按表已配({d_cnt})" if d_cnt else "按表指定")
        self._dedup_custom_key_badge.setVisible(d_cnt > 0)
        self._btn_dedup_clear_keys.setVisible(d_cnt > 0)

        self._btn_merge_per_table.setText(f"按表已配({m_cnt})" if m_cnt else "按表指定")
