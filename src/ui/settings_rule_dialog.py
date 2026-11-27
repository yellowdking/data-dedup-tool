"""Rule editor dialog and validation helpers for SettingsView."""

from __future__ import annotations

import re
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.core.models import CustomRule
from src.core.normalizer import clean_text, split_known_extension
from src.core.rules import RuleRegistry
from src.ui.icons import render_icon_pixmap
from src.ui.proxy_style import AppProxyStyle
from src.ui.theme import get_palette
from src.ui.widgets import WorkerThread

# Defense-in-depth detector for nested quantifiers such as (a+)+, (.*)*, (\w+){2,}
_RE_NESTED_QUANTIFIER = re.compile(
    r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\s*\d+\s*,)"
)


def _detect_theme(widget: Optional[QWidget] = None) -> str:
    cur = widget
    while cur is not None:
        t = getattr(getattr(getattr(cur, "ctx", None), "config", None), "theme", None)
        if t in ("light", "dark"):
            return str(t)
        cur = cur.parentWidget()
    app = QApplication.instance()
    proxy = getattr(app, "_app_proxy_style", None) if app is not None else None
    if isinstance(proxy, AppProxyStyle):
        return proxy.theme()
    return "light"


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
    """Modal dialog for creating or editing a custom regex rule with vector live validation."""

    def __init__(
        self,
        rule: Optional[CustomRule] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑自定义规则" if rule else "新增自定义正则规则")
        self.resize(580, 440)
        self._orig_rule = rule
        self._theme = _detect_theme(parent)

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
        self._pattern.setProperty("mono", True)
        self._pattern.setPlaceholderText(r"例如：(?<![A-Za-z0-9])(PRJ)[-_]?(\d{4,6})(?![0-9])")
        self._pattern.textChanged.connect(lambda _: self._validate_timer.start())
        form.addRow("正则表达式：", self._pattern)

        self._template = QLineEdit(rule.format_template if rule else "")
        self._template.setProperty("mono", True)
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

        # Vector status icon + neutral validation text (zero raw emojis)
        val_box = QFrame()
        val_box.setProperty("summaryStrip", True)
        val_row = QHBoxLayout(val_box)
        val_row.setContentsMargins(10, 8, 10, 8)
        val_row.setSpacing(8)

        self._validation_icon = QLabel()
        self._validation_icon.setFixedSize(16, 16)
        self._validation_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        val_row.addWidget(self._validation_icon, 0, Qt.AlignmentFlag.AlignTop)

        self._validation_label = QLabel("输入正则表达式与测试样例可即时验证匹配效果（按清洗后文本匹配）")
        self._validation_label.setWordWrap(True)
        self._validation_label.setProperty("role", "subtitle")
        val_row.addWidget(self._validation_label, 1)
        layout.addWidget(val_box)

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

    def _set_validation_status(self, level: str, text: str) -> None:
        p = get_palette(self._theme)
        icon_map = {
            "success": ("check-circle", p["success"]),
            "error": ("x-circle", p["danger"]),
            "warning": ("alert-triangle", p["warning"]),
            "info": ("info", p["accent"]),
        }
        icon_name, color = icon_map.get(level, ("info", p["text_secondary"]))
        self._validation_icon.setPixmap(render_icon_pixmap(icon_name, 15, color, stroke_width=1.65))
        self._validation_label.setText(text)
        fg = color if level in ("success", "error", "warning") else p["text_secondary"]
        self._validation_label.setStyleSheet(f"color: {fg}; font-size: 12px;")

    def done(self, result: int) -> None:
        self._validate_timer.stop()
        super().done(result)

    def _live_validate(self) -> None:
        pattern = self._pattern.text().strip()
        if not pattern:
            self._set_validation_status("info", "请输入正则表达式与测试样例")
            return
        ok, err = _validate_custom_regex_safe(pattern)
        if not ok:
            self._set_validation_status("error", f"匹配失败：{err}")
            return

        sample = self._example.text().strip()
        if sample:
            cleaned = clean_text(sample)
            cleaned_hint = f"\n（实际送入引擎的清洗后文本：{cleaned or '（空）'}）" if cleaned != sample else ""
            if not cleaned:
                self._set_validation_status(
                    "warning", f"匹配失败：样例经预清洗后为空，实际解析时将无法命中{cleaned_hint}"
                )
                return
            m = re.search(pattern, cleaned, re.IGNORECASE)
            if m:
                if not m.group(0):
                    self._set_validation_status(
                        "error", f"匹配失败：匹配结果为空字符串，请调整正则表达式{cleaned_hint}"
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
                    self._set_validation_status(
                        "error",
                        f"匹配失败：格式化后的标准编码为空，请检查捕获组或格式模板{cleaned_hint}",
                    )
                    return
                self._set_validation_status(
                    "success",
                    f"匹配成功：标准编码 = {canonical}（前缀={prefix}，编号={number}）{cleaned_hint}",
                )
            else:
                self._set_validation_status(
                    "warning", f"匹配失败：正则语法有效，但未命中清洗后的测试样例{cleaned_hint}"
                )
        else:
            self._set_validation_status(
                "success", "正则语法有效（建议输入测试样例验证清洗后匹配效果）"
            )

    def _on_accept(self) -> None:
        self._validate_timer.stop()
        name = self._name.text().strip()
        pattern = self._pattern.text().strip()
        if not name:
            self._set_validation_status("error", "匹配失败：请填写规则名称")
            self._name.setFocus()
            return
        ok, err = _validate_custom_regex_safe(pattern)
        if not ok:
            self._set_validation_status("error", f"匹配失败：{err}")
            self._pattern.setFocus()
            return

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
                        self._set_validation_status(
                            "error", "匹配失败：测试样例匹配产出空编码，请修正正则或模板"
                        )
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
