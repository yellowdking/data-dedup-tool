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
from pathlib import PurePosixPath, PureWindowsPath
from typing import Callable, Collection, Optional, Sequence

from .io_utils import normalize_companion_extensions
from .models import CustomRule, ParseResult, ScannedItem
from .normalizer import (
    NON_SEGMENT_TOKENS,
    clean_text,
    extract_part,
    extract_tags,
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

# Glued ASCII letter suffixes allowed immediately after a numeric serial without separator.
# Rejects measurement units (`kg`, `px`, `ms`, `cm`), hex tails (`4372a5`), and word continuations.
_RE_GLUED_ALPHA_TAIL = re.compile(r"^[A-Za-z]+")
_ALLOWED_GLUED_SINGLE_LETTERS: frozenset[str] = frozenset({"A", "B", "C", "D", "E", "F", "U", "R"})
_ALLOWED_GLUED_MULTI_TOKENS: frozenset[str] = frozenset(
    {
        "UC",
        "CH",
        "CHS",
        "CHT",
        "UNC",
        "CD",
        "PT",
        "PART",
        "DISC",
        "DISK",
        "HD",
        "FHD",
        "UHD",
        "SD",
        "VR",
        "SUB",
        "SUBS",
        "RAW",
        "FULL",
        "LEAK",
        "PL",
        "PS",
        "HHB",
        "SO",
        "RE",
    }
)
_RESOLUTION_TAGS: frozenset[str] = frozenset({"8K", "4K", "FHD", "HD"})
_RE_SLASH_DATE = re.compile(r"(?<![A-Za-z0-9])(?:19|20)\d{2}/\d{1,2}/\d{1,2}(?![A-Za-z0-9])")


def _has_invalid_alnum_tail(cleaned: str, end_pos: int) -> bool:
    """Return True if match ends right before glued ASCII letters that are not known tag/part suffixes."""
    tail_slice = cleaned[end_pos:]
    if not tail_slice:
        return False
    m_tail = _RE_GLUED_ALPHA_TAIL.match(tail_slice)
    if not m_tail:
        return False
    tok = m_tail.group(0).upper()
    if len(tok) == 1:
        if tok not in _ALLOWED_GLUED_SINGLE_LETTERS:
            return True
        # Reject hex continuations like `4372a5`.
        if len(tail_slice) > 1 and tail_slice[1].isdigit():
            return True
        return False
    return tok not in _ALLOWED_GLUED_MULTI_TOKENS


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
    r"(?<![A-Za-z0-9])(\d{1,3})?([A-Za-z]{2,6})(0\d{3,4})(pl|ps|hhb|so|re|r)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# Compact Number+Letter series without separator (e.g. 300ABC123, 200XYZ2345).
_RE_NUM_ALPHA_COMPACT = re.compile(
    r"(?<![A-Za-z0-9])(\d{2,3}[A-Za-z]{2,6})([1-9]\d{2,3})(?![A-Za-z0-9])"
)

# Mixed letter-number prefixes with separator (e.g. A28-123, B2M-001, A3B2C-12).
_RE_MIXED_ALNUM = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,11})[-_](\d{2,5})(?![0-9])"
)

# Standard separated alphanumeric codes (hyphen, underscore, space, or dot).
_RE_STD_SEP = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{2,7})([-_\s.]+)(\d{2,6})(?![0-9])"
)

# Standard compact alphanumeric codes without separator (e.g. ABC123, PRJ0042).
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


def _merge_unique_tags(existing: list[str], extra: Sequence[str]) -> list[str]:
    """Merge tags while preserving order and avoiding duplicates."""
    merged = list(existing)
    for tag in extra:
        if tag not in merged:
            merged.append(tag)
    return merged


