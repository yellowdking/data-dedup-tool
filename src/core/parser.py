"""Four-stage multi-pattern code extraction engine.

Stages:
1. Pre-cleaning & tag/extension extraction
2. Ordered structural rule matching (custom rules -> composite -> multi-segment ->
   date-indexed -> number+alpha -> raw compact ID -> mixed prefix -> standard -> short prefix)
   with non-overlapping span masking to support multiple distinct codes on a single line
3. Parent-folder fallback when the filename itself carries no valid catalog code
4. Confidence scoring & structured ParseResult assembly

All rules are purely structural and all terminology is strictly neutral.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import Callable, Collection, Optional, Sequence

from .models import CustomRule, ParseResult, ScannedItem
from .normalizer import (
    NON_SEGMENT_TOKENS,
    clean_text,
    extract_part,
    extract_tags,
    is_companion_extension,
    is_false_positive,
    is_plausible_six_digit_date,
    normalize_serial_number,
    normalize_width,
    split_known_extension,
)
from .rules import RuleRegistry


# Generic directory names skipped when climbing ancestor folders during Stage 3 fallback.
_GENERIC_DIR_NAMES: frozenset[str] = frozenset(
    {
        "video",
        "videos",
        "media",
        "movie",
        "movies",
        "data",
        "files",
        "downloads",
        "download",
        "new folder",
        "新建文件夹",
        "bdmv",
        "stream",
        "cd1",
        "cd2",
        "cd3",
        "part1",
        "part2",
        "disc1",
        "disc2",
        "disk1",
        "disk2",
        "sub",
        "subs",
        "subtitle",
        "subtitles",
        "4k",
        "8k",
        "vr",
        "hd",
        "fhd",
    }
)


@dataclass
class _CandidateMatch:
    start: int
    end: int
    canonical_code: str
    prefix: str
    number: str
    raw_code: str
    rule_id: str
    confidence: float


# Composite prefix pattern (supports -, _, ., space, #, and No. separators).
_RE_COMPOSITE_FC2 = re.compile(
    r"(?<![A-Za-z0-9])FC2(?:[-_.\s#]*PPV)?[-_.\s]*(?:(?:No\.?|#)[-_.\s]*)?(\d{5,7})(?![0-9])",
    re.IGNORECASE,
)

# Three-segment & structured composite patterns (purely structural).
_RE_THREE_SEG_NUM = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{4,10})[-_\s]*(\d{4})[-_\s]+(\d{3,4})(?![0-9])"
)
_RE_ALPHA_FOUR_DIGIT_SUB = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]\d{4})[-_\s]*([A-Za-z]{2,4}\d{3,6})(?![0-9])"
)
_RE_THREE_SEG_ALPHA = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{2,4})([-_])([A-Za-z]{1,2})\2(\d{2,5})(?![0-9])"
)
_RE_ALPHA_SUB_PREFIX = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{2,5})([-_])([A-Za-z]\d{2,4})(?![A-Za-z0-9])"
)

# Bare six-digit date-indexed pattern (e.g. 010124-001, 010124_001).
_RE_DATE_INDEXED = re.compile(
    r"(?<![A-Za-z0-9])(\d{6})([-_])(\d{2,3})(?![0-9])"
)

# Number+Letter series prefixes with explicit separator (2-4 leading digits + 2-6 letters).
_RE_NUM_ALPHA_SEP = re.compile(
    r"(?<![A-Za-z0-9])(\d{2,4})([A-Za-z]{2,6})[-_\s]+(\d{2,5})(?![0-9])"
)

# Raw compact platform IDs (explicit prefix markers like h_1133 / n_1428 / 4k2_, or zero-padded digits).
_RE_RAW_COMPACT_EXPLICIT = re.compile(
    r"(?<![A-Za-z0-9])(?:[hn]_\d{2,4}|4k2_?)([A-Za-z]{2,6})(\d{3,5})(?:pl|ps|hhb|so|re|r)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_RE_RAW_COMPACT_ZERO_PADDED = re.compile(
    r"(?<![A-Za-z0-9])(\d{1,3})?([A-Za-z]{2,6})(0\d{3,4})(?:pl|ps|hhb|so|re|r)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# Compact Number+Letter series without separator (e.g. 300ABC123, 200XYZ2345).
_RE_NUM_ALPHA_COMPACT = re.compile(
    r"(?<![A-Za-z0-9])(\d{2,3}[A-Za-z]{2,6})([1-9]\d{2,3})(?![A-Za-z0-9])"
)

# Mixed letter-number prefixes with separator (e.g. T28-123, S2M-001, A3B2C-12).
_RE_MIXED_ALNUM = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,11})[-_](\d{2,5})(?![0-9])"
)

# Standard separated alphanumeric codes (hyphen, underscore, space, or dot).
_RE_STD_SEP = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{2,7})([-_\s.]+)(\d{2,6})(?![0-9])"
)

# Standard compact alphanumeric codes without separator (e.g. ABC123, PRJ001).
_RE_STD_COMPACT = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{2,6})(\d{3,5})(?![A-Za-z0-9])"
)

# Short single-letter + 4-digit codes (e.g. n0123, k1234).
_RE_SHORT_PREFIX = re.compile(
    r"(?<![A-Za-z0-9])([nk])(\d{4})(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _overlaps(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    s, e = span
    for os_, oe in occupied:
        if s < oe and e > os_:
            return True
    return False


class CodeParser:
    """Multi-stage catalog code extraction engine."""

    def __init__(self, rule_registry: Optional[RuleRegistry] = None) -> None:
        self.registry = rule_registry or RuleRegistry()

    def _collect_candidates(self, cleaned: str) -> list[_CandidateMatch]:
        """Run all enabled rules in priority order with span masking."""
        if not cleaned:
            return []

        occupied: list[tuple[int, int]] = []
        candidates: list[_CandidateMatch] = []

        def _try_add(cand: _CandidateMatch) -> None:
            if _overlaps((cand.start, cand.end), occupied):
                return
            occupied.append((cand.start, cand.end))
            candidates.append(cand)

        ordered_rules = self.registry.get_all_rules(enabled_only=True)
        for rule in ordered_rules:
            if not rule.is_builtin:
                self._apply_custom_rule(rule, cleaned, _try_add)
            else:
                self._apply_builtin_rule(rule.id, cleaned, _try_add)

        candidates.sort(key=lambda c: c.start)
        return candidates

    def _apply_custom_rule(
        self,
        rule: CustomRule,
        cleaned: str,
        emit: Callable[[_CandidateMatch], None],
    ) -> None:
        try:
            regex = re.compile(rule.pattern, re.IGNORECASE)
        except re.error:
            return
        for m in regex.finditer(cleaned):
            canonical, prefix, number = RuleRegistry.format_custom_match(rule, m)
            if not canonical:
                continue
            emit(
                _CandidateMatch(
                    start=m.start(),
                    end=m.end(),
                    canonical_code=canonical,
                    prefix=prefix,
                    number=number,
                    raw_code=m.group(0),
                    rule_id=rule.id,
                    confidence=0.99,
                )
            )

    def _apply_builtin_rule(
        self,
        rule_id: str,
        cleaned: str,
        emit: Callable[[_CandidateMatch], None],
    ) -> None:
        if rule_id == "builtin_composite_code":
            for m in _RE_COMPOSITE_FC2.finditer(cleaned):
                num = m.group(1)
                emit(
                    _CandidateMatch(
                        start=m.start(),
                        end=m.end(),
                        canonical_code=f"FC2-PPV-{num}",
                        prefix="FC2-PPV",
                        number=num,
                        raw_code=m.group(0),
                        rule_id=rule_id,
                        confidence=0.98,
                    )
                )

        elif rule_id == "builtin_three_segment":
            for m in _RE_THREE_SEG_NUM.finditer(cleaned):
                p1, seg1, seg2 = m.group(1).upper(), m.group(2), m.group(3)
                if p1 in NON_SEGMENT_TOKENS or is_false_positive(p1, seg1, m.group(0)):
                    continue
                emit(
                    _CandidateMatch(
                        start=m.start(),
                        end=m.end(),
                        canonical_code=f"{p1}-{seg1}-{seg2}",
                        prefix=p1,
                        number=f"{seg1}-{seg2}",
                        raw_code=m.group(0),
                        rule_id=rule_id,
                        confidence=0.97,
                    )
                )
            for m in _RE_ALPHA_FOUR_DIGIT_SUB.finditer(cleaned):
                p, sub = m.group(1).upper(), m.group(2).upper()
                if is_false_positive(p, sub, m.group(0)):
                    continue
                emit(
                    _CandidateMatch(
                        start=m.start(),
                        end=m.end(),
                        canonical_code=f"{p}-{sub}",
                        prefix=p,
                        number=sub,
                        raw_code=m.group(0),
                        rule_id=rule_id,
                        confidence=0.97,
                    )
                )
            for m in _RE_THREE_SEG_ALPHA.finditer(cleaned):
                raw_p1, raw_p2, num_raw = m.group(1), m.group(3), m.group(4)
                if raw_p1.islower() and raw_p2.isupper():
                    continue
                p1, p2 = raw_p1.upper(), raw_p2.upper()
                if p1 in NON_SEGMENT_TOKENS or p2 in NON_SEGMENT_TOKENS:
                    continue
                if is_false_positive(p1, num_raw, m.group(0)):
                    continue
                num = normalize_serial_number(num_raw)
                emit(
                    _CandidateMatch(
                        start=m.start(),
                        end=m.end(),
                        canonical_code=f"{p1}-{p2}-{num}",
                        prefix=f"{p1}-{p2}",
                        number=num,
                        raw_code=m.group(0),
                        rule_id=rule_id,
                        confidence=0.96,
                    )
                )
            for m in _RE_ALPHA_SUB_PREFIX.finditer(cleaned):
                raw_p1, sep, raw_sub = m.group(1), m.group(2), m.group(3)
                p1, sub = raw_p1.upper(), raw_sub.upper()
                if p1 in NON_SEGMENT_TOKENS or is_false_positive(p1, sub, m.group(0)):
                    continue
                if sub[0] in {"N", "K"} and len(sub) == 5 and (sep == "_" or raw_p1.islower()):
                    continue
                if sub[0] in {"V", "P"} and len(sub) <= 3:
                    continue
                emit(
                    _CandidateMatch(
                        start=m.start(),
                        end=m.end(),
                        canonical_code=f"{p1}-{sub}",
                        prefix=p1,
                        number=sub,
                        raw_code=m.group(0),
                        rule_id=rule_id,
                        confidence=0.95,
                    )
                )

        elif rule_id == "builtin_date_indexed":
            for m in _RE_DATE_INDEXED.finditer(cleaned):
                date_part, sep, seq = m.group(1), m.group(2), m.group(3)
                if not is_plausible_six_digit_date(date_part):
                    continue
                emit(
                    _CandidateMatch(
                        start=m.start(),
                        end=m.end(),
                        canonical_code=f"{date_part}{sep}{seq}",
                        prefix="DATE-SEQ",
                        number=f"{date_part}{sep}{seq}",
                        raw_code=m.group(0),
                        rule_id=rule_id,
                        confidence=0.94,
                    )
                )

        elif rule_id == "builtin_num_alpha_prefix":
            for m in _RE_NUM_ALPHA_SEP.finditer(cleaned):
                lead_digits, alpha_part, raw_num = m.group(1), m.group(2).upper(), m.group(3)
                # Avoid treating a 4-digit calendar year prefix (19xx/20xx) as a series prefix.
                if len(lead_digits) == 4 and lead_digits[:2] in {"19", "20"}:
                    continue
                prefix = f"{lead_digits}{alpha_part}"
                num = normalize_serial_number(raw_num)
                if is_false_positive(prefix, num, m.group(0)):
                    continue
                emit(
                    _CandidateMatch(
                        start=m.start(),
                        end=m.end(),
                        canonical_code=f"{prefix}-{num}",
                        prefix=prefix,
                        number=num,
                        raw_code=m.group(0),
                        rule_id=rule_id,
                        confidence=0.97,
                    )
                )

        elif rule_id == "builtin_raw_compact_id":
            for m in _RE_RAW_COMPACT_EXPLICIT.finditer(cleaned):
                prefix = m.group(1).upper()
                raw_num = m.group(2)
                if is_false_positive(prefix, raw_num, m.group(0)):
                    continue
                num = normalize_serial_number(raw_num, from_raw_cid=True)
                emit(
                    _CandidateMatch(
                        start=m.start(),
                        end=m.end(),
                        canonical_code=f"{prefix}-{num}",
                        prefix=prefix,
                        number=num,
                        raw_code=m.group(0),
                        rule_id=rule_id,
                        confidence=0.95,
                    )
                )
            for m in _RE_RAW_COMPACT_ZERO_PADDED.finditer(cleaned):
                lead_digits = m.group(1) or ""
                alpha_part = m.group(2).upper()
                raw_num = m.group(3)
                # Leading digits >= 200 (e.g. 200-599 series) belong to the canonical prefix;
                # leading digits < 200 (1-199) represent a strippable platform vendor prefix.
                if lead_digits and int(lead_digits) >= 200:
                    prefix = f"{lead_digits}{alpha_part}"
                else:
                    prefix = alpha_part
                if is_false_positive(prefix, raw_num, m.group(0)):
                    continue
                num = normalize_serial_number(raw_num, from_raw_cid=True)
                emit(
                    _CandidateMatch(
                        start=m.start(),
                        end=m.end(),
                        canonical_code=f"{prefix}-{num}",
                        prefix=prefix,
                        number=num,
                        raw_code=m.group(0),
                        rule_id=rule_id,
                        confidence=0.94,
                    )
                )

        elif rule_id == "builtin_num_alpha_compact":
            for m in _RE_NUM_ALPHA_COMPACT.finditer(cleaned):
                prefix = m.group(1).upper()
                num = normalize_serial_number(m.group(2))
                if is_false_positive(prefix, num, m.group(0)):
                    continue
                emit(
                    _CandidateMatch(
                        start=m.start(),
                        end=m.end(),
                        canonical_code=f"{prefix}-{num}",
                        prefix=prefix,
                        number=num,
                        raw_code=m.group(0),
                        rule_id=rule_id,
                        confidence=0.92,
                    )
                )

        elif rule_id == "builtin_mixed_alnum_prefix":
            for m in _RE_MIXED_ALNUM.finditer(cleaned):
                prefix = m.group(1).upper()
                num = normalize_serial_number(m.group(2))
                if not (any(c.isalpha() for c in prefix) and any(c.isdigit() for c in prefix)):
                    continue
                if is_false_positive(prefix, num, m.group(0)):
                    continue
                emit(
                    _CandidateMatch(
                        start=m.start(),
                        end=m.end(),
                        canonical_code=f"{prefix}-{num}",
                        prefix=prefix,
                        number=num,
                        raw_code=m.group(0),
                        rule_id=rule_id,
                        confidence=0.95,
                    )
                )

        elif rule_id == "builtin_standard_hyphen":
            for m in _RE_STD_SEP.finditer(cleaned):
                prefix = m.group(1).upper()
                sep = m.group(2)
                raw_num = m.group(3)
                if is_false_positive(prefix, raw_num, m.group(0)):
                    continue
                is_weak_sep = "-" not in sep and "_" not in sep
                if is_weak_sep and len(raw_num) < 3:
                    continue
                num = normalize_serial_number(raw_num)
                emit(
                    _CandidateMatch(
                        start=m.start(),
                        end=m.end(),
                        canonical_code=f"{prefix}-{num}",
                        prefix=prefix,
                        number=num,
                        raw_code=m.group(0),
                        rule_id=rule_id,
                        confidence=0.96 if not is_weak_sep else 0.90,
                    )
                )

        elif rule_id == "builtin_standard_compact":
            for m in _RE_STD_COMPACT.finditer(cleaned):
                prefix = m.group(1).upper()
                raw_num = m.group(2)
                if is_false_positive(prefix, raw_num, m.group(0)):
                    continue
                num = normalize_serial_number(raw_num)
                emit(
                    _CandidateMatch(
                        start=m.start(),
                        end=m.end(),
                        canonical_code=f"{prefix}-{num}",
                        prefix=prefix,
                        number=num,
                        raw_code=m.group(0),
                        rule_id=rule_id,
                        confidence=0.88,
                    )
                )

        elif rule_id == "builtin_short_prefix":
            for m in _RE_SHORT_PREFIX.finditer(cleaned):
                prefix = m.group(1).upper()
                num = m.group(2)
                if is_false_positive(prefix, num, m.group(0)):
                    continue
                emit(
                    _CandidateMatch(
                        start=m.start(),
                        end=m.end(),
                        canonical_code=f"{prefix}{num}",
                        prefix=prefix,
                        number=num,
                        raw_code=m.group(0),
                        rule_id=rule_id,
                        confidence=0.85,
                    )
                )

    def _get_ancestor_candidates(self, parent_dir: str, full_path: str) -> list[str]:
        """Collect candidate parent/grandparent directory names for Stage 3 fallback."""
        dirs: list[str] = []
        if parent_dir:
            dirs.append(parent_dir)
        if full_path:
            normalized_path = full_path.replace("\\", "/")
            parts = [
                p
                for p in PurePath(normalized_path).parts[:-1]
                if p and p != "/" and not p.endswith(":")
            ]
            for folder in reversed(parts[-2:]):
                if folder not in dirs:
                    dirs.append(folder)
        return [d for d in dirs if d.strip().lower() not in _GENERIC_DIR_NAMES]

    def parse_line(
        self,
        text: str,
        parent_dir: str = "",
        full_path: str = "",
        source_name: str = "",
        source_type: str = "txt",
        line_number: int = 0,
        file_size: int = 0,
        file_ext: str = "",
    ) -> list[ParseResult]:
        """Parse a single line/filename into one or more ParseResult objects."""
        raw_text = (text or "").strip()
        if not raw_text:
            return []

        display_name = raw_text
        if not file_ext:
            _, detected_ext = split_known_extension(normalize_width(display_name))
            file_ext = detected_ext

        cleaned = clean_text(display_name)
        candidates = self._collect_candidates(cleaned)

        # Stage 2: direct matches from the filename/line.
        if candidates:
            results: list[ParseResult] = []
            seen_keys: set[tuple[str, str]] = set()
            is_multi = len(candidates) > 1

            for idx, cand in enumerate(candidates):
                next_start = (
                    candidates[idx + 1].start if idx + 1 < len(candidates) else len(cleaned)
                )
                local_suffix = cleaned[cand.end : next_start]
                tag_context = cleaned[cand.start : next_start] if is_multi else raw_text
                tags = extract_tags(tag_context, suffix=local_suffix, prefix=cand.prefix)
                part = extract_part(
                    cleaned[cand.start : next_start] if is_multi else raw_text,
                    suffix=local_suffix,
                )

                dedup_key = (cand.canonical_code, part)
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                results.append(
                    ParseResult(
                        canonical_code=cand.canonical_code,
                        prefix=cand.prefix,
                        number=cand.number,
                        raw_code=cand.raw_code,
                        raw_text=raw_text,
                        file_name=display_name,
                        parent_dir=parent_dir,
                        full_path=full_path,
                        source_name=source_name,
                        source_type=source_type,
                        line_number=line_number,
                        file_size=file_size,
                        file_ext=file_ext,
                        tags=tags,
                        part=part,
                        matched_rule=cand.rule_id,
                        cleaned_text=cleaned,
                        confidence=cand.confidence,
                        from_parent=False,
                        recognized=True,
                        note="",
                    )
                )
            return results

        # Stage 3: parent-folder fallback when filename has no recognizable code.
        # For Word/Excel document entries, never climb the host document's own disk path.
        fallback_full_path = full_path
        if source_type in {"word", "excel"} and full_path.lower().endswith(
            (".docx", ".xlsx", ".xlsm", ".doc", ".xls")
        ):
            fallback_full_path = ""
        for ancestor in self._get_ancestor_candidates(parent_dir, fallback_full_path):
            ancestor_cleaned = clean_text(ancestor)
            ancestor_cands = self._collect_candidates(ancestor_cleaned)
            if ancestor_cands:
                cand = ancestor_cands[0]
                ancestor_suffix = ancestor_cleaned[cand.end :]
                tags = extract_tags(
                    f"{ancestor} {raw_text}", suffix=ancestor_suffix, prefix=cand.prefix
                )
                part = extract_part(raw_text) or extract_part(ancestor, suffix=ancestor_suffix)
                return [
                    ParseResult(
                        canonical_code=cand.canonical_code,
                        prefix=cand.prefix,
                        number=cand.number,
                        raw_code=cand.raw_code,
                        raw_text=raw_text,
                        file_name=display_name,
                        parent_dir=parent_dir or ancestor,
                        full_path=full_path,
                        source_name=source_name,
                        source_type=source_type,
                        line_number=line_number,
                        file_size=file_size,
                        file_ext=file_ext,
                        tags=tags,
                        part=part,
                        matched_rule=cand.rule_id,
                        cleaned_text=cleaned,
                        confidence=round(max(0.65, cand.confidence - 0.18), 2),
                        from_parent=True,
                        recognized=True,
                        note="父目录回退识别",
                    )
                ]

        # Stage 4: unrecognized item (preserved for manual review/fix in UI).
        return [
            ParseResult(
                canonical_code="",
                prefix="",
                number="",
                raw_code="",
                raw_text=raw_text,
                file_name=display_name,
                parent_dir=parent_dir,
                full_path=full_path,
                source_name=source_name,
                source_type=source_type,
                line_number=line_number,
                file_size=file_size,
                file_ext=file_ext,
                tags=extract_tags(raw_text),
                part=extract_part(raw_text),
                matched_rule="",
                cleaned_text=cleaned,
                confidence=0.0,
                from_parent=False,
                recognized=False,
                note="未匹配有效编码",
            )
        ]

    def parse_single(
        self,
        text: str,
        parent_dir: str = "",
        full_path: str = "",
        source_name: str = "",
        source_type: str = "paste",
        line_number: int = 0,
    ) -> ParseResult:
        """Convenience helper returning the primary ParseResult for a single input line."""
        target_name = text.strip()
        derived_parent = parent_dir
        derived_path = full_path
        if not derived_parent and ("\\" in target_name or "/" in target_name):
            if _looks_like_file_path(target_name):
                derived_path = derived_path or target_name
                norm_p = target_name.replace("\\", "/").rstrip("/")
                if "/" in norm_p:
                    parts = [p for p in norm_p.split("/") if p]
                    if len(parts) >= 2:
                        target_name = parts[-1]
                        derived_parent = parts[-2]

        results = self.parse_line(
            target_name,
            parent_dir=derived_parent,
            full_path=derived_path,
            source_name=source_name,
            source_type=source_type,
            line_number=line_number,
        )
        if results:
            return results[0]
        return ParseResult(
            raw_text=text,
            file_name=target_name,
            parent_dir=derived_parent,
            full_path=derived_path,
            source_name=source_name,
            source_type=source_type,
            line_number=line_number,
            recognized=False,
            note="空输入",
        )

    def parse_item(self, item: ScannedItem) -> list[ParseResult]:
        """Parse a single ScannedItem produced by the scanner module."""
        target = item.file_name or item.raw_text
        results = self.parse_line(
            target,
            parent_dir=item.parent_dir,
            full_path=item.full_path,
            source_name=item.source_name,
            source_type=item.source_type,
            line_number=item.line_number,
            file_size=item.file_size,
            file_ext=item.file_ext,
        )
        for res in results:
            res.raw_text = item.raw_text
            if item.file_name:
                res.file_name = item.file_name
        return results

    def parse_items(
        self,
        items: Sequence[ScannedItem],
        ignore_companions: bool = True,
        companion_extensions: Optional[Collection[str]] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> list[ParseResult]:
        """Batch-parse a sequence of ScannedItems with optional companion filtering."""
        output: list[ParseResult] = []
        total = len(items)
        for idx, item in enumerate(items, start=1):
            if cancel_check and cancel_check():
                break
            if ignore_companions:
                if item.is_companion or (
                    item.file_ext and is_companion_extension(item.file_ext, companion_extensions)
                ):
                    if progress_callback and (idx % 100 == 0 or idx == total):
                        progress_callback(idx, total)
                    continue
            output.extend(self.parse_item(item))
            if progress_callback and (idx % 100 == 0 or idx == total):
                progress_callback(idx, total)
        return output

    def parse_text(
        self,
        text: str,
        source_name: str = "手动粘贴",
        ignore_companions: bool = True,
        companion_extensions: Optional[Collection[str]] = None,
    ) -> list[ParseResult]:
        """Scan and parse arbitrary multi-line text (plain list, paths, tree, dir, CSV)."""
        from .scanner import scan_text

        items = scan_text(
            text,
            source_name=source_name,
            source_type="paste",
            companion_extensions=companion_extensions,
        )
        return self.parse_items(
            items,
            ignore_companions=ignore_companions,
            companion_extensions=companion_extensions,
        )


def _looks_like_file_path(text: str) -> bool:
    """Distinguish a file path (`ABC-123/1.mp4`) from multi-code separators (`ABC-123 / DEF-456`)."""
    s = text.strip()
    if re.match(r"^[A-Za-z]:[\\/]", s) or s.startswith("\\\\") or s.startswith("/"):
        return True
    if "\\" in s and " \\ " not in s:
        return True
    if "/" in s and " / " not in s:
        if not re.match(r"^https?://", s, re.IGNORECASE):
            _, ext = split_known_extension(s.rsplit("/", 1)[-1])
            if ext:
                return True
    return False


def parse_code(text: str, registry: Optional[RuleRegistry] = None) -> ParseResult:
    """Module-level shortcut for parsing a single filename/string."""
    return CodeParser(registry).parse_single(text)
