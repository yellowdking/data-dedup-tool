"""Multi-format document and table loader (Excel, Word, CSV/TSV, TXT, SQLite DB).

Produces normalized `DataTable` instances for Multi-Table Studio as well as
structured text extraction items for Word/Excel batch scanning.
All terminology, identifiers, and messages are strictly neutral.
"""

from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from .io_utils import (
    decode_bytes_auto as _decode_bytes_auto,
    format_cell_value as _format_cell_value,
)
from .table_data import DataTable, generate_table_id

try:
    from lxml.etree import XMLSyntaxError as _LxmlSyntaxError
except Exception:  # pragma: no cover - optional when lxml is absent
    _LxmlSyntaxError = None  # type: ignore[assignment]

_XML_PARSE_ERRORS: tuple[type[BaseException], ...] = (
    (ET.ParseError, _LxmlSyntaxError) if _LxmlSyntaxError is not None else (ET.ParseError,)
)

SUPPORTED_TABLE_EXTENSIONS: frozenset[str] = frozenset(
    {".xlsx", ".xlsm", ".docx", ".csv", ".tsv", ".efu", ".txt", ".log"}
)

# Safety limits against abnormally huge files or OOXML zip-bomb archives.
MAX_OFFICE_FILE_BYTES: int = 150 * 1024 * 1024
MAX_OFFICE_UNCOMPRESSED_BYTES: int = 500 * 1024 * 1024
MAX_TEXT_TABLE_FILE_BYTES: int = 200 * 1024 * 1024
_CSV_SAFE_FIELD_LIMIT: int = 10 * 1024 * 1024
_OLE2_MAGIC: bytes = b"\xd0\xcf\x11\xe0"


def _ensure_csv_field_size_limit() -> None:
    """Safely raise Python's csv field size limit above the 128 KB default."""
    try:
        if csv.field_size_limit() < _CSV_SAFE_FIELD_LIMIT:
            csv.field_size_limit(_CSV_SAFE_FIELD_LIMIT)
    except (OverflowError, ValueError):  # pragma: no cover - 32-bit safeguard
        pass


def _check_office_zip_safety(path: Path, label: str) -> bool:
    """Validate file size, encrypted OLE2 header, and decompressed zip-bomb limits.

    Returns ``True`` if the archive contains worksheet XML formula tags (``<f``),
    allowing Excel readers to skip uncached-formula scanning on formula-free files.
    """
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"无法读取 {label} 文件「{path.name}」：{exc}") from exc

    if file_size > MAX_OFFICE_FILE_BYTES:
        limit_mb = MAX_OFFICE_FILE_BYTES // (1024 * 1024)
        raise ValueError(
            f"{label} 文件「{path.name}」体积过大（超过 {limit_mb} MB 安全上限），已中止读取。"
        )

    try:
        with path.open("rb") as fh:
            head = fh.read(8)
    except OSError as exc:
        raise ValueError(f"无法读取 {label} 文件「{path.name}」：{exc}") from exc

    if head.startswith(_OLE2_MAGIC):
        raise ValueError(
            f"{label} 文件「{path.name}」已启用密码加密保护或为旧版二进制格式，"
            "请先解除密码并另存为标准未加密格式后重试。"
        )

    has_formula_tag = False
    try:
        with zipfile.ZipFile(path, "r") as zf:
            total_uncompressed = 0
            for info in zf.infolist():
                total_uncompressed += max(0, int(info.file_size))
                if total_uncompressed > MAX_OFFICE_UNCOMPRESSED_BYTES:
                    limit_mb = MAX_OFFICE_UNCOMPRESSED_BYTES // (1024 * 1024)
                    raise ValueError(
                        f"{label} 文件「{path.name}」解压后体积超过安全上限（{limit_mb} MB），"
                        "疑似异常膨胀或压缩炸弹，已拒绝读取。"
                    )
            if label == "Excel":
                for info in zf.infolist():
                    lower_name = info.filename.lower()
                    if lower_name.startswith("xl/worksheets/") and lower_name.endswith(".xml"):
                        # Stream-check in chunks for <f> / <f ...> formula tags.
                        with zf.open(info, "r") as sheet_fh:
                            prev = b""
                            while True:
                                chunk = sheet_fh.read(65536)
                                if not chunk:
                                    break
                                window = prev + chunk
                                if b"<f>" in window or b"<f " in window:
                                    has_formula_tag = True
                                    break
                                prev = chunk[-4:]
                        if has_formula_tag:
                            break
    except ValueError:
        raise
    except (zipfile.BadZipFile, OSError, EOFError, KeyError) as exc:
        raise ValueError(f"无法读取 {label} 文件「{path.name}」：{exc}") from exc

    return has_formula_tag


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
    header_row: int = 0,
    single_row_as_data: bool = False,
) -> tuple[list[str], list[list[str]]]:
    """Split a trimmed 2D string matrix into normalized `(headers, data_rows)`.

    ``header_row`` is a 0-based index into ``matrix`` (default ``0``, i.e. the
    first non-empty row). Rows preceding ``header_row`` (such as merged title
    banners) are skipped cleanly.
    """
    if not matrix:
        return [], []

    hdr_idx = max(0, int(header_row))
    if hdr_idx >= len(matrix):
        return [], []

    col_count = max(len(r) for r in matrix)
    if not has_header or (single_row_as_data and hdr_idx == 0 and len(matrix) == 1):
        headers = _make_unique_headers([], col_count)
        return headers, matrix[hdr_idx:]

    headers = _make_unique_headers(matrix[hdr_idx], col_count)
    return headers, matrix[hdr_idx + 1 :]


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