def _split_path_parts_without_anchor(path_str: str) -> tuple[bool, list[str]]:
    """Split a Windows/UNC/POSIX path into directory/file parts with drive/UNC/root anchor stripped."""
    s = path_str.strip()
    if not s:
        return False, []
    is_win = bool(re.match(r"^[A-Za-z]:[\\/]", s)) or s.startswith("\\\\") or s.startswith("//") or "\\" in s
    if is_win:
        p = PureWindowsPath(s)
        anchor = p.anchor
        parts = list(p.parts)
        if anchor and parts and parts[0] == anchor:
            parts = parts[1:]
        return True, [part for part in parts if part and not part.endswith(":")]
    p_posix = PurePosixPath(s)
    parts = [part for part in p_posix.parts if part and part != "/"]
    return False, parts


def _is_anchor_like_dir(name: str) -> bool:
    s = name.strip()
    if not s:
        return True
    if s in {"/", "\\"} or s.startswith("\\\\") or s.startswith("//"):
        return True
    if re.match(r"^[A-Za-z]:[\\/]?$", s):
        return True
    return False


def _locate_candidates_in_raw(
    norm_raw: str, candidates: Sequence[_CandidateMatch]
) -> list[tuple[int, int]]:
    """Map candidate matches back to approximate spans in `norm_raw` so pre-cleaned tags survive."""
    raw_low = norm_raw.lower()
    spans: list[tuple[int, int]] = []
    cursor = 0
    for cand in candidates:
        needle = cand.raw_code.lower()
        pos = raw_low.find(needle, cursor) if needle else -1
        if pos == -1 and needle:
            pos = raw_low.find(needle)
        if pos != -1:
            spans.append((pos, pos + len(needle)))
            cursor = pos + len(needle)
        else:
            spans.append((cursor, cursor))
    return spans


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
            if cand.start >= cand.end:
                return
            if _overlaps((cand.start, cand.end), occupied):
                return
            occupied.append((cand.start, cand.end))
            candidates.append(cand)

        for rule in self.registry.get_enabled_rules_cached():
            if not rule.is_builtin:
                self._apply_custom_rule(rule, cleaned, _try_add)
            else:
                handler = _BUILTIN_HANDLERS.get(rule.id)
                if handler is not None:
                    handler(cleaned, _try_add)

        candidates.sort(key=lambda c: c.start)
        return candidates

    def _apply_custom_rule(
        self,
        rule: CustomRule,
        cleaned: str,
        emit: Callable[[_CandidateMatch], None],
    ) -> None:
        regex = self.registry.get_compiled_pattern(rule)
        if regex is None:
            return
        for m in regex.finditer(cleaned):
            # Never allow zero-width custom matches to emit bogus records.
            if m.start() == m.end():
                continue
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
        """Dispatch a single built-in extraction rule by ID."""
        handler = _BUILTIN_HANDLERS.get(rule_id)
        if handler is not None:
            handler(cleaned, emit)

    def _get_ancestor_candidates(
        self,
        parent_dir: str,
        full_path: str,
        source_type: str = "",
        source_name: str = "",
    ) -> list[str]:
        """Collect candidate parent/grandparent directory names for Stage 3 fallback.

        Strips drive/UNC anchors explicitly and clamps folder-scan ancestors inside the
        scanned root directory so parent folders above root never leak into results.
        """
        dirs: list[str] = []

        # When scanning a local folder, bound ancestor climbing strictly inside `source_name` (root).
        clamped_from_root = False
        if source_type == "folder" and source_name and full_path:
            is_win_fp, fp_parts = _split_path_parts_without_anchor(full_path)
            is_win_rt, rt_parts = _split_path_parts_without_anchor(source_name)
            if is_win_fp == is_win_rt:
                cmp_fp = [p.lower() for p in fp_parts] if is_win_fp else fp_parts
                cmp_rt = [p.lower() for p in rt_parts] if is_win_rt else rt_parts
                # Also verify same anchor on Windows (drive/UNC root).
                same_anchor = True
                if is_win_fp:
                    same_anchor = (
                        PureWindowsPath(full_path).anchor.lower()
                        == PureWindowsPath(source_name).anchor.lower()
                    )
                if same_anchor and len(fp_parts) > len(rt_parts) and cmp_fp[: len(rt_parts)] == cmp_rt:
                    rel_dirs = fp_parts[len(rt_parts) : -1]
                    for folder in reversed(rel_dirs[-2:]):
                        if folder not in dirs:
                            dirs.append(folder)
                    clamped_from_root = True

        if not clamped_from_root:
            if parent_dir and not _is_anchor_like_dir(parent_dir):
                dirs.append(parent_dir)
            if full_path:
                _, parts = _split_path_parts_without_anchor(full_path)
                for folder in reversed(parts[:-1][-2:]):
                    if folder not in dirs and not _is_anchor_like_dir(folder):
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
        norm_raw = normalize_width(display_name)
        if not file_ext:
            _, detected_ext = split_known_extension(norm_raw)
            file_ext = detected_ext

        cleaned = clean_text(display_name)
        candidates = self._collect_candidates(cleaned)

        # Stage 2: direct matches from the filename/line.
        if candidates:
            return self._assemble_stage2_results(
                candidates=candidates,
                cleaned=cleaned,
                raw_text=raw_text,
                norm_raw=norm_raw,
                display_name=display_name,
                parent_dir=parent_dir,
                full_path=full_path,
                source_name=source_name,
                source_type=source_type,
                line_number=line_number,
                file_size=file_size,
                file_ext=file_ext,
            )

        # Stage 3: parent-folder fallback when filename has no recognizable code.
        # For Word/Excel document entries, never climb the host document's own disk path.
        fallback_full_path = full_path
        if source_type in {"word", "excel"} and full_path.lower().endswith(
            (".docx", ".xlsx", ".xlsm", ".doc", ".xls")
        ):
            fallback_full_path = ""
        for ancestor in self._get_ancestor_candidates(
            parent_dir,
            fallback_full_path,
            source_type=source_type,
            source_name=source_name,
        ):
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

    def _assemble_stage2_results(
        self,
        *,
        candidates: list[_CandidateMatch],
        cleaned: str,
        raw_text: str,
        norm_raw: str,
        display_name: str,
        parent_dir: str,
        full_path: str,
        source_name: str,
        source_type: str,
        line_number: int,
        file_size: int,
        file_ext: str,
    ) -> list[ParseResult]:
        """Assemble Stage 2 ParseResult objects, preserving tags and merging repeated-code mentions."""
        raw_spans = _locate_candidates_in_raw(norm_raw, candidates)
        distinct_codes = {c.canonical_code for c in candidates}

        # Single distinct code (possibly mentioned multiple times, e.g. `[ABC-123] ABC-123-C [4K].mp4`
        # or `[ABC-123] ABC-123-CD1.mp4`): extract global tags and merge non-conflicting repeats.
        if len(distinct_codes) == 1:
            base_cand = candidates[0]
            merged_tags = extract_tags(raw_text, prefix=base_cand.prefix)
            parts_found: list[tuple[str, _CandidateMatch]] = []

            for idx, cand in enumerate(candidates):
                next_clean_start = (
                    candidates[idx + 1].start if idx + 1 < len(candidates) else len(cleaned)
                )
                local_suffix = cleaned[cand.end : next_clean_start]
                r_start, r_end = raw_spans[idx]
                next_raw_start = (
                    raw_spans[idx + 1][0] if idx + 1 < len(raw_spans) else len(norm_raw)
                )
                raw_suffix = norm_raw[r_end:next_raw_start] if next_raw_start >= r_end else ""
                occ_tags = extract_tags(
                    norm_raw[r_start:next_raw_start] if next_raw_start > r_start else local_suffix,
                    suffix=raw_suffix or local_suffix,
                    prefix=cand.prefix,
                )
                merged_tags = _merge_unique_tags(merged_tags, occ_tags)

                occ_part = extract_part(
                    norm_raw[r_start:next_raw_start] if next_raw_start > r_start else cleaned[cand.start : next_clean_start],
                    suffix=local_suffix,
                )
                if occ_part and all(existing_p != occ_part for existing_p, _ in parts_found):
                    parts_found.append((occ_part, cand))

            if not parts_found:
                global_part = extract_part(raw_text)
                parts_found.append((global_part, base_cand))

            return [
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
                    tags=list(merged_tags),
                    part=part_val,
                    matched_rule=cand.rule_id,
                    cleaned_text=cleaned,
                    confidence=cand.confidence,
                    from_parent=False,
                    recognized=True,
                    note="",
                )
                for part_val, cand in parts_found
            ]

        # Multiple distinct codes on the same line: use per-candidate raw slices so pre-cleaned
        # resolution/subtitle tags survive, plus inherit leading global resolution/VR tags.
        first_raw_start = raw_spans[0][0] if raw_spans else 0
        leading_tags = extract_tags(norm_raw[:first_raw_start]) if first_raw_start > 0 else []
        leading_global = [t for t in leading_tags if t in _RESOLUTION_TAGS or t == "VR"]

        results: list[ParseResult] = []
        by_code_idx: dict[str, list[int]] = {}

        for idx, cand in enumerate(candidates):
            next_clean_start = (
                candidates[idx + 1].start if idx + 1 < len(candidates) else len(cleaned)
            )
            local_suffix = cleaned[cand.end : next_clean_start]
            r_start, r_end = raw_spans[idx]
            next_raw_start = (
                raw_spans[idx + 1][0] if idx + 1 < len(raw_spans) else len(norm_raw)
            )
            raw_slice = (
                norm_raw[r_start:next_raw_start]
                if next_raw_start > r_start
                else cleaned[cand.start : next_clean_start]
            )
            raw_suffix = norm_raw[r_end:next_raw_start] if next_raw_start >= r_end else local_suffix

            tags = extract_tags(raw_slice, suffix=raw_suffix or local_suffix, prefix=cand.prefix)
            if leading_global:
                has_res = any(t in _RESOLUTION_TAGS for t in tags)
                for g_tag in leading_global:
                    if g_tag in _RESOLUTION_TAGS and has_res:
                        continue
                    if g_tag not in tags:
                        tags.append(g_tag)

            part = extract_part(raw_slice, suffix=local_suffix)

            existing_indices = by_code_idx.get(cand.canonical_code, [])
            merged_into_existing = False
            for r_idx in existing_indices:
                existing_res = results[r_idx]
                if existing_res.part == part:
                    existing_res.tags = _merge_unique_tags(existing_res.tags, tags)
                    merged_into_existing = True
                    break
                if not existing_res.part and part:
                    existing_res.part = part
                    existing_res.tags = _merge_unique_tags(existing_res.tags, tags)
                    merged_into_existing = True
                    break
                if existing_res.part and not part:
                    existing_res.tags = _merge_unique_tags(existing_res.tags, tags)
                    merged_into_existing = True
                    break

            if merged_into_existing:
                continue

            by_code_idx.setdefault(cand.canonical_code, []).append(len(results))
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
        original_text = (text or "").strip()
        target_name = original_text
        derived_parent = parent_dir
        derived_path = full_path
        if not derived_parent and ("\\" in target_name or "/" in target_name):
            if _looks_like_file_path(target_name):
                derived_path = derived_path or target_name
                _, parts = _split_path_parts_without_anchor(target_name)
                if parts:
                    target_name = parts[-1]
                    if len(parts) >= 2 and not _is_anchor_like_dir(parts[-2]):
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
            primary = results[0]
            primary.raw_text = original_text
            return primary
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
        """Batch-parse a sequence of ScannedItems with optional companion filtering.

        When `companion_extensions` is explicitly provided, it overrides scan-time
        default companion flags so callers can either widen or narrow excluded extensions.
        """
        output: list[ParseResult] = []
        total = len(items)
        comp_set = normalize_companion_extensions(companion_extensions)
        explicit_custom = companion_extensions is not None

        for idx, item in enumerate(items, start=1):
            if cancel_check and cancel_check():
                break
            if ignore_companions:
                ext_low = item.file_ext.lower() if item.file_ext else ""
                if explicit_custom:
                    is_comp = bool(ext_low and ext_low in comp_set)
                    if not is_comp and not ext_low and item.is_companion:
                        _, detected = split_known_extension(item.file_name or item.raw_text)
                        is_comp = bool(detected and detected in comp_set)
                else:
                    is_comp = item.is_companion or bool(ext_low and ext_low in comp_set)
                if is_comp:
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


