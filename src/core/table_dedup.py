"""Multi-table deduplication, set-analysis, and shared row-key helpers for 数据查重程序.

Provides ``run_multi_table_dedup`` supporting both ``smart_code`` extraction via
``CodeParser`` and general-purpose ``column_value`` (single or composite key)
matching, along with shared key-extraction and normalization utilities reused by
``table_merge``.

All identifiers, labels, and comments are strictly neutral.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from .models import ParseResult
from .normalizer import extract_part, extract_tags, normalize_width
from .parser import CodeParser
from .table_data import (
    DataTable,
    TableDedupConfig,
    TableDedupReport,
    TableDuplicateGroup,
    TableRowRef,
)

DEDUP_MODE_SMART_CODE = "smart_code"
DEDUP_MODE_COLUMN_VALUE = "column_value"

_VALID_DEDUP_MODES = {DEDUP_MODE_SMART_CODE, DEDUP_MODE_COLUMN_VALUE}
_RE_MULTI_SPACE = re.compile(r"\s+")


# ----------------------------------------------------------------------
# Key normalization & extraction helpers (shared with table_merge)
# ----------------------------------------------------------------------


def _resolve_table_key_columns(
    table: DataTable,
    key_columns_map: Dict[str, Any],
    *,
    fallback_headers: Optional[Sequence[str]] = None,
) -> List[int]:
    """Resolve 0-based column indices to use as key columns for ``table``.

    Lookup order in ``key_columns_map``:
    1. ``table.table_id``
    2. ``table.name``
    3. ``"*"`` (global default)

    When none match explicitly, falls back to ``fallback_headers`` if supplied,
    or column 0 if the table has at least one column.
    """
    raw_spec: Any = None
    if key_columns_map:
        if table.table_id in key_columns_map:
            raw_spec = key_columns_map[table.table_id]
        elif table.name in key_columns_map:
            raw_spec = key_columns_map[table.name]
        elif "*" in key_columns_map:
            raw_spec = key_columns_map["*"]

    col_names: List[str] = []
    if isinstance(raw_spec, str):
        if raw_spec.strip():
            col_names = [raw_spec.strip()]
    elif isinstance(raw_spec, (list, tuple)):
        col_names = [str(c).strip() for c in raw_spec if str(c).strip()]

    indices: List[int] = []
    for name in col_names:
        idx = table.get_column_index(name)
        if idx >= 0 and idx not in indices:
            indices.append(idx)

    if indices:
        return indices

    if fallback_headers:
        for h in fallback_headers:
            idx = table.get_column_index(h)
            if idx >= 0:
                return [idx]

    return [0] if table.col_count > 0 else []


def _find_common_headers(tables: Sequence[DataTable]) -> List[str]:
    """Return headers present in every table, preserving the first table's order."""
    if not tables:
        return []
    common = set(tables[0].headers)
    for t in tables[1:]:
        common &= set(t.headers)
    return [h for h in tables[0].headers if h in common]


def _normalize_value_key(
    value: str,
    *,
    ignore_case: bool = True,
    strip_whitespace: bool = True,
    ignore_punctuation: bool = False,
) -> str:
    """Normalize a single cell string for general column-value comparison."""
    text = normalize_width(str(value or ""))
    if strip_whitespace:
        text = text.strip()
    if ignore_punctuation:
        text = "".join(
            ch for ch in text if unicodedata.category(ch)[0] not in ("P", "S")
        )
        if strip_whitespace:
            text = _RE_MULTI_SPACE.sub(" ", text).strip()
    if ignore_case:
        text = text.lower()
    return text


def _extract_column_value_key(
    row: Sequence[str],
    col_indices: Sequence[int],
    *,
    ignore_case: bool = True,
    strip_whitespace: bool = True,
    ignore_punctuation: bool = False,
) -> str:
    """Build a normalized single or composite key from specified row columns."""
    if not col_indices:
        return ""
    parts: List[str] = []
    has_non_empty = False
    for idx in col_indices:
        raw = row[idx] if 0 <= idx < len(row) else ""
        norm = _normalize_value_key(
            raw,
            ignore_case=ignore_case,
            strip_whitespace=strip_whitespace,
            ignore_punctuation=ignore_punctuation,
        )
        if norm:
            has_non_empty = True
        parts.append(norm)
    if not has_non_empty:
        return ""
    if len(parts) == 1:
        return parts[0]
    return " | ".join(parts)


