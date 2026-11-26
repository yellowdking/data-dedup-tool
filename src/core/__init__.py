"""Core engine package for the data deduplication application."""

from __future__ import annotations

import importlib
from typing import Any

from .io_utils import (
    DEFAULT_COMPANION_EXTENSIONS,
    DEFAULT_COMPANION_EXTENSIONS_LIST,
    decode_bytes_auto,
    format_cell_value,
    normalize_cell_text,
    normalize_companion_extensions,
    read_text_auto_encoding,
)
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
    PRIMARY_FILE_EXTENSIONS,
    clean_text,
    extract_part,
    extract_tags,
    is_companion_extension,
    is_false_positive,
    normalize_serial_number,
    normalize_width,
)
from .parser import CodeParser, parse_code
from .rules import RuleRegistry, get_builtin_rules
from .scanner import (
    SUPPORTED_DOCUMENT_EXTENSIONS,
    detect_input_format,
    scan_document_file,
    scan_excel_file,
    scan_file,
    scan_files,
    scan_folder,
    scan_text,
    scan_word_file,
)

# Lazy-loaded heavy submodules so importing lightweight submodules like `src.core.parser`
# does not eagerly pull in openpyxl / python-docx / table studio modules.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    # doc_reader
    "SUPPORTED_TABLE_EXTENSIONS": (".doc_reader", "SUPPORTED_TABLE_EXTENSIONS"),
    "list_excel_sheets": (".doc_reader", "list_excel_sheets"),
    "read_excel_tables": (".doc_reader", "read_excel_tables"),
    "extract_excel_text_items": (".doc_reader", "extract_excel_text_items"),
    "read_word_tables": (".doc_reader", "read_word_tables"),
    "extract_word_text_items": (".doc_reader", "extract_word_text_items"),
    "read_csv_table": (".doc_reader", "read_csv_table"),
    "read_txt_as_table": (".doc_reader", "read_txt_as_table"),
    "read_database_as_table": (".doc_reader", "read_database_as_table"),
    "load_tables_from_file": (".doc_reader", "load_tables_from_file"),
    # table_data
    "generate_table_id": (".table_data", "generate_table_id"),
    "TableColumnInfo": (".table_data", "TableColumnInfo"),
    "DataTable": (".table_data", "DataTable"),
    "TableDedupConfig": (".table_data", "TableDedupConfig"),
    "TableRowRef": (".table_data", "TableRowRef"),
    "TableDuplicateGroup": (".table_data", "TableDuplicateGroup"),
    "TableDedupReport": (".table_data", "TableDedupReport"),
    "TableMergeConfig": (".table_data", "TableMergeConfig"),
    "TableMergeResult": (".table_data", "TableMergeResult"),
    # table_engine
    "DEDUP_MODE_SMART_CODE": (".table_engine", "DEDUP_MODE_SMART_CODE"),
    "DEDUP_MODE_COLUMN_VALUE": (".table_engine", "DEDUP_MODE_COLUMN_VALUE"),
    "MERGE_MODE_UNION_ALL": (".table_engine", "MERGE_MODE_UNION_ALL"),
    "MERGE_MODE_UNION_DEDUP": (".table_engine", "MERGE_MODE_UNION_DEDUP"),
    "MERGE_MODE_JOIN": (".table_engine", "MERGE_MODE_JOIN"),
    "ALIGN_MODE_BY_HEADER": (".table_engine", "ALIGN_MODE_BY_HEADER"),
    "ALIGN_MODE_BY_POSITION": (".table_engine", "ALIGN_MODE_BY_POSITION"),
    "KEEP_STRATEGY_FIRST": (".table_engine", "KEEP_STRATEGY_FIRST"),
    "KEEP_STRATEGY_LAST": (".table_engine", "KEEP_STRATEGY_LAST"),
    "KEEP_STRATEGY_MOST_COMPLETE": (".table_engine", "KEEP_STRATEGY_MOST_COMPLETE"),
    "KEEP_STRATEGY_HIGHEST_TAG_SCORE": (".table_engine", "KEEP_STRATEGY_HIGHEST_TAG_SCORE"),
    "KEEP_STRATEGY_COALESCE": (".table_engine", "KEEP_STRATEGY_COALESCE"),
    "JOIN_TYPE_LEFT": (".table_engine", "JOIN_TYPE_LEFT"),
    "JOIN_TYPE_INNER": (".table_engine", "JOIN_TYPE_INNER"),
    "JOIN_TYPE_OUTER": (".table_engine", "JOIN_TYPE_OUTER"),
    "KEEP_STRATEGY_LABELS": (".table_engine", "KEEP_STRATEGY_LABELS"),
    "run_multi_table_dedup": (".table_engine", "run_multi_table_dedup"),
    "merge_tables": (".table_engine", "merge_tables"),
    "import_table_to_database": (".table_engine", "import_table_to_database"),
    # table_exporter
    "sanitize_xml_text": (".table_exporter", "sanitize_xml_text"),
    "sanitize_sheet_name": (".table_exporter", "sanitize_sheet_name"),
    "export_table_excel": (".table_exporter", "export_table_excel"),
    "export_table_csv": (".table_exporter", "export_table_csv"),
    "export_table_word": (".table_exporter", "export_table_word"),
    "export_table_txt": (".table_exporter", "export_table_txt"),
    "export_multiple_tables_workbook": (".table_exporter", "export_multiple_tables_workbook"),
    "export_dedup_report_excel": (".table_exporter", "export_dedup_report_excel"),
    "export_dedup_report_word": (".table_exporter", "export_dedup_report_word"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_name, attr_name = target
    mod = importlib.import_module(mod_name, __name__)
    val = getattr(mod, attr_name)
    globals()[name] = val
    return val


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
    "DEFAULT_COMPANION_EXTENSIONS_LIST",
    "PRIMARY_FILE_EXTENSIONS",
    "decode_bytes_auto",
    "format_cell_value",
    "normalize_companion_extensions",
    "clean_text",
    "extract_tags",
    "extract_part",
    "is_companion_extension",
    "is_false_positive",
    "normalize_serial_number",
    "normalize_width",
    "RuleRegistry",
    "get_builtin_rules",
    "CodeParser",
    "parse_code",
    "SUPPORTED_DOCUMENT_EXTENSIONS",
    "normalize_cell_text",
    "read_text_auto_encoding",
    "detect_input_format",
    "scan_text",
    "scan_file",
    "scan_word_file",
    "scan_excel_file",
    "scan_document_file",
    "scan_files",
    "scan_folder",
    *_LAZY_EXPORTS.keys(),
]