# ---------------------------------------------------------------------------
# Built-in rule handlers (dispatched by rule_id for low complexity & clarity)
# ---------------------------------------------------------------------------

def _handle_composite_code(cleaned: str, emit: Callable[[_CandidateMatch], None]) -> None:
    for m in _RE_COMPOSITE_FC2.finditer(cleaned):
        if _has_invalid_alnum_tail(cleaned, m.end()):
            continue
        num = m.group(1)
        emit(
            _CandidateMatch(
                start=m.start(),
                end=m.end(),
                canonical_code=f"FC2-PPV-{num}",
                prefix="FC2-PPV",
                number=num,
                raw_code=m.group(0),
                rule_id="builtin_composite_code",
                confidence=0.98,
            )
        )


def _handle_three_segment(cleaned: str, emit: Callable[[_CandidateMatch], None]) -> None:
    for m in _RE_THREE_SEG_NUM.finditer(cleaned):
        if _has_invalid_alnum_tail(cleaned, m.end()):
            continue
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
                rule_id="builtin_three_segment",
                confidence=0.97,
            )
        )
    for m in _RE_ALPHA_FOUR_DIGIT_SUB.finditer(cleaned):
        if _has_invalid_alnum_tail(cleaned, m.end()):
            continue
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
                rule_id="builtin_three_segment",
                confidence=0.97,
            )
        )
    for m in _RE_THREE_SEG_ALPHA.finditer(cleaned):
        if _has_invalid_alnum_tail(cleaned, m.end()):
            continue
        p1, p2, num_raw = m.group(1).upper(), m.group(3).upper(), m.group(4)
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
                rule_id="builtin_three_segment",
                confidence=0.96,
            )
        )
    for m in _RE_ALPHA_SUB_PREFIX.finditer(cleaned):
        p1, sub = m.group(1).upper(), m.group(3).upper()
        if p1 in NON_SEGMENT_TOKENS or is_false_positive(p1, sub, m.group(0)):
            continue
        # Defer short single-letter N/K 4-digit codes (e.g. `Tokyo-n1287` / `tokyo-n1287`)
        # consistently to `builtin_short_prefix` regardless of prefix casing.
        if sub[0] in {"N", "K"} and len(sub) == 5:
            continue
        # Reject software version suffixes (`app-v100`, `pkg-v20`) and short page markers (`p12`).
        if sub[0] == "V" or (sub[0] == "P" and len(sub) <= 3):
            continue
        emit(
            _CandidateMatch(
                start=m.start(),
                end=m.end(),
                canonical_code=f"{p1}-{sub}",
                prefix=p1,
                number=sub,
                raw_code=m.group(0),
                rule_id="builtin_three_segment",
                confidence=0.95,
            )
        )