@dataclass
class _ExtractedRowKey:
    key_value: str
    parsed_code: str = ""
    tags: List[str] = None  # type: ignore[assignment]
    part: str = ""
    parse_result: Optional[ParseResult] = None

    def __post_init__(self) -> None:
        if self.tags is None:
            self.tags = []

    @property
    def tags_str(self) -> str:
        return ",".join(self.tags)


def _extract_smart_code_for_row(
    table: DataTable,
    row: Sequence[str],
    row_index_1based: int,
    parser: CodeParser,
    *,
    preferred_col_indices: Sequence[int] = (),
    scan_all_columns: bool = True,
) -> _ExtractedRowKey:
    """Extract canonical catalog code, version tags, and part marker from a table row."""
    col_order: List[int] = []
    for idx in preferred_col_indices:
        if 0 <= idx < len(row) and idx not in col_order:
            col_order.append(idx)
    if scan_all_columns or not preferred_col_indices:
        for idx in range(len(row)):
            if idx not in col_order:
                col_order.append(idx)

    matched_res: Optional[ParseResult] = None
    for idx in col_order:
        cell = str(row[idx] or "").strip()
        if not cell:
            continue
        results = parser.parse_line(
            cell,
            source_name=table.name,
            source_type=table.source_type or "table",
            line_number=row_index_1based,
        )
        for res in results:
            if res.recognized and res.canonical_code:
                matched_res = res
                break
        if matched_res is not None:
            break

    # Fallback: try joining non-empty candidate cells with safe separator
    if matched_res is None and (scan_all_columns or len(col_order) > 1):
        candidate_cells = [
            str(row[idx] or "").strip()
            for idx in col_order
            if 0 <= idx < len(row) and str(row[idx] or "").strip()
        ]
        if len(candidate_cells) >= 2:
            combined = " | ".join(candidate_cells)
            for res in parser.parse_line(
                combined,
                source_name=table.name,
                source_type=table.source_type or "table",
                line_number=row_index_1based,
            ):
                if res.recognized and res.canonical_code:
                    matched_res = res
                    break

    if matched_res is None:
        return _ExtractedRowKey(key_value="")

    canonical = matched_res.canonical_code.strip().upper()
    full_row_text = " ".join(str(v or "").strip() for v in row if str(v or "").strip())
    tags: List[str] = list(matched_res.tags)
    for t in extract_tags(full_row_text, prefix=matched_res.prefix):
        if t not in tags:
            tags.append(t)
    part = matched_res.part or extract_part(full_row_text)

    matched_res.tags = tags
    matched_res.part = part

    return _ExtractedRowKey(
        key_value=canonical,
        parsed_code=canonical,
        tags=tags,
        part=part,
        parse_result=matched_res,
    )


def _pad_row(row: Sequence[str], width: int) -> List[str]:
    """Return a list of strings padded or truncated cleanly to ``width``."""
    out = [str(v) if v is not None else "" for v in row[:width]]
    if len(out) < width:
        out.extend([""] * (width - len(out)))
    return out


def _make_unique_header(candidate: str, existing: set[str]) -> str:
    """Ensure ``candidate`` does not collide with ``existing`` headers."""
    if candidate not in existing:
        return candidate
    suffix = 2
    while f"{candidate}_{suffix}" in existing:
        suffix += 1
    return f"{candidate}_{suffix}"


# ----------------------------------------------------------------------
# Multi-table deduplication & set analysis
# ----------------------------------------------------------------------