def _load_openpyxl_workbook(
    path: Path,
    *,
    read_only: bool = False,
    data_only: bool = True,
    check_safety: bool = True,
):
    import openpyxl
    from openpyxl.utils.exceptions import InvalidFileException

    if check_safety:
        _check_office_zip_safety(path, "Excel")

    try:
        return openpyxl.load_workbook(
            filename=str(path),
            read_only=read_only,
            data_only=data_only,
        )
    except (
        InvalidFileException,
        zipfile.BadZipFile,
        KeyError,
        OSError,
        ValueError,
        EOFError,
        *_XML_PARSE_ERRORS,
    ) as exc:
        raise ValueError(f"无法读取 Excel 文件「{path.name}」：{exc}") from exc


def _is_formula_cell_value(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith("=")
    cls_name = type(value).__name__
    return cls_name in ("ArrayFormula", "DataTableFormula")


def _count_uncached_formulas(ws_values: Any, ws_formulas: Any) -> int:
    """Count cells where a formula exists in ``ws_formulas`` but cached value is None."""
    count = 0
    for val_row, form_row in zip(
        ws_values.iter_rows(values_only=True),
        ws_formulas.iter_rows(values_only=True),
    ):
        for v_cell, f_cell in zip(val_row, form_row):
            if v_cell is None and _is_formula_cell_value(f_cell):
                count += 1
    return count


def list_excel_sheets(path: Union[str, Path]) -> list[str]:
    """Return the ordered list of worksheet names in an `.xlsx`/`.xlsm` workbook."""
    file_path = Path(path)
    _validate_excel_path(file_path)
    wb = _load_openpyxl_workbook(file_path, read_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def _extract_sheet_matrix(ws: Any) -> list[list[str]]:
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
    header_row: int = 0,
) -> list[DataTable]:
    """Load one or more worksheets from an `.xlsx`/`.xlsm` workbook into `DataTable`s."""
    file_path = Path(path)
    _validate_excel_path(file_path)
    has_formulas = _check_office_zip_safety(file_path, "Excel")

    wb = _load_openpyxl_workbook(
        file_path, read_only=False, data_only=True, check_safety=False
    )
    wb_formulas = (
        _load_openpyxl_workbook(
            file_path, read_only=True, data_only=False, check_safety=False
        )
        if has_formulas
        else None
    )
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
            uncached_count = 0
            if wb_formulas is not None and sheet_name in wb_formulas.sheetnames:
                uncached_count = _count_uncached_formulas(ws, wb_formulas[sheet_name])

            raw_matrix = _extract_sheet_matrix(ws)
            trimmed = _trim_matrix(raw_matrix)
            if not trimmed:
                continue

            headers, rows = _matrix_to_headers_and_rows(
                trimmed,
                has_header=has_header,
                header_row=header_row,
                single_row_as_data=False,
            )
            if not headers and not rows:
                continue

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
                        "header_row": max(0, int(header_row)),
                        "uncached_formula_count": uncached_count,
                    },
                )
            )
    except ValueError:
        raise
    except (zipfile.BadZipFile, KeyError, OSError, EOFError, *_XML_PARSE_ERRORS) as exc:
        raise ValueError(f"无法读取 Excel 文件「{file_path.name}」：{exc}") from exc
    finally:
        wb.close()
        if wb_formulas is not None:
            wb_formulas.close()

    return tables


