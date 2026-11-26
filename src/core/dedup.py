"""Deduplication preview, pure comparison, A-vs-B cross-comparison, and smart cleanup.

All terminology and strategy labels are strictly neutral.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .database import DatabaseRepository, normalize_part_key
from .models import (
    CompareResult,
    DataRecord,
    DedupPreview,
    DuplicateGroup,
    ImportBatch,
    ParseResult,
)

STRATEGY_KEEP_EARLIEST = "keep_earliest"
STRATEGY_KEEP_LATEST = "keep_latest"
STRATEGY_KEEP_HIGHEST_TAG_SCORE = "keep_highest_tag_score"
STRATEGY_KEEP_LONGEST_NAME = "keep_longest_name"

DEDUP_STRATEGY_LABELS: Dict[str, str] = {
    STRATEGY_KEEP_HIGHEST_TAG_SCORE: "保留最高标签评分（4K/字幕/高版本优先）",
    STRATEGY_KEEP_EARLIEST: "保留最早录入记录",
    STRATEGY_KEEP_LATEST: "保留最新录入记录",
    STRATEGY_KEEP_LONGEST_NAME: "保留最长文件名记录",
}


@dataclass
class GroupCleanupDecision:
    """Selection decision for a single duplicate group."""

    canonical_code: str
    is_possible_multipart: bool
    protected: bool
    keep_ids: List[int] = field(default_factory=list)
    delete_ids: List[int] = field(default_factory=list)


@dataclass
class CleanupPlan:
    """Aggregated duplicate cleanup selection plan."""

    strategy: str
    protect_multipart: bool
    keep_ids: List[int] = field(default_factory=list)
    delete_ids: List[int] = field(default_factory=list)
    protected_groups: List[str] = field(default_factory=list)
    decisions: List[GroupCleanupDecision] = field(default_factory=list)

    @property
    def delete_count(self) -> int:
        return len(self.delete_ids)

    @property
    def keep_count(self) -> int:
        return len(self.keep_ids)


# ----------------------------------------------------------------------
# 1. Dry-run import preview & commit
# ----------------------------------------------------------------------


def preview_import(
    parsed_items: Sequence[ParseResult],
    db: DatabaseRepository,
    *,
    skipped_companions: int = 0,
    total_scanned: Optional[int] = None,
) -> DedupPreview:
    """Categorize parsed items without mutating the database.

    Buckets:
    - ``unrecognized``: items without a recognized canonical code
    - ``new_items``: first occurrence of a code not present in DB
    - ``db_duplicates``: code already exists in DB with same/empty part marker
    - ``batch_duplicates``: duplicate code within the incoming batch (same/empty part)
    - ``possible_multiparts``: code collides with DB or earlier batch item, but carries
      a distinct multi-part marker (e.g. ``CD1`` vs ``CD2``)
    """
    codes_to_lookup = [
        item.canonical_code.strip().upper()
        for item in parsed_items
        if item.recognized and item.canonical_code.strip()
    ]
    existing_by_code = db.find_by_codes(codes_to_lookup)

    new_items: List[ParseResult] = []
    db_duplicates: List[Tuple[ParseResult, List[DataRecord]]] = []
    batch_duplicates: List[ParseResult] = []
    possible_multiparts: List[ParseResult] = []
    unrecognized: List[ParseResult] = []

    # Track parts already seen inside this batch per canonical code.
    batch_seen_parts: Dict[str, set[str]] = {}

    for item in parsed_items:
        code = item.canonical_code.strip().upper()
        if not item.recognized or not code:
            unrecognized.append(item)
            continue

        part_key = normalize_part_key(item.part)
        db_records = existing_by_code.get(code)
        seen_in_batch = batch_seen_parts.get(code)

        if db_records:
            db_parts = {normalize_part_key(r.part) for r in db_records}
            combined_parts = db_parts | (seen_in_batch or set())
            if _is_distinct_multipart(part_key, combined_parts):
                possible_multiparts.append(item)
                batch_seen_parts.setdefault(code, set()).add(part_key)
            elif part_key in db_parts:
                db_duplicates.append((item, db_records))
                batch_seen_parts.setdefault(code, set()).add(part_key)
            elif seen_in_batch is not None and part_key in seen_in_batch:
                batch_duplicates.append(item)
            else:
                db_duplicates.append((item, db_records))
                batch_seen_parts.setdefault(code, set()).add(part_key)
        elif seen_in_batch is not None:
            if _is_distinct_multipart(part_key, seen_in_batch):
                possible_multiparts.append(item)
                seen_in_batch.add(part_key)
            else:
                batch_duplicates.append(item)
        else:
            new_items.append(item)
            batch_seen_parts[code] = {part_key}

    total = total_scanned if total_scanned is not None else len(parsed_items) + skipped_companions
    return DedupPreview(
        new_items=new_items,
        db_duplicates=db_duplicates,
        batch_duplicates=batch_duplicates,
        possible_multiparts=possible_multiparts,
        unrecognized=unrecognized,
        skipped_companions=int(skipped_companions),
        total_scanned=int(total),
    )


def _is_distinct_multipart(incoming_part: str, existing_parts: set[str]) -> bool:
    """Return True if ``incoming_part`` represents a distinct part relative to ``existing_parts``."""
    if not existing_parts:
        return False
    if incoming_part in existing_parts:
        return False
    # At least one side has an explicit non-empty part marker and they differ.
    return bool(incoming_part) or any(bool(p) for p in existing_parts)


def commit_preview(
    preview: DedupPreview,
    db: DatabaseRepository,
    *,
    include_multiparts: bool = True,
    include_db_duplicates: bool = False,
    include_batch_duplicates: bool = False,
    extra_items: Optional[Sequence[ParseResult]] = None,
    source_name: str = "",
    source_type: str = "txt",
) -> Tuple[List[DataRecord], ImportBatch]:
    """Commit selected categories from a ``DedupPreview`` into the database."""
    selected: List[ParseResult] = list(preview.new_items)
    if include_multiparts:
        selected.extend(preview.possible_multiparts)
    if include_db_duplicates:
        selected.extend(item for item, _ in preview.db_duplicates)
    if include_batch_duplicates:
        selected.extend(preview.batch_duplicates)
    if extra_items:
        selected.extend(
            item for item in extra_items if item.recognized and item.canonical_code.strip()
        )

    records = [DataRecord.from_parse_result(item) for item in selected]
    batch = ImportBatch(
        source_name=source_name or _infer_source_name(selected),
        source_type=source_type or _infer_source_type(selected),
        total_count=preview.total_scanned or (
            len(preview.new_items)
            + len(preview.db_duplicates)
            + len(preview.batch_duplicates)
            + len(preview.possible_multiparts)
            + len(preview.unrecognized)
        ),
        added_count=len(records),
        duplicate_count=len(preview.db_duplicates) + len(preview.batch_duplicates),
        unrecognized_count=len(preview.unrecognized),
    )
    saved, saved_batch = db.add_records(records, batch=batch)
    assert saved_batch is not None
    return saved, saved_batch


def _infer_source_name(items: Sequence[ParseResult]) -> str:
    for item in items:
        if item.source_name:
            return item.source_name
    return "批量导入"


def _infer_source_type(items: Sequence[ParseResult]) -> str:
    for item in items:
        if item.source_type:
            return item.source_type
    return "txt"


# ----------------------------------------------------------------------
# 2. Pure compare against DB (non-mutating)
# ----------------------------------------------------------------------


def compare_with_db(
    parsed_items: Sequence[ParseResult],
    db: DatabaseRepository,
    *,
    source_name: str = "",
) -> CompareResult:
    """Compare parsed items against the database without modifying any records."""
    codes = [
        item.canonical_code.strip().upper()
        for item in parsed_items
        if item.recognized and item.canonical_code.strip()
    ]
    existing_by_code = db.find_by_codes(codes)

    matched_in_db: List[Tuple[ParseResult, List[DataRecord]]] = []
    not_in_db: List[ParseResult] = []
    unrecognized_a: List[ParseResult] = []

    for item in parsed_items:
        code = item.canonical_code.strip().upper()
        if not item.recognized or not code:
            unrecognized_a.append(item)
            continue
        existing = existing_by_code.get(code)
        if existing:
            matched_in_db.append((item, existing))
        else:
            not_in_db.append(item)

    return CompareResult(
        mode="db",
        source_a_name=source_name,
        matched_in_db=matched_in_db,
        not_in_db=not_in_db,
        unrecognized_a=unrecognized_a,
    )


# ----------------------------------------------------------------------
# 3. Two-source A-vs-B cross-comparison
# ----------------------------------------------------------------------


def compare_two_sources(
    items_a: Sequence[ParseResult],
    items_b: Sequence[ParseResult],
    *,
    source_a_name: str = "来源A",
    source_b_name: str = "来源B",
) -> CompareResult:
    """Cross-compare two parsed item lists by canonical code.

    Returns intersection codes & matching items from both sides, items only in A,
    items only in B, and unrecognized items from each source.
    """
    codes_a: set[str] = set()
    codes_b: set[str] = set()
    unrecognized_a: List[ParseResult] = []
    unrecognized_b: List[ParseResult] = []
    recognized_a: List[ParseResult] = []
    recognized_b: List[ParseResult] = []

    for item in items_a:
        code = item.canonical_code.strip().upper()
        if item.recognized and code:
            codes_a.add(code)
            recognized_a.append(item)
        else:
            unrecognized_a.append(item)

    for item in items_b:
        code = item.canonical_code.strip().upper()
        if item.recognized and code:
            codes_b.add(code)
            recognized_b.append(item)
        else:
            unrecognized_b.append(item)

    intersection_set = codes_a & codes_b
    intersection_codes = sorted(intersection_set)

    intersection_a = [
        item for item in recognized_a if item.canonical_code.strip().upper() in intersection_set
    ]
    intersection_b = [
        item for item in recognized_b if item.canonical_code.strip().upper() in intersection_set
    ]
    only_in_a = [
        item for item in recognized_a if item.canonical_code.strip().upper() not in intersection_set
    ]
    only_in_b = [
        item for item in recognized_b if item.canonical_code.strip().upper() not in intersection_set
    ]

    return CompareResult(
        mode="ab",
        source_a_name=source_a_name,
        source_b_name=source_b_name,
        unrecognized_a=unrecognized_a,
        unrecognized_b=unrecognized_b,
        intersection_codes=intersection_codes,
        intersection_a=intersection_a,
        intersection_b=intersection_b,
        only_in_a=only_in_a,
        only_in_b=only_in_b,
    )


# ----------------------------------------------------------------------
# 4. Smart duplicate cleanup selection strategies
# ----------------------------------------------------------------------


def _pick_keeper(records: Sequence[DataRecord], strategy: str) -> DataRecord:
    """Choose the single record to KEEP from a non-empty duplicate bucket."""
    if not records:
        raise ValueError("记录列表不能为空")

    if strategy == STRATEGY_KEEP_EARLIEST:
        return min(
            records,
            key=lambda r: (
                r.created_at or "9999-99-99 99:99:99",
                r.id if r.id is not None else 10**12,
            ),
        )

    if strategy == STRATEGY_KEEP_LATEST:
        return max(
            records,
            key=lambda r: (
                r.created_at or "",
                r.id if r.id is not None else -1,
            ),
        )

    if strategy == STRATEGY_KEEP_LONGEST_NAME:
        return max(
            records,
            key=lambda r: (
                len(r.file_name or r.raw_text or ""),
                r.tag_score,
                r.file_size,
                -(r.id if r.id is not None else 10**12),
            ),
        )

    if strategy == STRATEGY_KEEP_HIGHEST_TAG_SCORE:
        return max(
            records,
            key=lambda r: (
                r.tag_score,
                r.file_size,
                len(r.file_name or r.raw_text or ""),
                -(r.id if r.id is not None else 10**12),
            ),
        )

    raise ValueError(f"不支持的去重策略：{strategy}")


def plan_duplicate_cleanup(
    groups: Sequence[DuplicateGroup],
    *,
    strategy: str = STRATEGY_KEEP_HIGHEST_TAG_SCORE,
    protect_multipart: bool = True,
) -> CleanupPlan:
    """Build a duplicate cleanup selection plan without deleting anything.

    When ``protect_multipart=True``:
    - Records with distinct multi-part markers (e.g. ``CD1`` vs ``CD2``) are never
      deleted against one another; selection only runs within records that share
      the exact same normalized part marker.
    """
    if strategy not in DEDUP_STRATEGY_LABELS:
        raise ValueError(f"不支持的去重策略：{strategy}")

    all_keep_ids: List[int] = []
    all_delete_ids: List[int] = []
    protected_groups: List[str] = []
    decisions: List[GroupCleanupDecision] = []

    for group in groups:
        valid_records = [r for r in group.records if r.id is not None]
        if len(valid_records) < 2:
            continue

        group_keep: List[int] = []
        group_delete: List[int] = []
        is_protected = False

        if protect_multipart and group.is_possible_multipart:
            # Partition by normalized part marker so CD1 and CD2 are both kept,
            # while true duplicate copies of the SAME part (e.g. two CD1s) can be cleaned.
            part_buckets: Dict[str, List[DataRecord]] = {}
            for rec in valid_records:
                part_buckets.setdefault(normalize_part_key(rec.part), []).append(rec)

            for bucket in part_buckets.values():
                if len(bucket) == 1:
                    assert bucket[0].id is not None
                    group_keep.append(bucket[0].id)
                else:
                    keeper = _pick_keeper(bucket, strategy)
                    assert keeper.id is not None
                    group_keep.append(keeper.id)
                    for rec in bucket:
                        assert rec.id is not None
                        if rec.id != keeper.id:
                            group_delete.append(rec.id)

            if not group_delete:
                is_protected = True
                protected_groups.append(group.canonical_code)
        else:
            keeper = _pick_keeper(valid_records, strategy)
            assert keeper.id is not None
            group_keep.append(keeper.id)
            for rec in valid_records:
                assert rec.id is not None
                if rec.id != keeper.id:
                    group_delete.append(rec.id)

        all_keep_ids.extend(group_keep)
        all_delete_ids.extend(group_delete)
        decisions.append(
            GroupCleanupDecision(
                canonical_code=group.canonical_code,
                is_possible_multipart=group.is_possible_multipart,
                protected=is_protected,
                keep_ids=group_keep,
                delete_ids=group_delete,
            )
        )

    return CleanupPlan(
        strategy=strategy,
        protect_multipart=protect_multipart,
        keep_ids=all_keep_ids,
        delete_ids=all_delete_ids,
        protected_groups=protected_groups,
        decisions=decisions,
    )


def execute_duplicate_cleanup(
    db: DatabaseRepository,
    record_ids_to_delete: Sequence[int],
    *,
    create_backup: bool = True,
) -> int:
    """Delete selected duplicate records with optional automatic ``.bak`` backup."""
    return db.delete_records(record_ids_to_delete, create_backup=create_backup)