def _handle_date_indexed(cleaned: str, emit: Callable[[_CandidateMatch], None]) -> None:
    for m in _RE_DATE_INDEXED.finditer(cleaned):
        if _has_invalid_alnum_tail(cleaned, m.end()):
            continue
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
                rule_id="builtin_date_indexed",
                confidence=0.94,
            )
        )


def _handle_num_alpha_prefix(cleaned: str, emit: Callable[[_CandidateMatch], None]) -> None:
    for m in _RE_NUM_ALPHA_SEP.finditer(cleaned):
        if _has_invalid_alnum_tail(cleaned, m.end()):
            continue
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
                rule_id="builtin_num_alpha_prefix",
                confidence=0.97,
            )
        )


def _handle_raw_compact_id(cleaned: str, emit: Callable[[_CandidateMatch], None]) -> None:
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
                rule_id="builtin_raw_compact_id",
                confidence=0.95,
            )
        )
    for m in _RE_RAW_COMPACT_ZERO_PADDED.finditer(cleaned):
        lead_digits = m.group(1) or ""
        alpha_part = m.group(2).upper()
        raw_num = m.group(3)
        trail_tag = m.group(4) or ""
        # Bare 4-digit zero-padded compact codes (e.g. `PRJ0042`, `MD0123`, `SER0001`) without
        # leading vendor digits or trailing platform tags must defer to `builtin_standard_compact`
        # so they normalize identically to hyphenated `PRJ-0042` / `MD-0123` / `SER-0001`.
        if len(raw_num) == 4 and not lead_digits and not trail_tag:
            continue
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
                rule_id="builtin_raw_compact_id",
                confidence=0.94,
            )
        )