def run_multi_table_dedup(
    tables: Sequence[DataTable],
    config: Optional[TableDedupConfig] = None,
    parser_engine: Optional[CodeParser] = None,
) -> TableDedupReport:
    """Analyze duplicates, intersection, and exclusives across one or more DataTables.

    Supports:
    - ``mode="smart_code"``: normalizes codes via ``CodeParser`` across arbitrary
      formats while preserving extracted tags and multi-part markers.
    - ``mode="column_value"``: general-purpose single or composite column-key
      matching with configurable case/whitespace/punctuation normalization.
    """
    cfg = config or TableDedupConfig()
    if cfg.mode not in _VALID_DEDUP_MODES:
        raise ValueError(f"不支持的查重模式：{cfg.mode}")

    table_list = list(tables)
    if not table_list:
        return TableDedupReport(config=cfg)

    parser = parser_engine or (
        CodeParser() if cfg.mode == DEDUP_MODE_SMART_CODE else None
    )
    common_headers = _find_common_headers(table_list)

    table_key_indices: Dict[str, List[int]] = {}
    for tbl in table_list:
        if cfg.mode == DEDUP_MODE_SMART_CODE:
            explicit: List[int] = []
            raw_spec = (
                cfg.key_columns.get(tbl.table_id)
                or cfg.key_columns.get(tbl.name)
                or cfg.key_columns.get("*")
            )
            if isinstance(raw_spec, str) and raw_spec.strip():
                idx = tbl.get_column_index(raw_spec.strip())
                if idx >= 0:
                    explicit.append(idx)
            elif isinstance(raw_spec, (list, tuple)):
                for col_name in raw_spec:
                    idx = tbl.get_column_index(str(col_name).strip())
                    if idx >= 0 and idx not in explicit:
                        explicit.append(idx)
            table_key_indices[tbl.table_id] = explicit
        else:
            table_key_indices[tbl.table_id] = _resolve_table_key_columns(
                tbl, cfg.key_columns, fallback_headers=common_headers
            )

    key_to_rows: Dict[str, List[TableRowRef]] = {}
    table_key_rows: Dict[str, Dict[str, List[TableRowRef]]] = {
        t.table_id: {} for t in table_list
    }
    unrecognized_rows: List[TableRowRef] = []
    unrecognized_count_by_table: Dict[str, int] = {t.table_id: 0 for t in table_list}
    total_rows = 0

    for tbl in table_list:
        total_rows += tbl.row_count
        key_cols = table_key_indices[tbl.table_id]
        headers_copy = list(tbl.headers)
        col_n = len(headers_copy)

        for idx_0, raw_row in enumerate(tbl.rows):
            row_idx_1 = idx_0 + 1
            row_vals = _pad_row(raw_row, col_n) if col_n else [str(v) for v in raw_row]

            if cfg.mode == DEDUP_MODE_SMART_CODE:
                assert parser is not None
                extracted = _extract_smart_code_for_row(
                    tbl,
                    row_vals,
                    row_idx_1,
                    parser,
                    preferred_col_indices=key_cols,
                    scan_all_columns=cfg.scan_all_columns_for_code,
                )
                key_val = extracted.key_value
                parsed_code = extracted.parsed_code
                tags_str = extracted.tags_str
                part = extracted.part
            else:
                key_val = _extract_column_value_key(
                    row_vals,
                    key_cols,
                    ignore_case=cfg.ignore_case,
                    strip_whitespace=cfg.strip_whitespace,
                    ignore_punctuation=cfg.ignore_punctuation,
                )
                parsed_code = ""
                tags_str = ""
                part = ""

            ref = TableRowRef(
                table_id=tbl.table_id,
                table_name=tbl.name,
                row_index=row_idx_1,
                key_value=key_val,
                row_data=row_vals,
                headers=headers_copy,
                parsed_code=parsed_code,
                tags=tags_str,
                part=part,
            )

            if not key_val:
                unrecognized_rows.append(ref)
                unrecognized_count_by_table[tbl.table_id] += 1
                continue

            key_to_rows.setdefault(key_val, []).append(ref)
            table_key_rows[tbl.table_id].setdefault(key_val, []).append(ref)

    cross_table_groups: List[TableDuplicateGroup] = []
    intersection_keys: List[str] = []
    intersection_rows: List[TableRowRef] = []
    unique_by_table: Dict[str, List[TableRowRef]] = {t.table_id: [] for t in table_list}
    cross_dup_keys: set[str] = set()

    num_tables = len(table_list)
    all_table_ids = {t.table_id for t in table_list}

    for key_val, refs in key_to_rows.items():
        distinct_tids = list(dict.fromkeys(r.table_id for r in refs))
        distinct_tnames = list(dict.fromkeys(r.table_name for r in refs))
        t_count = len(distinct_tids)

        if t_count >= 2:
            cross_dup_keys.add(key_val)
            if cfg.include_cross_table:
                cross_table_groups.append(
                    TableDuplicateGroup(
                        key_value=key_val,
                        is_cross_table=True,
                        table_names=distinct_tnames,
                        table_count=t_count,
                        total_rows=len(refs),
                        rows=list(refs),
                    )
                )
            if num_tables >= 2 and set(distinct_tids) == all_table_ids:
                intersection_keys.append(key_val)
        elif t_count == 1:
            unique_by_table[distinct_tids[0]].extend(refs)

    cross_table_groups.sort(key=lambda g: (-g.table_count, -g.total_rows, g.key_value))
    intersection_keys.sort()
    for key_val in intersection_keys:
        intersection_rows.extend(key_to_rows[key_val])

    for tid in unique_by_table:
        unique_by_table[tid].sort(key=lambda r: r.row_index)

    intra_table_groups: List[TableDuplicateGroup] = []
    intra_dup_row_count_by_table: Dict[str, int] = {t.table_id: 0 for t in table_list}
    intra_dup_group_count_by_table: Dict[str, int] = {t.table_id: 0 for t in table_list}

    for tbl in table_list:
        per_key = table_key_rows[tbl.table_id]
        for key_val, t_refs in per_key.items():
            if len(t_refs) >= 2:
                intra_dup_row_count_by_table[tbl.table_id] += len(t_refs)
                intra_dup_group_count_by_table[tbl.table_id] += 1
                if cfg.include_intra_table:
                    intra_table_groups.append(
                        TableDuplicateGroup(
                            key_value=key_val,
                            is_cross_table=False,
                            table_names=[tbl.name],
                            table_count=1,
                            total_rows=len(t_refs),
                            rows=list(t_refs),
                        )
                    )

    intra_table_groups.sort(
        key=lambda g: (-g.total_rows, g.table_names[0] if g.table_names else "", g.key_value)
    )

    table_stats: Dict[str, Dict[str, Any]] = {}
    for tbl in table_list:
        per_key = table_key_rows[tbl.table_id]
        cross_rows_in_tbl = sum(
            len(refs) for k, refs in per_key.items() if k in cross_dup_keys
        )
        table_stats[tbl.table_id] = {
            "table_id": tbl.table_id,
            "table_name": tbl.name,
            "source_type": tbl.source_type,
            "total_rows": tbl.row_count,
            "unique_keys": len(per_key),
            "cross_dup_rows": cross_rows_in_tbl,
            "intra_dup_rows": intra_dup_row_count_by_table[tbl.table_id],
            "intra_dup_groups": intra_dup_group_count_by_table[tbl.table_id],
            "exclusive_rows": len(unique_by_table[tbl.table_id]),
            "unrecognized_rows": unrecognized_count_by_table[tbl.table_id],
        }

    return TableDedupReport(
        config=cfg,
        table_names=[t.name for t in table_list],
        table_stats=table_stats,
        cross_table_groups=cross_table_groups,
        intra_table_groups=intra_table_groups,
        intersection_keys=intersection_keys,
        intersection_rows=intersection_rows,
        unique_by_table=unique_by_table,
        total_rows=total_rows,
        unique_key_count=len(key_to_rows),
        unrecognized_rows=unrecognized_rows,
    )
