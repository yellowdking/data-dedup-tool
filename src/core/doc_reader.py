"""Multi-format document and table loader (Excel, Word, CSV/TSV, TXT, SQLite DB).

Produces normalized `DataTable` instances for Multi-Table Studio as well as
structured text extraction items for Word/Excel batch scanning.
All terminology, identifiers, and messages are strictly neutral.
"""

from __future__ import annotations

import csv
import io
import math
import zipfile
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from .table_data import DataTable, generate_table_id

# Encodings attempted when reading CSV/TSV/TXT files without explicit BOM.
_CANDIDATE_ENCODINGS: tuple[str, ...] = (
    "utf-8-sig",
    "utf-8",
    "gb18030",
    "big5",
    "cp932",
    "utf-16",
)

SUPPORTED_TABLE_EXTENSIONS: frozenset[str] = frozenset(
    {".xlsx", ".xlsm", ".docx", ".csv", ".tsv", ".efu", ".txt", ".log"}
)


def _decode_bytes_auto(raw_bytes: bytes) -> tuple[str, str]:
    """Decode raw bytes by checking BOM first and trying common Windows/CJK encodings."""
    if not raw_bytes:
        return "", "utf-8"

    if raw_bytes.startswith(b"\xff\xfe") or raw_bytes.startswith(b"\xfe\xff"):
        return raw_bytes.decode("utf-16"), "utf-16"
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        return raw_bytes.decode("utf-8-sig"), "utf-8-sig"

    for enc in _CANDIDATE_ENCODINGS:
        if enc in ("utf-8-sig", "utf-16"):
            continue
        try:
            return raw_bytes.decode(enc), enc
        except UnicodeDecodeError:
            continue

    return raw_bytes.decode("utf-8", errors="replace"), "utf-8-replace"


