"""Built-in multi-pattern rule registry and JSON-backed user custom regex rules.

All rule IDs, names, categories, descriptions, and examples use strictly neutral
purely synthetic terminology.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional, Union

from .models import CustomRule

try:
    from re import _parser as _re_parser  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    import sre_parse as _re_parser  # type: ignore[no-redef]


_RE_TEMPLATE_TOKEN = re.compile(r"\{(\w+)\}")
_RE_NESTED_QUANTIFIER_FALLBACK = re.compile(
    r"\((?:[^()\\]|\\.)*(?:[+*]|\{\d*,\d*\})(?:[^()\\]|\\.)*\)(?:[+*]|\{\d*,\d*\})"
)


def _node_has_nested_quantifier(nodes: Any, inside_repeat: bool = False) -> bool:
    """Walk parsed regex AST to detect dangerous nested variable/unbounded quantifiers (ReDoS)."""
    max_repeat_const = getattr(_re_parser, "MAXREPEAT", 2**32 - 1)
    op_max_repeat = getattr(_re_parser, "MAX_REPEAT", None)
    op_min_repeat = getattr(_re_parser, "MIN_REPEAT", None)
    op_possessive = getattr(_re_parser, "POSSESSIVE_REPEAT", None)
    op_subpattern = getattr(_re_parser, "SUBPATTERN", None)
    op_branch = getattr(_re_parser, "BRANCH", None)
    op_assert = getattr(_re_parser, "ASSERT", None)
    op_assert_not = getattr(_re_parser, "ASSERT_NOT", None)

    for op, av in nodes:
        if op in (op_max_repeat, op_min_repeat):
            lo, hi, sub = av
            is_variable_large = (hi == max_repeat_const) or (hi > 10 and hi != lo)
            if inside_repeat and is_variable_large:
                return True
            if _node_has_nested_quantifier(sub, inside_repeat=inside_repeat or is_variable_large):
                return True
        elif op == op_possessive:
            _lo, _hi, sub = av
            if _node_has_nested_quantifier(sub, inside_repeat=inside_repeat):
                return True
        elif op == op_subpattern:
            # av = (group_id, add_flags, del_flags, subpattern)
            if _node_has_nested_quantifier(av[3], inside_repeat=inside_repeat):
                return True
        elif op == op_branch:
            # av = (None, [branch1, branch2, ...])
            for branch in av[1]:
                if _node_has_nested_quantifier(branch, inside_repeat=inside_repeat):
                    return True
        elif op in (op_assert, op_assert_not):
            if _node_has_nested_quantifier(av[1], inside_repeat=inside_repeat):
                return True
    return False


def _has_nested_quantifier(pattern: str) -> bool:
    """Return True if `pattern` contains nested variable/unbounded quantifiers prone to ReDoS."""
    try:
        parsed = _re_parser.parse(pattern)
        return _node_has_nested_quantifier(parsed, inside_repeat=False)
    except Exception:
        return bool(_RE_NESTED_QUANTIFIER_FALLBACK.search(pattern))


def get_builtin_rules() -> list[CustomRule]:
    """Return metadata descriptors for all built-in extraction rules in priority order.

    All displayed patterns, descriptions, and examples use 100% synthetic neutral labels.
    """
    return [
        CustomRule(
            id="builtin_composite_code",
            name="复合前缀编号",
            pattern=r"(?<![A-Za-z0-9])[A-Z]{2}\d(?:[-_.\s#]*[A-Z]{3})?[-_.\s]*(?:(?:No\.?|#)[-_.\s]*)?(\d{5,7})(?![0-9])",
            format_template="{PREFIX}-{SUB}-{1}",
            priority=10,
            enabled=True,
            is_builtin=True,
            category="复合编码",
            description="识别带复合子前缀的多位数字编码（支持连字符、下划线、点号、空格、# 与 No. 分隔）",
            example="M2.SUB.1234567 -> M2-SUB-1234567",
        ),
        CustomRule(
            id="builtin_three_segment",
            name="多段与复合子前缀编码",
            pattern=r"(?<![A-Za-z0-9])(?:[A-Za-z]{4,10}[-_\s]*\d{4}[-_\s]+\d{3,4}(?![0-9])|[A-Za-z]\d{4}[-_\s]*[A-Za-z]{2,4}\d{3,6}(?![0-9])|[A-Za-z]{2,4}([-_])[A-Za-z]{1,2}\1\d{2,5}(?![0-9])|[A-Za-z]{2,5}[-_][A-Za-z]\d{2,4}(?![A-Za-z0-9]))",
            format_template="",
            priority=15,
            enabled=True,
            is_builtin=True,
            category="多段编码",
            description="识别三段字母数字组合与带字母子前缀的复合编码（如 ABC-XY-001、CAT-S123、X1234-AB5678）",
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
            pattern=r"(?<![A-Za-z0-9])(\d{2,4})([A-Za-z]{2,6})[-_\s]+(\d{2,5})(?![0-9])",
            format_template="{1}{2}-{3}",
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
            pattern=r"(?<![A-Za-z0-9])(?:(?:[hn]_\d{2,4}|\dk\d_?)([A-Za-z]{2,6})(\d{3,5})|(\d{1,3})?([A-Za-z]{2,6})(0\d{3,4}))(?:pl|ps|hhb|so|re|r)?(?![A-Za-z0-9])",
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
            description="识别前缀内同时含字母与数字的编码（如 A28-123、B2M-001、A3B2C-12）",
            example="B2M-001 -> B2M-001",
        ),
        CustomRule(
            id="builtin_standard_hyphen",
            name="标准分隔字母数字编码",
            pattern=r"(?<![A-Za-z0-9])([A-Za-z]{2,7})([-_\s.]+)(\d{2,6})(?![0-9])",
            format_template="{1}-{3}",
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
        self._enabled_rules_cache: Optional[tuple[CustomRule, ...]] = None
        self._compiled_custom_cache: dict[str, Optional[re.Pattern[str]]] = {}
        self.last_load_error: str = ""
        if self._custom_rules_path and self._custom_rules_path.exists():
            self.load_custom_rules(self._custom_rules_path)

    def _invalidate_cache(self) -> None:
        """Clear cached sorted rule order and compiled custom regexes after any mutation."""
        self._enabled_rules_cache = None
        self._compiled_custom_cache.clear()

    @property
    def custom_rules_path(self) -> Optional[Path]:
        return self._custom_rules_path

    @staticmethod
    def validate_pattern(pattern: str) -> tuple[bool, str]:
        """Validate a regular expression string (syntax + basic nested-quantifier ReDoS guard)."""
        if not pattern or not pattern.strip():
            return False, "正则表达式不能为空"
        try:
            re.compile(pattern)
        except re.error as exc:
            return False, f"正则表达式语法错误: {exc}"
        if _has_nested_quantifier(pattern):
            return False, "正则表达式包含嵌套量词，可能导致回溯性能风险，请简化表达式"
        return True, ""

    def load_custom_rules(
        self, path: Optional[Union[str, Path]] = None
    ) -> list[CustomRule]:
        """Load custom rules and built-in toggle overrides from JSON safely."""
        target = Path(path) if path else self._custom_rules_path
        if target is None:
            return list(self._custom_rules)
        self._custom_rules_path = target
        self.last_load_error = ""
        if not target.exists():
            self._custom_rules = []
            self._invalidate_cache()
            return []

        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.last_load_error = f"自定义规则文件解析失败，已回退到默认规则: {exc}"
            self._custom_rules = []
            for b_rule in self._builtin_rules.values():
                b_rule.enabled = True
            self._invalidate_cache()
            return []

        if isinstance(raw, list):
            rules_data = raw
            disabled_builtins: list[Any] = []
        elif isinstance(raw, dict):
            raw_rules = raw.get("custom_rules", [])
            rules_data = raw_rules if isinstance(raw_rules, list) else []
            raw_disabled = raw.get("disabled_builtins", [])
            disabled_builtins = raw_disabled if isinstance(raw_disabled, list) else []
        else:
            rules_data = []
            disabled_builtins = []

        loaded: list[CustomRule] = []
        seen_ids: set[str] = set(self._builtin_rules.keys())
        for idx, item in enumerate(rules_data):
            if not isinstance(item, dict):
                continue
            rule = CustomRule.from_dict(item)
            if not rule.id or rule.id in seen_ids:
                if rule.id in self._builtin_rules:
                    # Never allow a loaded custom rule to shadow a built-in rule ID.
                    continue
                n = idx + 1
                while f"custom_{n:03d}" in seen_ids:
                    n += 1
                rule.id = f"custom_{n:03d}"
            rule.is_builtin = False
            ok, _ = self.validate_pattern(rule.pattern)
            if ok:
                seen_ids.add(rule.id)
                loaded.append(rule)

        self._custom_rules = loaded
        disabled_set = {str(rid) for rid in disabled_builtins if isinstance(rid, str)}
        for rid, b_rule in self._builtin_rules.items():
            b_rule.enabled = rid not in disabled_set
        self._invalidate_cache()
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

    def get_enabled_rules_cached(self) -> tuple[CustomRule, ...]:
        """Return cached priority-sorted tuple of all currently enabled rules."""
        if self._enabled_rules_cache is None:
            combined = [
                r
                for r in (*self._custom_rules, *self._builtin_rules.values())
                if r.enabled
            ]
            combined.sort(key=lambda r: (r.priority, r.is_builtin, r.id))
            self._enabled_rules_cache = tuple(combined)
        return self._enabled_rules_cache

    def get_compiled_pattern(self, rule: CustomRule) -> Optional[re.Pattern[str]]:
        """Return a cached compiled regex for a custom rule."""
        cache_key = f"{rule.id}\x00{rule.pattern}"
        if cache_key not in self._compiled_custom_cache:
            try:
                self._compiled_custom_cache[cache_key] = re.compile(
                    rule.pattern, re.IGNORECASE
                )
            except re.error:
                self._compiled_custom_cache[cache_key] = None
        return self._compiled_custom_cache[cache_key]

    def get_all_rules(self, enabled_only: bool = False) -> list[CustomRule]:
        if enabled_only:
            return list(self.get_enabled_rules_cached())
        combined = list(self._custom_rules) + list(self._builtin_rules.values())
        return sorted(combined, key=lambda r: (r.priority, r.is_builtin, r.id))

    def is_builtin_enabled(self, rule_id: str) -> bool:
        rule = self._builtin_rules.get(rule_id)
        return rule.enabled if rule else False

    def add_rule(self, rule: CustomRule) -> CustomRule:
        ok, err = self.validate_pattern(rule.pattern)
        if not ok:
            raise ValueError(err)
        if not rule.id:
            existing_ids = {r.id for r in self._custom_rules} | set(self._builtin_rules.keys())
            n = len(self._custom_rules) + 1
            while f"custom_{n:03d}" in existing_ids:
                n += 1
            rule.id = f"custom_{n:03d}"
        if rule.id in self._builtin_rules:
            raise ValueError(f"规则 ID 不能与内置规则冲突: {rule.id}")
        if any(r.id == rule.id for r in self._custom_rules):
            raise ValueError(f"规则 ID 已存在: {rule.id}")
        rule.is_builtin = False
        self._custom_rules.append(rule)
        self._invalidate_cache()
        return rule

    def update_rule(self, rule: CustomRule) -> CustomRule:
        ok, err = self.validate_pattern(rule.pattern)
        if not ok:
            raise ValueError(err)
        if rule.id in self._builtin_rules:
            raise ValueError(f"内置规则不支持通过自定义规则接口覆盖: {rule.id}")
        for idx, existing in enumerate(self._custom_rules):
            if existing.id == rule.id:
                rule.is_builtin = False
                self._custom_rules[idx] = rule
                self._invalidate_cache()
                return rule
        raise KeyError(f"未找到自定义规则: {rule.id}")

    def delete_rule(self, rule_id: str) -> bool:
        before = len(self._custom_rules)
        self._custom_rules = [r for r in self._custom_rules if r.id != rule_id]
        deleted = len(self._custom_rules) < before
        if deleted:
            self._invalidate_cache()
        return deleted

    def set_rule_enabled(self, rule_id: str, enabled: bool) -> bool:
        if rule_id in self._builtin_rules:
            self._builtin_rules[rule_id].enabled = enabled
            self._invalidate_cache()
            return True
        for r in self._custom_rules:
            if r.id == rule_id:
                r.enabled = enabled
                self._invalidate_cache()
                return True
        return False

    @staticmethod
    def format_custom_match(rule: CustomRule, match: re.Match[str]) -> tuple[str, str, str]:
        """Format a regex match from a custom rule into `(canonical_code, prefix, number)`."""
        groups = match.groups()
        groupdict = match.groupdict()

        prefix = str(groupdict.get("prefix") or "").upper()
        number = str(groupdict.get("number") or "").upper()

        if rule.format_template:
            def _sub_token(tok: re.Match[str]) -> str:
                key = tok.group(1)
                if key == "0":
                    return match.group(0)
                if key.isdigit():
                    idx = int(key)
                    if 1 <= idx <= len(groups):
                        return str(groups[idx - 1] or "")
                    return tok.group(0)
                if key in groupdict:
                    return str(groupdict[key] or "")
                return tok.group(0)

            canonical = _RE_TEMPLATE_TOKEN.sub(_sub_token, rule.format_template).strip().upper()
        elif prefix and number:
            canonical = f"{prefix}-{number}"
        elif len(groups) >= 2 and groups[0] and groups[1]:
            prefix = str(groups[0]).upper()
            number = str(groups[1]).upper()
            canonical = f"{prefix}-{number}"
        else:
            canonical = match.group(0).strip().upper()

        if not prefix or not number:
            m_split = re.match(r"^(.+?)[-_\s]+([A-Z0-9]+)$", canonical)
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