def _handle_num_alpha_compact(cleaned: str, emit: Callable[[_CandidateMatch], None]) -> None:
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
                rule_id="builtin_num_alpha_compact",
                confidence=0.92,
            )
        )


def _handle_mixed_alnum_prefix(cleaned: str, emit: Callable[[_CandidateMatch], None]) -> None:
    for m in _RE_MIXED_ALNUM.finditer(cleaned):
        if _has_invalid_alnum_tail(cleaned, m.end()):
            continue
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
                rule_id="builtin_mixed_alnum_prefix",
                confidence=0.95,
            )
        )


def _handle_standard_hyphen(cleaned: str, emit: Callable[[_CandidateMatch], None]) -> None:
    for m in _RE_STD_SEP.finditer(cleaned):
        if _has_invalid_alnum_tail(cleaned, m.end()):
            continue
        prefix = m.group(1).upper()
        sep = m.group(2)
        raw_num = m.group(3)
        if is_false_positive(prefix, raw_num, m.group(0)):
            continue
        is_weak_sep = "-" not in sep and "_" not in sep
        if is_weak_sep:
            if len(raw_num) < 3:
                continue
            # Reject space/dot-separated calendar years in prose titles (`Summary 2024`, `May 2024`).
            if len(raw_num) == 4 and 1900 <= int(raw_num) <= 2099:
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
                rule_id="builtin_standard_hyphen",
                confidence=0.96 if not is_weak_sep else 0.90,
            )
        )