def _format_cell_value(value: Any) -> str:
    """Convert an openpyxl/Python cell value into a clean display string.

    - Converts whole-number floats such as ``12345.0`` to ``"12345"`` without
      altering real decimals like ``3.14``.
    - Formats date/datetime/time objects consistently.
    - Normalizes line endings and trims outer whitespace.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        if value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite():
            return ""
        if value == value.to_integral_value():
            return str(int(value))
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        if (
            value.hour == 0
            and value.minute == 0
            and value.second == 0
            and value.microsecond == 0
        ):
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, time):
        return value.strftime("%H:%M:%S")

    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def _make_unique_headers(raw_headers: Sequence[str], col_count: int) -> list[str]:
    """Normalize empty or duplicate headers into clean unique column names.

    Empty headers become ``列1``, ``列2``, ...; duplicate headers become
    ``标题_2``, ``标题_3``, etc.
    """
    target_len = max(col_count, len(raw_headers))
    used: set[str] = set()
    result: list[str] = []

    for idx in range(target_len):
        raw = raw_headers[idx] if idx < len(raw_headers) else ""
        base = " ".join(str(raw or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()).strip()
        if not base:
            base = f"列{idx + 1}"

        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        result.append(candidate)

    return result


def _trim_matrix(matrix: list[list[str]]) -> list[list[str]]:
    """Trim leading/trailing blank rows, drop all-blank trailing cols, and pad rows uniformly."""
    if not matrix:
        return []

    # Strip leading blank rows.
    start = 0
    while start < len(matrix) and not any(cell.strip() for cell in matrix[start]):
        start += 1
    trimmed_rows = matrix[start:]

    # Strip trailing blank rows.
    while trimmed_rows and not any(cell.strip() for cell in trimmed_rows[-1]):
        trimmed_rows.pop()

    if not trimmed_rows:
        return []

    # Determine rightmost non-empty column across all remaining rows.
    max_col = 0
    for row in trimmed_rows:
        for col_idx in range(len(row) - 1, -1, -1):
            if row[col_idx].strip():
                if col_idx + 1 > max_col:
                    max_col = col_idx + 1
                break

    if max_col == 0:
        return []

    normalized: list[list[str]] = []
    for row in trimmed_rows:
        if not any(cell.strip() for cell in row[:max_col]):
            continue
        padded = [row[i] if i < len(row) else "" for i in range(max_col)]
        normalized.append(padded)

    return normalized


def _matrix_to_headers_and_rows(
    matrix: list[list[str]],
    *,
    has_header: bool = True,
    single_row_as_data: bool = False,
) -> tuple[list[str], list[list[str]]]:
    """Split a trimmed 2D string matrix into normalized `(headers, data_rows)`."""
    if not matrix:
        return [], []

    col_count = max(len(r) for r in matrix)
    if not has_header or (single_row_as_data and len(matrix) == 1):
        headers = _make_unique_headers([], col_count)
        return headers, matrix

    headers = _make_unique_headers(matrix[0], col_count)
    return headers, matrix[1:]


# ---------------------------------------------------------------------------
# Excel (.xlsx / .xlsm)
# ---------------------------------------------------------------------------


def _validate_excel_path(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"找不到 Excel 文件：{path}")
    ext = path.suffix.lower()
    if ext == ".xls":
        raise ValueError(
            "暂不支持旧版 Excel (.xls) 格式，请使用 Excel 或 WPS 将文件另存为 .xlsx 格式后重试。"
        )
    if ext not in {".xlsx", ".xlsm"}:
        raise ValueError(f"不支持的 Excel 扩展名「{ext}」，请提供 .xlsx 或 .xlsm 文件。")


def _load_openpyxl_workbook(path: Path, *, read_only: bool = False):
    import openpyxl
    from openpyxl.utils.exceptions import InvalidFileException

    try:
        return openpyxl.load_workbook(
            filename=str(path),
            read_only=read_only,
            data_only=True,
        )
    except (InvalidFileException, zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ValueError(f"无法读取 Excel 文件「{path.name}」：{exc}") from exc


def list_excel_sheets(path: Union[str, Path]) -> list[str]:
    """Return the ordered list of worksheet names in an `.xlsx`/`.xlsm` workbook."""
    file_path = Path(path)
    _validate_excel_path(file_path)
    wb = _load_openpyxl_workbook(file_path, read_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def _extract_sheet_matrix(ws) -> list[list[str]]:
    """Read an openpyxl worksheet into a 2D list of formatted strings.

    Propagates values for single-column vertical merges in data rows so
    grouped category columns remain populated without smearing horizontal
    title banners across header columns.
    """
    raw_rows: list[list[str]] = [
        [_format_cell_value(cell) for cell in row]
        for row in ws.iter_rows(values_only=True)
    ]

    # Propagate vertical-only merged cells (same column, spanning multiple rows).
    merged_ranges = getattr(ws, "merged_cells", None)
    if merged_ranges is not None and raw_rows:
        for rng in merged_ranges.ranges:
            if rng.min_col == rng.max_col and rng.max_row > rng.min_row:
                r0 = rng.min_row - 1
                c0 = rng.min_col - 1
                if r0 < len(raw_rows) and c0 < len(raw_rows[r0]):
                    top_val = raw_rows[r0][c0]
                    if top_val:
                        for r_idx in range(r0 + 1, min(rng.max_row, len(raw_rows))):
                            if c0 < len(raw_rows[r_idx]) and not raw_rows[r_idx][c0]:
                                raw_rows[r_idx][c0] = top_val

    return raw_rows


def read_excel_tables(
    path: Union[str, Path],
    sheet_names: Optional[Sequence[str]] = None,
    has_header: bool = True,
) -> list[DataTable]:
    """Load one or more worksheets from an `.xlsx`/`.xlsm` workbook into `DataTable`s."""
    file_path = Path(path)
    _validate_excel_path(file_path)

    wb = _load_openpyxl_workbook(file_path, read_only=False)
    tables: list[DataTable] = []
    try:
        available = list(wb.sheetnames)
        if sheet_names is not None:
            requested = list(sheet_names)
            missing = [s for s in requested if s not in available]
            target_sheets = [s for s in requested if s in available]
            if not target_sheets:
                raise ValueError(
                    f"工作簿「{file_path.name}」中未找到指定工作表：{', '.join(missing)}"
                )
        else:
            target_sheets = available

        for sheet_name in target_sheets:
            ws = wb[sheet_name]
            raw_matrix = _extract_sheet_matrix(ws)
            trimmed = _trim_matrix(raw_matrix)
            if not trimmed:
                continue

            headers, rows = _matrix_to_headers_and_rows(
                trimmed,
                has_header=has_header,
                single_row_as_data=False,
            )
            table_name = f"{file_path.stem} - {sheet_name}"
            tables.append(
                DataTable(
                    table_id=generate_table_id(),
                    name=table_name,
                    headers=headers,
                    rows=rows,
                    source_path=str(file_path),
                    sheet_name=sheet_name,
                    source_type="excel",
                    metadata={
                        "file_name": file_path.name,
                        "sheet_name": sheet_name,
                        "has_header": has_header,
                    },
                )
            )
    finally:
        wb.close()

    return tables


def extract_excel_text_items(
    path: Union[str, Path],
    sheet_names: Optional[Sequence[str]] = None,
) -> list[dict[str, Any]]:
    """Extract non-empty rows/cells from an Excel workbook with `工作表名 行R` locators.

    Designed for document-level code scanning in `scanner.py`. Multi-line cells
    in single-cell rows are emitted line-by-line to preserve per-line context;
    multi-column rows are joined cleanly with ``" | "`` while keeping raw cell
    values in ``cells``.
    """
    file_path = Path(path)
    _validate_excel_path(file_path)

    wb = _load_openpyxl_workbook(file_path, read_only=False)
    items: list[dict[str, Any]] = []
    try:
        available = list(wb.sheetnames)
        if sheet_names is not None:
            target_sheets = [s for s in sheet_names if s in available]
            if not target_sheets:
                raise ValueError(
                    f"工作簿「{file_path.name}」中未找到指定工作表：{', '.join(sheet_names)}"
                )
        else:
            target_sheets = available

        for sheet_name in target_sheets:
            ws = wb[sheet_name]
            for row_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
                formatted_cells = [_format_cell_value(cell) for cell in row]
                non_empty = [c for c in formatted_cells if c]
                if not non_empty:
                    continue

                location = f"{sheet_name} 行{row_idx}"
                if len(non_empty) == 1 and "\n" in non_empty[0]:
                    sub_lines = [ln.strip() for ln in non_empty[0].splitlines() if ln.strip()]
                    for sub in sub_lines:
                        items.append(
                            {
                                "text": sub,
                                "cells": [sub],
                                "location": location,
                                "sheet_name": sheet_name,
                                "row_index": row_idx,
                                "source_type": "excel",
                            }
                        )
                else:
                    flattened_cells = [
                        " ".join(ln.strip() for ln in c.splitlines() if ln.strip())
                        for c in non_empty
                    ]
                    items.append(
                        {
                            "text": " | ".join(flattened_cells),
                            "cells": non_empty,
                            "location": location,
                            "sheet_name": sheet_name,
                            "row_index": row_idx,
                            "source_type": "excel",
                        }
                    )
    finally:
        wb.close()

    return items


# ---------------------------------------------------------------------------
# Word (.docx)
# ---------------------------------------------------------------------------


def _validate_word_path(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"找不到 Word 文件：{path}")
    ext = path.suffix.lower()
    if ext == ".doc":
        raise ValueError(
            "暂不支持旧版 Word (.doc) 格式，请使用 Word 或 WPS 将文件另存为 .docx 格式后重试。"
        )
    if ext != ".docx":
        raise ValueError(f"不支持的 Word 扩展名「{ext}」，请提供 .docx 文件。")


def _load_docx_document(path: Path):
    import docx
    from docx.opc.exceptions import PackageNotFoundError

    try:
        return docx.Document(str(path))
    except (PackageNotFoundError, zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ValueError(f"无法读取 Word 文件「{path.name}」：{exc}") from exc


def _clean_word_text(text: str) -> str:
    """Normalize line breaks and outer whitespace in Word paragraph/cell text."""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _extract_word_table_matrix(table) -> list[list[str]]:
    """Convert a `python-docx` Table into a 2D matrix with merged-cell handling.

    Horizontally merged cells within the same row emit their value once in the
    leftmost grid column and ``""`` in subsequent spanned columns so column
    indices stay aligned. Vertically merged cells retain their text in each
    spanned row so tabular category fields remain intact.
    """
    matrix: list[list[str]] = []
    for row in table.rows:
        seen_tcs_in_row: set[Any] = set()
        row_vals: list[str] = []
        for cell in row.cells:
            tc = cell._tc
            if tc in seen_tcs_in_row:
                row_vals.append("")
            else:
                seen_tcs_in_row.add(tc)
                row_vals.append(_clean_word_text(cell.text))
        matrix.append(row_vals)
    return matrix


def read_word_tables(
    path: Union[str, Path],
    include_paragraphs_as_table: bool = False,
    has_header: bool = True,
) -> list[DataTable]:
    """Extract embedded tables (and optionally body paragraphs) from a `.docx` file."""
    file_path = Path(path)
    _validate_word_path(file_path)

    doc = _load_docx_document(file_path)
    tables: list[DataTable] = []

    for tbl_idx, word_table in enumerate(doc.tables, start=1):
        raw_matrix = _extract_word_table_matrix(word_table)
        trimmed = _trim_matrix(raw_matrix)
        if not trimmed:
            continue

        headers, rows = _matrix_to_headers_and_rows(
            trimmed,
            has_header=has_header,
            single_row_as_data=True,
        )
        sheet_label = f"表格{tbl_idx}"
        tables.append(
            DataTable(
                table_id=generate_table_id(),
                name=f"{file_path.stem} - {sheet_label}",
                headers=headers,
                rows=rows,
                source_path=str(file_path),
                sheet_name=sheet_label,
                source_type="word",
                metadata={
                    "file_name": file_path.name,
                    "table_index": tbl_idx,
                    "has_header": has_header,
                },
            )
        )

    if include_paragraphs_as_table or not tables:
        para_rows: list[list[str]] = []
        for para in doc.paragraphs:
            cleaned = _clean_word_text(para.text)
            if not cleaned:
                continue
            for line in cleaned.splitlines():
                line_s = line.strip()
                if line_s:
                    para_rows.append([line_s])
        if para_rows:
            tables.append(
                DataTable(
                    table_id=generate_table_id(),
                    name=f"{file_path.stem} - 正文段落",
                    headers=["段落内容"],
                    rows=para_rows,
                    source_path=str(file_path),
                    sheet_name="正文段落",
                    source_type="word",
                    metadata={
                        "file_name": file_path.name,
                        "kind": "paragraphs",
                    },
                )
            )

    return tables


def extract_word_text_items(path: Union[str, Path]) -> list[dict[str, Any]]:
    """Extract paragraphs, table cells (`表格N 行R列C`), and headers/footers from `.docx`.

    Deduplicates merged `<w:tc>` table cells so merged text is emitted once.
    """
    file_path = Path(path)
    _validate_word_path(file_path)

    doc = _load_docx_document(file_path)
    items: list[dict[str, Any]] = []

    # 1. Body paragraphs.
    para_num = 0
    for para in doc.paragraphs:
        cleaned = _clean_word_text(para.text)
        if not cleaned:
            continue
        para_num += 1
        for line in cleaned.splitlines():
            line_s = line.strip()
            if line_s:
                items.append(
                    {
                        "text": line_s,
                        "location": f"正文 段落{para_num}",
                        "kind": "paragraph",
                        "index": para_num,
                        "source_type": "word",
                    }
                )

    # 2. Embedded tables (with merged-cell deduplication & nested table support).
    def _collect_word_table(tbl, tbl_label: str, tbl_idx: int) -> None:
        seen_tcs: set[Any] = set()
        for row_idx, row in enumerate(tbl.rows, start=1):
            for col_idx, cell in enumerate(row.cells, start=1):
                tc = cell._tc
                if tc in seen_tcs:
                    continue
                seen_tcs.add(tc)
                loc = f"{tbl_label} 行{row_idx}列{col_idx}"
                cleaned = _clean_word_text(cell.text)
                if cleaned:
                    for line in cleaned.splitlines():
                        line_s = line.strip()
                        if line_s:
                            items.append(
                                {
                                    "text": line_s,
                                    "location": loc,
                                    "kind": "table_cell",
                                    "table_index": tbl_idx,
                                    "row_index": row_idx,
                                    "col_index": col_idx,
                                    "source_type": "word",
                                }
                            )
                for nested_idx, nested_tbl in enumerate(cell.tables, start=1):
                    _collect_word_table(nested_tbl, f"{tbl_label}.{nested_idx}", tbl_idx)

    for tbl_idx, word_table in enumerate(doc.tables, start=1):
        _collect_word_table(word_table, f"表格{tbl_idx}", tbl_idx)

    # 3. Section headers and footers (deduplicating linked XML elements).
    seen_hf_elements: set[Any] = set()
    for sec_idx, section in enumerate(doc.sections, start=1):
        hf_candidates = [
            ("页眉", "header", section.header),
            ("页眉(首页)", "header", section.first_page_header),
            ("页眉(偶数页)", "header", section.even_page_header),
            ("页脚", "footer", section.footer),
            ("页脚(首页)", "footer", section.first_page_footer),
            ("页脚(偶数页)", "footer", section.even_page_footer),
        ]
        for label, kind, part in hf_candidates:
            if part is None:
                continue
            if getattr(part, "is_linked_to_previous", False) and sec_idx > 1:
                continue
            el = part._element
            if el in seen_hf_elements:
                continue
            seen_hf_elements.add(el)

            loc = f"{label} 节{sec_idx}"
            for para in part.paragraphs:
                cleaned = _clean_word_text(para.text)
                if not cleaned:
                    continue
                for line in cleaned.splitlines():
                    line_s = line.strip()
                    if line_s:
                        items.append(
                            {
                                "text": line_s,
                                "location": loc,
                                "kind": kind,
                                "section_index": sec_idx,
                                "source_type": "word",
                            }
                        )

            seen_hf_tcs: set[Any] = set()
            for hf_tbl in part.tables:
                for row in hf_tbl.rows:
                    for cell in row.cells:
                        tc = cell._tc
                        if tc in seen_hf_tcs:
                            continue
                        seen_hf_tcs.add(tc)
                        cleaned = _clean_word_text(cell.text)
                        if not cleaned:
                            continue
                        for line in cleaned.splitlines():
                            line_s = line.strip()
                            if line_s:
                                items.append(
                                    {
                                        "text": line_s,
                                        "location": loc,
                                        "kind": kind,
                                        "section_index": sec_idx,
                                        "source_type": "word",
                                    }
                                )

    return items


# ---------------------------------------------------------------------------
# CSV / TSV
# ---------------------------------------------------------------------------


def _sniff_delimiter(text: str, default: str = ",") -> str:
    """Detect whether a CSV/TSV sample uses `,`, `\\t`, or `;` as its delimiter."""
    sample_lines = [ln for ln in text.splitlines() if ln.strip()][:20]
    if not sample_lines:
        return default
    sample = "\n".join(sample_lines)

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        if dialect.delimiter in {",", "\t", ";"}:
            return dialect.delimiter
    except csv.Error:
        pass

    # Fallback heuristic: pick the delimiter with highest consistent per-line count.
    best_delim = default
    best_score = 0
    for cand in ("\t", ",", ";"):
        counts = [ln.count(cand) for ln in sample_lines]
        non_zero = [c for c in counts if c > 0]
        if non_zero:
            score = sum(non_zero) * len(non_zero)
            if score > best_score:
                best_score = score
                best_delim = cand
    return best_delim


def read_csv_table(
    path: Union[str, Path],
    has_header: bool = True,
    delimiter: Optional[str] = None,
) -> DataTable:
    """Read a CSV/TSV/EFU file with automatic encoding and delimiter detection."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"找不到 CSV 文件：{file_path}")

    raw_bytes = file_path.read_bytes()
    text, encoding = _decode_bytes_auto(raw_bytes)

    default_delim = "\t" if file_path.suffix.lower() == ".tsv" else ","
    detected_delim = delimiter or _sniff_delimiter(text, default=default_delim)

    reader = csv.reader(io.StringIO(text), delimiter=detected_delim)
    raw_matrix = [[_format_cell_value(cell) for cell in row] for row in reader]
    trimmed = _trim_matrix(raw_matrix)

    headers, rows = _matrix_to_headers_and_rows(
        trimmed,
        has_header=has_header,
        single_row_as_data=False,
    )

    return DataTable(
        table_id=generate_table_id(),
        name=file_path.stem,
        headers=headers,
        rows=rows,
        source_path=str(file_path),
        sheet_name="",
        source_type="csv",
        metadata={
            "file_name": file_path.name,
            "encoding": encoding,
            "delimiter": detected_delim,
            "has_header": has_header,
        },
    )


