"""View 5: 规则测试 & 设置 — Live parser tester, custom regex editor, extensions & DB backups."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont
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
from src.core.database import DatabaseError
from src.core.models import CustomRule
from src.core.normalizer import clean_text, split_known_extension
from src.core.rules import RuleRegistry
from src.ui.app_context import AppContext
from src.ui.theme import get_palette
from src.ui.widgets import ScrollableDetailDialog, WorkerThread, format_file_size

_MAX_TEST_PREVIEW_ROWS: int = 500

# Defense-in-depth detector for nested quantifiers such as (a+)+, (.*)*, (\w+){2,}
_RE_NESTED_QUANTIFIER = re.compile(
    r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\s*\d+\s*,)"
)


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


def _validate_custom_regex_safe(pattern: str) -> tuple[bool, str]:
    """Validate regex syntax, reject dangerous nested quantifiers, and reject empty matches."""
    ok, err = RuleRegistry.validate_pattern(pattern)
    if not ok:
        return False, err
    if _RE_NESTED_QUANTIFIER.search(pattern):
        return False, "检测到嵌套量词（如 (a+)+），存在回溯卡顿风险，请改写为非嵌套正则"
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return False, f"正则表达式语法错误: {exc}"
    if compiled.search("") is not None:
        return False, "正则表达式不能匹配空字符串（如 a*、.*），请至少要求匹配 1 个有效字符"
    return True, ""


def _extract_test_line_target(line: str) -> tuple[str, str, str]:
    """Extract `(target_name, parent_dir, full_path)` matching CodeParser.parse_single path logic."""
    s = line.strip()
    is_path = (
        bool(re.match(r"^[A-Za-z]:[\\/]", s))
        or s.startswith("\\\\")
        or s.startswith("/")
        or ("\\" in s and " \\ " not in s)
        or (
            "/" in s
            and " / " not in s
            and not re.match(r"^https?://", s, re.IGNORECASE)
            and bool(split_known_extension(s.rsplit("/", 1)[-1])[1])
        )
    )
    if is_path:
        norm_p = s.replace("\\", "/").rstrip("/")
        parts = [p for p in norm_p.split("/") if p]
        if len(parts) >= 2:
            return parts[-1], parts[-2], s
    return s, "", ""


class RuleEditorDialog(QDialog):
    """Modal dialog for creating or editing a custom regex extraction rule with debounced live test."""

    def __init__(
        self,
        rule: Optional[CustomRule] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑自定义规则" if rule else "新增自定义正则规则")
        self.resize(580, 440)
        self._orig_rule = rule

        self._validate_timer = QTimer(self)
        self._validate_timer.setSingleShot(True)
        self._validate_timer.setInterval(250)
        self._validate_timer.timeout.connect(self._live_validate)

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
        self._pattern.textChanged.connect(lambda _: self._validate_timer.start())
        form.addRow("正则表达式：", self._pattern)

        self._template = QLineEdit(rule.format_template if rule else "")
        self._template.setPlaceholderText("可选，例如：{1}-{2}（留空则自动拼接捕获组）")
        self._template.textChanged.connect(lambda _: self._validate_timer.start())
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
        self._example.textChanged.connect(lambda _: self._validate_timer.start())
        form.addRow("实时测试样例：", self._example)

        layout.addLayout(form)

        self._validation_label = QLabel("输入正则表达式与测试样例可即时验证匹配效果（按实际清洗后文本匹配）")
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

    def done(self, result: int) -> None:
        self._validate_timer.stop()
        super().done(result)

    def _live_validate(self) -> None:
        pattern = self._pattern.text().strip()
        if not pattern:
            self._validation_label.setText("请输入正则表达式")
            return
        ok, err = _validate_custom_regex_safe(pattern)
        if not ok:
            self._validation_label.setText(f"❌ {err}")
            return

        sample = self._example.text().strip()
        if sample:
            cleaned = clean_text(sample)
            cleaned_hint = f"\n（实际送入引擎的清洗后文本：{cleaned or '（空）'}）" if cleaned != sample else ""
            if not cleaned:
                self._validation_label.setText(
                    f"⚠️ 样例经预清洗后为空，实际解析时将无法命中{cleaned_hint}"
                )
                return
            m = re.search(pattern, cleaned, re.IGNORECASE)
            if m:
                if not m.group(0):
                    self._validation_label.setText(
                        f"❌ 匹配结果为空字符串，请调整正则表达式{cleaned_hint}"
                    )
                    return
                tmp_rule = CustomRule(
                    id="preview",
                    name="preview",
                    pattern=pattern,
                    format_template=self._template.text().strip(),
                )
                canonical, prefix, number = RuleRegistry.format_custom_match(tmp_rule, m)
                if not canonical.strip():
                    self._validation_label.setText(
                        f"❌ 格式化后的标准编码为空，请检查正则表达式捕获组或格式模板{cleaned_hint}"
                    )
                    return
                self._validation_label.setText(
                    f"✅ 匹配成功：标准编码 = {canonical}（前缀={prefix}，编号={number}）{cleaned_hint}"
                )
            else:
                self._validation_label.setText(
                    f"⚠️ 正则语法有效，但未命中清洗后的测试样例{cleaned_hint}"
                )
        else:
            self._validation_label.setText("✅ 正则语法有效（建议输入测试样例验证清洗后匹配效果）")

    def _on_accept(self) -> None:
        self._validate_timer.stop()
        name = self._name.text().strip()
        pattern = self._pattern.text().strip()
        if not name:
            self._validation_label.setText("❌ 请填写规则名称")
            self._name.setFocus()
            return
        ok, err = _validate_custom_regex_safe(pattern)
        if not ok:
            self._validation_label.setText(f"❌ {err}")
            self._pattern.setFocus()
            return

        # If user provided a sample and it matches, verify formatted output is non-empty
        sample = self._example.text().strip()
        if sample:
            cleaned = clean_text(sample)
            if cleaned:
                m = re.search(pattern, cleaned, re.IGNORECASE)
                if m:
                    tmp_rule = CustomRule(
                        id="preview",
                        name=name,
                        pattern=pattern,
                        format_template=self._template.text().strip(),
                    )
                    canonical, _, _ = RuleRegistry.format_custom_match(tmp_rule, m)
                    if not m.group(0) or not canonical.strip():
                        self._validation_label.setText("❌ 测试样例匹配产出空编码，请修正正则或模板")
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
        self._backups_dirty: bool = False
        self._db_worker: Optional[WorkerThread] = None

        self._test_timer = QTimer(self)
        self._test_timer.setSingleShot(True)
        self._test_timer.setInterval(200)
        self._test_timer.timeout.connect(self.run_parser_test)

        self._setup_ui()
        self.ctx.db_switched.connect(self._on_db_switched)
        self.ctx.data_changed.connect(self._on_data_changed)
        self.ctx.settings_changed.connect(self._sync_settings_ui)
        self.ctx.theme_changed.connect(self.apply_theme)

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
    # Lifecycle, lazy refresh & theme
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self._test_timer.stop()
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

    def _on_db_switched(self, _db_path: str) -> None:
        if not self.isVisible():
            self._backups_dirty = True
            return
        self._refresh_backups()

    def _on_data_changed(self) -> None:
        if not self.isVisible():
            self._backups_dirty = True
            return
        self._refresh_backups()

    def _sync_settings_ui(self) -> None:
        self._chk_default_ignore.blockSignals(True)
        self._chk_default_ignore.setChecked(self.ctx.config.ignore_companion_files)
        self._chk_default_ignore.blockSignals(False)

        self._chk_auto_backup.blockSignals(True)
        self._chk_auto_backup.setChecked(self.ctx.config.auto_backup)
        self._chk_auto_backup.blockSignals(False)

        self._spin_backup_keep.blockSignals(True)
        self._spin_backup_keep.setValue(max(1, int(self.ctx.config.backup_keep_count)))
        self._spin_backup_keep.blockSignals(False)

    def _palette(self, theme: Optional[str] = None) -> dict[str, str]:
        return get_palette(theme or self.ctx.config.theme)

    def apply_theme(self, theme: str) -> None:
        p = self._palette(theme)
        success_color = QColor(p["success"])
        muted_color = QColor(p["text_muted"])
        secondary_color = QColor(p["text_secondary"])
        accent_color = QColor(p["accent"])

        # Re-color parser tester table
        for row in range(self._test_table.rowCount()):
            code_item = self._test_table.item(row, 2)
            if code_item is not None:
                is_rec = bool(code_item.data(Qt.ItemDataRole.UserRole))
                code_item.setForeground(success_color if is_rec else muted_color)

        # Re-color rules table
        for row in range(self._rules_table.rowCount()):
            chk_item = self._rules_table.item(row, 0)
            kind_item = self._rules_table.item(row, 2)
            if chk_item is not None and kind_item is not None:
                is_builtin = bool(chk_item.data(Qt.ItemDataRole.UserRole + 1))
                kind_item.setForeground(secondary_color if is_builtin else accent_color)

    def _is_worker_running(self, worker: Optional[WorkerThread]) -> bool:
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            return False

    # ------------------------------------------------------------------
    # Sub-tab 1: Live parser tester (debounced, capped, multi-code aware)
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
            "ABC-101 / DEF-202 多编码同行测试.mkv\n"
            "D:\\Archive\\DEV-108\\1.mp4\n"
            "document_H264_1080P.mp4"
        )
        self._test_input.textChanged.connect(lambda: self._test_timer.start())
        layout.addWidget(self._test_input)

        self._test_notice_label = QLabel("")
        self._test_notice_label.setProperty("role", "subtitle")
        self._test_notice_label.setVisible(False)
        layout.addWidget(self._test_notice_label)

        self._test_table = QTableWidget(0, 8)
        self._test_table.setHorizontalHeaderLabels(
            ["原始输入", "清洗后文本", "标准编码", "前缀", "版本标签", "分集", "匹配规则", "置信度 / 备注"]
        )
        self._test_table.setAlternatingRowColors(True)
        self._test_table.setWordWrap(False)
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
        text = self._test_input.toPlainText()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

        p = self._palette()
        success_color = QColor(p["success"])
        muted_color = QColor(p["text_muted"])
        mono = QFont("Consolas", 10)
        mono.setBold(True)

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
                    code_item.setForeground(success_color)
                else:
                    code_item.setForeground(muted_color)
                self._test_table.setItem(row_idx, 2, code_item)

                self._test_table.setItem(row_idx, 3, QTableWidgetItem(res.prefix or "-"))
                self._test_table.setItem(
                    row_idx, 4, QTableWidgetItem(" / ".join(res.tags) if res.tags else "-")
                )
                self._test_table.setItem(row_idx, 5, QTableWidgetItem(res.part or "-"))
                self._test_table.setItem(row_idx, 6, QTableWidgetItem(res.matched_rule or "-"))
                note = f"{res.confidence:.2f}" + (f" · {res.note}" if res.note else "")
                self._test_table.setItem(row_idx, 7, QTableWidgetItem(note))
        finally:
            self._test_table.setUpdatesEnabled(True)

        if truncated:
            self._test_notice_label.setText(
                f"⚠️ 输入共 {len(lines):,} 行，实时预览已截断为前 {_MAX_TEST_PREVIEW_ROWS} 条解析结果以保证流畅响应"
            )
            self._test_notice_label.setVisible(True)
        else:
            self._test_notice_label.setVisible(False)

    # ------------------------------------------------------------------
    # Sub-tab 2: Custom regex rule editor
    # ------------------------------------------------------------------

    def _build_rules_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        tb = QHBoxLayout()
        lbl = QLabel("规则按优先级数字从小到大执行；双击自定义规则行可直接编辑")
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
        self._rules_table.setWordWrap(False)
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
        self._rules_table.cellDoubleClicked.connect(self._on_rule_double_clicked)
        layout.addWidget(self._rules_table, 1)

        self._refresh_rules_table()
        return widget

    def _refresh_rules_table(self) -> None:
        p = self._palette()
        secondary_color = QColor(p["text_secondary"])
        accent_color = QColor(p["accent"])

        self._rules_table.blockSignals(True)
        self._rules_table.setRowCount(0)
        rules = self.ctx.rule_registry.get_all_rules(enabled_only=False)
        for row_idx, rule in enumerate(rules):
            self._rules_table.insertRow(row_idx)
            chk_item = QTableWidgetItem()
            chk_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            )
            chk_item.setCheckState(Qt.CheckState.Checked if rule.enabled else Qt.CheckState.Unchecked)
            chk_item.setData(Qt.ItemDataRole.UserRole, rule.id)
            chk_item.setData(Qt.ItemDataRole.UserRole + 1, rule.is_builtin)
            self._rules_table.setItem(row_idx, 0, chk_item)

            self._rules_table.setItem(row_idx, 1, QTableWidgetItem(str(rule.priority)))
            kind_item = QTableWidgetItem("内置" if rule.is_builtin else "自定义")
            kind_item.setForeground(secondary_color if rule.is_builtin else accent_color)
            self._rules_table.setItem(row_idx, 2, kind_item)
            self._rules_table.setItem(row_idx, 3, QTableWidgetItem(rule.name))
            self._rules_table.setItem(row_idx, 4, QTableWidgetItem(rule.pattern))
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

    def _on_rule_check_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        rule_id = item.data(Qt.ItemDataRole.UserRole)
        if not rule_id:
            return
        enabled = item.checkState() == Qt.CheckState.Checked
        try:
            self.ctx.rule_registry.set_rule_enabled(rule_id, enabled)
            self.ctx.save_rules()
            self.run_parser_test()
        except (ValueError, OSError, Exception) as exc:
            ScrollableDetailDialog("保存规则状态失败", "切换规则启用状态时发生错误：", str(exc), parent=self).exec()
            self._refresh_rules_table()

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
            try:
                self.ctx.rule_registry.add_rule(new_rule)
                self.ctx.save_rules()
                self._refresh_rules_table()
                self.run_parser_test()
                self.ctx.notify(f"已添加自定义规则：{new_rule.name}", "success")
            except (ValueError, OSError, Exception) as exc:
                ScrollableDetailDialog("添加规则失败", "保存自定义规则时发生错误：", str(exc), parent=self).exec()

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
            try:
                self.ctx.rule_registry.update_rule(updated)
                self.ctx.save_rules()
                self._refresh_rules_table()
                self.run_parser_test()
                self.ctx.notify(f"已更新规则：{updated.name}", "success")
            except (ValueError, KeyError, OSError, Exception) as exc:
                ScrollableDetailDialog("更新规则失败", "保存修改后的规则时发生错误：", str(exc), parent=self).exec()

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
        except (OSError, Exception) as exc:
            ScrollableDetailDialog("删除规则失败", "删除自定义规则时发生错误：", str(exc), parent=self).exec()

    # ------------------------------------------------------------------
    # Sub-tab 3: Companion extensions & DB backup management
    # ------------------------------------------------------------------

    def _build_general_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        # Companion extensions card
        ext_card = QFrame()
        ext_card.setProperty("card", True)
        ext_layout = QVBoxLayout(ext_card)
        ext_layout.setContentsMargins(16, 12, 16, 12)
        ext_layout.setSpacing(8)

        ext_title = QLabel("伴随扩展名过滤配置（批量导入/扫描时自动忽略非主体辅助文件）")
        ext_title.setProperty("role", "section")
        ext_layout.addWidget(ext_title)

        self._chk_default_ignore = QCheckBox("默认启用伴随扩展名过滤")
        self._chk_default_ignore.setChecked(self.ctx.config.ignore_companion_files)
        ext_layout.addWidget(self._chk_default_ignore)

        self._ext_editor = QPlainTextEdit()
        self._ext_editor.setMaximumHeight(76)
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
        db_layout.setContentsMargins(16, 12, 16, 12)
        db_layout.setSpacing(10)

        db_title = QLabel("数据库备份与维护")
        db_title.setProperty("role", "section")
        db_layout.addWidget(db_title)

        self._db_path_label = QLabel(f"当前库路径：{self.ctx.db.db_path}")
        self._db_path_label.setProperty("role", "subtitle")
        self._db_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        db_layout.addWidget(self._db_path_label)

        # Auto-backup policy controls
        policy_row = QHBoxLayout()
        policy_row.setSpacing(12)
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

        btn_save_policy = QPushButton("保存备份策略")
        btn_save_policy.setProperty("compact", True)
        btn_save_policy.clicked.connect(self._save_backup_policy)
        policy_row.addWidget(btn_save_policy)
        policy_row.addStretch()
        db_layout.addLayout(policy_row)

        # Backup action buttons
        db_btns = QHBoxLayout()
        db_btns.setSpacing(8)
        self._btn_backup_now = QPushButton("立即创建备份 (.bak)")
        self._btn_backup_now.setProperty("variant", "primary")
        self._btn_backup_now.clicked.connect(self._create_backup_now)
        db_btns.addWidget(self._btn_backup_now)

        self._btn_restore = QPushButton("恢复选中备份")
        self._btn_restore.clicked.connect(self._restore_selected_backup)
        db_btns.addWidget(self._btn_restore)

        self._btn_delete_bak = QPushButton("删除所选备份")
        self._btn_delete_bak.clicked.connect(self._delete_selected_backup)
        db_btns.addWidget(self._btn_delete_bak)

        self._btn_refresh_bak = QPushButton("刷新备份")
        self._btn_refresh_bak.clicked.connect(self._refresh_backups)
        db_btns.addWidget(self._btn_refresh_bak)

        self._btn_open_dir = QPushButton("打开目录")
        self._btn_open_dir.clicked.connect(self._open_db_folder)
        db_btns.addWidget(self._btn_open_dir)

        db_btns.addStretch()

        self._btn_clear_db = QPushButton("清空当前库")
        self._btn_clear_db.setProperty("variant", "danger")
        self._btn_clear_db.clicked.connect(self._clear_database)
        db_btns.addWidget(self._btn_clear_db)
        db_layout.addLayout(db_btns)

        self._backup_list = QListWidget()
        db_layout.addWidget(self._backup_list, 1)

        layout.addWidget(db_card, 1)
        self._refresh_backups()
        return widget

    def _set_db_actions_enabled(self, enabled: bool) -> None:
        for btn in (
            self._btn_backup_now,
            self._btn_restore,
            self._btn_delete_bak,
            self._btn_refresh_bak,
            self._btn_clear_db,
        ):
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
        except (OSError, Exception) as exc:
            ScrollableDetailDialog("保存设置失败", "保存伴随扩展名配置时发生错误：", str(exc), parent=self).exec()

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
            self.ctx.notify(
                f"已保存备份策略（自动备份：{'开启' if self.ctx.config.auto_backup else '关闭'}，最多保留 {self.ctx.config.backup_keep_count} 份）",
                "success",
            )
        except (OSError, Exception) as exc:
            ScrollableDetailDialog("保存备份策略失败", "写入配置文件时发生错误：", str(exc), parent=self).exec()

    def _refresh_backups(self) -> None:
        self._backups_dirty = False
        self._db_path_label.setText(f"当前库路径：{self.ctx.db.db_path}")
        self._backup_list.clear()
        try:
            backups = self.ctx.db.list_backups()
        except (DatabaseError, OSError, Exception) as exc:
            item = QListWidgetItem(f"读取备份列表失败：{exc}")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._backup_list.addItem(item)
            return

        if not backups:
            item = QListWidgetItem("暂无备份文件（每次批量删除或点击上方按钮时会自动生成）")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._backup_list.addItem(item)
            return

        for raw_bak in backups:
            bak = Path(raw_bak)
            try:
                st = bak.stat()
                sz_str = format_file_size(st.st_size)
                mtime_str = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                sz_str = "-"
                mtime_str = "-"
            item = QListWidgetItem(f"{bak.name}    [{sz_str} · {mtime_str}]")
            item.setData(Qt.ItemDataRole.UserRole, str(bak))
            item.setToolTip(str(bak))
            self._backup_list.addItem(item)

    def _run_db_maintenance_worker(
        self,
        fn,
        busy_msg: str,
        on_success,
        err_title: str,
    ) -> None:
        if self._is_worker_running(self._db_worker):
            self.ctx.notify("已有数据库维护任务在执行，请稍候...", "warning")
            return
        self._set_db_actions_enabled(False)
        self.ctx.notify(busy_msg, "info")

        worker = WorkerThread(fn, parent=self)
        self._db_worker = worker
        worker.succeeded.connect(on_success)
        worker.failed.connect(
            lambda detail, t=err_title: self._on_db_maintenance_failed(t, detail)
        )
        worker.finished.connect(lambda w=worker: self._on_db_maintenance_finished(w))
        if not hasattr(worker, "safe_stop"):
            worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_db_maintenance_failed(self, title: str, detail: str) -> None:
        self._set_db_actions_enabled(True)
        ScrollableDetailDialog(title, "操作执行失败，请查看详情：", detail, parent=self).exec()

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
            if bak_str:
                self.ctx.notify(f"已创建备份：{Path(bak_str).name}", "success")
            else:
                self.ctx.notify("当前数据库不支持文件备份（例如内存库）", "warning")

        self._run_db_maintenance_worker(
            _task, "正在后台创建数据库备份...", _done, "创建备份失败"
        )

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
        if dlg.exec() != ScrollableDetailDialog.DialogCode.Accepted:
            return

        def _task() -> str:
            self.ctx.db.restore_backup(bak_path)
            return str(bak_path)

        def _done(_restored: str) -> None:
            self._refresh_backups()
            self.ctx.data_changed.emit()
            self.ctx.notify("数据库备份已恢复", "success")

        self._run_db_maintenance_worker(
            _task, "正在后台恢复数据库备份...", _done, "恢复备份失败"
        )

    def _delete_selected_backup(self) -> None:
        item = self._backup_list.currentItem()
        if item is None:
            self.ctx.notify("请先在列表中选择要删除的备份文件", "warning")
            return
        bak_path = item.data(Qt.ItemDataRole.UserRole)
        if not bak_path:
            return
        p = Path(bak_path)
        dlg = ScrollableDetailDialog(
            title="确认删除备份文件",
            summary=f"确定永久删除备份文件「{p.name}」吗？（不会影响当前数据库数据）",
            confirm_mode=True,
            confirm_label="确认删除备份",
            danger=True,
            parent=self,
        )
        if dlg.exec() != ScrollableDetailDialog.DialogCode.Accepted:
            return
        try:
            if p.is_file():
                p.unlink()
            self._refresh_backups()
            self.ctx.notify(f"已删除备份：{p.name}", "success")
        except OSError as exc:
            ScrollableDetailDialog("删除备份失败", f"无法删除 {p.name}：", str(exc), parent=self).exec()

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
        except (DatabaseError, OSError, Exception) as exc:
            ScrollableDetailDialog("读取统计失败", "查询数据库状态时发生错误：", str(exc), parent=self).exec()
            return
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
        if dlg.exec() != ScrollableDetailDialog.DialogCode.Accepted:
            return

        def _task() -> None:
            self.ctx.db.clear_all_records()

        def _done(_res) -> None:
            self._refresh_backups()
            self.ctx.data_changed.emit()
            self.ctx.notify("当前数据库已清空（已自动备份）", "success")

        self._run_db_maintenance_worker(
            _task, "正在后台清空数据库（含自动备份）...", _done, "清空数据库失败"
        )