def _handle_standard_compact(cleaned: str, emit: Callable[[_CandidateMatch], None]) -> None:
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
                rule_id="builtin_standard_compact",
                confidence=0.88,
            )
        )


def _handle_short_prefix(cleaned: str, emit: Callable[[_CandidateMatch], None]) -> None:
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
                rule_id="builtin_short_prefix",
                confidence=0.85,
            )
        )


_BUILTIN_HANDLERS: dict[str, Callable[[str, Callable[[_CandidateMatch], None]], None]] = {
    "builtin_composite_code": _handle_composite_code,
    "builtin_three_segment": _handle_three_segment,
    "builtin_date_indexed": _handle_date_indexed,
    "builtin_num_alpha_prefix": _handle_num_alpha_prefix,
    "builtin_raw_compact_id": _handle_raw_compact_id,
    "builtin_num_alpha_compact": _handle_num_alpha_compact,
    "builtin_mixed_alnum_prefix": _handle_mixed_alnum_prefix,
    "builtin_standard_hyphen": _handle_standard_hyphen,
    "builtin_standard_compact": _handle_standard_compact,
    "builtin_short_prefix": _handle_short_prefix,
}


def _looks_like_file_path(text: str) -> bool:
    """Distinguish a file path (`ABC-123/1.mp4`) from multi-code separators or dates."""
    s = text.strip()
    if not s:
        return False
    s_low = s.lower()
    if "http://" in s_low or "https://" in s_low:
        return False
    if re.match(r"^[A-Za-z]:[\\/]", s) or s.startswith("\\\\") or s.startswith("//"):
        return True
    if s.startswith("/") and " / " not in s:
        return True
    if "\\" in s and " \\ " not in s:
        return True
    if "/" in s and " / " not in s and not _RE_SLASH_DATE.search(s):
        if s.startswith(("./", "../")):
            return True
        _, ext = split_known_extension(s.rsplit("/", 1)[-1])
        if ext:
            return True
    return False


def parse_code(text: str, registry: Optional[RuleRegistry] = None) -> ParseResult:
    """Module-level shortcut for parsing a single filename/string."""
    return CodeParser(registry).parse_single(text)
