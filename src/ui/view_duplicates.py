"""View 4: 重复项中心 — Grouped duplicates, multipart highlights, smart selection & safe cleanup."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
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
from src.core.exporter import ExportError, export_duplicate_groups_excel
from src.core.models import DataRecord, DuplicateGroup
from src.ui.app_context import AppContext
from src.ui.theme import get_palette
from src.ui.view_library import open_local_folder_safely
from src.ui.widgets import ScrollableDetailDialog, WorkerThread, format_file_size

_AUTO_EXPAND_GROUP_LIMIT: int = 200


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


class DuplicatesView(QWidget):
    """Grouped duplicate management center with smart selection, search & safe backed-up cleanup."""

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._groups: list[DuplicateGroup] = []
        self._record_items: dict[int, QTreeWidgetItem] = {}
        self._record_map: dict[int, DataRecord] = {}
        self._group_items: list[tuple[DuplicateGroup, QTreeWidgetItem]] = []
        self._dirty: bool = False
        self._cleanup_worker: Optional[WorkerThread] = None
        self._export_worker: Optional[WorkerThread] = None

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._apply_search_filter)

        self._setup_ui()
        self.ctx.data_changed.connect(self._on_data_changed)
        self.ctx.db_switched.connect(self._on_db_switched)
        self.ctx.theme_changed.connect(self.apply_theme)
        self.refresh()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # Smart strategy & search toolbar card (clean multi-row layout fitting 1080px width)
        top_card = QFrame()
        top_card.setProperty("card", True)
        top_layout = QVBoxLayout(top_card)
        top_layout.setContentsMargins(14, 10, 14, 10)
        top_layout.setSpacing(8)

        # Row 1: Smart selection strategy buttons
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        lbl = QLabel("智能勾选：")
        lbl.setProperty("role", "section")
        row1.addWidget(lbl)

        btn_quality = QPushButton("✨ 保留最高质量/标签")
        btn_quality.setProperty("variant", "primary")
        btn_quality.setProperty("compact", True)
        btn_quality.clicked.connect(lambda: self._apply_smart_plan(STRATEGY_KEEP_HIGHEST_TAG_SCORE))
        row1.addWidget(btn_quality)

        btn_earliest = QPushButton("保留最早")
        btn_earliest.setProperty("compact", True)
        btn_earliest.clicked.connect(lambda: self._apply_smart_plan(STRATEGY_KEEP_EARLIEST))
        row1.addWidget(btn_earliest)

        btn_latest = QPushButton("保留最新")
        btn_latest.setProperty("compact", True)
        btn_latest.clicked.connect(lambda: self._apply_smart_plan(STRATEGY_KEEP_LATEST))
        row1.addWidget(btn_latest)

        btn_longest = QPushButton("保留最长文件名")
        btn_longest.setProperty("compact", True)
        btn_longest.clicked.connect(lambda: self._apply_smart_plan(STRATEGY_KEEP_LONGEST_NAME))
        row1.addWidget(btn_longest)

        btn_uncheck = QPushButton("取消全选")
        btn_uncheck.setProperty("variant", "ghost")
        btn_uncheck.setProperty("compact", True)
        btn_uncheck.clicked.connect(self._clear_checks)
        row1.addWidget(btn_uncheck)
        row1.addStretch()
        top_layout.addLayout(row1)

        # Row 2: Search box + Expand/Collapse + Export
        row2 = QHBoxLayout()
        row2.setSpacing(8)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("🔍 筛选重复组（编码、文件名、标签、来源...）")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.textChanged.connect(lambda _: self._search_timer.start())
        row2.addWidget(self._search_input, 1)

        btn_expand = QPushButton("全部展开")
        btn_expand.setProperty("compact", True)
        btn_expand.clicked.connect(self._expand_all)
        row2.addWidget(btn_expand)

        btn_collapse = QPushButton("全部折叠")
        btn_collapse.setProperty("compact", True)
        btn_collapse.clicked.connect(self._collapse_all)
        row2.addWidget(btn_collapse)

        self._btn_export = QPushButton("导出重复清单 (.xlsx)")
        self._btn_export.setProperty("compact", True)
        self._btn_export.clicked.connect(self._export_groups)
        row2.addWidget(self._btn_export)
        top_layout.addLayout(row2)

        # Row 3: Options & summary
        row3 = QHBoxLayout()
        row3.setSpacing(14)

        self._chk_show_multipart = QCheckBox("显示疑似分集组 (CD1 / CD2 / Part)")
        self._chk_show_multipart.setChecked(True)
        self._chk_show_multipart.toggled.connect(lambda _: self.refresh())
        row3.addWidget(self._chk_show_multipart)

        self._chk_protect_multipart = QCheckBox("智能勾选时保护不同分集不被误删（推荐）")
        self._chk_protect_multipart.setChecked(True)
        row3.addWidget(self._chk_protect_multipart)

        row3.addStretch()

        self._summary_label = QLabel("共 0 组重复编码")
        self._summary_label.setProperty("role", "subtitle")
        row3.addWidget(self._summary_label)

        top_layout.addLayout(row3)
        root.addWidget(top_card)

        # Grouped TreeWidget
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(
            ["勾选删除 / 标准编码", "类型 / 分集", "版本标签", "原始文件名", "来源", "大小", "入库时间"]
        )
        self._tree.setAlternatingRowColors(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setAnimated(False)
        self._tree.setUniformRowHeights(True)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        self._tree.header().setStretchLastSection(True)
        self._tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._tree.setColumnWidth(0, 210)
        self._tree.setColumnWidth(1, 115)
        self._tree.setColumnWidth(2, 130)
        self._tree.setColumnWidth(3, 300)
        self._tree.setColumnWidth(4, 140)
        self._tree.setColumnWidth(5, 85)
        self._tree.itemChanged.connect(self._on_item_changed)
        root.addWidget(self._tree, 1)

        # Bottom delete confirmation bar
        bottom_bar = QHBoxLayout()
        self._checked_label = QLabel("已勾选 0 条待删除记录")
        self._checked_label.setProperty("role", "section")
        bottom_bar.addWidget(self._checked_label)
        bottom_bar.addStretch()

        self._btn_delete = QPushButton("预览并删除已勾选项（自动备份）")
        self._btn_delete.setProperty("variant", "danger")
        self._btn_delete.setEnabled(False)
        self._btn_delete.clicked.connect(self._confirm_and_delete)
        bottom_bar.addWidget(self._btn_delete)
        root.addLayout(bottom_bar)

    # ------------------------------------------------------------------
    # Lifecycle, theme & MainWindow hooks
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self._search_timer.stop()
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

    def refresh_if_dirty(self) -> None:
        self.on_activated()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dirty:
            self.refresh()

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
        if not self.isVisible():
            self._dirty = True
            return
        self.refresh()

    def _palette(self, theme: Optional[str] = None) -> dict[str, str]:
        return get_palette(theme or self.ctx.config.theme)

    def apply_theme(self, theme: str) -> None:
        p = self._palette(theme)
        purple_brush = QBrush(QColor(p["purple"]))
        warning_brush = QBrush(QColor(p["warning"]))
        danger_brush = QBrush(QColor(p["danger"]))
        success_brush = QBrush(QColor(p["success"]))

        self._tree.setUpdatesEnabled(False)
        self._tree.blockSignals(True)
        try:
            for group, parent_item in self._group_items:
                parent_item.setForeground(1, purple_brush if group.is_possible_multipart else warning_brush)
            for item in self._record_items.values():
                if item.checkState(0) == Qt.CheckState.Checked:
                    item.setForeground(0, danger_brush)
                elif item.data(0, Qt.ItemDataRole.ForegroundRole) is not None:
                    item.setForeground(0, success_brush)
        finally:
            self._tree.blockSignals(False)
            self._tree.setUpdatesEnabled(True)

    def _is_worker_running(self, worker: Optional[WorkerThread]) -> bool:
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            return False

    # ------------------------------------------------------------------
    # Data refresh, tree population & filtering
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        self._dirty = False
        include_mp = self._chk_show_multipart.isChecked()
        try:
            self._groups = self.ctx.db.get_duplicate_groups(include_multipart=include_mp)
        except (DatabaseError, OSError, Exception) as exc:
            ScrollableDetailDialog("加载重复项失败", "读取重复记录组时发生错误：", str(exc), parent=self).exec()
            return
        self._populate_tree()

    def _populate_tree(self) -> None:
        p = self._palette()
        purple_brush = QBrush(QColor(p["purple"]))
        warning_brush = QBrush(QColor(p["warning"]))

        bold_font = QFont()
        bold_font.setBold(True)
        mono_font = QFont("Consolas", 10)
        mono_font.setBold(True)

        auto_expand = len(self._groups) <= _AUTO_EXPAND_GROUP_LIMIT

        self._tree.setUpdatesEnabled(False)
        self._tree.blockSignals(True)
        try:
            self._tree.clear()
            self._record_items.clear()
            self._record_map.clear()
            self._group_items.clear()

            mp_count = 0
            true_dup_count = 0

            for group in self._groups:
                if group.is_possible_multipart:
                    mp_count += 1
                    badge = "疑似分集"
                    badge_brush = purple_brush
                else:
                    true_dup_count += 1
                    badge = "完全重复"
                    badge_brush = warning_brush

                parent_item = QTreeWidgetItem(self._tree)
                parent_item.setText(0, f"{group.canonical_code}  ({group.count} 条)")
                parent_item.setData(0, Qt.ItemDataRole.UserRole + 1, group.canonical_code)
                parent_item.setFont(0, mono_font)
                parent_item.setText(1, f"[{badge}]")
                parent_item.setForeground(1, badge_brush)
                parent_item.setFont(1, bold_font)
                parent_item.setFlags(parent_item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)

                parts_summary = ", ".join(sorted({r.part for r in group.records if r.part}))
                if parts_summary:
                    parent_item.setText(2, f"分集: {parts_summary}")

                for rec in group.records:
                    if rec.id is None:
                        continue
                    child = QTreeWidgetItem(parent_item)
                    child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    child.setCheckState(0, Qt.CheckState.Unchecked)
                    child.setData(0, Qt.ItemDataRole.UserRole, rec.id)
                    child.setText(0, f"保留  #{rec.id}")
                    child.setText(1, rec.part or "主文件")
                    child.setText(2, " / ".join(rec.tags) if rec.tags else "-")
                    child.setText(3, rec.file_name or rec.raw_text)
                    child.setText(4, rec.source_name or rec.source_type)
                    child.setText(5, format_file_size(rec.file_size))
                    child.setText(6, rec.created_at[:19] if rec.created_at else "-")
                    if rec.full_path:
                        child.setToolTip(3, rec.full_path)

                    self._record_items[rec.id] = child
                    self._record_map[rec.id] = rec

                parent_item.setExpanded(auto_expand)
                self._group_items.append((group, parent_item))
        finally:
            self._tree.blockSignals(False)
            self._tree.setUpdatesEnabled(True)

        hint = "（组数较多已默认折叠，可点「全部展开」）" if not auto_expand else ""
        self._summary_label.setText(
            f"共 {len(self._groups)} 组（完全重复 {true_dup_count} 组 · 疑似分集 {mp_count} 组）{hint}"
        )
        if self._search_input.text().strip():
            self._apply_search_filter()
        self._update_checked_summary()

    def _apply_search_filter(self) -> None:
        query = self._search_input.text().strip().lower()
        shown = 0
        self._tree.setUpdatesEnabled(False)
        try:
            for group, parent_item in self._group_items:
                if not query:
                    parent_item.setHidden(False)
                    shown += 1
                    continue
                matched = query in group.canonical_code.lower()
                if not matched:
                    for rec in group.records:
                        haystack = " ".join(
                            (
                                rec.file_name or "",
                                rec.raw_text or "",
                                rec.source_name or "",
                                rec.part or "",
                                " ".join(rec.tags),
                            )
                        ).lower()
                        if query in haystack:
                            matched = True
                            break
                parent_item.setHidden(not matched)
                if matched:
                    shown += 1
                    parent_item.setExpanded(True)
        finally:
            self._tree.setUpdatesEnabled(True)

        if query:
            self._summary_label.setText(f"筛选命中 {shown} / {len(self._groups)} 组重复编码")
        else:
            mp_count = sum(1 for g in self._groups if g.is_possible_multipart)
            true_dup_count = len(self._groups) - mp_count
            self._summary_label.setText(
                f"共 {len(self._groups)} 组（完全重复 {true_dup_count} 组 · 疑似分集 {mp_count} 组）"
            )

    def _expand_all(self) -> None:
        self._tree.setUpdatesEnabled(False)
        try:
            self._tree.expandAll()
        finally:
            self._tree.setUpdatesEnabled(True)

    def _collapse_all(self) -> None:
        self._tree.setUpdatesEnabled(False)
        try:
            self._tree.collapseAll()
        finally:
            self._tree.setUpdatesEnabled(True)

    # ------------------------------------------------------------------
    # Right-click context menu
    # ------------------------------------------------------------------

    def _show_context_menu(self, pos) -> None:
        item = self._tree.itemAt(pos)
        if item is None:
            return
        rec_id = item.data(0, Qt.ItemDataRole.UserRole)
        group_code = item.data(0, Qt.ItemDataRole.UserRole + 1)

        menu = QMenu(self)
        if rec_id is not None:
            rec = self._record_map.get(rec_id)
            if rec is None:
                return
            act_copy_code = QAction(f"复制编码 ({rec.canonical_code})", self)
            act_copy_code.triggered.connect(lambda: self._copy_text(rec.canonical_code, "标准编码"))
            menu.addAction(act_copy_code)

            path_or_name = rec.full_path or rec.file_name or rec.raw_text
            act_copy_path = QAction("复制路径" if rec.full_path else "复制文件名", self)
            act_copy_path.triggered.connect(
                lambda: self._copy_text(path_or_name, "路径" if rec.full_path else "文件名")
            )
            menu.addAction(act_copy_path)

            if rec.full_path:
                act_open_dir = QAction("打开所在目录", self)
                act_open_dir.triggered.connect(lambda: open_local_folder_safely(rec.full_path, self.ctx))
                menu.addAction(act_open_dir)
        elif group_code:
            act_copy_code = QAction(f"复制编码 ({group_code})", self)
            act_copy_code.triggered.connect(lambda: self._copy_text(str(group_code), "标准编码"))
            menu.addAction(act_copy_code)
        else:
            return

        menu.exec(self._tree.viewport().mapToGlobal(pos))

    def _copy_text(self, text: str, label: str) -> None:
        QApplication.clipboard().setText(text)
        self.ctx.notify(f"已复制{label}到剪贴板", "success")

    # ------------------------------------------------------------------
    # Smart selection & manual check handling
    # ------------------------------------------------------------------

    def _apply_smart_plan(self, strategy: str) -> None:
        if not self._groups:
            self.ctx.notify("当前没有重复记录组", "info")
            return
        protect_mp = self._chk_protect_multipart.isChecked()
        plan = plan_duplicate_cleanup(
            self._groups,
            strategy=strategy,
            protect_multipart=protect_mp,
        )
        delete_set = set(plan.delete_ids)

        p = self._palette()
        danger_brush = QBrush(QColor(p["danger"]))
        success_brush = QBrush(QColor(p["success"]))

        self._tree.setUpdatesEnabled(False)
        self._tree.blockSignals(True)
        try:
            for rec_id, item in self._record_items.items():
                if rec_id in delete_set:
                    item.setCheckState(0, Qt.CheckState.Checked)
                    item.setText(0, f"待删除 #{rec_id}")
                    item.setForeground(0, danger_brush)
                else:
                    item.setCheckState(0, Qt.CheckState.Unchecked)
                    item.setText(0, f"保留  #{rec_id}")
                    item.setForeground(0, success_brush)
        finally:
            self._tree.blockSignals(False)
            self._tree.setUpdatesEnabled(True)

        self._update_checked_summary()
        extra = f"，已保护 {len(plan.protected_groups)} 组疑似分集" if plan.protected_groups else ""
        self.ctx.notify(f"智能勾选完成：待删除 {plan.delete_count} 条，保留 {plan.keep_count} 条{extra}", "info")

    def _clear_checks(self) -> None:
        self._tree.setUpdatesEnabled(False)
        self._tree.blockSignals(True)
        try:
            for rec_id, item in self._record_items.items():
                item.setCheckState(0, Qt.CheckState.Unchecked)
                item.setText(0, f"保留  #{rec_id}")
                item.setData(0, Qt.ItemDataRole.ForegroundRole, None)
        finally:
            self._tree.blockSignals(False)
            self._tree.setUpdatesEnabled(True)
        self._update_checked_summary()

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        rec_id = item.data(0, Qt.ItemDataRole.UserRole)
        if rec_id is None:
            return
        p = self._palette()
        self._tree.blockSignals(True)
        if item.checkState(0) == Qt.CheckState.Checked:
            item.setText(0, f"待删除 #{rec_id}")
            item.setForeground(0, QBrush(QColor(p["danger"])))
        else:
            item.setText(0, f"保留  #{rec_id}")
            item.setForeground(0, QBrush(QColor(p["success"])))
        self._tree.blockSignals(False)
        self._update_checked_summary()

    def _checked_record_ids(self) -> list[int]:
        ids: list[int] = []
        for rec_id, item in self._record_items.items():
            if item.checkState(0) == Qt.CheckState.Checked:
                ids.append(rec_id)
        return ids

    def _find_fully_checked_groups(self, checked_set: set[int]) -> list[str]:
        """Return canonical codes of groups where EVERY record is checked for deletion."""
        fully_checked: list[str] = []
        for group in self._groups:
            valid_ids = [r.id for r in group.records if r.id is not None]
            if valid_ids and all(rid in checked_set for rid in valid_ids):
                fully_checked.append(group.canonical_code)
        return fully_checked

    def _update_checked_summary(self) -> None:
        ids = self._checked_record_ids()
        fully_checked = self._find_fully_checked_groups(set(ids))
        if fully_checked:
            self._checked_label.setText(
                f"已勾选 {len(ids)} 条待删除记录（⚠️ 注意：{len(fully_checked)} 组被全部勾选，将删除最后副本）"
            )
        else:
            self._checked_label.setText(f"已勾选 {len(ids)} 条待删除记录")
        self._btn_delete.setEnabled(len(ids) > 0 and not self._is_worker_running(self._cleanup_worker))

    # ------------------------------------------------------------------
    # Safe threaded cleanup & export
    # ------------------------------------------------------------------

    def _confirm_and_delete(self) -> None:
        if self._is_worker_running(self._cleanup_worker):
            self.ctx.notify("正在执行清理任务，请稍候...", "warning")
            return
        ids = self._checked_record_ids()
        if not ids:
            return

        fully_checked = self._find_fully_checked_groups(set(ids))
        lines: list[str] = []
        if fully_checked:
            lines.append("====== ⚠️ 警告：以下编码组的所有副本均已勾选（执行后该编码将在库中彻底消失）======")
            for code in fully_checked[:50]:
                lines.append(f"  [整组清空] {code}")
            if len(fully_checked) > 50:
                lines.append(f"  ... 共 {len(fully_checked)} 组被整组勾选")
            lines.append("")

        for rec_id in ids[:150]:
            rec = self._record_map.get(rec_id)
            if rec:
                part = f" [{rec.part}]" if rec.part else ""
                lines.append(f"[{rec.canonical_code}]{part}  {rec.file_name or rec.raw_text}")
        if len(ids) > 150:
            lines.append(f"... 共 {len(ids)} 条记录")

        if fully_checked:
            title = "⚠️ 危险操作警告：确认删除（含整组清空项）"
            summary = (
                f"⚠️ 数据丢失警告：您勾选了 {len(fully_checked)} 个重复组的【全部记录】"
                f"（{', '.join(fully_checked[:5])}{' 等' if len(fully_checked) > 5 else ''}），"
                f"删除后这些编码将不保留任何副本！\n"
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
        self._tree.setEnabled(False)
        self._checked_label.setText(f"正在后台安全清理 {len(ids)} 条重复记录（含自动备份）...")

        def _task() -> int:
            return execute_duplicate_cleanup(self.ctx.db, ids, create_backup=True)

        worker = WorkerThread(_task, parent=self)
        self._cleanup_worker = worker
        worker.succeeded.connect(self._on_cleanup_succeeded)
        worker.failed.connect(self._on_cleanup_failed)
        worker.finished.connect(lambda w=worker: self._on_cleanup_finished(w))
        if not hasattr(worker, "safe_stop"):
            worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_cleanup_succeeded(self, deleted: int) -> None:
        self._tree.setEnabled(True)
        self.ctx.notify(f"已安全清理 {deleted} 条重复记录（已自动创建备份）", "success")
        self.ctx.data_changed.emit()

    def _on_cleanup_failed(self, detail: str) -> None:
        self._tree.setEnabled(True)
        self._update_checked_summary()
        ScrollableDetailDialog("清理重复记录失败", "执行数据库删除时发生错误：", detail, parent=self).exec()

    def _on_cleanup_finished(self, worker: WorkerThread) -> None:
        if self._cleanup_worker is worker:
            self._cleanup_worker = None
        self._tree.setEnabled(True)
        self._update_checked_summary()

    def _export_groups(self) -> None:
        if self._is_worker_running(self._export_worker):
            self.ctx.notify("正在导出重复项报告，请稍候...", "warning")
            return
        if not self._groups:
            self.ctx.notify("当前没有可导出的重复项", "warning")
            return
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出重复项报告",
            str(Path(self.ctx.config.default_export_dir or Path.home()) / "重复项汇总报告.xlsx"),
            "Excel 工作簿 (*.xlsx)",
        )
        if not save_path:
            return

        try:
            self.ctx.config.default_export_dir = str(Path(save_path).parent)
            self.ctx.config_manager.save()
        except OSError:
            pass

        groups_snapshot = list(self._groups)
        self._btn_export.setEnabled(False)
        self.ctx.notify(f"正在后台导出 {Path(save_path).name}...", "info")

        def _task() -> str:
            export_duplicate_groups_excel(groups_snapshot, save_path)
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
        self._btn_export.setEnabled(True)
        self.ctx.notify(f"已导出重复项报告：{Path(save_path).name}", "success")

    def _on_export_failed(self, detail: str) -> None:
        self._btn_export.setEnabled(True)
        ScrollableDetailDialog("导出失败", "导出重复项报告时发生错误：", detail, parent=self).exec()

    def _on_export_finished(self, worker: WorkerThread) -> None:
        if self._export_worker is worker:
            self._export_worker = None
        self._btn_export.setEnabled(True)
