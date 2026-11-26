"""Built-in multi-pattern rule registry and JSON-backed user custom regex rules.

All rule IDs, names, categories, descriptions, and examples use strictly neutral terminology.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional, Union

from .models import CustomRule


def get_builtin_rules() -> list[CustomRule]:
    """Return metadata descriptors for all built-in extraction rules in priority order."""
    return [
        CustomRule(
            id="builtin_composite_code",
            name="复合前缀编号",
            pattern=r"(?<![A-Za-z0-9])FC2(?:[-_.\s]*PPV)?[-_.\s]*(\d{5,7})(?![0-9])",
            format_template="FC2-PPV-{1}",
            priority=10,
            enabled=True,
            is_builtin=True,
            category="复合编码",
            description="识别带复合前缀的多位数字编码（支持连字符、下划线、点号或空格分隔）",
            example="FC2.PPV.1234567 -> FC2-PPV-1234567",
        ),
        CustomRule(
            id="builtin_three_segment",
            name="多段与复合子前缀编码",
            pattern=r"(?<![A-Za-z0-9])(?:[A-Za-z]{4,10}[-_\s]*\d{4}[-_\s]+\d{3,4}|[A-Za-z]\d{4}[-_\s]*[A-Za-z]{2,4}\d{3,6}|[A-Za-z]{2,4}([-_])[A-Za-z]{1,2}\1\d{2,5}|[A-Za-z]{2,5}[-_][A-Za-z]\d{2,4})(?![0-9])",
            format_template="",
            priority=15,
            enabled=True,
            is_builtin=True,
            category="多段编码",
            description="识别三段字母数字组合与带字母子前缀的复合编码（如 ABC-XY-001、CAT-S123、H4610-KI1234）",
            example="ABC-XY-001 -> ABC-XY-001",
        ),
        CustomRule(
            id="builtin_date_indexed",
            name="六位日期序列编码",
            pattern=r"(?<![A-Za-z0-9])(\d{6})([-_])(\d{2,3})(?![0-9])",
            format_template="{1}{2}{3}",
            priority=25,
            enabled=True,
            is_builtin=True,
            category="日期序列",
            description="识别 6 位日期加序号格式，自动保留连字符或下划线以区分不同来源序列",
            example="010124-001 -> 010124-001",
        ),
        CustomRule(
            id="builtin_num_alpha_prefix",
            name="数字+字母前缀编码",
            pattern=r"(?<![A-Za-z0-9])(\d{2,4}[A-Za-z]{2,6})[-_\s]+(\d{2,5})(?![0-9])",
            format_template="{1}-{2}",
            priority=30,
            enabled=True,
            is_builtin=True,
            category="数字字母前缀",
            description="识别以 2-4 位数字开头的字母系列编码（如 300ABC-123、200XYZ-1234、1000PRO-1234）",
            example="300ABC-123 -> 300ABC-123",
        ),
        CustomRule(
            id="builtin_raw_compact_id",
            name="原始紧凑编号",
            pattern=r"(?<![A-Za-z0-9])(?:(?:[hn]_\d{2,4}|4k2_?|\d{1,3}))?([A-Za-z]{2,6})(0\d{3,4})(?:pl|ps|hhb|so|re|r)?(?![A-Za-z0-9])",
            format_template="{1}-{2}",
            priority=35,
            enabled=True,
            is_builtin=True,
            category="原始紧凑ID",
            description="将紧凑型原始编号规范化为标准编码，自动处理厂商数字前缀与多余前导零",
            example="118abc00123 -> ABC-123",
        ),
        CustomRule(
            id="builtin_num_alpha_compact",
            name="数字+字母紧凑编码",
            pattern=r"(?<![A-Za-z0-9])(\d{2,3}[A-Za-z]{2,6})([1-9]\d{2,3})(?![A-Za-z0-9])",
            format_template="{1}-{2}",
            priority=38,
            enabled=True,
            is_builtin=True,
            category="数字字母前缀",
            description="识别无分隔符的数字+字母系列编码（如 300ABC123、200XYZ2345）",
            example="300ABC123 -> 300ABC-123",
        ),
        CustomRule(
            id="builtin_mixed_alnum_prefix",
            name="混合字母数字前缀编码",
            pattern=r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,11})[-_](\d{2,5})(?![0-9])",
            format_template="{1}-{2}",
            priority=42,
            enabled=True,
            is_builtin=True,
            category="混合前缀",
            description="识别前缀内同时含字母与数字的编码（如 T28-123、S2M-001、A3B2C-12）",
            example="S2M-001 -> S2M-001",
        ),
        CustomRule(
            id="builtin_standard_hyphen",
            name="标准分隔字母数字编码",
            pattern=r"(?<![A-Za-z0-9])([A-Za-z]{2,7})[-_\s.]+(\d{2,6})(?![0-9])",
            format_template="{1}-{2}",
            priority=50,
            enabled=True,
            is_builtin=True,
            category="标准编码",
            description="识别标准字母+分隔符（连字符/下划线/空格/点号）+数字编码，自动清理多余前导零",
            example="ABC-000123 -> ABC-123",
        ),
        CustomRule(
            id="builtin_standard_compact",
            name="标准紧凑字母数字编码",
            pattern=r"(?<![A-Za-z0-9])([A-Za-z]{2,6})(\d{3,5})(?![A-Za-z0-9])",
            format_template="{1}-{2}",
            priority=60,
            enabled=True,
            is_builtin=True,
            category="紧凑编码",
            description="识别无分隔符的字母+数字编码（如 ABC123、PRJ001），结合黑名单过滤技术噪声词",
            example="ABC123 -> ABC-123",
        ),
        CustomRule(
            id="builtin_short_prefix",
            name="短前缀编码",
            pattern=r"(?<![A-Za-z0-9])([nk])(\d{4})(?![A-Za-z0-9])",
            format_template="{1}{2}",
            priority=70,
            enabled=True,
            is_builtin=True,
            category="短前缀",
            description="识别单字母加 4 位数字的短前缀编码（如 n0123、k1234）",
            example="n0123 -> N0123",
        ),
    ]


class RuleRegistry:
    """Manages built-in rules (enable/disable state) and persistent user custom rules."""

    def __init__(self, custom_rules_path: Optional[Union[str, Path]] = None) -> None:
        self._custom_rules_path: Optional[Path] = (
            Path(custom_rules_path) if custom_rules_path else None
        )
        self._builtin_rules: dict[str, CustomRule] = {
            r.id: r for r in get_builtin_rules()
        }
        self._custom_rules: list[CustomRule] = []
        if self._custom_rules_path and self._custom_rules_path.exists():
            self.load_custom_rules(self._custom_rules_path)

    @property
    def custom_rules_path(self) -> Optional[Path]:
        return self._custom_rules_path

    @staticmethod
    def validate_pattern(pattern: str) -> tuple[bool, str]:
        """Validate a regular expression string and return `(is_valid, error_message)`."""
        if not pattern or not pattern.strip():
            return False, "正则表达式不能为空"
        try:
            re.compile(pattern)
        except re.error as exc:
            return False, f"正则表达式语法错误: {exc}"
        return True, ""

    def load_custom_rules(
        self, path: Optional[Union[str, Path]] = None
    ) -> list[CustomRule]:
        """Load custom rules and built-in toggle overrides from JSON."""
        target = Path(path) if path else self._custom_rules_path
        if target is None:
            return list(self._custom_rules)
        self._custom_rules_path = target
        if not target.exists():
            self._custom_rules = []
            return []

        raw = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            rules_data = raw
            disabled_builtins: list[str] = []
        elif isinstance(raw, dict):
            rules_data = raw.get("custom_rules", [])
            disabled_builtins = raw.get("disabled_builtins", [])
        else:
            rules_data = []
            disabled_builtins = []

        loaded: list[CustomRule] = []
        for idx, item in enumerate(rules_data):
            if not isinstance(item, dict):
                continue
            rule = CustomRule.from_dict(item)
            if not rule.id:
                rule.id = f"custom_{idx + 1:03d}"
            rule.is_builtin = False
            ok, _ = self.validate_pattern(rule.pattern)
            if ok:
                loaded.append(rule)

        self._custom_rules = loaded
        disabled_set = set(disabled_builtins)
        for rid, b_rule in self._builtin_rules.items():
            b_rule.enabled = rid not in disabled_set
        return list(self._custom_rules)

    def save_custom_rules(self, path: Optional[Union[str, Path]] = None) -> None:
        """Persist custom rules and disabled built-in rule IDs to JSON."""
        target = Path(path) if path else self._custom_rules_path
        if target is None:
            raise ValueError("未指定自定义规则保存路径")
        self._custom_rules_path = target
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "custom_rules": [r.to_dict() for r in self._custom_rules],
            "disabled_builtins": [
                rid for rid, r in self._builtin_rules.items() if not r.enabled
            ],
        }
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_builtin_rules(self) -> list[CustomRule]:
        return sorted(self._builtin_rules.values(), key=lambda r: r.priority)

    def get_custom_rules(self) -> list[CustomRule]:
        return sorted(self._custom_rules, key=lambda r: (r.priority, r.id))

    def get_all_rules(self, enabled_only: bool = False) -> list[CustomRule]:
        combined = list(self._custom_rules) + list(self._builtin_rules.values())
        if enabled_only:
            combined = [r for r in combined if r.enabled]
        return sorted(combined, key=lambda r: (r.priority, r.is_builtin, r.id))

    def is_builtin_enabled(self, rule_id: str) -> bool:
        rule = self._builtin_rules.get(rule_id)
        return rule.enabled if rule else False

    def add_rule(self, rule: CustomRule) -> CustomRule:
        ok, err = self.validate_pattern(rule.pattern)
        if not ok:
            raise ValueError(err)
        if not rule.id:
            existing_ids = {r.id for r in self._custom_rules}
            n = len(self._custom_rules) + 1
            while f"custom_{n:03d}" in existing_ids:
                n += 1
            rule.id = f"custom_{n:03d}"
        if any(r.id == rule.id for r in self._custom_rules):
            raise ValueError(f"规则 ID 已存在: {rule.id}")
        rule.is_builtin = False
        self._custom_rules.append(rule)
        return rule

    def update_rule(self, rule: CustomRule) -> CustomRule:
        ok, err = self.validate_pattern(rule.pattern)
        if not ok:
            raise ValueError(err)
        for idx, existing in enumerate(self._custom_rules):
            if existing.id == rule.id:
                rule.is_builtin = False
                self._custom_rules[idx] = rule
                return rule
        raise KeyError(f"未找到自定义规则: {rule.id}")

    def delete_rule(self, rule_id: str) -> bool:
        before = len(self._custom_rules)
        self._custom_rules = [r for r in self._custom_rules if r.id != rule_id]
        return len(self._custom_rules) < before

    def set_rule_enabled(self, rule_id: str, enabled: bool) -> bool:
        if rule_id in self._builtin_rules:
            self._builtin_rules[rule_id].enabled = enabled
            return True
        for r in self._custom_rules:
            if r.id == rule_id:
                r.enabled = enabled
                return True
        return False

    @staticmethod
    def format_custom_match(rule: CustomRule, match: re.Match) -> tuple[str, str, str]:
        """Format a regex match from a custom rule into `(canonical_code, prefix, number)`."""
        groups = match.groups()
        groupdict = match.groupdict()

        prefix = str(groupdict.get("prefix") or "").upper()
        number = str(groupdict.get("number") or "").upper()

        if rule.format_template:
            formatted = rule.format_template
            for key, val in groupdict.items():
                formatted = formatted.replace(f"{{{key}}}", str(val or ""))
            formatted = formatted.replace("{0}", match.group(0))
            for idx, val in enumerate(groups, start=1):
                formatted = formatted.replace(f"{{{idx}}}", str(val or ""))
            canonical = formatted.strip().upper()
        elif prefix and number:
            canonical = f"{prefix}-{number}"
        elif len(groups) >= 2 and groups[0] and groups[1]:
            prefix = str(groups[0]).upper()
            number = str(groups[1]).upper()
            canonical = f"{prefix}-{number}"
        else:
            canonical = match.group(0).strip().upper()

        if not prefix or not number:
            m_split = re.match(r"^([A-Z0-9]+?)[-_\s]+([A-Z0-9]+)$", canonical)
            if m_split:
                prefix = prefix or m_split.group(1)
                number = number or m_split.group(2)
            else:
                m_alnum = re.match(r"^([A-Z]+)(\d+)$", canonical)
                if m_alnum:
                    prefix = prefix or m_alnum.group(1)
                    number = number or m_alnum.group(2)
                else:
                    prefix = prefix or canonical
                    number = number or ""

        return canonical, prefix, number
