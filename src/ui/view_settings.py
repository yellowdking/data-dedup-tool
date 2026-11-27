"""View 5: 规则与设置 — Live parser tester, custom regex rules, UI/table preferences & DB backups."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.config import DEFAULT_COMPANION_EXTENSIONS
from src.core.database import DatabaseError
from src.core.models import CustomRule
from src.ui.app_context import AppContext
from src.ui.delegates import (
    ROLE_COPY_TEXT,
    apply_slim_scrollbars,
    enhance_table_view,
    sync_widget_craft_theme,
)
from src.ui.icons import get_icon
from src.ui.settings_rule_dialog import (
    RuleEditorDialog,
    _extract_test_line_target,
    _stop_worker_safely,
    _validate_custom_regex_safe,
)
from src.ui.table_header import ColumnSpec, TableHeaderManager
from src.ui.theme import get_mono_font, get_palette
from src.ui.widgets import (
    EmptyStateWidget,
    PageHeaderBar,
    ScrollableDetailDialog,
    WorkerThread,
    format_file_size,
)

__all__ = [
    "RuleEditorDialog",
    "SettingsView",
    "_extract_test_line_target",
    "_validate_custom_regex_safe",
]

_MAX_TEST_PREVIEW_ROWS: int = 500

def _tspec(k: str, lbl: str, w: int, vis: bool = True, mw: int = 90, lock: bool = False, sw: float = 0.0, cat: str = "basic") -> ColumnSpec:
    return ColumnSpec(k, lbl, w, vis, mw, False, lock, sw, cat)


_TESTER_SPECS: tuple[ColumnSpec, ...] = (
    _tspec("raw", "原始输入", 210, True, 100, True, 0.4),
    _tspec("cleaned", "清洗后文本", 170, True, 90, False, 0.3),
    _tspec("code", "标准编码", 136, True, 90, True),
    _tspec("prefix", "前缀", 78, False, 56, False, 0.0, "advanced"),
    _tspec("tags", "版本标签", 110, False, 70, False, 0.0, "advanced"),
    _tspec("part", "分集", 68, False, 52, False, 0.0, "advanced"),
    _tspec("rule", "匹配规则", 150, True, 90, False, 0.15),
    _tspec("note", "置信度 / 备注", 150, True, 90, False, 0.15),
)


def _compact_btn(
    text: str,
    slot,
    *,
    variant: str = "",
    icon_size: int = 0,
    tooltip: str = "",
) -> QPushButton:
    btn = QPushButton(text)
    btn.setProperty("compact", True)
    if variant:
        btn.setProperty("variant", variant)
    if icon_size > 0:
        btn.setIconSize(QSize(icon_size, icon_size))
    if tooltip:
        btn.setToolTip(tooltip)
    btn.clicked.connect(slot)
    return btn


class SettingsView(QWidget):
    """View 5: Live parser tester, custom regex rules, UI/table preferences & DB backups."""

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._backups_dirty: bool = False
        self._db_worker: Optional[WorkerThread] = None
        self._tester_header_mgr: Optional[TableHeaderManager] = None

        self._test_timer = QTimer(self)
        self._test_timer.setSingleShot(True)
        self._test_timer.setInterval(200)
        self._test_timer.timeout.connect(self.run_parser_test)

        self._setup_ui()
        self._refresh_icons(self.ctx.config.theme)
        self.ctx.db_switched.connect(self._on_db_switched)
        self.ctx.data_changed.connect(self._on_data_changed)
        self.ctx.settings_changed.connect(self._sync_settings_ui)
        self.ctx.theme_changed.connect(self.apply_theme)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._page_header = PageHeaderBar("规则与设置", parent=self)
        root.addWidget(self._page_header)

        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(20, 10, 20, 14)
        body_lay.setSpacing(0)

        self._tabs = QTabWidget()
        self._tabs.setProperty("borderless", True)
        self._tabs.addTab(self._build_tester_tab(), "解析测试")
        self._tabs.addTab(self._build_rules_tab(), "规则管理")
        self._tabs.addTab(self._build_general_tab(), "常规与备份")
        body_lay.addWidget(self._tabs, 1)
        root.addWidget(body, 1)

    # ------------------------------------------------------------------
    # Lifecycle, lazy refresh & theme
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self._test_timer.stop()
        if self._tester_header_mgr is not None:
            try:
                self._tester_header_mgr.flush()
            except RuntimeError:
                pass
        _stop_worker_safely(self._db_worker)
        self._db_worker = None

    def focus_search(self) -> None:
        if self._tabs.currentIndex() == 0:
            self._test_input.setFocus()
            self._test_input.selectAll()

    def mark_dirty(self) -> None:
        self._backups_dirty = True
        if self.isVisible():
            self._refresh_backups()

    def on_activated(self) -> None:
        if self._backups_dirty:
            self._refresh_backups()

    def refresh_if_dirty(self) -> None:
        self.on_activated()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._backups_dirty:
            self._refresh_backups()

    def _on_db_switched(self, _db_path: str = "") -> None:
        if not self.isVisible():
            self._backups_dirty = True
            return
        self._refresh_backups()

    def _on_data_changed(self) -> None:
        self._on_db_switched()

    def _sync_settings_ui(self) -> None:
        for w, val in (
            (self._chk_show_advanced, bool(self.ctx.config.show_advanced_fields)),
            (self._chk_default_ignore, bool(self.ctx.config.ignore_companion_files)),
            (self._chk_auto_backup, bool(self.ctx.config.auto_backup)),
        ):
            w.blockSignals(True)
            w.setChecked(val)
            w.blockSignals(False)
        self._spin_backup_keep.blockSignals(True)
        self._spin_backup_keep.setValue(max(1, int(self.ctx.config.backup_keep_count)))
        self._spin_backup_keep.blockSignals(False)

    def _palette(self, theme: Optional[str] = None) -> dict[str, str]:
        return get_palette(theme or self.ctx.config.theme)

    def _refresh_icons(self, theme: str) -> None:
        p = self._palette(theme)
        cta, std, dng = p["cta_text"], p["icon_default"], p["danger"]
        for btn, name, color in (
            (self._btn_run_test, "play", cta),
            (self._btn_add_rule, "plus", cta),
            (self._btn_edit_rule, "edit", std),
            (self._btn_del_rule, "trash", dng),
            (self._btn_reset_layouts, "refresh-cw", std),
            (self._btn_backup_now, "archive", cta),
            (self._btn_refresh_bak, "refresh-cw", std),
            (self._btn_open_dir, "folder-open", std),
            (self._btn_clear_db, "trash", dng),
        ):
            btn.setIcon(get_icon(name, 14, color, theme=theme))

    def apply_theme(self, theme: str) -> None:
        p = self._palette(theme)
        success_c, muted_c = QColor(p["success"]), QColor(p["text_muted"])
        secondary_c, accent_c = QColor(p["text_secondary"]), QColor(p["accent"])

        for row in range(self._test_table.rowCount()):
            code_item = self._test_table.item(row, 2)
            if code_item is not None:
                is_rec = bool(code_item.data(Qt.ItemDataRole.UserRole))
                code_item.setForeground(success_c if is_rec else muted_c)

        for row in range(self._rules_table.rowCount()):
            chk_item = self._rules_table.item(row, 0)
            kind_item = self._rules_table.item(row, 2)
            if chk_item is not None and kind_item is not None:
                is_builtin = bool(chk_item.data(Qt.ItemDataRole.UserRole + 1))
                kind_item.setForeground(secondary_c if is_builtin else accent_c)

        self._backup_empty.apply_theme(theme)
        self._refresh_icons(theme)
        sync_widget_craft_theme(self, theme)

    def _is_worker_running(self, worker: Optional[WorkerThread]) -> bool:
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            return False

    # ------------------------------------------------------------------
    # Sub-tab 1: 解析测试
    # ------------------------------------------------------------------

    def _build_tester_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)
        theme = self.ctx.config.theme

        header = QHBoxLayout()
        header.setSpacing(8)
        lbl = QLabel("输入单行或多行文件名或路径，实时查看清洗结果、标准编码与匹配规则")
        lbl.setProperty("role", "subtitle")
        header.addWidget(lbl)
        header.addStretch()
        header.addWidget(_compact_btn("填入示例", self._fill_tester_samples))
        self._btn_run_test = _compact_btn("立即解析测试", self.run_parser_test, variant="primary", icon_size=14)
        header.addWidget(self._btn_run_test)
        layout.addLayout(header)

        self._test_input = QPlainTextEdit()
        self._test_input.setMaximumHeight(124)
        self._test_input.setProperty("mono", True)
        self._test_input.setPlaceholderText(
            "例如：\n"
            "[site.example]@ABC-000123_4K_CHS_CD1.mp4\n"
            "ABC-101 / DEF-202 多编码同行测试.mkv\n"
            "D:\\Archive\\DEV-108\\1.mp4\n"
            "document_H264_1080P.mp4"
        )
        apply_slim_scrollbars(self._test_input, theme=theme)
        self._test_input.textChanged.connect(lambda: self._test_timer.start())
        layout.addWidget(self._test_input)

        self._test_notice_label = QLabel("")
        self._test_notice_label.setProperty("badge", "warning")
        self._test_notice_label.setVisible(False)
        layout.addWidget(self._test_notice_label)

        self._test_table = QTableWidget(0, len(_TESTER_SPECS))
        self._test_table.setHorizontalHeaderLabels([s.label for s in _TESTER_SPECS])
        self._test_table.setWordWrap(False)
        self._test_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._test_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        enhance_table_view(
            self._test_table,
            theme=theme,
            on_code_copied=lambda code: self.ctx.notify(f"已复制编码：{code}", "success"),
            row_height=34,
            header_height=34,
        )
        self._tester_header_mgr = TableHeaderManager.attach(
            self._test_table, "settings_tester", _TESTER_SPECS, self.ctx, persist=True
        )
        layout.addWidget(self._test_table, 1)

        self._fill_tester_samples()
        self.run_parser_test()
        return widget

    def _fill_tester_samples(self) -> None:
        self._test_input.setPlainText(
            "ABC-000123_4K_CHS_CD1.mp4\n"
            "PRJ-0042_Part2.mkv\n"
            "ABC-101 / DEF-202.mkv\n"
            "D:\\Data\\DEV-108\\1.mp4\n"
            "100SEC-456_FHD.mkv\n"
            "CAT-2024-0088.mkv\n"
            "report_H264_1080P.mp4"
        )

    def run_parser_test(self) -> None:
        self._test_timer.stop()
        lines = [ln.strip() for ln in self._test_input.toPlainText().splitlines() if ln.strip()]

        p = self._palette()
        success_c, muted_c = QColor(p["success"]), QColor(p["text_muted"])
        mono = get_mono_font(9.5, QFont.Weight.DemiBold)
        rule_names = {r.id: r.name for r in self.ctx.rule_registry.get_all_rules(enabled_only=False)}

        self._test_table.setUpdatesEnabled(False)
        try:
            self._test_table.setRowCount(0)
            if not lines:
                self._test_notice_label.setVisible(False)
                return

            rows_data: list[tuple[str, object]] = []
            truncated = False
            for line in lines:
                target_name, derived_parent, derived_path = _extract_test_line_target(line)
                results = self.ctx.parser.parse_line(
                    target_name,
                    parent_dir=derived_parent,
                    full_path=derived_path,
                    source_type="paste",
                )
                for res in results:
                    if len(rows_data) >= _MAX_TEST_PREVIEW_ROWS:
                        truncated = True
                        break
                    rows_data.append((line, res))
                if truncated:
                    break

            self._test_table.setRowCount(len(rows_data))
            for row_idx, (line, res) in enumerate(rows_data):
                self._test_table.setItem(row_idx, 0, QTableWidgetItem(line))
                self._test_table.setItem(row_idx, 1, QTableWidgetItem(res.cleaned_text or "-"))

                code_item = QTableWidgetItem(res.canonical_code if res.recognized else "（未识别）")
                code_item.setData(Qt.ItemDataRole.UserRole, bool(res.recognized))
                if res.recognized:
                    code_item.setFont(mono)
                    code_item.setForeground(success_c)
                    code_item.setData(ROLE_COPY_TEXT, res.canonical_code)
                else:
                    code_item.setForeground(muted_c)
                self._test_table.setItem(row_idx, 2, code_item)

                self._test_table.setItem(row_idx, 3, QTableWidgetItem(res.prefix or "-"))
                self._test_table.setItem(row_idx, 4, QTableWidgetItem(" / ".join(res.tags) if res.tags else "-"))
                self._test_table.setItem(row_idx, 5, QTableWidgetItem(res.part or "-"))
                self._test_table.setItem(row_idx, 6, QTableWidgetItem(rule_names.get(res.matched_rule, res.matched_rule or "-")))
                note = f"{res.confidence:.2f}" + (f" · {res.note}" if res.note else "")
                self._test_table.setItem(row_idx, 7, QTableWidgetItem(note))
        finally:
            self._test_table.setUpdatesEnabled(True)

        if truncated:
            self._test_notice_label.setText(
                f"输入共 {len(lines):,} 行，实时预览已截断为前 {_MAX_TEST_PREVIEW_ROWS} 条解析结果以保证流畅响应"
            )
            self._test_notice_label.setVisible(True)
        else:
            self._test_notice_label.setVisible(False)

    # ------------------------------------------------------------------
    # Sub-tab 2: 规则管理
    # ------------------------------------------------------------------

    def _build_rules_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)
        theme = self.ctx.config.theme

        tb = QHBoxLayout()
        tb.setSpacing(8)
        lbl = QLabel("规则按优先级数字从小到大执行；双击自定义规则行可直接编辑")
        lbl.setProperty("role", "subtitle")
        tb.addWidget(lbl)
        tb.addStretch()

        self._btn_add_rule = _compact_btn("新增自定义规则", self._add_rule, variant="primary", icon_size=14)
        self._btn_edit_rule = _compact_btn("编辑选中", self._edit_selected_rule, icon_size=14)
        self._btn_del_rule = _compact_btn("删除规则", self._delete_selected_rule, variant="danger-outline", icon_size=14)
        tb.addWidget(self._btn_add_rule)
        tb.addWidget(self._btn_edit_rule)
        tb.addWidget(self._btn_del_rule)
        layout.addLayout(tb)

        self._rules_table = QTableWidget(0, 7)
        self._rules_table.setHorizontalHeaderLabels(
            ["启用", "优先级", "类型", "规则名称", "正则表达式", "格式模板", "示例"]
        )
        self._rules_table.setWordWrap(False)
        self._rules_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._rules_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._rules_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        enhance_table_view(self._rules_table, theme=theme, row_height=34, header_height=34)

        hh = self._rules_table.horizontalHeader()
        hh.setStretchLastSection(True)
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for col, width in ((0, 56), (1, 64), (2, 82), (3, 175), (4, 280), (5, 110)):
            self._rules_table.setColumnWidth(col, width)
        self._rules_table.itemChanged.connect(self._on_rule_check_changed)
        self._rules_table.cellDoubleClicked.connect(self._on_rule_double_clicked)
        layout.addWidget(self._rules_table, 1)

        self._refresh_rules_table()
        return widget

    def _refresh_rules_table(self) -> None:
        p = self._palette()
        secondary_c, accent_c = QColor(p["text_secondary"]), QColor(p["accent"])
        mono = get_mono_font(9.0, QFont.Weight.Normal)

        self._rules_table.blockSignals(True)
        self._rules_table.setRowCount(0)
        for row_idx, rule in enumerate(self.ctx.rule_registry.get_all_rules(enabled_only=False)):
            self._rules_table.insertRow(row_idx)
            chk_item = QTableWidgetItem()
            chk_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            )
            chk_item.setCheckState(Qt.CheckState.Checked if rule.enabled else Qt.CheckState.Unchecked)
            chk_item.setData(Qt.ItemDataRole.UserRole, rule.id)
            chk_item.setData(Qt.ItemDataRole.UserRole + 1, rule.is_builtin)
            self._rules_table.setItem(row_idx, 0, chk_item)

            prio_item = QTableWidgetItem(str(rule.priority))
            prio_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._rules_table.setItem(row_idx, 1, prio_item)

            kind_item = QTableWidgetItem("内置" if rule.is_builtin else "自定义")
            kind_item.setForeground(secondary_c if rule.is_builtin else accent_c)
            self._rules_table.setItem(row_idx, 2, kind_item)

            self._rules_table.setItem(row_idx, 3, QTableWidgetItem(rule.name))

            pat_item = QTableWidgetItem(rule.pattern)
            pat_item.setFont(mono)
            self._rules_table.setItem(row_idx, 4, pat_item)
            self._rules_table.setItem(row_idx, 5, QTableWidgetItem(rule.format_template or "-"))
            self._rules_table.setItem(row_idx, 6, QTableWidgetItem(rule.example or rule.description))
        self._rules_table.blockSignals(False)

    def _on_rule_double_clicked(self, row: int, col: int) -> None:
        if col == 0:
            return
        self._rules_table.selectRow(row)
        rule = self._selected_rule()
        if rule is not None and not rule.is_builtin:
            self._edit_selected_rule()

    def _show_err(self, title: str, summary: str, detail: object) -> None:
        ScrollableDetailDialog(title, summary, str(detail), parent=self).exec()

    def _confirm_danger(self, title: str, summary: str, confirm_label: str) -> bool:
        dlg = ScrollableDetailDialog(
            title=title,
            summary=summary,
            confirm_mode=True,
            confirm_label=confirm_label,
            danger=True,
            parent=self,
        )
        return dlg.exec() == ScrollableDetailDialog.DialogCode.Accepted

    def _make_card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setProperty("card", True)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(8)
        lbl = QLabel(title)
        lbl.setProperty("role", "section")
        lay.addWidget(lbl)
        return card, lay

    def _on_rule_check_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        rule_id = item.data(Qt.ItemDataRole.UserRole)
        if not rule_id:
            return
        try:
            self.ctx.rule_registry.set_rule_enabled(rule_id, item.checkState() == Qt.CheckState.Checked)
            self.ctx.save_rules()
            self.run_parser_test()
        except Exception as exc:
            self._show_err("保存规则状态失败", "切换规则启用状态时发生错误：", exc)
            self._refresh_rules_table()

    def _selected_rule(self) -> Optional[CustomRule]:
        row = self._rules_table.currentRow()
        if row < 0:
            return None
        chk_item = self._rules_table.item(row, 0)
        rule_id = chk_item.data(Qt.ItemDataRole.UserRole) if chk_item is not None else None
        return next((r for r in self.ctx.rule_registry.get_all_rules(enabled_only=False) if r.id == rule_id), None)

    def _add_rule(self) -> None:
        dlg = RuleEditorDialog(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_rule = dlg.build_rule()
        try:
            self.ctx.rule_registry.add_rule(new_rule)
            self.ctx.save_rules()
            self._refresh_rules_table()
            self.run_parser_test()
            self.ctx.notify(f"已添加自定义规则：{new_rule.name}", "success")
        except Exception as exc:
            self._show_err("添加规则失败", "保存自定义规则时发生错误：", exc)

    def _edit_selected_rule(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            self.ctx.notify("请先选择一条规则", "warning")
            return
        if rule.is_builtin:
            self.ctx.notify("内置规则不可修改内容，可勾选启停或新建更高优先级的自定义规则覆盖", "info")
            return
        dlg = RuleEditorDialog(rule=rule, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dlg.build_rule()
        try:
            self.ctx.rule_registry.update_rule(updated)
            self.ctx.save_rules()
            self._refresh_rules_table()
            self.run_parser_test()
            self.ctx.notify(f"已更新规则：{updated.name}", "success")
        except Exception as exc:
            self._show_err("更新规则失败", "保存修改后的规则时发生错误：", exc)

    def _delete_selected_rule(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            self.ctx.notify("请先选择要删除的自定义规则", "warning")
            return
        if rule.is_builtin:
            self.ctx.notify("内置规则不可删除，可取消左侧勾选来禁用", "warning")
            return
        try:
            self.ctx.rule_registry.delete_rule(rule.id)
            self.ctx.save_rules()
            self._refresh_rules_table()
            self.run_parser_test()
            self.ctx.notify(f"已删除自定义规则：{rule.name}", "success")
        except Exception as exc:
            self._show_err("删除规则失败", "删除自定义规则时发生错误：", exc)

    # ------------------------------------------------------------------
    # Sub-tab 3: 常规与备份
    # ------------------------------------------------------------------

    def _build_general_tab(self) -> QWidget:
        theme = self.ctx.config.theme
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        apply_slim_scrollbars(scroll, theme=theme)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 12, 4, 8)
        layout.setSpacing(12)

        # Card 1: 界面与表格偏好
        pref_card, pref_lay = self._make_card("界面与表格偏好")
        pref_row = QHBoxLayout()
        pref_row.setSpacing(12)

        self._chk_show_advanced = QCheckBox("显示进阶字段（版本标签 / 分集 / 匹配规则）")
        self._chk_show_advanced.setChecked(bool(self.ctx.config.show_advanced_fields))
        self._chk_show_advanced.setToolTip(
            "开启后在筛选器、合并策略与属性面板中显示进阶元数据选项（右键表头随时可单独切换列显示）"
        )
        self._chk_show_advanced.toggled.connect(self._on_show_advanced_toggled)
        pref_row.addWidget(self._chk_show_advanced)
        pref_row.addStretch()

        self._btn_reset_layouts = _compact_btn(
            "重置所有表格列布局",
            self._reset_all_table_layouts,
            icon_size=14,
            tooltip="将所有视图的列显示、拖拽顺序、列宽与排序状态恢复为默认设置",
        )
        pref_row.addWidget(self._btn_reset_layouts)
        pref_lay.addLayout(pref_row)
        layout.addWidget(pref_card)

        # Card 2: 伴随扩展名过滤
        ext_card, ext_layout = self._make_card("伴随扩展名过滤（批量导入与扫描时自动忽略辅助文件）")
        self._chk_default_ignore = QCheckBox("默认启用伴随扩展名过滤")
        self._chk_default_ignore.setChecked(self.ctx.config.ignore_companion_files)
        ext_layout.addWidget(self._chk_default_ignore)

        self._ext_editor = QPlainTextEdit()
        self._ext_editor.setMaximumHeight(64)
        self._ext_editor.setProperty("mono", True)
        self._ext_editor.setPlainText(", ".join(self.ctx.config.companion_extensions))
        apply_slim_scrollbars(self._ext_editor, theme=theme)
        ext_layout.addWidget(self._ext_editor)

        ext_btns = QHBoxLayout()
        ext_btns.addWidget(_compact_btn("恢复默认列表", self._reset_companion_exts))
        ext_btns.addStretch()
        ext_btns.addWidget(_compact_btn("保存过滤设置", self._save_companion_exts, variant="primary"))
        ext_layout.addLayout(ext_btns)
        layout.addWidget(ext_card)

        # Card 3: 数据库备份与维护
        db_card, db_layout = self._make_card("数据库备份与维护")
        self._db_path_label = QLabel(f"当前库路径：{self.ctx.db.db_path}")
        self._db_path_label.setProperty("role", "muted")
        self._db_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        db_layout.addWidget(self._db_path_label)

        policy_row = QHBoxLayout()
        policy_row.setSpacing(10)
        self._chk_auto_backup = QCheckBox("删除/清空/恢复前自动创建备份 (.bak)")
        self._chk_auto_backup.setChecked(self.ctx.config.auto_backup)
        policy_row.addWidget(self._chk_auto_backup)

        keep_lbl = QLabel("最多保留份数：")
        keep_lbl.setProperty("role", "subtitle")
        policy_row.addWidget(keep_lbl)

        self._spin_backup_keep = QSpinBox()
        self._spin_backup_keep.setRange(1, 50)
        self._spin_backup_keep.setValue(max(1, int(self.ctx.config.backup_keep_count)))
        self._spin_backup_keep.setFixedWidth(72)
        policy_row.addWidget(self._spin_backup_keep)
        policy_row.addWidget(_compact_btn("保存备份策略", self._save_backup_policy))
        policy_row.addStretch()
        db_layout.addLayout(policy_row)

        db_btns = QHBoxLayout()
        db_btns.setSpacing(6)
        self._btn_backup_now = _compact_btn("立即备份", self._create_backup_now, variant="primary", icon_size=14)
        self._btn_restore = _compact_btn("恢复选中", self._restore_selected_backup)
        self._btn_delete_bak = _compact_btn("删除备份", self._delete_selected_backup)
        self._btn_refresh_bak = _compact_btn("刷新", self._refresh_backups, icon_size=14)
        self._btn_open_dir = _compact_btn("打开目录", self._open_db_folder, icon_size=14)
        self._btn_clear_db = _compact_btn("清空当前库", self._clear_database, variant="danger-outline", icon_size=14)
        for btn in (self._btn_backup_now, self._btn_restore, self._btn_delete_bak, self._btn_refresh_bak, self._btn_open_dir):
            db_btns.addWidget(btn)
        db_btns.addStretch()
        db_btns.addWidget(self._btn_clear_db)
        db_layout.addLayout(db_btns)

        self._backup_stack = QStackedWidget()
        self._backup_stack.setMinimumHeight(168)
        self._backup_empty = EmptyStateWidget(
            "archive", "暂无备份文件", "点击上方「立即备份」，或在删除与清空数据时自动创建", theme=theme, parent=self
        )
        if self._backup_empty.layout() is not None:
            self._backup_empty.layout().setContentsMargins(16, 14, 16, 14)
        self._backup_stack.addWidget(self._backup_empty)

        self._backup_list = QListWidget()
        apply_slim_scrollbars(self._backup_list, theme=theme)
        self._backup_stack.addWidget(self._backup_list)
        db_layout.addWidget(self._backup_stack, 1)

        layout.addWidget(db_card, 1)
        scroll.setWidget(container)
        self._refresh_backups()
        return scroll

    def _on_show_advanced_toggled(self, checked: bool) -> None:
        try:
            self.ctx.config.show_advanced_fields = bool(checked)
            self.ctx.config_manager.save()
            self.ctx.settings_changed.emit()
            self.ctx.notify("已开启进阶字段显示" if checked else "已隐藏进阶字段（保持界面简洁）", "success")
        except Exception as exc:
            self._show_err("保存偏好失败", "写入配置文件时发生错误：", exc)

    def _reset_all_table_layouts(self) -> None:
        try:
            self.ctx.reset_all_table_layouts()
            self.ctx.notify("已重置所有表格的列显示、顺序与列宽布局", "success")
        except Exception as exc:
            self._show_err("重置表格布局失败", "重置表格列布局时发生错误：", exc)

    def _set_db_actions_enabled(self, enabled: bool) -> None:
        for btn in (self._btn_backup_now, self._btn_restore, self._btn_delete_bak, self._btn_refresh_bak, self._btn_clear_db):
            btn.setEnabled(enabled)

    def _reset_companion_exts(self) -> None:
        self._ext_editor.setPlainText(", ".join(DEFAULT_COMPANION_EXTENSIONS))

    def _save_companion_exts(self) -> None:
        try:
            raw = self._ext_editor.toPlainText().replace("\n", ",").replace(";", ",").replace(" ", ",")
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            self.ctx.config.ignore_companion_files = self._chk_default_ignore.isChecked()
            cleaned = self.ctx.config_manager.set_companion_extensions(parts)
            self.ctx.config_manager.save()
            self._ext_editor.setPlainText(", ".join(cleaned))
            self.ctx.settings_changed.emit()
            self.ctx.notify(f"已保存 {len(cleaned)} 个伴随扩展名规则", "success")
        except Exception as exc:
            self._show_err("保存设置失败", "保存伴随扩展名配置时发生错误：", exc)

    def _save_backup_policy(self) -> None:
        try:
            self.ctx.config.auto_backup = self._chk_auto_backup.isChecked()
            self.ctx.config.backup_keep_count = int(self._spin_backup_keep.value())
            if hasattr(self.ctx, "sync_db_backup_settings"):
                self.ctx.sync_db_backup_settings()
            else:
                self.ctx.db.auto_backup = self.ctx.config.auto_backup
                self.ctx.db.backup_keep_count = self.ctx.config.backup_keep_count
            self.ctx.config_manager.save()
            self.ctx.settings_changed.emit()
            status = "开启" if self.ctx.config.auto_backup else "关闭"
            self.ctx.notify(f"已保存备份策略（自动备份：{status}，最多保留 {self.ctx.config.backup_keep_count} 份）", "success")
        except Exception as exc:
            self._show_err("保存备份策略失败", "写入配置文件时发生错误：", exc)

    def _refresh_backups(self) -> None:
        self._backups_dirty = False
        self._db_path_label.setText(f"当前库路径：{self.ctx.db.db_path}")
        self._backup_list.clear()
        try:
            backups = self.ctx.db.list_backups()
        except (DatabaseError, OSError, Exception) as exc:
            self._backup_stack.setCurrentIndex(1)
            item = QListWidgetItem(f"读取备份列表失败：{exc}")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._backup_list.addItem(item)
            return

        if not backups:
            self._backup_stack.setCurrentIndex(0)
            return

        self._backup_stack.setCurrentIndex(1)
        for raw_bak in backups:
            bak = Path(raw_bak)
            try:
                st = bak.stat()
                sz_str = format_file_size(st.st_size)
                mtime_str = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                sz_str, mtime_str = "-", "-"
            item = QListWidgetItem(f"{bak.name}    [{sz_str} · {mtime_str}]")
            item.setData(Qt.ItemDataRole.UserRole, str(bak))
            item.setToolTip(str(bak))
            self._backup_list.addItem(item)

    def _run_db_maintenance_worker(self, fn, busy_msg: str, on_success, err_title: str) -> None:
        if self._is_worker_running(self._db_worker):
            self.ctx.notify("已有数据库维护任务在执行，请稍候...", "warning")
            return
        self._set_db_actions_enabled(False)
        self.ctx.notify(busy_msg, "info")

        worker = WorkerThread(fn, parent=self)
        self._db_worker = worker
        worker.succeeded.connect(on_success)
        worker.failed.connect(lambda detail, t=err_title: self._on_db_maintenance_failed(t, detail))
        worker.finished.connect(lambda w=worker: self._on_db_maintenance_finished(w))
        if not hasattr(worker, "safe_stop"):
            worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_db_maintenance_failed(self, title: str, detail: str) -> None:
        self._set_db_actions_enabled(True)
        self._show_err(title, "操作执行失败，请查看详情：", detail)

    def _on_db_maintenance_finished(self, worker: WorkerThread) -> None:
        if self._db_worker is worker:
            self._db_worker = None
        self._set_db_actions_enabled(True)

    def _create_backup_now(self) -> None:
        def _task() -> Optional[str]:
            bak = self.ctx.db.create_backup(reason="manual")
            return str(bak) if bak else None

        def _done(bak_str: Optional[str]) -> None:
            self._refresh_backups()
            self.ctx.notify(
                f"已创建备份：{Path(bak_str).name}" if bak_str else "当前数据库不支持文件备份（例如内存库）",
                "success" if bak_str else "warning",
            )

        self._run_db_maintenance_worker(_task, "正在后台创建数据库备份...", _done, "创建备份失败")

    def _selected_backup_path(self, warn_msg: str) -> Optional[str]:
        item = self._backup_list.currentItem()
        if item is None:
            self.ctx.notify(warn_msg, "warning")
            return None
        return item.data(Qt.ItemDataRole.UserRole) or None

    def _restore_selected_backup(self) -> None:
        bak_path = self._selected_backup_path("请先在列表中选择一个备份文件")
        if not bak_path:
            return
        if not self._confirm_danger(
            "确认恢复备份",
            f"确定使用备份「{Path(bak_path).name}」覆盖当前数据库吗？（覆盖前会自动留存当前库备份）",
            "确认恢复",
        ):
            return

        def _done(_restored: str) -> None:
            self._refresh_backups()
            self.ctx.data_changed.emit()
            self.ctx.notify("数据库备份已恢复", "success")

        self._run_db_maintenance_worker(
            lambda: (self.ctx.db.restore_backup(bak_path), str(bak_path))[1],
            "正在后台恢复数据库备份...",
            _done,
            "恢复备份失败",
        )

    def _delete_selected_backup(self) -> None:
        bak_path = self._selected_backup_path("请先在列表中选择要删除的备份文件")
        if not bak_path:
            return
        p = Path(bak_path)
        if not self._confirm_danger(
            "确认删除备份文件",
            f"确定永久删除备份文件「{p.name}」吗？（不会影响当前数据库数据）",
            "确认删除备份",
        ):
            return
        try:
            if p.is_file():
                p.unlink()
            self._refresh_backups()
            self.ctx.notify(f"已删除备份：{p.name}", "success")
        except OSError as exc:
            self._show_err("删除备份失败", f"无法删除 {p.name}：", exc)

    def _open_db_folder(self) -> None:
        if self.ctx.db.is_memory:
            self.ctx.notify("当前为内存数据库，无本地目录", "info")
            return
        folder = Path(self.ctx.db.db_path).parent
        if not folder.is_dir():
            self.ctx.notify("未找到数据库所在目录", "warning")
            return
        try:
            if os.name == "nt":
                os.startfile(os.path.normpath(str(folder)))
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        except OSError as exc:
            self.ctx.notify(f"打开目录失败：{exc}", "error")

    def _clear_database(self) -> None:
        try:
            stats = self.ctx.db.get_stats()
        except Exception as exc:
            self._show_err("读取统计失败", "查询数据库状态时发生错误：", exc)
            return
        if stats.total_records == 0:
            self.ctx.notify("当前数据库已经是空的", "info")
            return
        if not self._confirm_danger(
            "确认清空数据库",
            f"即将清空当前数据库中的全部 {stats.total_records:,} 条记录（清空前将自动生成 .bak 备份）。是否继续？",
            "确认清空",
        ):
            return

        def _done(_res) -> None:
            self._refresh_backups()
            self.ctx.data_changed.emit()
            self.ctx.notify("当前数据库已清空（已自动备份）", "success")

        self._run_db_maintenance_worker(
            self.ctx.db.clear_all_records, "正在后台清空数据库（含自动备份）...", _done, "清空数据库失败"
        )
