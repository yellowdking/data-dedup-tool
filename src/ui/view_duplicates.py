"""View 4: 重复项中心 — Grouped duplicates, multipart highlights, smart selection & cleanup."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

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
from src.ui.widgets import ScrollableDetailDialog, format_file_size


class DuplicatesView(QWidget):
    """Grouped duplicate management center with smart selection and safe backed-up cleanup."""

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._groups: list[DuplicateGroup] = []
        self._record_items: dict[int, QTreeWidgetItem] = {}
        self._record_map: dict[int, DataRecord] = {}

        self._setup_ui()
        self.ctx.data_changed.connect(self.refresh)
        self.refresh()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # Smart strategy toolbar card
        top_card = QFrame()
        top_card.setProperty("card", True)
        top_layout = QVBoxLayout(top_card)
        top_layout.setContentsMargins(14, 12, 14, 12)
        top_layout.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(8)

        lbl = QLabel("智能勾选策略（勾选将待删除项）：")
        lbl.setProperty("role", "section")
        row1.addWidget(lbl)

        btn_quality = QPushButton("✨ 智能勾选：每组保留最高质量/带标签")
        btn_quality.setProperty("variant", "primary")
        btn_quality.clicked.connect(lambda: self._apply_smart_plan(STRATEGY_KEEP_HIGHEST_TAG_SCORE))
        row1.addWidget(btn_quality)

        btn_earliest = QPushButton("每组保留最早")
        btn_earliest.clicked.connect(lambda: self._apply_smart_plan(STRATEGY_KEEP_EARLIEST))
        row1.addWidget(btn_earliest)

        btn_latest = QPushButton("每组保留最新")
        btn_latest.clicked.connect(lambda: self._apply_smart_plan(STRATEGY_KEEP_LATEST))
        row1.addWidget(btn_latest)

        btn_longest = QPushButton("每组保留最长文件名")
        btn_longest.clicked.connect(lambda: self._apply_smart_plan(STRATEGY_KEEP_LONGEST_NAME))
        row1.addWidget(btn_longest)

        btn_uncheck = QPushButton("取消全选")
        btn_uncheck.setProperty("variant", "ghost")
        btn_uncheck.clicked.connect(self._clear_checks)
        row1.addWidget(btn_uncheck)
        row1.addStretch()
        top_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(16)

        self._chk_show_multipart = QCheckBox("显示疑似分集组 (CD1 / CD2 / Part)")
        self._chk_show_multipart.setChecked(True)
        self._chk_show_multipart.toggled.connect(lambda _: self.refresh())
        row2.addWidget(self._chk_show_multipart)

        self._chk_protect_multipart = QCheckBox("智能勾选时保护不同分集不被误删（推荐）")
        self._chk_protect_multipart.setChecked(True)
        row2.addWidget(self._chk_protect_multipart)

        row2.addStretch()

        self._summary_label = QLabel("共 0 组重复编码")
        self._summary_label.setProperty("role", "subtitle")
        row2.addWidget(self._summary_label)

        btn_export = QPushButton("导出重复清单 (.xlsx)")
        btn_export.clicked.connect(self._export_groups)
        row2.addWidget(btn_export)

        top_layout.addLayout(row2)
        root.addWidget(top_card)

        # Grouped TreeWidget
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(
            ["勾选删除 / 标准编码", "类型 / 分集", "版本标签", "原始文件名", "来源", "大小", "入库时间"]
        )
        self._tree.setAlternatingRowColors(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setAnimated(True)
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

    def refresh(self) -> None:
        include_mp = self._chk_show_multipart.isChecked()
        self._groups = self.ctx.db.get_duplicate_groups(include_multipart=include_mp)
        self._populate_tree()

    def _populate_tree(self) -> None:
        self._tree.blockSignals(True)
        self._tree.clear()
        self._record_items.clear()
        self._record_map.clear()

        bold_font = QFont()
        bold_font.setBold(True)
        mono_font = QFont("Consolas", 10)
        mono_font.setBold(True)

        mp_count = 0
        true_dup_count = 0

        for group in self._groups:
            if group.is_possible_multipart:
                mp_count += 1
                badge = "疑似分集"
                badge_color = QColor("#7C3AED")
            else:
                true_dup_count += 1
                badge = "完全重复"
                badge_color = QColor("#D97706")

            parent_item = QTreeWidgetItem(self._tree)
            parent_item.setText(0, f"{group.canonical_code}  ({group.count} 条)")
            parent_item.setFont(0, mono_font)
            parent_item.setText(1, f"[{badge}]")
            parent_item.setForeground(1, QBrush(badge_color))
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

            parent_item.setExpanded(True)

        self._tree.blockSignals(False)
        self._summary_label.setText(
            f"共 {len(self._groups)} 组（完全重复 {true_dup_count} 组 · 疑似分集 {mp_count} 组）"
        )
        self._update_checked_summary()

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

        self._tree.blockSignals(True)
        for rec_id, item in self._record_items.items():
            if rec_id in delete_set:
                item.setCheckState(0, Qt.CheckState.Checked)
                item.setText(0, f"待删除 #{rec_id}")
                item.setForeground(0, QBrush(QColor("#DC2626")))
            else:
                item.setCheckState(0, Qt.CheckState.Unchecked)
                item.setText(0, f"保留  #{rec_id}")
                item.setForeground(0, QBrush(QColor("#059669")))
        self._tree.blockSignals(False)

        self._update_checked_summary()
        extra = f"，已保护 {len(plan.protected_groups)} 组疑似分集" if plan.protected_groups else ""
        self.ctx.notify(f"智能勾选完成：待删除 {plan.delete_count} 条，保留 {plan.keep_count} 条{extra}", "info")

    def _clear_checks(self) -> None:
        self._tree.blockSignals(True)
        for rec_id, item in self._record_items.items():
            item.setCheckState(0, Qt.CheckState.Unchecked)
            item.setText(0, f"保留  #{rec_id}")
            item.setData(0, Qt.ItemDataRole.ForegroundRole, None)
        self._tree.blockSignals(False)
        self._update_checked_summary()

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        rec_id = item.data(0, Qt.ItemDataRole.UserRole)
        if rec_id is None:
            return
        self._tree.blockSignals(True)
        if item.checkState(0) == Qt.CheckState.Checked:
            item.setText(0, f"待删除 #{rec_id}")
            item.setForeground(0, QBrush(QColor("#DC2626")))
        else:
            item.setText(0, f"保留  #{rec_id}")
            item.setForeground(0, QBrush(QColor("#059669")))
        self._tree.blockSignals(False)
        self._update_checked_summary()

    def _checked_record_ids(self) -> list[int]:
        ids: list[int] = []
        for rec_id, item in self._record_items.items():
            if item.checkState(0) == Qt.CheckState.Checked:
                ids.append(rec_id)
        return ids

    def _update_checked_summary(self) -> None:
        ids = self._checked_record_ids()
        self._checked_label.setText(f"已勾选 {len(ids)} 条待删除记录")
        self._btn_delete.setEnabled(len(ids) > 0)

    def _confirm_and_delete(self) -> None:
        ids = self._checked_record_ids()
        if not ids:
            return
        lines: list[str] = []
        for rec_id in ids[:150]:
            rec = self._record_map.get(rec_id)
            if rec:
                part = f" [{rec.part}]" if rec.part else ""
                lines.append(f"[{rec.canonical_code}]{part}  {rec.file_name or rec.raw_text}")
        if len(ids) > 150:
            lines.append(f"... 共 {len(ids)} 条记录")

        dlg = ScrollableDetailDialog(
            title="确认清理重复记录",
            summary=f"即将从当前库删除以下 {len(ids)} 条重复记录（执行前自动创建 .bak 备份，不删除硬盘上的实际文件）：",
            detail_text="\n".join(lines),
            confirm_mode=True,
            confirm_label=f"确认删除 ({len(ids)} 条)",
            danger=True,
            parent=self,
        )
        if dlg.exec() == ScrollableDetailDialog.DialogCode.Accepted:
            deleted = execute_duplicate_cleanup(self.ctx.db, ids, create_backup=True)
            self.ctx.notify(f"已安全清理 {deleted} 条重复记录（已自动创建备份）", "success")
            self.ctx.data_changed.emit()

    def _export_groups(self) -> None:
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
            export_duplicate_groups_excel(self._groups, save_path)
            self.ctx.notify(f"已导出重复项报告：{Path(save_path).name}", "success")
        except ExportError as exc:
            ScrollableDetailDialog("导出失败", str(exc), parent=self).exec()
