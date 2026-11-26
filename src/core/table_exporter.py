"""Multi-format table and deduplication report exporter for 数据查重程序.

Exports ``DataTable`` and ``TableDedupReport`` objects to:
- Styled Excel ``.xlsx`` (single table, multi-table workbook, and rich multi-sheet
  deduplication reports)
- UTF-8-SIG ``.csv``
- Formatted Word ``.docx`` documents (via ``python-docx``)
- Plain UTF-8 ``.txt``

Sanitizes illegal XML 1.0 control characters for both ``openpyxl`` and
``python-docx``, sanitizes Excel sheet names (<= 31 chars, unique), and raises
friendly Chinese ``ExportError`` messages when target files are locked.
"""

from __future__ import annotations

import csv
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .exporter import (
    ExportError,
    _format_file_error,
    _save_workbook_safely,
    _write_styled_sheet,
    _write_text_safely,
)
from .table_data import DataTable, TableDedupReport, TableRowRef

try:
    from openpyxl import Workbook

    _HAS_OPENPYXL = True
except ImportError:  # pragma: no cover - surfaced with friendly ExportError
    _HAS_OPENPYXL = False

try:
    import docx
    from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
    from docx.oxml import OxmlElement, parse_xml
    from docx.oxml.ns import nsdecls, qn
    from docx.shared import Pt, RGBColor

    _HAS_DOCX = True
except ImportError:  # pragma: no cover - surfaced with friendly ExportError
    _HAS_DOCX = False


# XML 1.0 forbids NUL and C0 control chars except TAB (\x09), LF (\x0a), CR (\x0d), plus FFFE/FFFF.
_ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_INVALID_SHEET_CHARS_RE = re.compile(r"[\[\]:*?/\\]")

_WORD_HEADER_FILL = "1F4E78"
_WORD_ZEBRA_FILL = "F4F7FA"
_WORD_BORDER_COLOR = "CBD5E1"


def sanitize_xml_text(value: object) -> str:
    """Convert any value to an XML-safe string for openpyxl and python-docx."""
    if value is None:
        return ""
    return _ILLEGAL_XML_RE.sub("", str(value))


def sanitize_sheet_name(name: str, used_names_lower: Optional[set[str]] = None) -> str:
    """Produce a valid, unique Excel worksheet title (<= 31 characters)."""
    cleaned = _INVALID_SHEET_CHARS_RE.sub("_", sanitize_xml_text(name)).strip()
    cleaned = cleaned.strip("'").strip()
    if not cleaned:
        cleaned = "工作表"

    base = cleaned[:31]
    if used_names_lower is None:
        return base

    candidate = base
    suffix_num = 2
    while candidate.lower() in used_names_lower:
        suffix = f"_{suffix_num}"
        candidate = f"{base[: 31 - len(suffix)]}{suffix}"
        suffix_num += 1
    used_names_lower.add(candidate.lower())
    return candidate


def _require_openpyxl() -> None:
    if not _HAS_OPENPYXL:
        raise ExportError("缺少 openpyxl 依赖，无法导出 Excel (.xlsx) 文件。")


def _require_docx() -> None:
    if not _HAS_DOCX:
        raise ExportError("缺少 python-docx 依赖，无法导出 Word (.docx) 文档。")


