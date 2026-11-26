"""Multi-table merging (union_all, union_dedup, join) and DB import for 数据查重程序.

Provides:
- ``merge_tables``: vertical concatenation (``union_all``), vertical merge with
  deduplication across 5 keep strategies including ``coalesce`` (``union_dedup``),
  and horizontal ``left``/``inner``/``outer`` ``join``.
- ``import_table_to_database``: clean conversion of a ``DataTable`` into
  ``DataRecord`` items written into ``DatabaseRepository`` via ``preview_import``
  and ``commit_preview``.

All identifiers, labels, and comments are strictly neutral.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .database import DatabaseRepository
from .dedup import commit_preview, preview_import
from .models import ParseResult, compute_tag_score
from .normalizer import extract_tags
from .parser import CodeParser
from .table_data import (
    DataTable,
    TableMergeConfig,
    TableMergeResult,
    TableRowRef,
    generate_table_id,
)
from .table_dedup import (
    DEDUP_MODE_COLUMN_VALUE,
    DEDUP_MODE_SMART_CODE,
    _VALID_DEDUP_MODES,
    _extract_column_value_key,
    _extract_smart_code_for_row,
    _find_common_headers,
    _make_unique_header,
    _pad_row,
    _resolve_table_key_columns,
)

MERGE_MODE_UNION_ALL = "union_all"
MERGE_MODE_UNION_DEDUP = "union_dedup"
MERGE_MODE_JOIN = "join"

ALIGN_MODE_BY_HEADER = "by_header"
ALIGN_MODE_BY_POSITION = "by_position"

KEEP_STRATEGY_FIRST = "first"
KEEP_STRATEGY_LAST = "last"
KEEP_STRATEGY_MOST_COMPLETE = "most_complete"
KEEP_STRATEGY_HIGHEST_TAG_SCORE = "highest_tag_score"
KEEP_STRATEGY_COALESCE = "coalesce"

JOIN_TYPE_LEFT = "left"
JOIN_TYPE_INNER = "inner"
JOIN_TYPE_OUTER = "outer"

KEEP_STRATEGY_LABELS: Dict[str, str] = {
    KEEP_STRATEGY_FIRST: "保留首条",
    KEEP_STRATEGY_LAST: "保留末条",
    KEEP_STRATEGY_MOST_COMPLETE: "保留字段最全",
    KEEP_STRATEGY_HIGHEST_TAG_SCORE: "保留最高标签质量",
    KEEP_STRATEGY_COALESCE: "字段互补合并(Coalesce)",
}

_VALID_MERGE_MODES = {MERGE_MODE_UNION_ALL, MERGE_MODE_UNION_DEDUP, MERGE_MODE_JOIN}
_VALID_ALIGN_MODES = {ALIGN_MODE_BY_HEADER, ALIGN_MODE_BY_POSITION}
_VALID_KEEP_STRATEGIES = {
    KEEP_STRATEGY_FIRST,
    KEEP_STRATEGY_LAST,
    KEEP_STRATEGY_MOST_COMPLETE,
    KEEP_STRATEGY_HIGHEST_TAG_SCORE,
    KEEP_STRATEGY_COALESCE,
}
_VALID_JOIN_TYPES = {JOIN_TYPE_LEFT, JOIN_TYPE_INNER, JOIN_TYPE_OUTER}


# ----------------------------------------------------------------------
# Vertical merge helpers (union_all / union_dedup)
# ----------------------------------------------------------------------


@dataclass
class _AlignedRowEntry:
    table_id: str
    table_name: str
    row_index: int
    order_seq: int
    aligned_values: List[str]
    raw_row_ref: TableRowRef
    key_value: str
    parsed_code: str
    tags: List[str]
    part: str

    @property
    def non_empty_count(self) -> int:
        return sum(1 for v in self.aligned_values if str(v).strip())

    @property
    def total_text_len(self) -> int:
        return sum(len(str(v).strip()) for v in self.aligned_values)

    @property
    def tag_score(self) -> int:
        return compute_tag_score(self.tags)


def _compute_vertical_schema(
    tables: Sequence[DataTable],
    align_mode: str,
) -> Tuple[List[str], Dict[str, List[int]]]:
    """Compute unified output headers and per-table column mappings for vertical merges."""
    if align_mode == ALIGN_MODE_BY_HEADER:
        out_headers: List[str] = []
        seen: set[str] = set()
        for tbl in tables:
            for h in tbl.headers:
                if h not in seen:
                    seen.add(h)
                    out_headers.append(h)

        mapping: Dict[str, List[int]] = {}
        for tbl in tables:
            header_to_idx = {h: idx for idx, h in enumerate(tbl.headers)}
            mapping[tbl.table_id] = [header_to_idx.get(h, -1) for h in out_headers]
        return out_headers, mapping

    max_cols = max((tbl.col_count for tbl in tables), default=0)
    out_headers = []
    used: set[str] = set()
    for pos in range(max_cols):
        chosen = ""
        for tbl in tables:
            if pos < len(tbl.headers) and str(tbl.headers[pos]).strip():
                chosen = str(tbl.headers[pos]).strip()
                break
        if not chosen:
            chosen = f"列{pos + 1}"
        unique_name = _make_unique_header(chosen, used)
        used.add(unique_name)
        out_headers.append(unique_name)

    mapping = {}
    for tbl in tables:
        mapping[tbl.table_id] = [
            pos if pos < tbl.col_count else -1 for pos in range(max_cols)
        ]
    return out_headers, mapping


def _append_meta_headers(
    base_headers: List[str],
    cfg: TableMergeConfig,
) -> Tuple[List[str], Dict[str, str]]:
    """Append optional metadata columns cleanly without colliding with data headers."""
    out = list(base_headers)
    used = set(out)
    meta_names: Dict[str, str] = {}
    if cfg.add_source_column:
        name = _make_unique_header("_来源表", used)
        used.add(name)
        out.append(name)
        meta_names["source"] = name
    if cfg.add_row_index_column:
        name = _make_unique_header("_原行号", used)
        used.add(name)
        out.append(name)
        meta_names["row_index"] = name
    if cfg.add_extracted_code_column:
        name = _make_unique_header("_标准编码", used)
        used.add(name)
        out.append(name)
        meta_names["code"] = name
    return out, meta_names


def _append_meta_values(
    row_values: List[str],
    cfg: TableMergeConfig,
    *,
    source_label: str,
    row_index_label: str,
    extracted_code: str,
) -> List[str]:
    out = list(row_values)
    if cfg.add_source_column:
        out.append(source_label)
    if cfg.add_row_index_column:
        out.append(row_index_label)
    if cfg.add_extracted_code_column:
        out.append(extracted_code)
    return out


def _select_group_keeper(
    group: Sequence[_AlignedRowEntry],
    strategy: str,
) -> Tuple[List[str], _AlignedRowEntry, List[_AlignedRowEntry], str, str]:
    """Apply keep_strategy to a duplicate group."""
    if len(group) == 1:
        only = group[0]
        return (
            list(only.aligned_values),
            only,
            [],
            only.table_name,
            str(only.row_index),
        )

    if strategy == KEEP_STRATEGY_FIRST:
        keeper = group[0]
        return (
            list(keeper.aligned_values),
            keeper,
            list(group[1:]),
            keeper.table_name,
            str(keeper.row_index),
        )

    if strategy == KEEP_STRATEGY_LAST:
        keeper = group[-1]
        return (
            list(keeper.aligned_values),
            keeper,
            list(group[:-1]),
            keeper.table_name,
            str(keeper.row_index),
        )

    if strategy == KEEP_STRATEGY_MOST_COMPLETE:
        keeper = max(
            group,
            key=lambda e: (e.non_empty_count, e.total_text_len, -e.order_seq),
        )
        dropped = [e for e in group if e is not keeper]
        return (
            list(keeper.aligned_values),
            keeper,
            dropped,
            keeper.table_name,
            str(keeper.row_index),
        )

    if strategy == KEEP_STRATEGY_HIGHEST_TAG_SCORE:
        keeper = max(
            group,
            key=lambda e: (
                e.tag_score,
                e.non_empty_count,
                e.total_text_len,
                -e.order_seq,
            ),
        )
        dropped = [e for e in group if e is not keeper]
        return (
            list(keeper.aligned_values),
            keeper,
            dropped,
            keeper.table_name,
            str(keeper.row_index),
        )

    if strategy == KEEP_STRATEGY_COALESCE:
        base = group[0]
        merged_vals = list(base.aligned_values)
        contributing_sources: List[str] = [base.table_name]
        contributing_rows: List[str] = [str(base.row_index)]
        for candidate in group[1:]:
            contributed = False
            for col_idx in range(len(merged_vals)):
                if not str(merged_vals[col_idx]).strip():
                    cand_val = (
                        candidate.aligned_values[col_idx]
                        if col_idx < len(candidate.aligned_values)
                        else ""
                    )
                    if str(cand_val).strip():
                        merged_vals[col_idx] = cand_val
                        contributed = True
            if contributed:
                if candidate.table_name not in contributing_sources:
                    contributing_sources.append(candidate.table_name)
                contributing_rows.append(str(candidate.row_index))
        return (
            merged_vals,
            base,
            list(group[1:]),
            " / ".join(contributing_sources),
            " / ".join(contributing_rows),
        )

    raise ValueError(f"不支持的去重保留策略：{strategy}")


def _merge_vertical(
    tables: Sequence[DataTable],
    cfg: TableMergeConfig,
    parser: Optional[CodeParser],
) -> TableMergeResult:
    """Execute ``union_all`` or ``union_dedup`` vertical merge."""
    data_headers, col_map = _compute_vertical_schema(tables, cfg.align_mode)
    out_headers, _ = _append_meta_headers(data_headers, cfg)
    common_headers = _find_common_headers(tables)

    need_parser = (
        cfg.add_extracted_code_column
        or (cfg.mode == MERGE_MODE_UNION_DEDUP and cfg.dedup_mode == DEDUP_MODE_SMART_CODE)
    )
    active_parser = parser or (CodeParser() if need_parser else None)

    entries: List[_AlignedRowEntry] = []
    seq = 0
    total_input_rows = 0

    for tbl in tables:
        total_input_rows += tbl.row_count
        mapping = col_map[tbl.table_id]
        headers_copy = list(tbl.headers)
        col_n = len(headers_copy)

        explicit_smart_cols: List[int] = []
        col_val_indices: List[int] = []
        if cfg.mode == MERGE_MODE_UNION_DEDUP:
            if cfg.dedup_mode == DEDUP_MODE_SMART_CODE:
                raw_spec = (
                    cfg.key_columns.get(tbl.table_id)
                    or cfg.key_columns.get(tbl.name)
                    or cfg.key_columns.get("*")
                )
                if isinstance(raw_spec, str) and raw_spec.strip():
                    idx = tbl.get_column_index(raw_spec.strip())
                    if idx >= 0:
                        explicit_smart_cols.append(idx)
                elif isinstance(raw_spec, (list, tuple)):
                    for cn in raw_spec:
                        idx = tbl.get_column_index(str(cn).strip())
                        if idx >= 0 and idx not in explicit_smart_cols:
                            explicit_smart_cols.append(idx)
            else:
                col_val_indices = _resolve_table_key_columns(
                    tbl, cfg.key_columns, fallback_headers=common_headers
                )

        for idx_0, raw_row in enumerate(tbl.rows):
            row_idx_1 = idx_0 + 1
            row_vals = _pad_row(raw_row, col_n) if col_n else [str(v) for v in raw_row]
            aligned = [
                row_vals[src_idx] if 0 <= src_idx < len(row_vals) else ""
                for src_idx in mapping
            ]

            parsed_code = ""
            tags: List[str] = []
            part = ""
            key_val = ""

            if active_parser is not None and (
                cfg.add_extracted_code_column
                or (
                    cfg.mode == MERGE_MODE_UNION_DEDUP
                    and cfg.dedup_mode == DEDUP_MODE_SMART_CODE
                )
            ):
                extracted = _extract_smart_code_for_row(
                    tbl,
                    row_vals,
                    row_idx_1,
                    active_parser,
                    preferred_col_indices=explicit_smart_cols,
                    scan_all_columns=True,
                )
                parsed_code = extracted.parsed_code
                tags = list(extracted.tags)
                part = extracted.part
                if cfg.mode == MERGE_MODE_UNION_DEDUP and cfg.dedup_mode == DEDUP_MODE_SMART_CODE:
                    key_val = extracted.key_value

            if cfg.mode == MERGE_MODE_UNION_DEDUP and cfg.dedup_mode == DEDUP_MODE_COLUMN_VALUE:
                key_val = _extract_column_value_key(
                    row_vals,
                    col_val_indices,
                    ignore_case=cfg.ignore_case,
                    strip_whitespace=cfg.strip_whitespace,
                )
                if not tags and cfg.keep_strategy == KEEP_STRATEGY_HIGHEST_TAG_SCORE:
                    tags = extract_tags(" ".join(v for v in row_vals if v))

            ref = TableRowRef(
                table_id=tbl.table_id,
                table_name=tbl.name,
                row_index=row_idx_1,
                key_value=key_val,
                row_data=row_vals,
                headers=headers_copy,
                parsed_code=parsed_code,
                tags=",".join(tags),
                part=part,
            )
            entries.append(
                _AlignedRowEntry(
                    table_id=tbl.table_id,
                    table_name=tbl.name,
                    row_index=row_idx_1,
                    order_seq=seq,
                    aligned_values=aligned,
                    raw_row_ref=ref,
                    key_value=key_val,
                    parsed_code=parsed_code,
                    tags=tags,
                    part=part,
                )
            )
            seq += 1

    if cfg.mode == MERGE_MODE_UNION_ALL:
        out_rows = [
            _append_meta_values(
                e.aligned_values,
                cfg,
                source_label=e.table_name,
                row_index_label=str(e.row_index),
                extracted_code=e.parsed_code,
            )
            for e in entries
        ]
        merged = DataTable(
            table_id=generate_table_id(),
            name="合并数据表(纵向追加)",
            headers=out_headers,
            rows=out_rows,
            source_type="merged",
            metadata={"merge_mode": cfg.mode, "source_tables": [t.name for t in tables]},
        )
        return TableMergeResult(
            merged_table=merged,
            dropped_rows=[],
            stats={
                "mode": cfg.mode,
                "align_mode": cfg.align_mode,
                "input_tables": len(tables),
                "input_rows": total_input_rows,
                "output_rows": len(out_rows),
                "dropped_rows": 0,
                "output_cols": len(out_headers),
            },
        )

    slots: List[Tuple[str, Any]] = []
    grouped: Dict[str, List[_AlignedRowEntry]] = {}
    unrecognized_kept = 0

    for entry in entries:
        if not entry.key_value:
            slots.append(("raw", entry))
            unrecognized_kept += 1
            continue
        if entry.key_value not in grouped:
            grouped[entry.key_value] = [entry]
            slots.append(("key", entry.key_value))
        else:
            grouped[entry.key_value].append(entry)

    out_rows = []
    dropped_refs: List[TableRowRef] = []
    dup_groups_merged = 0

    for kind, payload in slots:
        if kind == "raw":
            e: _AlignedRowEntry = payload
            out_rows.append(
                _append_meta_values(
                    e.aligned_values,
                    cfg,
                    source_label=e.table_name,
                    row_index_label=str(e.row_index),
                    extracted_code=e.parsed_code,
                )
            )
            continue

        group = grouped[payload]
        if len(group) > 1:
            dup_groups_merged += 1
        merged_vals, keeper, dropped, src_label, row_idx_label = _select_group_keeper(
            group, cfg.keep_strategy
        )
        eff_code = keeper.parsed_code or next(
            (g.parsed_code for g in group if g.parsed_code), ""
        )
        out_rows.append(
            _append_meta_values(
                merged_vals,
                cfg,
                source_label=src_label,
                row_index_label=row_idx_label,
                extracted_code=eff_code,
            )
        )
        for d in dropped:
            dropped_refs.append(d.raw_row_ref)

    merged = DataTable(
        table_id=generate_table_id(),
        name="合并数据表(去重)",
        headers=out_headers,
        rows=out_rows,
        source_type="merged",
        metadata={
            "merge_mode": cfg.mode,
            "keep_strategy": cfg.keep_strategy,
            "dedup_mode": cfg.dedup_mode,
            "source_tables": [t.name for t in tables],
        },
    )
    return TableMergeResult(
        merged_table=merged,
        dropped_rows=dropped_refs,
        stats={
            "mode": cfg.mode,
            "align_mode": cfg.align_mode,
            "dedup_mode": cfg.dedup_mode,
            "keep_strategy": cfg.keep_strategy,
            "input_tables": len(tables),
            "input_rows": total_input_rows,
            "output_rows": len(out_rows),
            "dropped_rows": len(dropped_refs),
            "duplicate_groups_merged": dup_groups_merged,
            "unrecognized_kept": unrecognized_kept,
            "output_cols": len(out_headers),
        },
    )


# ----------------------------------------------------------------------
# Horizontal join helpers (left / inner / outer)
# ----------------------------------------------------------------------


@dataclass
class _JoinRowRecord:
    key: str
    cells: List[str]
    source_names: List[str]
    row_index_label: str
    parsed_code: str
    origin_ref: TableRowRef


def _extract_join_entries(
    tbl: DataTable,
    cfg: TableMergeConfig,
    parser: Optional[CodeParser],
    common_headers: Sequence[str],
) -> List[_JoinRowRecord]:
    """Extract normalized join keys and padded cell lists for every row of ``tbl``."""
    headers_copy = list(tbl.headers)
    col_n = len(headers_copy)

    explicit_smart_cols: List[int] = []
    col_val_indices: List[int] = []
    if cfg.dedup_mode == DEDUP_MODE_SMART_CODE:
        raw_spec = (
            cfg.key_columns.get(tbl.table_id)
            or cfg.key_columns.get(tbl.name)
            or cfg.key_columns.get("*")
        )
        if isinstance(raw_spec, str) and raw_spec.strip():
            idx = tbl.get_column_index(raw_spec.strip())
            if idx >= 0:
                explicit_smart_cols.append(idx)
        elif isinstance(raw_spec, (list, tuple)):
            for cn in raw_spec:
                idx = tbl.get_column_index(str(cn).strip())
                if idx >= 0 and idx not in explicit_smart_cols:
                    explicit_smart_cols.append(idx)
    else:
        col_val_indices = _resolve_table_key_columns(
            tbl, cfg.key_columns, fallback_headers=common_headers
        )

    records: List[_JoinRowRecord] = []
    for idx_0, raw_row in enumerate(tbl.rows):
        row_idx_1 = idx_0 + 1
        vals = _pad_row(raw_row, col_n)
        parsed_code = ""
        tags_str = ""
        part = ""

        if cfg.dedup_mode == DEDUP_MODE_SMART_CODE:
            assert parser is not None
            ext = _extract_smart_code_for_row(
                tbl,
                vals,
                row_idx_1,
                parser,
                preferred_col_indices=explicit_smart_cols,
                scan_all_columns=True,
            )
            key_val = ext.key_value
            parsed_code = ext.parsed_code
            tags_str = ext.tags_str
            part = ext.part
        else:
            key_val = _extract_column_value_key(
                vals,
                col_val_indices,
                ignore_case=cfg.ignore_case,
                strip_whitespace=cfg.strip_whitespace,
            )
            if cfg.add_extracted_code_column and parser is not None:
                ext = _extract_smart_code_for_row(
                    tbl, vals, row_idx_1, parser, scan_all_columns=True
                )
                parsed_code = ext.parsed_code
                tags_str = ext.tags_str
                part = ext.part

        ref = TableRowRef(
            table_id=tbl.table_id,
            table_name=tbl.name,
            row_index=row_idx_1,
            key_value=key_val,
            row_data=vals,
            headers=headers_copy,
            parsed_code=parsed_code,
            tags=tags_str,
            part=part,
        )
        records.append(
            _JoinRowRecord(
                key=key_val,
                cells=vals,
                source_names=[tbl.name],
                row_index_label=str(row_idx_1),
                parsed_code=parsed_code,
                origin_ref=ref,
            )
        )
    return records


def _merge_join(
    tables: Sequence[DataTable],
    cfg: TableMergeConfig,
    parser: Optional[CodeParser],
) -> TableMergeResult:
    """Execute horizontal left/inner/outer join anchored on primary table."""
    if cfg.join_type not in _VALID_JOIN_TYPES:
        raise ValueError(f"不支持的横向拼接类型：{cfg.join_type}")

    need_parser = (
        cfg.dedup_mode == DEDUP_MODE_SMART_CODE or cfg.add_extracted_code_column
    )
    active_parser = parser or (CodeParser() if need_parser else None)
    common_headers = _find_common_headers(tables)

    primary: Optional[DataTable] = None
    if cfg.primary_table_id:
        for t in tables:
            if t.table_id == cfg.primary_table_id:
                primary = t
                break
        if primary is None:
            for t in tables:
                if t.name == cfg.primary_table_id:
                    primary = t
                    break
    if primary is None:
        primary = tables[0]

    secondaries = [t for t in tables if t.table_id != primary.table_id]

    data_headers: List[str] = list(primary.headers)
    used_headers = set(data_headers)
    for sec in secondaries:
        for h in sec.headers:
            if h in used_headers:
                renamed = _make_unique_header(f"{h}({sec.name})", used_headers)
                data_headers.append(renamed)
                used_headers.add(renamed)
            else:
                data_headers.append(h)
                used_headers.add(h)

    working = _extract_join_entries(primary, cfg, active_parser, common_headers)
    current_width = primary.col_count
    dropped_refs: List[TableRowRef] = []

    for sec in secondaries:
        sec_entries = _extract_join_entries(sec, cfg, active_parser, common_headers)
        sec_width = sec.col_count
        sec_index: Dict[str, List[Tuple[int, _JoinRowRecord]]] = {}
        for pos, s_rec in enumerate(sec_entries):
            if s_rec.key:
                sec_index.setdefault(s_rec.key, []).append((pos, s_rec))

        matched_sec_positions: set[int] = set()
        next_working: List[_JoinRowRecord] = []

        for left_rec in working:
            matches = sec_index.get(left_rec.key, []) if left_rec.key else []
            if matches:
                for pos, right_rec in matches:
                    matched_sec_positions.add(pos)
                    merged_sources = list(left_rec.source_names)
                    if sec.name not in merged_sources:
                        merged_sources.append(sec.name)
                    next_working.append(
                        _JoinRowRecord(
                            key=left_rec.key,
                            cells=left_rec.cells + right_rec.cells,
                            source_names=merged_sources,
                            row_index_label=left_rec.row_index_label,
                            parsed_code=left_rec.parsed_code or right_rec.parsed_code,
                            origin_ref=left_rec.origin_ref,
                        )
                    )
            else:
                if cfg.join_type in (JOIN_TYPE_LEFT, JOIN_TYPE_OUTER):
                    next_working.append(
                        _JoinRowRecord(
                            key=left_rec.key,
                            cells=left_rec.cells + ([""] * sec_width),
                            source_names=list(left_rec.source_names),
                            row_index_label=left_rec.row_index_label,
                            parsed_code=left_rec.parsed_code,
                            origin_ref=left_rec.origin_ref,
                        )
                    )
                else:
                    dropped_refs.append(left_rec.origin_ref)

        if cfg.join_type == JOIN_TYPE_OUTER:
            for pos, right_rec in enumerate(sec_entries):
                if pos not in matched_sec_positions:
                    next_working.append(
                        _JoinRowRecord(
                            key=right_rec.key,
                            cells=([""] * current_width) + right_rec.cells,
                            source_names=[sec.name],
                            row_index_label="",
                            parsed_code=right_rec.parsed_code,
                            origin_ref=right_rec.origin_ref,
                        )
                    )
        else:
            for pos, right_rec in enumerate(sec_entries):
                if pos not in matched_sec_positions:
                    dropped_refs.append(right_rec.origin_ref)

        working = next_working
        current_width += sec_width

    out_headers, _ = _append_meta_headers(data_headers, cfg)
    out_rows = [
        _append_meta_values(
            w.cells,
            cfg,
            source_label=" + ".join(w.source_names),
            row_index_label=w.row_index_label,
            extracted_code=w.parsed_code,
        )
        for w in working
    ]

    join_label = {
        JOIN_TYPE_LEFT: "左连接",
        JOIN_TYPE_INNER: "内连接",
        JOIN_TYPE_OUTER: "全外连接",
    }[cfg.join_type]
    merged = DataTable(
        table_id=generate_table_id(),
        name=f"合并数据表(横向{join_label})",
        headers=out_headers,
        rows=out_rows,
        source_type="merged",
        metadata={
            "merge_mode": cfg.mode,
            "join_type": cfg.join_type,
            "primary_table_id": primary.table_id,
            "primary_table_name": primary.name,
            "source_tables": [t.name for t in tables],
        },
    )
    total_input_rows = sum(t.row_count for t in tables)
    return TableMergeResult(
        merged_table=merged,
        dropped_rows=dropped_refs,
        stats={
            "mode": cfg.mode,
            "join_type": cfg.join_type,
            "dedup_mode": cfg.dedup_mode,
            "primary_table": primary.name,
            "input_tables": len(tables),
            "input_rows": total_input_rows,
            "output_rows": len(out_rows),
            "dropped_rows": len(dropped_refs),
            "output_cols": len(out_headers),
        },
    )


def merge_tables(
    tables: Sequence[DataTable],
    config: Optional[TableMergeConfig] = None,
    parser_engine: Optional[CodeParser] = None,
) -> TableMergeResult:
    """Merge multiple ``DataTable`` instances vertically or horizontally."""
    cfg = config or TableMergeConfig()
    if cfg.mode not in _VALID_MERGE_MODES:
        raise ValueError(f"不支持的合并模式：{cfg.mode}")
    if cfg.align_mode not in _VALID_ALIGN_MODES:
        raise ValueError(f"不支持的列对齐方式：{cfg.align_mode}")
    if cfg.dedup_mode not in _VALID_DEDUP_MODES:
        raise ValueError(f"不支持的查重键模式：{cfg.dedup_mode}")
    if cfg.mode == MERGE_MODE_UNION_DEDUP and cfg.keep_strategy not in _VALID_KEEP_STRATEGIES:
        raise ValueError(f"不支持的去重保留策略：{cfg.keep_strategy}")

    table_list = list(tables)
    if not table_list:
        empty_tbl = DataTable(
            table_id=generate_table_id(),
            name="合并数据表",
            headers=[],
            rows=[],
            source_type="merged",
        )
        return TableMergeResult(
            merged_table=empty_tbl,
            dropped_rows=[],
            stats={
                "mode": cfg.mode,
                "input_tables": 0,
                "input_rows": 0,
                "output_rows": 0,
                "dropped_rows": 0,
                "output_cols": 0,
            },
        )

    if cfg.mode in (MERGE_MODE_UNION_ALL, MERGE_MODE_UNION_DEDUP):
        return _merge_vertical(table_list, cfg, parser_engine)
    return _merge_join(table_list, cfg, parser_engine)


# ----------------------------------------------------------------------
# Import DataTable back into SQLite DatabaseRepository
# ----------------------------------------------------------------------

_CODE_COL_HINTS = ("_标准编码", "标准编码", "编码", "编号", "code", "canonical_code")
_FILENAME_COL_HINTS = ("原始文件名", "文件名", "名称", "标题", "file_name", "name")
_PATH_COL_HINTS = ("完整路径", "路径", "full_path", "path")
_TAGS_COL_HINTS = ("标签", "tags")
_PART_COL_HINTS = ("分集", "part")
_NOTE_COL_HINTS = ("备注", "说明", "note")


def _find_hint_column(headers: Sequence[str], hints: Sequence[str]) -> int:
    lowered = [h.strip().lower() for h in headers]
    for hint in hints:
        target = hint.lower()
        for idx, h in enumerate(lowered):
            if h == target:
                return idx
    return -1


def import_table_to_database(
    table: DataTable,
    db_repo: DatabaseRepository,
    parser_engine: Optional[CodeParser] = None,
    code_column: Optional[str | int] = None,
    source_name: str = "",
    *,
    skip_existing: bool = True,
) -> Dict[str, Any]:
    """Convert a ``DataTable`` into ``DataRecord``s and write them to ``DatabaseRepository``."""
    parser = parser_engine or CodeParser()
    eff_source_name = (source_name or table.name or "表格导入").strip()
    eff_source_type = table.source_type or "merged"

    if table.row_count == 0:
        return {
            "total": 0,
            "added": 0,
            "skipped": 0,
            "db_duplicates": 0,
            "batch_duplicates": 0,
            "multiparts_added": 0,
            "unrecognized": 0,
            "batch_id": None,
        }

    preferred_cols: List[int] = []
    scan_all = True
    if code_column is not None and code_column != "":
        if isinstance(code_column, int):
            if 0 <= code_column < table.col_count:
                preferred_cols = [code_column]
                scan_all = False
        else:
            idx = table.get_column_index(str(code_column).strip())
            if idx >= 0:
                preferred_cols = [idx]
                scan_all = False
    else:
        hint_idx = _find_hint_column(table.headers, _CODE_COL_HINTS)
        if hint_idx >= 0:
            preferred_cols = [hint_idx]

    fname_idx = _find_hint_column(table.headers, _FILENAME_COL_HINTS)
    path_idx = _find_hint_column(table.headers, _PATH_COL_HINTS)
    tags_idx = _find_hint_column(table.headers, _TAGS_COL_HINTS)
    part_idx = _find_hint_column(table.headers, _PART_COL_HINTS)
    note_idx = _find_hint_column(table.headers, _NOTE_COL_HINTS)

    parsed_items: List[ParseResult] = []
    col_n = table.col_count

    for idx_0, raw_row in enumerate(table.rows):
        row_idx_1 = idx_0 + 1
        row_vals = _pad_row(raw_row, col_n) if col_n else [str(v) for v in raw_row]
        joined_row = " | ".join(v.strip() for v in row_vals if v.strip())

        extracted = _extract_smart_code_for_row(
            table,
            row_vals,
            row_idx_1,
            parser,
            preferred_col_indices=preferred_cols,
            scan_all_columns=scan_all,
        )

        if extracted.parse_result is not None and extracted.parsed_code:
            res = extracted.parse_result
            if fname_idx >= 0 and row_vals[fname_idx].strip():
                res.file_name = row_vals[fname_idx].strip()
            elif not res.file_name:
                res.file_name = joined_row or extracted.parsed_code

            if path_idx >= 0 and row_vals[path_idx].strip():
                res.full_path = row_vals[path_idx].strip()
            elif not res.full_path and table.source_path:
                res.full_path = table.source_path

            if tags_idx >= 0 and row_vals[tags_idx].strip():
                extra_tags = [
                    t.strip()
                    for t in re.split(r"[,，;/|]+", row_vals[tags_idx])
                    if t.strip()
                ]
                for t in extra_tags:
                    if t not in res.tags:
                        res.tags.append(t)

            if part_idx >= 0 and row_vals[part_idx].strip() and not res.part:
                res.part = row_vals[part_idx].strip().upper()

            if note_idx >= 0 and row_vals[note_idx].strip():
                res.note = row_vals[note_idx].strip()

            res.raw_text = joined_row or res.raw_text
            res.source_name = eff_source_name
            res.source_type = eff_source_type
            res.line_number = row_idx_1
            parsed_items.append(res)
        else:
            parsed_items.append(
                ParseResult(
                    raw_text=joined_row,
                    file_name=row_vals[fname_idx].strip()
                    if fname_idx >= 0 and row_vals[fname_idx].strip()
                    else joined_row,
                    full_path=table.source_path,
                    source_name=eff_source_name,
                    source_type=eff_source_type,
                    line_number=row_idx_1,
                    recognized=False,
                    note="未匹配有效编码",
                )
            )

    preview = preview_import(parsed_items, db_repo, total_scanned=table.row_count)

    will_insert_count = len(preview.new_items) + len(preview.possible_multiparts)
    if not skip_existing:
        will_insert_count += len(preview.db_duplicates) + len(preview.batch_duplicates)

    batch_id: Optional[int] = None
    added_count = 0
    if will_insert_count > 0:
        saved, saved_batch = commit_preview(
            preview,
            db_repo,
            include_multiparts=True,
            include_db_duplicates=not skip_existing,
            include_batch_duplicates=not skip_existing,
            source_name=eff_source_name,
            source_type=eff_source_type,
        )
        added_count = len(saved)
        batch_id = saved_batch.id

    skipped_count = (
        len(preview.db_duplicates) + len(preview.batch_duplicates)
        if skip_existing
        else 0
    )

    return {
        "total": table.row_count,
        "added": added_count,
        "skipped": skipped_count,
        "db_duplicates": len(preview.db_duplicates),
        "batch_duplicates": len(preview.batch_duplicates),
        "multiparts_added": len(preview.possible_multiparts),
        "unrecognized": len(preview.unrecognized),
        "batch_id": batch_id,
    }