# ---------------------------------------------------------------------------
# TXT / Log / Tree -> DataTable
# ---------------------------------------------------------------------------


def read_txt_as_table(
    path: Union[str, Path],
    parser_engine: Optional[Any] = None,
) -> DataTable:
    """Load a TXT/log/tree file into a structured `DataTable`.

    When `parser_engine` (e.g. `CodeParser`) is provided, scans and parses the
    file into columns: ``标准编码 / 前缀分类 / 数字编号 / 标签 / 分集 / 原始文本 / 来源``.
    Otherwise loads non-empty lines into ``原始文本 / 来源``.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"找不到文本文件：{file_path}")

    if parser_engine is not None:
        from .scanner import scan_file

        scanned = scan_file(file_path)
        parsed = parser_engine.parse_items(scanned, ignore_companions=True)
        headers = ["标准编码", "前缀分类", "数字编号", "标签", "分集", "原始文本", "来源"]
        rows = [
            [
                r.canonical_code,
                r.prefix,
                r.number,
                r.tags_str,
                r.part,
                r.raw_text,
                r.source_name or file_path.name,
            ]
            for r in parsed
        ]
        return DataTable(
            table_id=generate_table_id(),
            name=file_path.stem,
            headers=headers,
            rows=rows,
            source_path=str(file_path),
            sheet_name="",
            source_type="txt",
            metadata={
                "file_name": file_path.name,
                "parsed": True,
            },
        )

    raw_bytes = file_path.read_bytes()
    text, encoding = _decode_bytes_auto(raw_bytes)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    headers = ["原始文本", "来源"]
    rows = [[ln, file_path.name] for ln in lines]
    return DataTable(
        table_id=generate_table_id(),
        name=file_path.stem,
        headers=headers,
        rows=rows,
        source_path=str(file_path),
        sheet_name="",
        source_type="txt",
        metadata={
            "file_name": file_path.name,
            "encoding": encoding,
            "parsed": False,
        },
    )


# ---------------------------------------------------------------------------
# SQLite Database -> DataTable
# ---------------------------------------------------------------------------


def read_database_as_table(
    db_repo: Any,
    name: str = "当前数据总库",
) -> DataTable:
    """Load all records from a `DatabaseRepository` into an in-memory `DataTable`."""
    records = db_repo.query_records(sort_by="id", sort_desc=False)
    headers = [
        "记录ID",
        "标准编码",
        "前缀分类",
        "数字编号",
        "标签",
        "分集",
        "原始名称",
        "完整路径",
        "来源",
        "来源类型",
        "备注",
        "入库时间",
    ]
    rows: list[list[str]] = []
    for rec in records:
        rows.append(
            [
                str(rec.id) if rec.id is not None else "",
                str(rec.canonical_code or ""),
                str(rec.prefix or ""),
                str(rec.number or ""),
                rec.tags_str if hasattr(rec, "tags_str") else ",".join(rec.tags or []),
                str(rec.part or ""),
                str(rec.file_name or rec.raw_text or ""),
                str(rec.full_path or ""),
                str(rec.source_name or ""),
                str(rec.source_type or ""),
                str(rec.note or ""),
                str(rec.created_at or ""),
            ]
        )

    db_path = str(getattr(db_repo, "db_path", ""))
    return DataTable(
        table_id=generate_table_id(),
        name=name,
        headers=headers,
        rows=rows,
        source_path=db_path,
        sheet_name="",
        source_type="database",
        metadata={
            "db_path": db_path,
            "record_count": len(rows),
        },
    )


# ---------------------------------------------------------------------------
# Unified Dispatcher
# ---------------------------------------------------------------------------


def load_tables_from_file(
    path: Union[str, Path],
    *,
    sheet_names: Optional[Sequence[str]] = None,
    has_header: bool = True,
    include_paragraphs_as_table: bool = False,
    parser_engine: Optional[Any] = None,
) -> list[DataTable]:
    """Dispatch a file by extension and return one or more normalized `DataTable`s.

    Supported formats: `.xlsx`, `.xlsm`, `.docx`, `.csv`, `.tsv`, `.efu`, `.txt`, `.log`.
    Legacy `.xls` and `.doc` files raise a user-friendly `ValueError`.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"找不到文件：{file_path}")

    ext = file_path.suffix.lower()
    if ext in {".xlsx", ".xlsm"}:
        return read_excel_tables(
            file_path,
            sheet_names=sheet_names,
            has_header=has_header,
        )
    if ext == ".xls":
        raise ValueError(
            "暂不支持旧版 Excel (.xls) 格式，请使用 Excel 或 WPS 将文件另存为 .xlsx 格式后重试。"
        )
    if ext == ".docx":
        return read_word_tables(
            file_path,
            include_paragraphs_as_table=include_paragraphs_as_table,
            has_header=has_header,
        )
    if ext == ".doc":
        raise ValueError(
            "暂不支持旧版 Word (.doc) 格式，请使用 Word 或 WPS 将文件另存为 .docx 格式后重试。"
        )
    if ext in {".csv", ".tsv", ".efu"}:
        return [read_csv_table(file_path, has_header=has_header)]
    if ext in {".txt", ".log"}:
        return [read_txt_as_table(file_path, parser_engine=parser_engine)]

    raise ValueError(
        f"暂不支持的文件格式「{ext or '无扩展名'}」，支持 .xlsx/.xlsm/.docx/.csv/.tsv/.txt/.log。"
    )