def _check_target_writable(target: Path) -> None:
    """Fail fast with a friendly ExportError if an existing target file is locked."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            with open(target, "r+b"):
                pass
        except OSError as exc:
            raise _format_file_error(target, exc) from exc


def _save_docx_safely(document: "docx.Document", output_path: Path) -> Path:
    """Save a python-docx Document atomically with locked-file protection."""
    _check_target_writable(output_path)
    tmp_path = output_path.with_name(f".{output_path.stem}.{os.getpid()}.tmp.docx")
    try:
        document.save(str(tmp_path))
        os.replace(tmp_path, output_path)
    except OSError as exc:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise _format_file_error(output_path, exc) from exc
    return output_path


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
# Word styling helpers
# ----------------------------------------------------------------------


def _set_docx_cell_shading(cell, fill_hex: str) -> None:
    shading = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill_hex}"/>')
    cell._tc.get_or_add_tcPr().append(shading)


def _set_docx_table_borders(table, color_hex: str = _WORD_BORDER_COLOR) -> None:
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


def _set_docx_repeat_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tr_pr.append(tbl_header)


def _configure_docx_base_styles(doc: "docx.Document") -> None:
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


def _add_styled_docx_table(
    doc: "docx.Document",
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
) -> None:
    """Render a styled Word table with shaded header, repeating header, and zebra rows."""
    if not headers:
        doc.add_paragraph("（无列数据）")
        return

    tbl = doc.add_table(rows=1, cols=len(headers))
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    _set_docx_table_borders(tbl)

    hdr_row = tbl.rows[0]
    _set_docx_repeat_header(hdr_row)
    for col_idx, h_text in enumerate(headers):
        cell = hdr_row.cells[col_idx]
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        _set_docx_cell_shading(cell, _WORD_HEADER_FILL)
        p = cell.paragraphs[0]
        p.paragraph_format.space_before = Pt(3)
        p.paragraph_format.space_after = Pt(3)
        run = p.add_run(sanitize_xml_text(h_text))
        run.bold = True
        run.font.name = "Microsoft YaHei"
        run.font.size = Pt(9.5)
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

    for row_idx, row_vals in enumerate(rows):
        row = tbl.add_row()
        is_zebra = (row_idx % 2) == 1
        for col_idx in range(len(headers)):
            cell = row.cells[col_idx]
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            if is_zebra:
                _set_docx_cell_shading(cell, _WORD_ZEBRA_FILL)
            val = row_vals[col_idx] if col_idx < len(row_vals) else ""
            p = cell.paragraphs[0]
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            run = p.add_run(sanitize_xml_text(val))
            run.font.name = "Microsoft YaHei"
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(0x1E, 0x29, 0x3B)


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
    if col_idx < 0 or col_idx >= table.col_count:
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
) -> Path:
    """Export a single ``DataTable`` to a styled ``.xlsx`` workbook."""
    _require_openpyxl()
    target = Path(output_path)
    wb = Workbook()
    ws = wb.active
    ws.title = sanitize_sheet_name(table.name or "数据表")

    headers = [sanitize_xml_text(h) for h in table.headers]
    col_n = len(headers)
    rows = [
        [sanitize_xml_text(row[i] if i < len(row) else "") for i in range(col_n)]
        for row in table.rows
    ]
    dup_indices = _resolve_highlight_indices(table, highlight_duplicates_col)
    _write_styled_sheet(ws, headers, rows, highlight_row_indices=dup_indices)
    return _save_workbook_safely(wb, target)


def export_table_csv(
    table: DataTable,
    output_path: str | Path,
) -> Path:
    """Export a ``DataTable`` to UTF-8-SIG ``.csv`` (Excel-friendly)."""
    target = Path(output_path)
    _check_target_writable(target)
    tmp_path = target.with_name(f".{target.stem}.{os.getpid()}.tmp.csv")
    col_n = len(table.headers)
    try:
        with open(tmp_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            if table.headers:
                writer.writerow([sanitize_xml_text(h) for h in table.headers])
            for row in table.rows:
                if col_n:
                    writer.writerow(
                        [
                            sanitize_xml_text(row[i] if i < len(row) else "")
                            for i in range(col_n)
                        ]
                    )
                else:
                    writer.writerow([sanitize_xml_text(v) for v in row])
        os.replace(tmp_path, target)
    except OSError as exc:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise _format_file_error(target, exc) from exc
    return target


def export_table_word(
    table: DataTable,
    output_path: str | Path,
    title: str = "导出数据表",
    *,
    max_rows: int = 3000,
) -> Path:
    """Export a ``DataTable`` into a formatted Word (``.docx``) document.

    Gracefully caps very large tables at ``max_rows`` with an explanatory note so
    Word rendering remains fast and responsive.
    """
    _require_docx()
    target = Path(output_path)
    _check_target_writable(target)
    doc = docx.Document()
    _configure_docx_base_styles(doc)

    doc_title = sanitize_xml_text(title or table.name or "导出数据表")
    doc.add_heading(doc_title, level=1)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta_p = doc.add_paragraph()
    meta_run = meta_p.add_run(
        f"数据表：{sanitize_xml_text(table.name or '未命名表')}  |  "
        f"行数：{table.row_count}  |  列数：{table.col_count}  |  导出时间：{now_str}"
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

    _add_styled_docx_table(doc, table.headers, rows_to_write)
    return _save_docx_safely(doc, target)


def export_table_txt(
    table: DataTable,
    output_path: str | Path,
    column_index: Optional[int] = None,
) -> Path:
    """Export a ``DataTable`` (full TSV or a single column) to UTF-8 ``.txt``."""
    target = Path(output_path)
    lines: List[str] = []

    if column_index is not None:
        if column_index < 0 or (table.col_count > 0 and column_index >= table.col_count):
            raise ValueError(f"列索引超出范围：{column_index}")
        for row in table.rows:
            if column_index < len(row):
                val = sanitize_xml_text(row[column_index]).strip()
                if val:
                    lines.append(val)
    else:
        col_n = len(table.headers)
        if table.headers:
            lines.append("\t".join(sanitize_xml_text(h) for h in table.headers))
        for row in table.rows:
            if col_n:
                cells = [
                    sanitize_xml_text(row[i] if i < len(row) else "").replace("\t", " ")
                    for i in range(col_n)
                ]
            else:
                cells = [sanitize_xml_text(v).replace("\t", " ") for v in row]
            lines.append("\t".join(cells))

    content = "\n".join(lines) + ("\n" if lines else "")
    return _write_text_safely(target, content, encoding="utf-8")


def export_multiple_tables_workbook(
    tables: Sequence[DataTable],
    output_path: str | Path,
) -> Path:
    """Package multiple ``DataTable`` instances into one multi-sheet ``.xlsx`` file."""
    _require_openpyxl()
    target = Path(output_path)
    wb = Workbook()

    if not tables:
        ws = wb.active
        ws.title = "空数据"
        _write_styled_sheet(ws, ["说明"], [["暂无可导出的表格数据"]])
        return _save_workbook_safely(wb, target)

    used_names: set[str] = set()
    for idx, tbl in enumerate(tables):
        ws = wb.active if idx == 0 else wb.create_sheet()
        ws.title = sanitize_sheet_name(tbl.name or f"工作表{idx + 1}", used_names)
        headers = [sanitize_xml_text(h) for h in tbl.headers]
        col_n = len(headers)
        rows = [
            [sanitize_xml_text(row[i] if i < len(row) else "") for i in range(col_n)]
            for row in tbl.rows
        ]
        _write_styled_sheet(ws, headers, rows)

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
    )

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
        ws_cross, cross_headers, cross_rows, highlight_row_indices=cross_highlight or None
    )

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
    _write_styled_sheet(ws_intra, intra_headers, intra_rows)

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
    _write_styled_sheet(ws_inter, inter_headers, inter_rows)

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
    _write_styled_sheet(ws_unique, unique_headers, unique_rows)

    # 6. Optional unrecognized rows sheet
    if report.unrecognized_rows:
        ws_unrec = wb.create_sheet(sanitize_sheet_name("未识别行", used_names))
        unrec_rows = [
            [ref.table_name, ref.row_index, _format_row_summary(ref)]
            for ref in report.unrecognized_rows
        ]
        _write_styled_sheet(ws_unrec, ["来源表", "原行号", "行内容明细"], unrec_rows)

    # 7. Optional merged table sheet
    if merged_table is not None:
        ws_merged = wb.create_sheet(sanitize_sheet_name("合并去重结果", used_names))
        m_headers = [sanitize_xml_text(h) for h in merged_table.headers]
        m_col_n = len(m_headers)
        m_rows = [
            [sanitize_xml_text(r[i] if i < len(r) else "") for i in range(m_col_n)]
            for r in merged_table.rows
        ]
        _write_styled_sheet(ws_merged, m_headers, m_rows)

    return _save_workbook_safely(wb, target)


def export_dedup_report_word(
    report: TableDedupReport,
    output_path: str | Path,
    *,
    max_detail_rows: int = 500,
) -> Path:
    """Export a formatted Word (``.docx``) multi-table deduplication report."""
    _require_docx()
    target = Path(output_path)
    _check_target_writable(target)
    doc = docx.Document()
    _configure_docx_base_styles(doc)

    doc.add_heading("多表查重报告", level=1)

    summary_rows, per_table_rows = _build_report_overview_rows(report)

    # Section 1: Global overview
    doc.add_heading("一、查重总览", level=2)
    _add_styled_docx_table(doc, ["统计指标", "数值 / 详情"], summary_rows)

    if per_table_rows:
        doc.add_heading("二、各表统计明细", level=2)
        _add_styled_docx_table(
            doc,
            ["表格名称", "来源类型", "总行数", "唯一键数", "跨表重复行", "表内重复行", "本表独有行", "未识别行"],
            per_table_rows,
        )

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
        )

    # Section 4: Intra-table duplicates & intersection summary
    doc.add_heading("四、表内重复与集合分析", level=2)
    if report.intra_table_groups:
        intra_summary = [
            [
                grp.table_names[0] if grp.table_names else "",
                grp.key_value,
                grp.total_rows,
                ", ".join(str(r.row_index) for r in grp.rows[:20]),
            ]
            for grp in report.intra_table_groups[:200]
        ]
        _add_styled_docx_table(
            doc,
            ["所属表格", "重复键", "表内重复次数", "原行号列表"],
            intra_summary,
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

    return _save_docx_safely(doc, target)
