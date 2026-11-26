"""Facade module re-exporting the multi-table dedup, merge, and DB import APIs.

Implementation is split cleanly across:
- ``src/core/table_dedup.py``: multi-table deduplication & set-analysis engine
- ``src/core/table_merge.py``: vertical/horizontal table merging & DB import

All public symbols and import paths remain 100% backward-compatible.
"""

from __future__ import annotations

from .table_dedup import (
    DEDUP_MODE_COLUMN_VALUE,
    DEDUP_MODE_SMART_CODE,
    run_multi_table_dedup,
)
from .table_merge import (
    ALIGN_MODE_BY_HEADER,
    ALIGN_MODE_BY_POSITION,
    JOIN_MATCH_ALL,
    JOIN_MATCH_FIRST,
    JOIN_TYPE_INNER,
    JOIN_TYPE_LEFT,
    JOIN_TYPE_OUTER,
    KEEP_STRATEGY_COALESCE,
    KEEP_STRATEGY_FIRST,
    KEEP_STRATEGY_HIGHEST_TAG_SCORE,
    KEEP_STRATEGY_LABELS,
    KEEP_STRATEGY_LAST,
    KEEP_STRATEGY_MOST_COMPLETE,
    MERGE_MODE_JOIN,
    MERGE_MODE_UNION_ALL,
    MERGE_MODE_UNION_DEDUP,
    import_table_to_database,
    merge_tables,
)

__all__ = [
    "DEDUP_MODE_SMART_CODE",
    "DEDUP_MODE_COLUMN_VALUE",
    "MERGE_MODE_UNION_ALL",
    "MERGE_MODE_UNION_DEDUP",
    "MERGE_MODE_JOIN",
    "ALIGN_MODE_BY_HEADER",
    "ALIGN_MODE_BY_POSITION",
    "KEEP_STRATEGY_FIRST",
    "KEEP_STRATEGY_LAST",
    "KEEP_STRATEGY_MOST_COMPLETE",
    "KEEP_STRATEGY_HIGHEST_TAG_SCORE",
    "KEEP_STRATEGY_COALESCE",
    "JOIN_TYPE_LEFT",
    "JOIN_TYPE_INNER",
    "JOIN_TYPE_OUTER",
    "JOIN_MATCH_ALL",
    "JOIN_MATCH_FIRST",
    "KEEP_STRATEGY_LABELS",
    "run_multi_table_dedup",
    "merge_tables",
    "import_table_to_database",
]
