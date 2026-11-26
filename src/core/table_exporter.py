"""Multi-format table and deduplication report exporter for 数据查重程序.

Exports ``DataTable`` and ``TableDedupReport`` objects to:
- Styled Excel ``.xlsx`` (single table, multi-table workbook, and rich multi-sheet
  deduplication reports)
- UTF-8-SIG ``.csv`` (formula-injection safe)
- High-speed formatted Word ``.docx`` documents (via ``python-docx`` + direct
  OOXML/lxml batch row construction)
- Plain UTF-8 ``.txt`` (newline/NUL safe)

Sanitizes illegal XML 1.0 control characters for both ``openpyxl`` and
``python-docx``, sanitizes Excel sheet names (<= 31 chars, rejects reserved
``History``, strips edge apostrophes after truncation, unique), performs early
lock & directory checks, and writes atomically with guaranteed temp-file cleanup.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape as _xml_escape

from .exporter import (
    ExportError,
    _atomic_write_file,
    _check_cancelled,
    _check_target_writable,
    _require_openpyxl,
    _save_workbook_safely,
    _write_csv_safely,
    _write_styled_sheet,
    _write_text_safely,
    sanitize_xml_text,
)
from .table_data import DataTable, TableDedupReport, TableRowRef

try:
    from openpyxl import Workbook
except ImportError:  # pragma: no cover - surfaced with friendly ExportError
    Workbook = None  # type: ignore[assignment]

try:
    import docx
    from docx.enum.style import WD_STYLE_TYPE
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml import OxmlElement, parse_xml
    from docx.oxml.ns import nsdecls, qn
    from docx.oxml.parser import oxml_parser
    from docx.shared import Pt, RGBColor
    from lxml import etree as _lxml_etree

    _HAS_DOCX = True
except ImportError:  # pragma: no cover - surfaced with friendly ExportError
    _HAS_DOCX = False


_INVALID_SHEET_CHARS_RE = re.compile(r"[\[\]:*?/\\]")
_RESERVED_SHEET_NAMES_LOWER = frozenset({"history"})

_WORD_HEADER_FILL = "1F4E78"
_WORD_ZEBRA_FILL = "F4F7FA"
_WORD_BORDER_COLOR = "CBD5E1"
_WORD_TABLE_BODY_STYLE = "DataTableBody"
_WORD_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WORD_CHUNK_ROWS = 250


def _trim_sheet_edges(text: str) -> str:
    """Repeatedly strip leading/trailing whitespace and apostrophes."""
    prev: Optional[str] = None
    cur = text
    while cur != prev:
        prev = cur
        cur = cur.strip().strip("'")
    return cur


def sanitize_sheet_name(name: str, used_names_lower: Optional[set[str]] = None) -> str:
    """Produce a valid, unique Excel worksheet title (<= 31 characters).

    - Replaces invalid OOXML characters ``[ ] : * ? / \\`` with ``_``.
    - Strips leading/trailing whitespace and apostrophes *after* 31-char truncation.
    - Rejects Excel's reserved ``History`` worksheet name (case-insensitive).
    - Guarantees uniqueness against ``used_names_lower`` while keeping ``<= 31`` chars.
    """
    cleaned = _INVALID_SHEET_CHARS_RE.sub("_", sanitize_xml_text(name))
    cleaned = _trim_sheet_edges(cleaned)
    base = _trim_sheet_edges(cleaned[:31])
    if not base:
        base = "工作表"

    if base.lower() in _RESERVED_SHEET_NAMES_LOWER:
        base = f"{base[:29]}_1"

    if used_names_lower is None:
        return base

    candidate = base
    suffix_num = 2
    while (
        candidate.lower() in used_names_lower
        or candidate.lower() in _RESERVED_SHEET_NAMES_LOWER
    ):
        suffix = f"_{suffix_num}"
        stem = _trim_sheet_edges(base[: 31 - len(suffix)])
        if not stem:
            stem = "工作表"[: 31 - len(suffix)]
        candidate = f"{stem}{suffix}"
        suffix_num += 1
    used_names_lower.add(candidate.lower())
    return candidate


def _require_docx() -> None:
    if not _HAS_DOCX:
        raise ExportError("缺少 python-docx 依赖，无法导出 Word (.docx) 文档。")


def _save_docx_safely(document: Any, output_path: Path) -> Path:
    """Save a python-docx Document atomically with locked-file protection."""
    return _atomic_write_file(
        output_path,
        lambda tmp_path: document.save(str(tmp_path)),
        tmp_suffix=".tmp.docx",
    )


def _extract_table_grid(table: DataTable) -> Tuple[List[str], List[List[object]]]:
    """Normalize a ``DataTable`` into ``(headers, rows)`` without dropping cells on empty headers."""
    headers = [sanitize_xml_text(h) for h in table.headers]
    if headers:
        col_n = len(headers)
        rows: List[List[object]] = [
            [row[i] if i < len(row) else "" for i in range(col_n)]
            for row in table.rows
        ]
    else:
        max_cols = max((len(r) for r in table.rows), default=0)
        rows = [
            [row[i] if i < len(row) else "" for i in range(max_cols)]
            for row in table.rows
        ]
    return headers, rows


def _format_row_summary(ref: TableRowRef, max_chars: int = 300) -> str:
    """Format a TableRowRef's cells into a clean, readable summary string."""
    parts: List[str] = []
    for idx, val in enumerate(ref.row_data):
        text = sanitize_xml_text(val).strip()
        if not text:
            continue
        header = (
            sanitize_xml_text(ref.headers[idx]).strip()
            if idx < len(ref.headers)
            else f"列{idx + 1}"
        )
        parts.append(f"{header}: {text}" if header else text)
    summary = " | ".join(parts)
    if len(summary) > max_chars:
        summary = summary[: max_chars - 3] + "..."
    return summary


