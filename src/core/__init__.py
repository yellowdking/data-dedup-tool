"""Core engine package for the data deduplication application."""

from .models import (
    TAG_SCORE_WEIGHTS,
    CompareResult,
    CustomRule,
    DataRecord,
    DedupPreview,
    DuplicateGroup,
    ImportBatch,
    LibraryStats,
    ParseResult,
    ScannedItem,
    compute_tag_score,
)
from .normalizer import (
    DEFAULT_COMPANION_EXTENSIONS,
    PRIMARY_FILE_EXTENSIONS,
    clean_text,
    extract_part,
    extract_tags,
    is_companion_extension,
    is_false_positive,
)
from .parser import CodeParser, parse_code
from .rules import RuleRegistry, get_builtin_rules
from .scanner import (
    detect_input_format,
    read_text_auto_encoding,
    scan_file,
    scan_files,
    scan_folder,
    scan_text,
)

__all__ = [
    "TAG_SCORE_WEIGHTS",
    "compute_tag_score",
    "CustomRule",
    "ScannedItem",
    "ParseResult",
    "DataRecord",
    "ImportBatch",
    "DedupPreview",
    "CompareResult",
    "DuplicateGroup",
    "LibraryStats",
    "DEFAULT_COMPANION_EXTENSIONS",
    "PRIMARY_FILE_EXTENSIONS",
    "clean_text",
    "extract_tags",
    "extract_part",
    "is_companion_extension",
    "is_false_positive",
    "RuleRegistry",
    "get_builtin_rules",
    "CodeParser",
    "parse_code",
    "read_text_auto_encoding",
    "detect_input_format",
    "scan_text",
    "scan_file",
    "scan_files",
    "scan_folder",
]