def extract_excel_text_items(
    path: Union[str, Path],
    sheet_names: Optional[Sequence[str]] = None,
) -> list[dict[str, Any]]:
    """Extract non-empty rows/cells from an Excel workbook with `工作表名 行R` locators.

    Uses ``read_only=True`` streaming mode for low memory footprint on large
    workbooks. Multi-line cells in single-cell rows are emitted line-by-line;
    multi-column rows are joined cleanly with ``" | "`` while keeping raw cell
    values in ``cells``.
    """
    file_path = Path(path)
    _validate_excel_path(file_path)

    wb = _load_openpyxl_workbook(file_path, read_only=True, data_only=True)
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
    except ValueError:
        raise
    except (zipfile.BadZipFile, KeyError, OSError, EOFError, *_XML_PARSE_ERRORS) as exc:
        raise ValueError(f"无法读取 Excel 文件「{file_path.name}」：{exc}") from exc
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


def _load_docx_document(path: Path) -> Any:
    import docx
    from docx.opc.exceptions import PackageNotFoundError

    _check_office_zip_safety(path, "Word")
    try:
        return docx.Document(str(path))
    except (
        PackageNotFoundError,
        zipfile.BadZipFile,
        KeyError,
        OSError,
        ValueError,
        EOFError,
        *_XML_PARSE_ERRORS,
    ) as exc:
        raise ValueError(f"无法读取 Word 文件「{path.name}」：{exc}") from exc


def _clean_word_text(text: str) -> str:
    """Normalize line breaks and outer whitespace in Word paragraph/cell text."""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _extract_word_table_matrix(table: Any) -> list[list[str]]:
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


def _iter_word_tables(
    word_table: Any,
    label: str,
    top_index: int,
    *,
    include_nested: bool = False,
) -> list[tuple[int, str, Any]]:
    """Collect a Word table and optionally its nested sub-tables in document order."""
    collected: list[tuple[int, str, Any]] = [(top_index, label, word_table)]
    if not include_nested:
        return collected
    seen_tcs: set[Any] = set()
    nested_counter = 0
    for row in word_table.rows:
        for cell in row.cells:
            tc = cell._tc
            if tc in seen_tcs:
                continue
            seen_tcs.add(tc)
            for nested_tbl in cell.tables:
                nested_counter += 1
                collected.extend(
                    _iter_word_tables(
                        nested_tbl,
                        f"{label}.{nested_counter}",
                        top_index,
                        include_nested=True,
                    )
                )
    return collected


def list_word_tables_info(
    path: Union[str, Path],
    *,
    include_nested_tables: bool = False,
) -> list[dict[str, Any]]:
    """Return lightweight metadata for embedded tables in a ``.docx`` document."""
    file_path = Path(path)
    _validate_word_path(file_path)
    doc = _load_docx_document(file_path)
    infos: list[dict[str, Any]] = []
    for tbl_idx, word_table in enumerate(doc.tables, start=1):
        for top_idx, label, tbl_obj in _iter_word_tables(
            word_table, f"表格{tbl_idx}", tbl_idx, include_nested=include_nested_tables
        ):
            trimmed = _trim_matrix(_extract_word_table_matrix(tbl_obj))
            if not trimmed:
                continue
            preview = " | ".join(c for c in trimmed[0][:4] if c)[:80]
            infos.append(
                {
                    "index": top_idx,
                    "label": label,
                    "rows": len(trimmed),
                    "cols": max(len(r) for r in trimmed),
                    "preview": preview,
                }
            )
    return infos


