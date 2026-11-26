"""Shared data models for the data deduplication application.

All terminology is strictly neutral and general-purpose (catalog/code records,
prefixes, serial numbers, version tags, and multi-part markers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Tag quality weights used by smart duplicate selection strategies.
TAG_SCORE_WEIGHTS: dict[str, int] = {
    "8K": 6,
    "4K": 5,
    "VR": 3,
    "字幕": 4,
    "特殊版": 3,
    "高码率": 2,
    "FHD": 2,
    "HD": 1,
}


def compute_tag_score(tags: list[str]) -> int:
    """Compute a quality priority score from a list of version/feature tags."""
    return sum(TAG_SCORE_WEIGHTS.get(tag, 1) for tag in tags)


@dataclass
class CustomRule:
    """Regex extraction rule (built-in or user-defined).

    Rules are evaluated in ascending order of `priority` (smaller number runs first).
    User custom rules default to priority 5 so they take precedence over built-in rules (10..90).
    """

    id: str
    name: str
    pattern: str
    format_template: str = ""
    priority: int = 5
    enabled: bool = True
    is_builtin: bool = False
    category: str = "自定义"
    description: str = ""
    example: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "pattern": self.pattern,
            "format_template": self.format_template,
            "priority": self.priority,
            "enabled": self.enabled,
            "is_builtin": self.is_builtin,
            "category": self.category,
            "description": self.description,
            "example": self.example,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CustomRule":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            pattern=str(data.get("pattern", "")),
            format_template=str(data.get("format_template", "")),
            priority=int(data.get("priority", 5)),
            enabled=bool(data.get("enabled", True)),
            is_builtin=bool(data.get("is_builtin", False)),
            category=str(data.get("category", "自定义")),
            description=str(data.get("description", "")),
            example=str(data.get("example", "")),
        )


@dataclass
class ScannedItem:
    """Raw item produced by TXT/tree/CSV/folder scanners prior to code parsing."""

    raw_text: str
    file_name: str = ""
    parent_dir: str = ""
    full_path: str = ""
    source_name: str = ""
    source_type: str = "txt"  # "txt" | "folder" | "tree" | "csv" | "paste"
    line_number: int = 0
    file_size: int = 0
    file_ext: str = ""
    is_companion: bool = False


@dataclass
class ParseResult:
    """Structured extraction output produced by the multi-stage code parser."""

    canonical_code: str = ""
    prefix: str = ""
    number: str = ""
    raw_code: str = ""
    raw_text: str = ""
    file_name: str = ""
    parent_dir: str = ""
    full_path: str = ""
    source_name: str = ""
    source_type: str = "txt"
    line_number: int = 0
    file_size: int = 0
    file_ext: str = ""
    tags: list[str] = field(default_factory=list)
    part: str = ""
    matched_rule: str = ""
    cleaned_text: str = ""
    confidence: float = 0.0
    from_parent: bool = False
    recognized: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if self.canonical_code:
            self.recognized = True

    @property
    def tags_str(self) -> str:
        return ",".join(self.tags)

    @property
    def is_multipart(self) -> bool:
        return bool(self.part)

    @property
    def tag_score(self) -> int:
        return compute_tag_score(self.tags)


@dataclass
class DataRecord:
    """Persisted record stored in SQLite and displayed in library tables."""

    id: Optional[int] = None
    canonical_code: str = ""
    prefix: str = ""
    number: str = ""
    raw_code: str = ""
    raw_text: str = ""
    file_name: str = ""
    full_path: str = ""
    source_name: str = ""
    source_type: str = "txt"
    file_size: int = 0
    file_ext: str = ""
    tags: list[str] = field(default_factory=list)
    part: str = ""
    matched_rule: str = ""
    confidence: float = 1.0
    batch_id: Optional[int] = None
    created_at: str = ""
    updated_at: str = ""
    note: str = ""

    @property
    def tags_str(self) -> str:
        return ",".join(self.tags)

    @property
    def is_multipart(self) -> bool:
        return bool(self.part)

    @property
    def tag_score(self) -> int:
        return compute_tag_score(self.tags)

    @classmethod
    def from_parse_result(
        cls,
        result: ParseResult,
        batch_id: Optional[int] = None,
    ) -> "DataRecord":
        return cls(
            canonical_code=result.canonical_code,
            prefix=result.prefix,
            number=result.number,
            raw_code=result.raw_code,
            raw_text=result.raw_text,
            file_name=result.file_name or result.raw_text,
            full_path=result.full_path,
            source_name=result.source_name,
            source_type=result.source_type,
            file_size=result.file_size,
            file_ext=result.file_ext,
            tags=list(result.tags),
            part=result.part,
            matched_rule=result.matched_rule,
            confidence=result.confidence,
            batch_id=batch_id,
            note=result.note,
        )


@dataclass
class ImportBatch:
    """Metadata for a single batch import operation."""

    id: Optional[int] = None
    source_name: str = ""
    source_type: str = "txt"
    total_count: int = 0
    added_count: int = 0
    duplicate_count: int = 0
    unrecognized_count: int = 0
    created_at: str = ""


@dataclass
class DedupPreview:
    """Dry-run categorization of parsed items prior to committing to SQLite."""

    new_items: list[ParseResult] = field(default_factory=list)
    db_duplicates: list[tuple[ParseResult, list[DataRecord]]] = field(default_factory=list)
    batch_duplicates: list[ParseResult] = field(default_factory=list)
    possible_multiparts: list[ParseResult] = field(default_factory=list)
    unrecognized: list[ParseResult] = field(default_factory=list)
    skipped_companions: int = 0
    total_scanned: int = 0


@dataclass
class CompareResult:
    """Comparison output for DB check (non-mutating) or A-vs-B cross-comparison."""

    mode: str = "db"  # "db" | "ab"
    source_a_name: str = ""
    source_b_name: str = ""
    matched_in_db: list[tuple[ParseResult, list[DataRecord]]] = field(default_factory=list)
    not_in_db: list[ParseResult] = field(default_factory=list)
    unrecognized_a: list[ParseResult] = field(default_factory=list)
    unrecognized_b: list[ParseResult] = field(default_factory=list)
    intersection_codes: list[str] = field(default_factory=list)
    intersection_a: list[ParseResult] = field(default_factory=list)
    intersection_b: list[ParseResult] = field(default_factory=list)
    only_in_a: list[ParseResult] = field(default_factory=list)
    only_in_b: list[ParseResult] = field(default_factory=list)


@dataclass
class DuplicateGroup:
    """Group of records sharing the same canonical code."""

    canonical_code: str
    records: list[DataRecord] = field(default_factory=list)
    is_possible_multipart: bool = False
    count: int = 0

    def __post_init__(self) -> None:
        if not self.count:
            self.count = len(self.records)


@dataclass
class LibraryStats:
    """Summary statistics for the active database library."""

    total_records: int = 0
    unique_codes: int = 0
    duplicate_groups: int = 0
    duplicate_records: int = 0
    possible_multipart_groups: int = 0
    prefix_counts: dict[str, int] = field(default_factory=dict)
