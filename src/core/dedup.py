"""Deduplication preview, pure comparison, A-vs-B cross-comparison, and smart cleanup.

All terminology and strategy labels are strictly neutral.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .database import DatabaseRepository, normalize_part_key
from .models import (
    ChecklistEntry,
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
            elif seen_in_batch is not None and part_key in seen_in_batch:
                batch_duplicates.append(item)
            elif part_key in db_parts:
                db_duplicates.append((item, db_records))
                batch_seen_parts.setdefault(code, set()).add(part_key)
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
    source_type: str = "",
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
    if saved_batch is None:
        raise RuntimeError("保存导入批次元数据失败")
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
# 3. Two-source A-vs-B dual-list coverage & missing-item comparison
# ----------------------------------------------------------------------


def data_record_to_parse_result(record: DataRecord) -> ParseResult:
    """Fast conversion of a persisted ``DataRecord`` into a ``ParseResult`` without re-parsing."""
    code = record.canonical_code.strip().upper()
    return ParseResult(
        canonical_code=code,
        prefix=record.prefix,
        number=record.number,
        raw_code=record.raw_code or code,
        raw_text=record.raw_text or record.file_name or code,
        file_name=record.file_name or record.raw_text or code,
        parent_dir="",
        full_path=record.full_path,
        source_name=record.source_name,
        source_type=record.source_type or "db",
        line_number=0,
        file_size=record.file_size,
        file_ext=record.file_ext,
        tags=list(record.tags),
        part=record.part,
        matched_rule=record.matched_rule or "database",
        cleaned_text="",
        confidence=record.confidence,
        from_parent=False,
        recognized=bool(code),
        note=record.note,
    )


def database_records_to_parse_results(db_repo: DatabaseRepository) -> List[ParseResult]:
    """Load all records from ``DatabaseRepository`` as ``ParseResult``s without regex parsing."""
    records = db_repo.query_records(sort_by="id", sort_desc=False)
    return [data_record_to_parse_result(rec) for rec in records]


def _collect_unique_names(items: Sequence[ParseResult]) -> List[str]:
    """Collect distinct display filenames/texts from matched Base List A items in order."""
    names: List[str] = []
    seen: set[str] = set()
    for item in items:
        label = (item.file_name or item.raw_text or item.canonical_code).strip()
        if label and label not in seen:
            seen.add(label)
            names.append(label)
    return names


def _collect_combined_tags(items: Sequence[ParseResult]) -> str:
    """Collect distinct version/feature tags across matched Base List A items in order."""
    tags: List[str] = []
    seen: set[str] = set()
    for item in items:
        for tag in item.tags:
            t = str(tag).strip()
            if t and t not in seen:
                seen.add(t)
                tags.append(t)
    return ",".join(tags)


def _normalize_exact_line_item(
    raw_item: ParseResult | str,
    *,
    source_name: str,
    line_number: int,
    strip_whitespace: bool,
    ignore_case: bool,
) -> Tuple[str, str, ParseResult]:
    """Normalize a string or ``ParseResult`` for ``exact_line`` comparison without mutating inputs."""
    if isinstance(raw_item, str):
        raw_text = raw_item
        val = raw_text.strip() if strip_whitespace else raw_text
        if not val:
            return (
                "",
                "",
                ParseResult(
                    raw_text=raw_text,
                    file_name=raw_text,
                    source_name=source_name,
                    source_type="paste",
                    line_number=line_number,
                    recognized=False,
                    note="空文本行",
                ),
            )
        cmp_key = val.casefold() if ignore_case else val
        pr = ParseResult(
            canonical_code=val,
            raw_code=val,
            raw_text=raw_text,
            file_name=raw_text,
            source_name=source_name,
            source_type="paste",
            line_number=line_number,
            matched_rule="exact_line",
            cleaned_text=val,
            confidence=1.0,
            recognized=True,
        )
        return cmp_key, val, pr

    raw_text = raw_item.raw_text or raw_item.file_name or raw_item.canonical_code
    val = raw_text.strip() if strip_whitespace else raw_text
    if not val:
        return "", "", raw_item
    cmp_key = val.casefold() if ignore_case else val
    pr = ParseResult(
        canonical_code=val,
        prefix=raw_item.prefix,
        number=raw_item.number,
        raw_code=val,
        raw_text=raw_text,
        file_name=raw_item.file_name or raw_text,
        parent_dir=raw_item.parent_dir,
        full_path=raw_item.full_path,
        source_name=raw_item.source_name or source_name,
        source_type=raw_item.source_type,
        line_number=raw_item.line_number or line_number,
        file_size=raw_item.file_size,
        file_ext=raw_item.file_ext,
        tags=list(raw_item.tags),
        part=raw_item.part,
        matched_rule="exact_line",
        cleaned_text=val,
        confidence=1.0,
        from_parent=raw_item.from_parent,
        recognized=True,
        note=raw_item.note,
    )
    return cmp_key, val, pr


def compare_two_sources(
    items_a: Sequence[ParseResult | str],
    items_b: Sequence[ParseResult | str],
    source_a_name: str = "基准清单A",
    source_b_name: str = "目标清单B",
    *,
    match_mode: str = "smart_code",
    ignore_case: bool = True,
    strip_whitespace: bool = True,
    include_unrecognized_in_checklist: bool = False,
) -> CompareResult:
    """Cross-compare Base List A against Target List B for coverage & missing-item verification.

    Modes:
    - ``"smart_code"``: compares canonical codes of A vs B, builds an ordered ``target_checklist``
      preserving Target List B's first-appearance order, and attaches matching A filenames & tags.
    - ``"exact_line"``: compares raw strings or ``ParseResult.raw_text`` lines directly (with
      configurable case folding and whitespace stripping) for general office/plain-text lists.
    """
    mode_norm = (match_mode or "smart_code").strip().lower()
    if mode_norm not in {"smart_code", "exact_line"}:
        raise ValueError(f"不支持的比对模式：{match_mode}")

    if mode_norm == "exact_line":
        return _compare_two_sources_exact_line(
            items_a,
            items_b,
            source_a_name=source_a_name,
            source_b_name=source_b_name,
            ignore_case=ignore_case,
            strip_whitespace=strip_whitespace,
            include_unrecognized_in_checklist=include_unrecognized_in_checklist,
        )

    # Smart canonical code mode.
    lazy_parser = None

    def _ensure_parse_result(item: ParseResult | str, src: str, idx: int) -> ParseResult:
        nonlocal lazy_parser
        if isinstance(item, ParseResult):
            return item
        if lazy_parser is None:
            from .parser import CodeParser

            lazy_parser = CodeParser()
        return lazy_parser.parse_single(item, source_name=src, line_number=idx)

    a_by_code: Dict[str, List[ParseResult]] = {}
    recognized_a: List[ParseResult] = []
    unrecognized_a: List[ParseResult] = []

    for idx, raw_item in enumerate(items_a, start=1):
        item = _ensure_parse_result(raw_item, source_a_name, idx)
        code = item.canonical_code.strip().upper()
        if item.recognized and code:
            a_by_code.setdefault(code, []).append(item)
            recognized_a.append(item)
        else:
            unrecognized_a.append(item)

    codes_a = set(a_by_code.keys())
    codes_b: set[str] = set()
    recognized_b: List[ParseResult] = []
    unrecognized_b: List[ParseResult] = []
    target_checklist: List[ChecklistEntry] = []
    matched_b_codes: List[str] = []
    missing_b_codes: List[str] = []
    seen_b_codes: set[str] = set()
    order_index = 0

    for idx, raw_item in enumerate(items_b, start=1):
        item = _ensure_parse_result(raw_item, source_b_name, idx)
        code = item.canonical_code.strip().upper()
        if item.recognized and code:
            codes_b.add(code)
            recognized_b.append(item)
            if code not in seen_b_codes:
                seen_b_codes.add(code)
                order_index += 1
                target_text = item.raw_text or item.file_name or code
                matched_a_list = a_by_code.get(code)
                if matched_a_list:
                    matched_b_codes.append(code)
                    target_checklist.append(
                        ChecklistEntry(
                            order_index=order_index,
                            canonical_code=code,
                            status="matched",
                            target_text=target_text,
                            target_item=item,
                            matched_a_count=len(matched_a_list),
                            matched_a_names=_collect_unique_names(matched_a_list),
                            matched_a_tags=_collect_combined_tags(matched_a_list),
                            matched_a_items=list(matched_a_list),
                        )
                    )
                else:
                    missing_b_codes.append(code)
                    target_checklist.append(
                        ChecklistEntry(
                            order_index=order_index,
                            canonical_code=code,
                            status="missing",
                            target_text=target_text,
                            target_item=item,
                        )
                    )
        else:
            unrecognized_b.append(item)
            if include_unrecognized_in_checklist:
                order_index += 1
                target_checklist.append(
                    ChecklistEntry(
                        order_index=order_index,
                        canonical_code="",
                        status="unrecognized",
                        target_text=item.raw_text or item.file_name,
                        target_item=item,
                    )
                )

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
        match_mode="smart_code",
        target_checklist=target_checklist,
        unique_a_count=len(codes_a),
        unique_b_count=len(codes_b),
        hit_b_count=len(matched_b_codes),
        missing_b_count=len(missing_b_codes),
        missing_b_codes=missing_b_codes,
        matched_b_codes=matched_b_codes,
    )


def _compare_two_sources_exact_line(
    items_a: Sequence[ParseResult | str],
    items_b: Sequence[ParseResult | str],
    *,
    source_a_name: str,
    source_b_name: str,
    ignore_case: bool,
    strip_whitespace: bool,
    include_unrecognized_in_checklist: bool,
) -> CompareResult:
    """Compare two lists by normalized text line for general-purpose plain-text auditing."""
    a_by_key: Dict[str, List[ParseResult]] = {}
    recognized_a_pairs: List[Tuple[str, ParseResult]] = []
    unrecognized_a: List[ParseResult] = []

    for idx, raw_item in enumerate(items_a, start=1):
        cmp_key, _, pr = _normalize_exact_line_item(
            raw_item,
            source_name=source_a_name,
            line_number=idx,
            strip_whitespace=strip_whitespace,
            ignore_case=ignore_case,
        )
        if cmp_key:
            a_by_key.setdefault(cmp_key, []).append(pr)
            recognized_a_pairs.append((cmp_key, pr))
        else:
            unrecognized_a.append(pr)

    b_display_by_key: Dict[str, str] = {}
    recognized_b_pairs: List[Tuple[str, ParseResult]] = []
    unrecognized_b: List[ParseResult] = []
    target_checklist: List[ChecklistEntry] = []
    matched_b_codes: List[str] = []
    missing_b_codes: List[str] = []
    order_index = 0

    for idx, raw_item in enumerate(items_b, start=1):
        cmp_key, display_val, pr = _normalize_exact_line_item(
            raw_item,
            source_name=source_b_name,
            line_number=idx,
            strip_whitespace=strip_whitespace,
            ignore_case=ignore_case,
        )
        if cmp_key:
            recognized_b_pairs.append((cmp_key, pr))
            if cmp_key not in b_display_by_key:
                b_display_by_key[cmp_key] = display_val
                order_index += 1
                matched_a_list = a_by_key.get(cmp_key)
                if matched_a_list:
                    matched_b_codes.append(display_val)
                    target_checklist.append(
                        ChecklistEntry(
                            order_index=order_index,
                            canonical_code=display_val,
                            status="matched",
                            target_text=pr.raw_text,
                            target_item=pr,
                            matched_a_count=len(matched_a_list),
                            matched_a_names=_collect_unique_names(matched_a_list),
                            matched_a_tags=_collect_combined_tags(matched_a_list),
                            matched_a_items=list(matched_a_list),
                        )
                    )
                else:
                    missing_b_codes.append(display_val)
                    target_checklist.append(
                        ChecklistEntry(
                            order_index=order_index,
                            canonical_code=display_val,
                            status="missing",
                            target_text=pr.raw_text,
                            target_item=pr,
                        )
                    )
        else:
            unrecognized_b.append(pr)
            if include_unrecognized_in_checklist:
                order_index += 1
                target_checklist.append(
                    ChecklistEntry(
                        order_index=order_index,
                        canonical_code="",
                        status="unrecognized",
                        target_text=pr.raw_text,
                        target_item=pr,
                    )
                )

    intersection_keys = set(a_by_key.keys()) & set(b_display_by_key.keys())
    intersection_codes = sorted(b_display_by_key[k] for k in intersection_keys)

    intersection_a = [pr for k, pr in recognized_a_pairs if k in intersection_keys]
    intersection_b = [pr for k, pr in recognized_b_pairs if k in intersection_keys]
    only_in_a = [pr for k, pr in recognized_a_pairs if k not in intersection_keys]
    only_in_b = [pr for k, pr in recognized_b_pairs if k not in intersection_keys]

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
        match_mode="exact_line",
        target_checklist=target_checklist,
        unique_a_count=len(a_by_key),
        unique_b_count=len(b_display_by_key),
        hit_b_count=len(matched_b_codes),
        missing_b_count=len(missing_b_codes),
        missing_b_codes=missing_b_codes,
        matched_b_codes=matched_b_codes,
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
                    rid = bucket[0].id
                    if rid is not None:
                        group_keep.append(rid)
                else:
                    keeper = _pick_keeper(bucket, strategy)
                    keeper_id = keeper.id
                    if keeper_id is not None:
                        group_keep.append(keeper_id)
                    for rec in bucket:
                        if rec.id is not None and rec.id != keeper_id:
                            group_delete.append(rec.id)

            if not group_delete:
                is_protected = True
                protected_groups.append(group.canonical_code)
        else:
            keeper = _pick_keeper(valid_records, strategy)
            keeper_id = keeper.id
            if keeper_id is not None:
                group_keep.append(keeper_id)
            for rec in valid_records:
                if rec.id is not None and rec.id != keeper_id:
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
