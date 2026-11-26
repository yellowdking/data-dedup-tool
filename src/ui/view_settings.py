"""View 5: 规则测试 & 设置 — Live parser tester, custom regex editor, extensions & DB backups."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.config import DEFAULT_COMPANION_EXTENSIONS
from src.core.models import CustomRule
from src.core.rules import RuleRegistry
from src.ui.app_context import AppContext
from src.ui.widgets import ScrollableDetailDialog


class RuleEditorDialog(QDialog):
    """Modal dialog for creating or editing a custom regex extraction rule with live test."""

    def __init__(
        self,
        rule: Optional[CustomRule] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑自定义规则" if rule else "新增自定义正则规则")
        self.resize(560, 420)
        self._orig_rule = rule

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)

        self._name = QLineEdit(rule.name if rule else "")
        self._name.setPlaceholderText("例如：内部项目归档编码")
        form.addRow("规则名称：", self._name)

        self._pattern = QLineEdit(rule.pattern if rule else "")
        self._pattern.setPlaceholderText(r"例如：(?<![A-Za-z0-9])(PRJ)[-_]?(\d{4,6})(?![0-9])")
        self._pattern.textChanged.connect(self._live_validate)
        form.addRow("正则表达式：", self._pattern)

        self._template = QLineEdit(rule.format_template if rule else "")
        self._template.setPlaceholderText("可选，例如：{1}-{2}（留空则自动拼接捕获组）")
        self._template.textChanged.connect(self._live_validate)
        form.addRow("格式化模板：", self._template)

        self._priority = QSpinBox()
        self._priority.setRange(1, 99)
        self._priority.setValue(rule.priority if rule else 5)
        self._priority.setToolTip("数字越小优先级越高（自定义规则默认 5，优先于内置规则 10~70）")
        form.addRow("优先级 (1-99)：", self._priority)

        self._desc = QLineEdit(rule.description if rule else "")
        self._desc.setPlaceholderText("规则说明（可选）")
        form.addRow("说明：", self._desc)

        self._example = QLineEdit(rule.example if rule else "")
        self._example.setPlaceholderText("测试样例，例如：PRJ_0042_4K.mkv")
        self._example.textChanged.connect(self._live_validate)
        form.addRow("实时测试样例：", self._example)

        layout.addLayout(form)

        self._validation_label = QLabel("输入正则表达式与测试样例可即时验证匹配效果")
        self._validation_label.setWordWrap(True)
        self._validation_label.setProperty("role", "subtitle")
        layout.addWidget(self._validation_label)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        self._ok_btn = QPushButton("保存规则")
        self._ok_btn.setProperty("variant", "primary")
        self._ok_btn.clicked.connect(self._on_accept)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self._ok_btn)
        layout.addLayout(btn_row)

        self._live_validate()

    def _live_validate(self) -> None:
        pattern = self._pattern.text().strip()
        if not pattern:
            self._validation_label.setText("请输入正则表达式")
            return
        ok, err = RuleRegistry.validate_pattern(pattern)
        if not ok:
            self._validation_label.setText(f"❌ {err}")
            return
        sample = self._example.text().strip()
        if sample:
            import re

            m = re.search(pattern, sample, re.IGNORECASE)
            if m:
                tmp_rule = CustomRule(
                    id="preview",
                    name="preview",
                    pattern=pattern,
                    format_template=self._template.text().strip(),
                )
                canonical, prefix, number = RuleRegistry.format_custom_match(tmp_rule, m)
                self._validation_label.setText(
                    f"✅ 匹配成功：标准编码 = {canonical}（前缀={prefix}，编号={number}）"
                )
            else:
                self._validation_label.setText("⚠️ 正则语法有效，但当前测试样例未匹配")
        else:
            self._validation_label.setText("✅ 正则语法有效")

    def _on_accept(self) -> None:
        name = self._name.text().strip()
        pattern = self._pattern.text().strip()
        if not name:
            self._validation_label.setText("❌ 请填写规则名称")
            return
        ok, err = RuleRegistry.validate_pattern(pattern)
        if not ok:
            self._validation_label.setText(f"❌ {err}")
            return
        self.accept()

    def build_rule(self) -> CustomRule:
        return CustomRule(
            id=self._orig_rule.id if self._orig_rule else "",
            name=self._name.text().strip(),
            pattern=self._pattern.text().strip(),
            format_template=self._template.text().strip(),
            priority=self._priority.value(),
            enabled=self._orig_rule.enabled if self._orig_rule else True,
            is_builtin=False,
            category="自定义",
            description=self._desc.text().strip(),
            example=self._example.text().strip(),
        )


class SettingsView(QWidget):
    """View 5: Live rule tester, custom regex rules manager, companion extensions & DB backups."""

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._setup_ui()
        self.ctx.db_switched.connect(lambda _: self._refresh_backups())

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(10)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_tester_tab(), "实时规则测试器")
        self._tabs.addTab(self._build_rules_tab(), "自定义正则规则")
        self._tabs.addTab(self._build_general_tab(), "伴随扩展名与数据库维护")
        root.addWidget(self._tabs, 1)

    # ------------------------------------------------------------------
    # Sub-tab 1: Live parser tester
    # ------------------------------------------------------------------

    def _build_tester_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        header = QHBoxLayout()
        lbl = QLabel("输入单行或多行文件名/路径，实时查看清洗结果、匹配规则、标准编码、标签与分集")
        lbl.setProperty("role", "section")
        header.addWidget(lbl)
        header.addStretch()

        btn_sample = QPushButton("填入示例样例")
        btn_sample.clicked.connect(self._fill_tester_samples)
        header.addWidget(btn_sample)

        btn_run = QPushButton("立即解析测试")
        btn_run.setProperty("variant", "primary")
        btn_run.clicked.connect(self.run_parser_test)
        header.addWidget(btn_run)
        layout.addLayout(header)

        self._test_input = QPlainTextEdit()
        self._test_input.setMaximumHeight(135)
        self._test_input.setPlaceholderText(
            "例如：\n"
            "[site.example]@ABC-000123_4K_CHS_CD1.mp4\n"
            "118prj00420.mkv\n"
            "D:\\Archive\\DEV-108\\1.mp4\n"
            "document_H264_1080P.mp4"
        )
        self._test_input.textChanged.connect(self.run_parser_test)
        layout.addWidget(self._test_input)

        self._test_table = QTableWidget(0, 8)
        self._test_table.setHorizontalHeaderLabels(
            ["原始输入", "清洗后文本", "标准编码", "前缀", "版本标签", "分集", "匹配规则", "置信度 / 备注"]
        )
        self._test_table.setAlternatingRowColors(True)
        self._test_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._test_table.verticalHeader().setVisible(False)
        self._test_table.horizontalHeader().setStretchLastSection(True)
        self._test_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._test_table.setColumnWidth(0, 220)
        self._test_table.setColumnWidth(1, 180)
        self._test_table.setColumnWidth(2, 130)
        self._test_table.setColumnWidth(3, 80)
        self._test_table.setColumnWidth(4, 110)
        self._test_table.setColumnWidth(5, 70)
        self._test_table.setColumnWidth(6, 150)
        layout.addWidget(self._test_table, 1)

        self._fill_tester_samples()
        return widget

    def _fill_tester_samples(self) -> None:
        self._test_input.setPlainText(
            "ABC-000123_4K_CHS_CD1.mp4\n"
            "PRJ-0042_Part2.mkv\n"
            "D:\\Data\\DEV-108\\1.mp4\n"
            "100SEC-456_FHD.mkv\n"
            "CAT-2024-0088.mkv\n"
            "report_H264_1080P.mp4"
        )

    def run_parser_test(self) -> None:
        text = self._test_input.toPlainText()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        self._test_table.setRowCount(0)
        if not lines:
            return

        mono = QFont("Consolas", 10)
        mono.setBold(True)

        row_idx = 0
        for line in lines:
            res = self.ctx.parser.parse_single(line)
            self._test_table.insertRow(row_idx)

            self._test_table.setItem(row_idx, 0, QTableWidgetItem(line))
            self._test_table.setItem(row_idx, 1, QTableWidgetItem(res.cleaned_text or "-"))

            code_item = QTableWidgetItem(res.canonical_code if res.recognized else "（未识别）")
            if res.recognized:
                code_item.setFont(mono)
                code_item.setForeground(QColor("#059669"))
            else:
                code_item.setForeground(QColor("#94A3B8"))
            self._test_table.setItem(row_idx, 2, code_item)

            self._test_table.setItem(row_idx, 3, QTableWidgetItem(res.prefix or "-"))
            self._test_table.setItem(
                row_idx, 4, QTableWidgetItem(" / ".join(res.tags) if res.tags else "-")
            )
            self._test_table.setItem(row_idx, 5, QTableWidgetItem(res.part or "-"))
            self._test_table.setItem(row_idx, 6, QTableWidgetItem(res.matched_rule or "-"))
            note = f"{res.confidence:.2f}" + (f" · {res.note}" if res.note else "")
            self._test_table.setItem(row_idx, 7, QTableWidgetItem(note))
            row_idx += 1

    # ------------------------------------------------------------------
    # Sub-tab 2: Custom regex rule editor
    # ------------------------------------------------------------------

    def _build_rules_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        tb = QHBoxLayout()
        lbl = QLabel("规则按优先级数字从小到大执行；自定义规则默认优先级 5，优先于内置规则")
        lbl.setProperty("role", "subtitle")
        tb.addWidget(lbl)
        tb.addStretch()

        btn_add = QPushButton("+ 新增自定义规则")
        btn_add.setProperty("variant", "primary")
        btn_add.clicked.connect(self._add_rule)
        tb.addWidget(btn_add)

        btn_edit = QPushButton("编辑选中")
        btn_edit.clicked.connect(self._edit_selected_rule)
        tb.addWidget(btn_edit)

        btn_del = QPushButton("删除自定义规则")
        btn_del.setProperty("variant", "danger")
        btn_del.clicked.connect(self._delete_selected_rule)
        tb.addWidget(btn_del)
        layout.addLayout(tb)

        self._rules_table = QTableWidget(0, 7)
        self._rules_table.setHorizontalHeaderLabels(
            ["启用", "优先级", "类型", "规则名称", "正则表达式", "格式模板", "示例"]
        )
        self._rules_table.setAlternatingRowColors(True)
        self._rules_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._rules_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._rules_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._rules_table.verticalHeader().setVisible(False)
        self._rules_table.horizontalHeader().setStretchLastSection(True)
        self._rules_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._rules_table.setColumnWidth(0, 56)
        self._rules_table.setColumnWidth(1, 64)
        self._rules_table.setColumnWidth(2, 90)
        self._rules_table.setColumnWidth(3, 175)
        self._rules_table.setColumnWidth(4, 280)
        self._rules_table.setColumnWidth(5, 110)
        self._rules_table.itemChanged.connect(self._on_rule_check_changed)
        layout.addWidget(self._rules_table, 1)

        self._refresh_rules_table()
        return widget

    def _refresh_rules_table(self) -> None:
        self._rules_table.blockSignals(True)
        self._rules_table.setRowCount(0)
        rules = self.ctx.rule_registry.get_all_rules(enabled_only=False)
        for row_idx, rule in enumerate(rules):
            self._rules_table.insertRow(row_idx)
            chk_item = QTableWidgetItem()
            chk_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            chk_item.setCheckState(Qt.CheckState.Checked if rule.enabled else Qt.CheckState.Unchecked)
            chk_item.setData(Qt.ItemDataRole.UserRole, rule.id)
            chk_item.setData(Qt.ItemDataRole.UserRole + 1, rule.is_builtin)
            self._rules_table.setItem(row_idx, 0, chk_item)

            self._rules_table.setItem(row_idx, 1, QTableWidgetItem(str(rule.priority)))
            kind_item = QTableWidgetItem("内置" if rule.is_builtin else "自定义")
            kind_item.setForeground(QColor("#64748B") if rule.is_builtin else QColor("#2563EB"))
            self._rules_table.setItem(row_idx, 2, kind_item)
            self._rules_table.setItem(row_idx, 3, QTableWidgetItem(rule.name))
            self._rules_table.setItem(row_idx, 4, QTableWidgetItem(rule.pattern))
            self._rules_table.setItem(row_idx, 5, QTableWidgetItem(rule.format_template or "-"))
            self._rules_table.setItem(row_idx, 6, QTableWidgetItem(rule.example or rule.description))
        self._rules_table.blockSignals(False)

    def _on_rule_check_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        rule_id = item.data(Qt.ItemDataRole.UserRole)
        if not rule_id:
            return
        enabled = item.checkState() == Qt.CheckState.Checked
        self.ctx.rule_registry.set_rule_enabled(rule_id, enabled)
        self.ctx.save_rules()
        self.run_parser_test()

    def _selected_rule(self) -> Optional[CustomRule]:
        row = self._rules_table.currentRow()
        if row < 0:
            return None
        chk_item = self._rules_table.item(row, 0)
        if chk_item is None:
            return None
        rule_id = chk_item.data(Qt.ItemDataRole.UserRole)
        for r in self.ctx.rule_registry.get_all_rules(enabled_only=False):
            if r.id == rule_id:
                return r
        return None

    def _add_rule(self) -> None:
        dlg = RuleEditorDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_rule = dlg.build_rule()
            self.ctx.rule_registry.add_rule(new_rule)
            self.ctx.save_rules()
            self._refresh_rules_table()
            self.run_parser_test()
            self.ctx.notify(f"已添加自定义规则：{new_rule.name}", "success")

    def _edit_selected_rule(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            self.ctx.notify("请先选择一条规则", "warning")
            return
        if rule.is_builtin:
            self.ctx.notify("内置规则不可修改内容，但可勾选启用/禁用或新建更高优先级的自定义规则覆盖", "info")
            return
        dlg = RuleEditorDialog(rule=rule, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            updated = dlg.build_rule()
            self.ctx.rule_registry.update_rule(updated)
            self.ctx.save_rules()
            self._refresh_rules_table()
            self.run_parser_test()
            self.ctx.notify(f"已更新规则：{updated.name}", "success")

    def _delete_selected_rule(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            self.ctx.notify("请先选择要删除的自定义规则", "warning")
            return
        if rule.is_builtin:
            self.ctx.notify("内置规则不可删除，可取消左侧勾选来禁用", "warning")
            return
        self.ctx.rule_registry.delete_rule(rule.id)
        self.ctx.save_rules()
        self._refresh_rules_table()
        self.run_parser_test()
        self.ctx.notify(f"已删除自定义规则：{rule.name}", "success")

    # ------------------------------------------------------------------
    # Sub-tab 3: Companion extensions & DB backup management
    # ------------------------------------------------------------------

    def _build_general_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(14)

        # Companion extensions card
        ext_card = QFrame()
        ext_card.setProperty("card", True)
        ext_layout = QVBoxLayout(ext_card)
        ext_layout.setContentsMargins(16, 14, 16, 14)
        ext_layout.setSpacing(8)

        ext_title = QLabel("伴随扩展名过滤配置（批量导入/扫描时自动忽略非主体辅助文件）")
        ext_title.setProperty("role", "section")
        ext_layout.addWidget(ext_title)

        self._chk_default_ignore = QCheckBox("默认启用伴随扩展名过滤")
        self._chk_default_ignore.setChecked(self.ctx.config.ignore_companion_files)
        ext_layout.addWidget(self._chk_default_ignore)

        self._ext_editor = QPlainTextEdit()
        self._ext_editor.setMaximumHeight(82)
        self._ext_editor.setPlainText(", ".join(self.ctx.config.companion_extensions))
        ext_layout.addWidget(self._ext_editor)

        ext_btns = QHBoxLayout()
        btn_reset_ext = QPushButton("恢复默认列表")
        btn_reset_ext.clicked.connect(self._reset_companion_exts)
        ext_btns.addWidget(btn_reset_ext)
        ext_btns.addStretch()
        btn_save_ext = QPushButton("保存过滤设置")
        btn_save_ext.setProperty("variant", "primary")
        btn_save_ext.clicked.connect(self._save_companion_exts)
        ext_btns.addWidget(btn_save_ext)
        ext_layout.addLayout(ext_btns)
        layout.addWidget(ext_card)

        # Database backup & maintenance card
        db_card = QFrame()
        db_card.setProperty("card", True)
        db_layout = QVBoxLayout(db_card)
        db_layout.setContentsMargins(16, 14, 16, 14)
        db_layout.setSpacing(10)

        db_title = QLabel("数据库备份与维护")
        db_title.setProperty("role", "section")
        db_layout.addWidget(db_title)

        self._db_path_label = QLabel(f"当前库路径：{self.ctx.db.db_path}")
        self._db_path_label.setProperty("role", "subtitle")
        self._db_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        db_layout.addWidget(self._db_path_label)

        db_btns = QHBoxLayout()
        btn_backup_now = QPushButton("立即创建备份 (.bak)")
        btn_backup_now.setProperty("variant", "primary")
        btn_backup_now.clicked.connect(self._create_backup_now)
        db_btns.addWidget(btn_backup_now)

        btn_restore = QPushButton("恢复选中备份")
        btn_restore.clicked.connect(self._restore_selected_backup)
        db_btns.addWidget(btn_restore)

        btn_open_dir = QPushButton("打开数据库目录")
        btn_open_dir.clicked.connect(self._open_db_folder)
        db_btns.addWidget(btn_open_dir)

        db_btns.addStretch()

        btn_clear_db = QPushButton("清空当前库全部记录")
        btn_clear_db.setProperty("variant", "danger")
        btn_clear_db.clicked.connect(self._clear_database)
        db_btns.addWidget(btn_clear_db)
        db_layout.addLayout(db_btns)

        self._backup_list = QListWidget()
        db_layout.addWidget(self._backup_list, 1)

        layout.addWidget(db_card, 1)
        self._refresh_backups()
        return widget

    def _reset_companion_exts(self) -> None:
        self._ext_editor.setPlainText(", ".join(DEFAULT_COMPANION_EXTENSIONS))

    def _save_companion_exts(self) -> None:
        raw = self._ext_editor.toPlainText().replace("\n", ",").replace(";", ",").replace(" ", ",")
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        self.ctx.config.ignore_companion_files = self._chk_default_ignore.isChecked()
        cleaned = self.ctx.config_manager.set_companion_extensions(parts)
        self.ctx.config_manager.save()
        self._ext_editor.setPlainText(", ".join(cleaned))
        self.ctx.settings_changed.emit()
        self.ctx.notify(f"已保存 {len(cleaned)} 个伴随扩展名规则", "success")

    def _refresh_backups(self) -> None:
        self._db_path_label.setText(f"当前库路径：{self.ctx.db.db_path}")
        self._backup_list.clear()
        backups = self.ctx.db.list_backups()
        if not backups:
            item = QListWidgetItem("暂无备份文件（每次批量删除或点击上方按钮时会自动生成）")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._backup_list.addItem(item)
            return
        for raw_bak in backups:
            bak = Path(raw_bak)
            sz_kb = bak.stat().st_size / 1024 if bak.exists() else 0
            item = QListWidgetItem(f"{bak.name}   ({sz_kb:.1f} KB)")
            item.setData(Qt.ItemDataRole.UserRole, str(bak))
            self._backup_list.addItem(item)

    def _create_backup_now(self) -> None:
        bak = self.ctx.db.create_backup(reason="manual")
        self._refresh_backups()
        if bak:
            self.ctx.notify(f"已创建备份：{Path(bak).name}", "success")

    def _restore_selected_backup(self) -> None:
        item = self._backup_list.currentItem()
        if item is None:
            self.ctx.notify("请先在列表中选择一个备份文件", "warning")
            return
        bak_path = item.data(Qt.ItemDataRole.UserRole)
        if not bak_path:
            return
        dlg = ScrollableDetailDialog(
            title="确认恢复备份",
            summary=f"确定使用备份「{Path(bak_path).name}」覆盖当前数据库吗？（覆盖前会自动留存当前库备份）",
            confirm_mode=True,
            confirm_label="确认恢复",
            danger=True,
            parent=self,
        )
        if dlg.exec() == ScrollableDetailDialog.DialogCode.Accepted:
            self.ctx.db.restore_backup(bak_path)
            self._refresh_backups()
            self.ctx.data_changed.emit()
            self.ctx.notify("数据库备份已恢复", "success")

    def _open_db_folder(self) -> None:
        folder = Path(self.ctx.db.db_path).parent
        if folder.exists() and os.name == "nt":
            os.startfile(str(folder))

    def _clear_database(self) -> None:
        stats = self.ctx.db.get_stats()
        if stats.total_records == 0:
            self.ctx.notify("当前数据库已经是空的", "info")
            return
        dlg = ScrollableDetailDialog(
            title="确认清空数据库",
            summary=f"即将清空当前数据库中的全部 {stats.total_records:,} 条记录（清空前将自动生成 .bak 备份）。是否继续？",
            confirm_mode=True,
            confirm_label="确认清空",
            danger=True,
            parent=self,
        )
        if dlg.exec() == ScrollableDetailDialog.DialogCode.Accepted:
            self.ctx.db.clear_all_records()
            self._refresh_backups()
            self.ctx.data_changed.emit()
            self.ctx.notify("当前数据库已清空（已自动备份）", "success")