def read_word_tables(
    path: Union[str, Path],
    include_paragraphs_as_table: bool = False,
    has_header: bool = True,
    header_row: int = 0,
    include_nested_tables: bool = False,
    table_indices: Optional[Sequence[int]] = None,
) -> list[DataTable]:
    """Extract embedded tables (and optionally nested tables / body paragraphs) from `.docx`."""
    file_path = Path(path)
    _validate_word_path(file_path)

    doc = _load_docx_document(file_path)
    tables: list[DataTable] = []
    allowed_indices: Optional[set[int]] = (
        {int(i) for i in table_indices} if table_indices is not None else None
    )

    try:
        for tbl_idx, word_table in enumerate(doc.tables, start=1):
            if allowed_indices is not None and tbl_idx not in allowed_indices:
                continue
            for top_idx, sheet_label, tbl_obj in _iter_word_tables(
                word_table,
                f"表格{tbl_idx}",
                tbl_idx,
                include_nested=include_nested_tables,
            ):
                raw_matrix = _extract_word_table_matrix(tbl_obj)
                trimmed = _trim_matrix(raw_matrix)
                if not trimmed:
                    continue

                headers, rows = _matrix_to_headers_and_rows(
                    trimmed,
                    has_header=has_header,
                    header_row=header_row,
                    single_row_as_data=True,
                )
                if not headers and not rows:
                    continue
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
                            "table_index": top_idx,
                            "table_label": sheet_label,
                            "has_header": has_header,
                            "header_row": max(0, int(header_row)),
                            "nested": "." in sheet_label,
                        },
                    )
                )

        if include_paragraphs_as_table or (not tables and allowed_indices is None):
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
    except ValueError:
        raise
    except (zipfile.BadZipFile, KeyError, OSError, EOFError, *_XML_PARSE_ERRORS) as exc:
        raise ValueError(f"无法读取 Word 文件「{file_path.name}」：{exc}") from exc

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
    header_row: int = 0,
) -> DataTable:
    """Read a CSV/TSV/EFU file with automatic encoding and delimiter detection."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"找不到 CSV 文件：{file_path}")

    file_size = file_path.stat().st_size
    if file_size > MAX_TEXT_TABLE_FILE_BYTES:
        limit_mb = MAX_TEXT_TABLE_FILE_BYTES // (1024 * 1024)
        raise ValueError(
            f"CSV 文件「{file_path.name}」体积过大（超过 {limit_mb} MB 安全上限），已中止读取。"
        )

    raw_bytes = file_path.read_bytes()
    text, encoding = _decode_bytes_auto(raw_bytes)
    if "\x00" in text:
        text = text.replace("\x00", "")

    default_delim = "\t" if file_path.suffix.lower() == ".tsv" else ","
    detected_delim = delimiter or _sniff_delimiter(text, default=default_delim)

    _ensure_csv_field_size_limit()
    try:
        reader = csv.reader(io.StringIO(text), delimiter=detected_delim)
        raw_matrix = [[_format_cell_value(cell) for cell in row] for row in reader]
    except csv.Error as exc:
        raise ValueError(f"无法解析 CSV 文件「{file_path.name}」：{exc}") from exc

    trimmed = _trim_matrix(raw_matrix)

    headers, rows = _matrix_to_headers_and_rows(
        trimmed,
        has_header=has_header,
        header_row=header_row,
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
            "header_row": max(0, int(header_row)),
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

    file_size = file_path.stat().st_size
    if file_size > MAX_TEXT_TABLE_FILE_BYTES:
        limit_mb = MAX_TEXT_TABLE_FILE_BYTES // (1024 * 1024)
        raise ValueError(
            f"文本文件「{file_path.name}」体积过大（超过 {limit_mb} MB 安全上限），已中止读取。"
        )

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
    header_row: int = 0,
    include_paragraphs_as_table: bool = False,
    include_nested_tables: bool = False,
    table_indices: Optional[Sequence[int]] = None,
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
            header_row=header_row,
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
            header_row=header_row,
            include_nested_tables=include_nested_tables,
            table_indices=table_indices,
        )
    if ext == ".doc":
        raise ValueError(
            "暂不支持旧版 Word (.doc) 格式，请使用 Word 或 WPS 将文件另存为 .docx 格式后重试。"
        )
    if ext in {".csv", ".tsv", ".efu"}:
        return [
            read_csv_table(
                file_path,
                has_header=has_header,
                header_row=header_row,
            )
        ]
    if ext in {".txt", ".log"}:
        return [read_txt_as_table(file_path, parser_engine=parser_engine)]

    raise ValueError(
        f"暂不支持的文件格式「{ext or '无扩展名'}」，支持 .xlsx/.xlsm/.docx/.csv/.tsv/.txt/.log。"
    )