# ----------------------------------------------------------------------
# Word styling helpers (high-speed direct OOXML construction)
# ----------------------------------------------------------------------


def _set_docx_table_borders_and_margins(
    table: Any,
    color_hex: str = _WORD_BORDER_COLOR,
) -> None:
    tbl_pr = table._tbl.tblPr
    borders = parse_xml(
        f'<w:tblBorders {nsdecls("w")}>'
        f'<w:top w:val="single" w:sz="4" w:space="0" w:color="{color_hex}"/>'
        f'<w:bottom w:val="single" w:sz="4" w:space="0" w:color="{color_hex}"/>'
        f'<w:left w:val="single" w:sz="4" w:space="0" w:color="{color_hex}"/>'
        f'<w:right w:val="single" w:sz="4" w:space="0" w:color="{color_hex}"/>'
        f'<w:insideH w:val="single" w:sz="4" w:space="0" w:color="{color_hex}"/>'
        f'<w:insideV w:val="single" w:sz="4" w:space="0" w:color="{color_hex}"/>'
        f"</w:tblBorders>"
    )
    tbl_pr.append(borders)
    margins = parse_xml(
        f'<w:tblCellMar {nsdecls("w")}>'
        f'<w:top w:w="40" w:type="dxa"/>'
        f'<w:bottom w:w="40" w:type="dxa"/>'
        f'<w:left w:w="80" w:type="dxa"/>'
        f'<w:right w:w="80" w:type="dxa"/>'
        f"</w:tblCellMar>"
    )
    tbl_pr.append(margins)


def _ensure_docx_table_body_style(doc: Any) -> str:
    """Create a compact shared paragraph style for table body cells once per document."""
    try:
        style = doc.styles[_WORD_TABLE_BODY_STYLE]
        return style.style_id
    except KeyError:
        pass

    style = doc.styles.add_style(_WORD_TABLE_BODY_STYLE, WD_STYLE_TYPE.PARAGRAPH)
    style.base_style = doc.styles["Normal"]
    style.font.name = "Microsoft YaHei"
    style.font.size = Pt(9)
    style.font.color.rgb = RGBColor(0x1E, 0x29, 0x3B)
    style.paragraph_format.space_before = Pt(2)
    style.paragraph_format.space_after = Pt(2)
    rpr = style._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    rfonts.set(qn("w:ascii"), "Microsoft YaHei")
    rfonts.set(qn("w:hAnsi"), "Microsoft YaHei")
    return style.style_id


def _configure_docx_base_styles(doc: Any) -> None:
    """Configure neutral Microsoft YaHei default font for CJK readability."""
    normal = doc.styles["Normal"]
    normal.font.name = "Microsoft YaHei"
    normal.font.size = Pt(10)
    normal.font.color.rgb = RGBColor(0x1E, 0x29, 0x3B)
    rpr = normal._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    rfonts.set(qn("w:ascii"), "Microsoft YaHei")
    rfonts.set(qn("w:hAnsi"), "Microsoft YaHei")
    _ensure_docx_table_body_style(doc)


