"""Shared data models for multi-table loading, deduplication, and merging.

All terminology, identifiers, and comments are strictly neutral and general-purpose.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


def _now_iso() -> str:
    """Return current local timestamp formatted as YYYY-MM-DD HH:MM:SS."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def generate_table_id() -> str:
    """Generate a compact unique identifier for an in-memory DataTable."""
    return f"tbl_{uuid.uuid4().hex[:10]}"


@dataclass
class TableColumnInfo:
    """Summary metadata for a single column in a DataTable."""

    name: str
    index: int
    non_empty_count: int = 0
    unique_count: int = 0
    sample_values: list[str] = field(default_factory=list)


@dataclass
class DataTable:
    """Normalized in-memory tabular dataset loaded from Excel, Word, CSV, TXT, or DB."""

    table_id: str
    name: str
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    source_path: str = ""
    sheet_name: str = ""
    source_type: str = "excel"  # "excel" | "word" | "csv" | "txt" | "database" | "merged"
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.table_id:
            self.table_id = generate_table_id()
        if not self.created_at:
            self.created_at = _now_iso()

    @property
    def row_count(self) -> int:
        """Return total number of data rows (excluding header)."""
        return len(self.rows)

    @property
    def col_count(self) -> int:
        """Return number of columns defined by headers."""
        return len(self.headers)

    def get_cell(self, row_idx: int, col_idx: int, default: str = "") -> str:
        """Safely retrieve a cell value by 0-based row and column index."""
        if 0 <= row_idx < len(self.rows):
            row = self.rows[row_idx]
            if 0 <= col_idx < len(row):
                return row[col_idx]
        return default

    def get_column_index(self, column_name: str) -> int:
        """Return the 0-based index of a column header, or -1 if absent."""
        try:
            return self.headers.index(column_name)
        except ValueError:
            return -1

    def to_dicts(self) -> list[dict[str, str]]:
        """Convert rows into a list of `{header: value}` dictionaries."""
        col_n = len(self.headers)
        result: list[dict[str, str]] = []
        for row in self.rows:
            item: dict[str, str] = {}
            for idx in range(col_n):
                item[self.headers[idx]] = row[idx] if idx < len(row) else ""
            result.append(item)
        return result

    def clone(
        self,
        *,
        new_id: Optional[str] = None,
        new_name: Optional[str] = None,
    ) -> "DataTable":
        """Return an independent deep copy of this table."""
        return DataTable(
            table_id=new_id if new_id is not None else generate_table_id(),
            name=new_name if new_name is not None else self.name,
            headers=list(self.headers),
            rows=[list(r) for r in self.rows],
            source_path=self.source_path,
            sheet_name=self.sheet_name,
            source_type=self.source_type,
            created_at=_now_iso(),
            metadata=copy.deepcopy(self.metadata),
        )

    def rename(self, new_name: str) -> "DataTable":
        """Rename this table in place and return self for chaining."""
        cleaned = str(new_name or "").strip()
        if cleaned:
            self.name = cleaned
        return self

    def get_column_info(self, max_samples: int = 5) -> list[TableColumnInfo]:
        """Compute basic profiling statistics for every column in this table."""
        infos: list[TableColumnInfo] = []
        for col_idx, header in enumerate(self.headers):
            non_empty = 0
            unique_vals: set[str] = set()
            samples: list[str] = []
            for row in self.rows:
                val = row[col_idx].strip() if col_idx < len(row) else ""
                if val:
                    non_empty += 1
                    if val not in unique_vals:
                        unique_vals.add(val)
                        if len(samples) < max_samples:
                            samples.append(val)
            infos.append(
                TableColumnInfo(
                    name=header,
                    index=col_idx,
                    non_empty_count=non_empty,
                    unique_count=len(unique_vals),
                    sample_values=samples,
                )
            )
        return infos


@dataclass
class TableDedupConfig:
    """Configuration for multi-table and intra-table deduplication analysis."""

    mode: str = "smart_code"  # "smart_code" | "column_value"
    key_columns: dict[str, list[str]] = field(default_factory=dict)
    ignore_case: bool = True
    strip_whitespace: bool = True
    ignore_punctuation: bool = False
    include_intra_table: bool = True
    include_cross_table: bool = True
    scan_all_columns_for_code: bool = True


@dataclass
class TableRowRef:
    """Reference to a specific row inside a loaded DataTable with its dedup key."""

    table_id: str
    table_name: str
    row_index: int  # 1-based data row number for user display
    key_value: str
    row_data: list[str] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    parsed_code: str = ""
    tags: str = ""
    part: str = ""

    def to_dict(self) -> dict[str, str]:
        """Convert the referenced row into a `{header: value}` mapping."""
        return {
            self.headers[i]: (self.row_data[i] if i < len(self.row_data) else "")
            for i in range(len(self.headers))
        }


@dataclass
class TableDuplicateGroup:
    """Group of rows sharing the same normalized key across or within tables."""

    key_value: str
    is_cross_table: bool = False
    table_names: list[str] = field(default_factory=list)
    table_count: int = 0
    total_rows: int = 0
    rows: list[TableRowRef] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.table_names and self.rows:
            seen_tids: set[str] = set()
            names: list[str] = []
            for r in self.rows:
                if r.table_id not in seen_tids:
                    seen_tids.add(r.table_id)
                    names.append(r.table_name)
            self.table_names = names
        if not self.table_count:
            if self.rows:
                self.table_count = len({r.table_id for r in self.rows})
            elif self.table_names:
                self.table_count = len(self.table_names)
        if not self.total_rows and self.rows:
            self.total_rows = len(self.rows)


@dataclass
class TableDedupReport:
    """Structured output of a multi-table deduplication and set-analysis run."""

    config: TableDedupConfig = field(default_factory=TableDedupConfig)
    table_names: list[str] = field(default_factory=list)
    table_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    cross_table_groups: list[TableDuplicateGroup] = field(default_factory=list)
    intra_table_groups: list[TableDuplicateGroup] = field(default_factory=list)
    intersection_keys: list[str] = field(default_factory=list)
    intersection_rows: list[TableRowRef] = field(default_factory=list)
    unique_by_table: dict[str, list[TableRowRef]] = field(default_factory=dict)
    total_rows: int = 0
    unique_key_count: int = 0
    unrecognized_rows: list[TableRowRef] = field(default_factory=list)


@dataclass
class TableMergeConfig:
    """Configuration for combining multiple DataTables vertically or horizontally."""

    mode: str = "union_dedup"  # "union_all" | "union_dedup" | "join"
    align_mode: str = "by_header"  # "by_header" | "by_position"
    dedup_mode: str = "smart_code"  # "smart_code" | "column_value"
    key_columns: dict[str, list[str]] = field(default_factory=dict)
    keep_strategy: str = "first"  # "first" | "last" | "most_complete" | "highest_tag_score" | "coalesce"
    join_type: str = "left"  # "left" | "inner" | "outer"
    primary_table_id: str = ""
    add_source_column: bool = True
    add_row_index_column: bool = False
    add_extracted_code_column: bool = False
    ignore_case: bool = True
    strip_whitespace: bool = True
    ignore_punctuation: bool = False
    scan_all_columns_for_code: bool = True
    join_match_mode: str = "all"  # "all" | "first"
    max_join_rows: int = 200_000


@dataclass
class TableMergeResult:
    """Result returned by multi-table merge operations, including audit trail."""

    merged_table: DataTable
    dropped_rows: list[TableRowRef] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