def _xml_text_runs(text: str) -> str:
    """Encode XML-safe text into OOXML ``<w:t>`` elements (preserving line breaks)."""
    if not text:
        return ""
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\n" not in text:
        return f'<w:t xml:space="preserve">{_xml_escape(text)}</w:t>'
    parts = [_xml_escape(part) for part in text.split("\n")]
    return '<w:t xml:space="preserve">' + '</w:t><w:br/><w:t xml:space="preserve">'.join(parts) + "</w:t>"


def _append_ooxml_rows(tbl_elem: Any, rows_xml: List[str]) -> None:
    """Parse a batch of ``<w:tr>`` XML strings via ``oxml_parser`` and append to ``<w:tbl>``."""
    if not rows_xml:
        return
    wrapped = (
        f'<w:tbl xmlns:w="{_WORD_W_NS}">'
        + "".join(rows_xml)
        + "</w:tbl>"
    )
    parsed_tbl = _lxml_etree.fromstring(wrapped.encode("utf-8"), parser=oxml_parser)
    for tr in list(parsed_tbl):
        tbl_elem.append(tr)


def _add_styled_docx_table(
    doc: Any,
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> None:
    """Render a styled Word table using high-speed batch OOXML/lxml construction.

    Preserves shaded repeating headers, zebra striping, borders, vertical
    centering, and clean Microsoft YaHei CJK typography while running 20x+ faster
    than per-cell ``python-docx`` API loops.
    """
    max_row_cols = max((len(r) for r in rows), default=0)
    col_count = len(headers) if headers else max_row_cols
    if col_count == 0:
        doc.add_paragraph("（无列数据）")
        return

    body_style_id = _ensure_docx_table_body_style(doc)

    tbl = doc.add_table(rows=0, cols=col_count)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    _set_docx_table_borders_and_margins(tbl)
    tbl_elem = tbl._tbl

    # Determine per-column width (dxa) from python-docx's generated tblGrid.
    grid_cols = tbl_elem.tblGrid.gridCol_lst if tbl_elem.tblGrid is not None else []
    if len(grid_cols) == col_count and grid_cols[0].w is not None:
        col_widths_dxa = [int(gc.w.twips) for gc in grid_cols]
    else:
        default_w = max(720, 9000 // col_count)
        col_widths_dxa = [default_w] * col_count

    # Pre-build per-column <w:tcPr> fragments so inner cell loops do minimal string work.
    hdr_tc_pr = [
        f'<w:tcPr><w:tcW w:w="{w}" w:type="dxa"/>'
        f'<w:shd w:val="clear" w:color="auto" w:fill="{_WORD_HEADER_FILL}"/>'
        f'<w:vAlign w:val="center"/></w:tcPr>'
        for w in col_widths_dxa
    ]
    normal_tc_pr = [
        f'<w:tcPr><w:tcW w:w="{w}" w:type="dxa"/><w:vAlign w:val="center"/></w:tcPr>'
        for w in col_widths_dxa
    ]
    zebra_tc_pr = [
        f'<w:tcPr><w:tcW w:w="{w}" w:type="dxa"/>'
        f'<w:shd w:val="clear" w:color="auto" w:fill="{_WORD_ZEBRA_FILL}"/>'
        f'<w:vAlign w:val="center"/></w:tcPr>'
        for w in col_widths_dxa
    ]

    hdr_p_open = (
        '<w:p><w:pPr><w:spacing w:before="60" w:after="60"/></w:pPr>'
        '<w:r><w:rPr>'
        '<w:rFonts w:ascii="Microsoft YaHei" w:hAnsi="Microsoft YaHei" w:eastAsia="Microsoft YaHei"/>'
        '<w:b/><w:color w:val="FFFFFF"/><w:sz w:val="19"/><w:szCs w:val="19"/>'
        "</w:rPr>"
    )
    hdr_p_close = "</w:r></w:p></w:tc>"
    hdr_p_empty = '<w:p><w:pPr><w:spacing w:before="60" w:after="60"/></w:pPr></w:p></w:tc>'

    body_p_prefix = f'<w:p><w:pPr><w:pStyle w:val="{body_style_id}"/></w:pPr>'
    body_p_empty = f"{body_p_prefix}</w:p></w:tc>"
    body_p_run_open = f"{body_p_prefix}<w:r>"
    body_p_run_close = "</w:r></w:p></w:tc>"

    chunk_xml: List[str] = []

    if headers:
        hdr_cells: List[str] = ['<w:tr><w:trPr><w:tblHeader/></w:trPr>']
        for col_idx, h_val in enumerate(headers):
            text = sanitize_xml_text(h_val)
            if text:
                hdr_cells.append(f"<w:tc>{hdr_tc_pr[col_idx]}{hdr_p_open}{_xml_text_runs(text)}{hdr_p_close}")
            else:
                hdr_cells.append(f"<w:tc>{hdr_tc_pr[col_idx]}{hdr_p_empty}")
        hdr_cells.append("</w:tr>")
        chunk_xml.append("".join(hdr_cells))

    total_rows = len(rows)
    for row_idx, row_vals in enumerate(rows):
        if row_idx % _WORD_CHUNK_ROWS == 0:
            _check_cancelled(cancel_check)
            if chunk_xml:
                _append_ooxml_rows(tbl_elem, chunk_xml)
                chunk_xml.clear()
            if progress_callback and total_rows > 0:
                progress_callback(row_idx, total_rows)

        tc_pr_list = zebra_tc_pr if (row_idx & 1) else normal_tc_pr
        row_len = len(row_vals)
        row_parts: List[str] = ["<w:tr>"]
        for col_idx in range(col_count):
            raw_val = row_vals[col_idx] if col_idx < row_len else ""
            text = sanitize_xml_text(raw_val)
            if text:
                row_parts.append(
                    f"<w:tc>{tc_pr_list[col_idx]}{body_p_run_open}{_xml_text_runs(text)}{body_p_run_close}"
                )
            else:
                row_parts.append(f"<w:tc>{tc_pr_list[col_idx]}{body_p_empty}")
        row_parts.append("</w:tr>")
        chunk_xml.append("".join(row_parts))

    _check_cancelled(cancel_check)
    if chunk_xml:
        _append_ooxml_rows(tbl_elem, chunk_xml)
        chunk_xml.clear()
    if progress_callback and total_rows > 0:
        progress_callback(total_rows, total_rows)


# ----------------------------------------------------------------------
# Public single/multi-table exporters
# ----------------------------------------------------------------------


def _resolve_highlight_indices(
    table: DataTable,
    highlight_duplicates_col: Optional[str | int],
) -> Optional[set[int]]:
    if highlight_duplicates_col is None or highlight_duplicates_col == "":
        return None
    if isinstance(highlight_duplicates_col, int):
        col_idx = highlight_duplicates_col
    else:
        col_idx = table.get_column_index(str(highlight_duplicates_col))
    effective_cols = max(
        len(table.headers),
        max((len(r) for r in table.rows), default=0),
    )
    if col_idx < 0 or col_idx >= effective_cols:
        return None

    counts: Dict[str, int] = {}
    norm_vals: List[str] = []
    for row in table.rows:
        val = str(row[col_idx]).strip().lower() if col_idx < len(row) else ""
        norm_vals.append(val)
        if val:
            counts[val] = counts.get(val, 0) + 1

    dup_rows = {i for i, v in enumerate(norm_vals) if v and counts.get(v, 0) > 1}
    return dup_rows or None


def export_table_excel(
    table: DataTable,
    output_path: str | Path,
    highlight_duplicates_col: Optional[str | int] = None,
    *,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Path:
    """Export a single ``DataTable`` to a styled ``.xlsx`` workbook."""
    _require_openpyxl()
    target = Path(output_path)
    _check_target_writable(target)

    wb = Workbook()
    ws = wb.active
    ws.title = sanitize_sheet_name(table.name or "数据表")

    headers, rows = _extract_table_grid(table)
    dup_indices = _resolve_highlight_indices(table, highlight_duplicates_col)
    _write_styled_sheet(
        ws,
        headers,
        rows,
        highlight_row_indices=dup_indices,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )
    return _save_workbook_safely(wb, target)


def export_table_csv(
    table: DataTable,
    output_path: str | Path,
) -> Path:
    """Export a ``DataTable`` to UTF-8-SIG ``.csv`` (Excel-friendly & formula-safe)."""
    target = Path(output_path)
    _check_target_writable(target)
    headers, rows = _extract_table_grid(table)
    return _write_csv_safely(target, headers, rows)


def export_table_word(
    table: DataTable,
    output_path: str | Path,
    title: str = "导出数据表",
    *,
    max_rows: int = 3000,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Path:
    """Export a ``DataTable`` into a formatted Word (``.docx``) document.

    Gracefully caps very large tables at ``max_rows`` with an explanatory note so
    Word rendering remains fast and responsive.
    """
    _require_docx()
    target = Path(output_path)
    _check_target_writable(target)
    _check_cancelled(cancel_check)

    doc = docx.Document()
    _configure_docx_base_styles(doc)

    doc_title = sanitize_xml_text(title or table.name or "导出数据表")
    doc.add_heading(doc_title, level=1)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    display_cols = max(
        table.col_count,
        max((len(r) for r in table.rows), default=0),
    )
    meta_p = doc.add_paragraph()
    meta_run = meta_p.add_run(
        f"数据表：{sanitize_xml_text(table.name or '未命名表')}  |  "
        f"行数：{table.row_count}  |  列数：{display_cols}  |  导出时间：{now_str}"
    )
    meta_run.font.size = Pt(9)
    meta_run.font.color.rgb = RGBColor(0x64, 0x74, 0x8B)

    effective_limit = max(1, int(max_rows))
    rows_to_write = table.rows[:effective_limit]
    if table.row_count > effective_limit:
        warn_p = doc.add_paragraph()
        warn_run = warn_p.add_run(
            f"提示：当前数据共 {table.row_count} 行，为保障 Word 文档打开流畅度，"
            f"本文档仅展示前 {effective_limit} 行。如需完整数据，建议导出为 Excel (.xlsx) 或 CSV 格式。"
        )
        warn_run.bold = True
        warn_run.font.size = Pt(9.5)
        warn_run.font.color.rgb = RGBColor(0xB4, 0x53, 0x09)

    _add_styled_docx_table(
        doc,
        table.headers,
        rows_to_write,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )
    return _save_docx_safely(doc, target)


def _clean_txt_cell(value: object, *, strip_tabs: bool) -> str:
    text = sanitize_xml_text(value)
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    if strip_tabs:
        text = text.replace("\t", " ")
    return text


def export_table_txt(
    table: DataTable,
    output_path: str | Path,
    column_index: Optional[int] = None,
) -> Path:
    """Export a ``DataTable`` (full TSV or a single column) to UTF-8 ``.txt``."""
    effective_cols = max(
        len(table.headers),
        max((len(r) for r in table.rows), default=0),
    )
    if column_index is not None:
        if column_index < 0 or column_index >= effective_cols:
            raise ValueError(f"列索引超出范围：{column_index}")

    target = Path(output_path)
    _check_target_writable(target)
    lines: List[str] = []

    if column_index is not None:
        for row in table.rows:
            if column_index < len(row):
                val = _clean_txt_cell(row[column_index], strip_tabs=False).strip()
                if val:
                    lines.append(val)
    else:
        col_n = len(table.headers)
        if table.headers:
            lines.append("\t".join(_clean_txt_cell(h, strip_tabs=True) for h in table.headers))
        for row in table.rows:
            if col_n:
                cells = [
                    _clean_txt_cell(row[i] if i < len(row) else "", strip_tabs=True)
                    for i in range(col_n)
                ]
            else:
                cells = [_clean_txt_cell(v, strip_tabs=True) for v in row]
            lines.append("\t".join(cells))

    content = "\n".join(lines) + ("\n" if lines else "")
    return _write_text_safely(target, content, encoding="utf-8")


def export_multiple_tables_workbook(
    tables: Sequence[DataTable],
    output_path: str | Path,
    *,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Path:
    """Package multiple ``DataTable`` instances into one multi-sheet ``.xlsx`` file."""
    _require_openpyxl()
    target = Path(output_path)
    _check_target_writable(target)
    _check_cancelled(cancel_check)

    wb = Workbook()

    if not tables:
        ws = wb.active
        ws.title = "空数据"
        _write_styled_sheet(ws, ["说明"], [["暂无可导出的表格数据"]])
        return _save_workbook_safely(wb, target)

    used_names: set[str] = set()
    total_tables = len(tables)
    for idx, tbl in enumerate(tables):
        _check_cancelled(cancel_check)
        if progress_callback:
            progress_callback(idx, total_tables)
        ws = wb.active if idx == 0 else wb.create_sheet()
        ws.title = sanitize_sheet_name(tbl.name or f"工作表{idx + 1}", used_names)
        headers, rows = _extract_table_grid(tbl)
        _write_styled_sheet(ws, headers, rows, cancel_check=cancel_check)

    if progress_callback:
        progress_callback(total_tables, total_tables)

    return _save_workbook_safely(wb, target)


# ----------------------------------------------------------------------
# Multi-table deduplication report exporters (Excel & Word)
# ----------------------------------------------------------------------


def _build_report_overview_rows(report: TableDedupReport) -> Tuple[List[List[object]], List[List[object]]]:
    """Build global summary rows and per-table stat rows for Excel/Word reports."""
    mode_label = (
        "智能编码查重" if report.config.mode == "smart_code" else "通用列值查重"
    )
    summary_rows: List[List[object]] = [
        ["查重模式", mode_label],
        ["参与表格数", len(report.table_names)],
        ["参与表格列表", "、".join(report.table_names)],
        ["数据总行数", report.total_rows],
        ["唯一键总数", report.unique_key_count],
        ["跨表重复分组数", len(report.cross_table_groups)],
        ["表内重复分组数", len(report.intra_table_groups)],
        ["全表共有交集键数", len(report.intersection_keys)],
        ["未识别/空键行数", len(report.unrecognized_rows)],
        ["生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
    ]

    per_table_rows: List[List[object]] = []
    for stat in report.table_stats.values():
        per_table_rows.append(
            [
                stat.get("table_name", ""),
                stat.get("source_type", ""),
                stat.get("total_rows", 0),
                stat.get("unique_keys", 0),
                stat.get("cross_dup_rows", 0),
                stat.get("intra_dup_rows", 0),
                stat.get("exclusive_rows", 0),
                stat.get("unrecognized_rows", 0),
            ]
        )
    return summary_rows, per_table_rows


def export_dedup_report_excel(
    report: TableDedupReport,
    output_path: str | Path,
    merged_table: Optional[DataTable] = None,
    *,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Path:
    """Export a ``TableDedupReport`` into a styled multi-sheet ``.xlsx`` workbook.

    Worksheets:
    - ``查重总览`` (global summary + per-table breakdown)
    - ``跨表重复明细``
    - ``表内重复明细``
    - ``全表共有交集``
    - ``各表独有差集``
    - ``未识别行`` (included when unrecognized rows exist)
    - ``合并去重结果`` (included when ``merged_table`` is provided)
    """
    _require_openpyxl()
    target = Path(output_path)
    _check_target_writable(target)
    _check_cancelled(cancel_check)

    total_steps = 5 + int(bool(report.unrecognized_rows)) + int(merged_table is not None)
    step = 0

    def _advance() -> None:
        nonlocal step
        step += 1
        _check_cancelled(cancel_check)
        if progress_callback:
            progress_callback(step, total_steps)

    wb = Workbook()
    used_names: set[str] = set()

    # 1. Overview sheet
    ws_overview = wb.active
    ws_overview.title = sanitize_sheet_name("查重总览", used_names)
    summary_rows, per_table_rows = _build_report_overview_rows(report)
    overview_combined: List[List[object]] = []
    for k, v in summary_rows:
        overview_combined.append([k, v, "", "", "", "", "", ""])
    overview_combined.append(["", "", "", "", "", "", "", ""])
    overview_combined.append(
        [
            "表格名称",
            "来源类型",
            "总行数",
            "唯一键数",
            "跨表重复行数",
            "表内重复行数",
            "本表独有行数",
            "未识别行数",
        ]
    )
    overview_combined.extend(per_table_rows)
    _write_styled_sheet(
        ws_overview,
        ["指标 / 表格名称", "数值 / 来源类型", "总行数", "唯一键数", "跨表重复行数", "表内重复行数", "本表独有行数", "未识别行数"],
        overview_combined,
        cancel_check=cancel_check,
    )
    _advance()

    # 2. Cross-table duplicate details
    ws_cross = wb.create_sheet(sanitize_sheet_name("跨表重复明细", used_names))
    cross_headers = [
        "重复键",
        "涉及表数",
        "涉及总行数",
        "涉及表名",
        "当前来源表",
        "原行号",
        "标准编码",
        "标签",
        "分集",
        "行内容明细",
    ]
    cross_rows: List[List[object]] = []
    cross_highlight: set[int] = set()
    for g_idx, grp in enumerate(report.cross_table_groups):
        tnames_str = " / ".join(grp.table_names)
        for ref in grp.rows:
            if g_idx % 2 == 1:
                cross_highlight.add(len(cross_rows))
            cross_rows.append(
                [
                    grp.key_value,
                    grp.table_count,
                    grp.total_rows,
                    tnames_str,
                    ref.table_name,
                    ref.row_index,
                    ref.parsed_code,
                    ref.tags,
                    ref.part,
                    _format_row_summary(ref),
                ]
            )
    _write_styled_sheet(
        ws_cross,
        cross_headers,
        cross_rows,
        highlight_row_indices=cross_highlight or None,
        cancel_check=cancel_check,
    )
    _advance()

    # 3. Intra-table duplicate details
    ws_intra = wb.create_sheet(sanitize_sheet_name("表内重复明细", used_names))
    intra_headers = [
        "重复键",
        "所属表格",
        "表内重复行数",
        "原行号",
        "标准编码",
        "标签",
        "分集",
        "行内容明细",
    ]
    intra_rows: List[List[object]] = []
    for grp in report.intra_table_groups:
        tname = grp.table_names[0] if grp.table_names else ""
        for ref in grp.rows:
            intra_rows.append(
                [
                    grp.key_value,
                    tname,
                    grp.total_rows,
                    ref.row_index,
                    ref.parsed_code,
                    ref.tags,
                    ref.part,
                    _format_row_summary(ref),
                ]
            )
    _write_styled_sheet(ws_intra, intra_headers, intra_rows, cancel_check=cancel_check)
    _advance()

    # 4. Full intersection
    ws_inter = wb.create_sheet(sanitize_sheet_name("全表共有交集", used_names))
    inter_headers = ["共有键", "来源表", "原行号", "标准编码", "标签", "分集", "行内容明细"]
    inter_rows = [
        [
            ref.key_value,
            ref.table_name,
            ref.row_index,
            ref.parsed_code,
            ref.tags,
            ref.part,
            _format_row_summary(ref),
        ]
        for ref in report.intersection_rows
    ]
    _write_styled_sheet(ws_inter, inter_headers, inter_rows, cancel_check=cancel_check)
    _advance()

    # 5. Per-table exclusive rows
    ws_unique = wb.create_sheet(sanitize_sheet_name("各表独有差集", used_names))
    unique_headers = ["所属表", "独有键", "原行号", "标准编码", "标签", "分集", "行内容明细"]
    unique_rows: List[List[object]] = []
    for refs in report.unique_by_table.values():
        for ref in refs:
            unique_rows.append(
                [
                    ref.table_name,
                    ref.key_value,
                    ref.row_index,
                    ref.parsed_code,
                    ref.tags,
                    ref.part,
                    _format_row_summary(ref),
                ]
            )
    _write_styled_sheet(ws_unique, unique_headers, unique_rows, cancel_check=cancel_check)
    _advance()

    # 6. Optional unrecognized rows sheet
    if report.unrecognized_rows:
        ws_unrec = wb.create_sheet(sanitize_sheet_name("未识别行", used_names))
        unrec_rows = [
            [ref.table_name, ref.row_index, _format_row_summary(ref)]
            for ref in report.unrecognized_rows
        ]
        _write_styled_sheet(
            ws_unrec,
            ["来源表", "原行号", "行内容明细"],
            unrec_rows,
            cancel_check=cancel_check,
        )
        _advance()

    # 7. Optional merged table sheet
    if merged_table is not None:
        ws_merged = wb.create_sheet(sanitize_sheet_name("合并去重结果", used_names))
        m_headers, m_rows = _extract_table_grid(merged_table)
        _write_styled_sheet(ws_merged, m_headers, m_rows, cancel_check=cancel_check)
        _advance()

    return _save_workbook_safely(wb, target)


def export_dedup_report_word(
    report: TableDedupReport,
    output_path: str | Path,
    *,
    max_detail_rows: int = 500,
    max_intra_groups: int = 200,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Path:
    """Export a formatted Word (``.docx``) multi-table deduplication report."""
    _require_docx()
    target = Path(output_path)
    _check_target_writable(target)
    _check_cancelled(cancel_check)

    total_steps = 4
    doc = docx.Document()
    _configure_docx_base_styles(doc)

    doc.add_heading("多表查重报告", level=1)

    summary_rows, per_table_rows = _build_report_overview_rows(report)

    # Section 1: Global overview
    doc.add_heading("一、查重总览", level=2)
    _add_styled_docx_table(doc, ["统计指标", "数值 / 详情"], summary_rows, cancel_check=cancel_check)
    if progress_callback:
        progress_callback(1, total_steps)

    if per_table_rows:
        doc.add_heading("二、各表统计明细", level=2)
        _add_styled_docx_table(
            doc,
            ["表格名称", "来源类型", "总行数", "唯一键数", "跨表重复行", "表内重复行", "本表独有行", "未识别行"],
            per_table_rows,
            cancel_check=cancel_check,
        )
    if progress_callback:
        progress_callback(2, total_steps)

    # Section 3: Cross-table duplicates
    doc.add_heading("三、跨表重复明细", level=2)
    if not report.cross_table_groups:
        doc.add_paragraph("未发现跨表重复数据。")
    else:
        detail_rows: List[List[object]] = []
        total_cross_rows = sum(len(g.rows) for g in report.cross_table_groups)
        limit = max(1, int(max_detail_rows))
        for grp in report.cross_table_groups:
            for ref in grp.rows:
                if len(detail_rows) >= limit:
                    break
                detail_rows.append(
                    [
                        grp.key_value,
                        grp.table_count,
                        ref.table_name,
                        ref.row_index,
                        _format_row_summary(ref, max_chars=140),
                    ]
                )
            if len(detail_rows) >= limit:
                break

        if total_cross_rows > limit:
            note_p = doc.add_paragraph()
            note_run = note_p.add_run(
                f"提示：跨表重复明细共 {total_cross_rows} 条，本文档展示前 {limit} 条。"
                "完整明细请导出多 Sheet Excel 报告。"
            )
            note_run.bold = True
            note_run.font.size = Pt(9)
            note_run.font.color.rgb = RGBColor(0xB4, 0x53, 0x09)

        _add_styled_docx_table(
            doc,
            ["重复键", "涉及表数", "来源表", "原行号", "行内容摘要"],
            detail_rows,
            cancel_check=cancel_check,
        )
    if progress_callback:
        progress_callback(3, total_steps)

    # Section 4: Intra-table duplicates & intersection summary
    doc.add_heading("四、表内重复与集合分析", level=2)
    if report.intra_table_groups:
        intra_limit = max(1, int(max_intra_groups))
        total_intra_groups = len(report.intra_table_groups)
        if total_intra_groups > intra_limit:
            intra_note_p = doc.add_paragraph()
            intra_note_run = intra_note_p.add_run(
                f"提示：表内重复分组共 {total_intra_groups} 组，本文档展示前 {intra_limit} 组。"
                "完整明细请导出多 Sheet Excel 报告。"
            )
            intra_note_run.bold = True
            intra_note_run.font.size = Pt(9)
            intra_note_run.font.color.rgb = RGBColor(0xB4, 0x53, 0x09)

        intra_summary = [
            [
                grp.table_names[0] if grp.table_names else "",
                grp.key_value,
                grp.total_rows,
                ", ".join(str(r.row_index) for r in grp.rows[:20]),
            ]
            for grp in report.intra_table_groups[:intra_limit]
        ]
        _add_styled_docx_table(
            doc,
            ["所属表格", "重复键", "表内重复次数", "原行号列表"],
            intra_summary,
            cancel_check=cancel_check,
        )
    else:
        doc.add_paragraph("未发现表内重复数据。")

    inter_p = doc.add_paragraph()
    if report.intersection_keys:
        preview_keys = "、".join(report.intersection_keys[:30])
        suffix = (
            f" 等共 {len(report.intersection_keys)} 项"
            if len(report.intersection_keys) > 30
            else f"（共 {len(report.intersection_keys)} 项）"
        )
        inter_p.add_run(f"全表共有交集键：{preview_keys}{suffix}")
    else:
        inter_p.add_run("全表共有交集键：无")

    if progress_callback:
        progress_callback(4, total_steps)

    return _save_docx_safely(doc, target)
