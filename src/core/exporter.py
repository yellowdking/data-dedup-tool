"""Excel (.xlsx), CSV (UTF-8-SIG), and TXT exporters for 数据查重程序.

Exports full/filtered database records, duplicate groups, or comparison results
with clean styling, auto-fitted column widths, neutral Chinese headers, and
friendly error messages when target files are locked by Excel/WPS.
"""

from __future__ import annotations

import csv
import errno
import os
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import CompareResult, DataRecord, DuplicateGroup, LibraryStats, ParseResult

try:
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE as _OPENPYXL_ILLEGAL_RE
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    _HAS_OPENPYXL = True
except ImportError:  # pragma: no cover - surfaced with friendly ExportError
    _HAS_OPENPYXL = False
    _OPENPYXL_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")


_FALLBACK_ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")


def _sanitize_excel_value(value: object) -> object:
    """Strip XML 1.0 illegal control characters so openpyxl never raises IllegalCharacterError."""
    if isinstance(value, str):
        cleaned = _OPENPYXL_ILLEGAL_RE.sub("", value)
        return _FALLBACK_ILLEGAL_XML_RE.sub("", cleaned)
    return value


class ExportError(Exception):
    """User-friendly export failure exception (e.g. locked target file)."""


_HEADER_FILL_HEX = "1F4E78"
_ZEBRA_FILL_HEX = "F4F7FA"
_DUP_HIGHLIGHT_HEX = "FFF3CD"
_BORDER_COLOR_HEX = "D9E1E8"


def _is_locked_os_error(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    return (
        isinstance(exc, PermissionError)
        or exc.errno in (errno.EACCES, errno.EPERM, errno.EBUSY)
        or winerror in (5, 32, 33)
    )


def _format_file_error(path: Path, exc: OSError) -> ExportError:
    if _is_locked_os_error(exc):
        return ExportError(
            f"无法写入文件「{path.name}」：该文件可能正被 Excel、WPS 或其他程序占用，请关闭后重试。"
        )
    return ExportError(f"导出文件「{path.name}」失败：{exc}")


def _display_width(value: object) -> int:
    """Estimate visual character width (counting CJK wide characters as 2)."""
    text = str(value if value is not None else "")
    max_line = 0
    for line in text.splitlines() or [""]:
        width = 0
        for ch in line:
            width += 2 if ord(ch) > 127 else 1
        if width > max_line:
            max_line = width
    return max_line


def _require_openpyxl() -> None:
    if not _HAS_OPENPYXL:
        raise ExportError("缺少 openpyxl 依赖，无法导出 Excel (.xlsx) 文件。")


def _write_styled_sheet(
    ws,
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    highlight_row_indices: Optional[set[int]] = None,
) -> None:
    """Populate and style an openpyxl worksheet cleanly."""
    header_font = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
    body_font = Font(name="Microsoft YaHei", size=10, color="1E293B")
    header_fill = PatternFill(
        start_color=_HEADER_FILL_HEX, end_color=_HEADER_FILL_HEX, fill_type="solid"
    )
    zebra_fill = PatternFill(
        start_color=_ZEBRA_FILL_HEX, end_color=_ZEBRA_FILL_HEX, fill_type="solid"
    )
    highlight_fill = PatternFill(
        start_color=_DUP_HIGHLIGHT_HEX, end_color=_DUP_HIGHLIGHT_HEX, fill_type="solid"
    )
    thin_side = Side(border_style="thin", color=_BORDER_COLOR_HEX)
    cell_border = Border(
        left=thin_side, right=thin_side, top=thin_side, bottom=thin_side
    )
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    body_align = Alignment(horizontal="left", vertical="center")

    safe_headers = [_sanitize_excel_value(h) for h in headers]
    ws.append(safe_headers)
    col_widths = [_display_width(h) for h in safe_headers]

    for col_idx in range(1, len(safe_headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = cell_border
    ws.row_dimensions[1].height = 24

    for row_idx, row_values in enumerate(rows, start=2):
        safe_row = [_sanitize_excel_value(v) for v in row_values]
        ws.append(safe_row)
        is_highlighted = bool(highlight_row_indices and (row_idx - 2) in highlight_row_indices)
        is_zebra = (row_idx % 2) == 1
        for col_idx, val in enumerate(safe_row, start=1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.font = body_font
            cell.alignment = body_align
            cell.border = cell_border
            if is_highlighted:
                cell.fill = highlight_fill
            elif is_zebra:
                cell.fill = zebra_fill
            w = _display_width(val)
            if col_idx - 1 < len(col_widths) and w > col_widths[col_idx - 1]:
                col_widths[col_idx - 1] = w
        ws.row_dimensions[row_idx].height = 20

    for col_idx, raw_width in enumerate(col_widths, start=1):
        clamped = max(10, min(60, raw_width + 4))
        ws.column_dimensions[get_column_letter(col_idx)].width = clamped

    ws.freeze_panes = "A2"
    if headers:
        last_col = get_column_letter(len(headers))
        last_row = max(1, len(rows) + 1)
        ws.auto_filter.ref = f"A1:{last_col}{last_row}"


def _save_workbook_safely(wb: "Workbook", output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Fail fast with a friendly message if target exists and is locked by Excel/WPS.
    if output_path.exists():
        try:
            with open(output_path, "r+b"):
                pass
        except OSError as exc:
            raise _format_file_error(output_path, exc) from exc

    tmp_path = output_path.with_name(f".{output_path.stem}.{os.getpid()}.tmp.xlsx")
    try:
        wb.save(str(tmp_path))
        os.replace(tmp_path, output_path)
    except OSError as exc:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise _format_file_error(output_path, exc) from exc
    return output_path


def _write_text_safely(output_path: Path, content: str, *, encoding: str = "utf-8") -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        try:
            with open(output_path, "r+b"):
                pass
        except OSError as exc:
            raise _format_file_error(output_path, exc) from exc

    tmp_path = output_path.with_name(f".{output_path.stem}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(content, encoding=encoding)
        os.replace(tmp_path, output_path)
    except OSError as exc:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise _format_file_error(output_path, exc) from exc
    return output_path


def _write_csv_safely(
    output_path: Path,
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        try:
            with open(output_path, "r+b"):
                pass
        except OSError as exc:
            raise _format_file_error(output_path, exc) from exc

    tmp_path = output_path.with_name(f".{output_path.stem}.{os.getpid()}.tmp.csv")
    try:
        with open(tmp_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for row in rows:
                writer.writerow(row)
        os.replace(tmp_path, output_path)
    except OSError as exc:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise _format_file_error(output_path, exc) from exc
    return output_path


# ----------------------------------------------------------------------
# Record rows helper
# ----------------------------------------------------------------------

RECORD_EXPORT_HEADERS: List[str] = [
    "标准编码",
    "前缀分类",
    "数字编号",
    "重复次数",
    "分集",
    "标签",
    "原始文件名",
    "完整路径",
    "来源",
    "来源类型",
    "识别规则",
    "置信度",
    "录入时间",
    "备注",
]


def _build_record_rows(
    records: Sequence[DataRecord],
    code_counts: Optional[Dict[str, int]] = None,
) -> Tuple[List[List[object]], set[int]]:
    counts = code_counts
    if counts is None:
        counts = {}
        for r in records:
            if r.canonical_code:
                counts[r.canonical_code] = counts.get(r.canonical_code, 0) + 1

    rows: List[List[object]] = []
    dup_indices: set[int] = set()
    for idx, rec in enumerate(records):
        cnt = counts.get(rec.canonical_code, 1)
        if cnt > 1:
            dup_indices.add(idx)
        rows.append(
            [
                rec.canonical_code,
                rec.prefix,
                rec.number,
                cnt,
                rec.part,
                rec.tags_str,
                rec.file_name or rec.raw_text,
                rec.full_path,
                rec.source_name,
                rec.source_type,
                rec.matched_rule,
                round(float(rec.confidence), 2),
                rec.created_at,
                rec.note,
            ]
        )
    return rows, dup_indices


# ----------------------------------------------------------------------
# Public export functions
# ----------------------------------------------------------------------


def export_records_excel(
    records: Sequence[DataRecord],
    output_path: str | Path,
    *,
    code_counts: Optional[Dict[str, int]] = None,
    stats: Optional[LibraryStats] = None,
) -> Path:
    """Export full or filtered ``DataRecord`` items to a styled ``.xlsx`` workbook."""
    _require_openpyxl()
    target = Path(output_path)
    wb = Workbook()
    ws = wb.active
    ws.title = "数据清单"

    rows, dup_indices = _build_record_rows(records, code_counts=code_counts)
    _write_styled_sheet(ws, RECORD_EXPORT_HEADERS, rows, highlight_row_indices=dup_indices)

    if stats is not None:
        ws_stats = wb.create_sheet("汇总统计")
        summary_rows = [
            ["总记录数", stats.total_records],
            ["唯一编码数", stats.unique_codes],
            ["重复分组数", stats.duplicate_groups],
            ["冗余重复记录数", stats.duplicate_records],
            ["疑似分集分组数", stats.possible_multipart_groups],
        ]
        _write_styled_sheet(ws_stats, ["统计指标", "数值"], summary_rows)

    return _save_workbook_safely(wb, target)


def export_records_csv(
    records: Sequence[DataRecord],
    output_path: str | Path,
    *,
    code_counts: Optional[Dict[str, int]] = None,
) -> Path:
    """Export ``DataRecord`` items to UTF-8-SIG ``.csv`` (Excel-friendly)."""
    target = Path(output_path)
    rows, _ = _build_record_rows(records, code_counts=code_counts)
    return _write_csv_safely(target, RECORD_EXPORT_HEADERS, rows)


def export_codes_txt(
    items: Iterable[DataRecord | ParseResult | str],
    output_path: str | Path,
    *,
    unique: bool = True,
    sort_codes: bool = True,
) -> Path:
    """Export canonical codes to a plain UTF-8 ``.txt`` file (one code per line)."""
    target = Path(output_path)
    codes: List[str] = []
    seen = set()

    for item in items:
        if isinstance(item, str):
            code = item.strip().upper()
        else:
            code = item.canonical_code.strip().upper()
        if not code:
            continue
        if unique:
            if code in seen:
                continue
            seen.add(code)
        codes.append(code)

    if sort_codes:
        codes.sort()

    content = "\n".join(codes) + ("\n" if codes else "")
    return _write_text_safely(target, content, encoding="utf-8")


def export_duplicate_groups_excel(
    groups: Sequence[DuplicateGroup],
    output_path: str | Path,
) -> Path:
    """Export duplicate groups to a two-sheet styled ``.xlsx`` workbook."""
    _require_openpyxl()
    target = Path(output_path)
    wb = Workbook()

    ws_detail = wb.active
    ws_detail.title = "重复项明细"
    detail_headers = [
        "标准编码",
        "重复次数",
        "疑似分集",
        "前缀分类",
        "数字编号",
        "分集",
        "标签",
        "原始文件名",
        "完整路径",
        "来源",
        "录入时间",
        "备注",
    ]
    detail_rows: List[List[object]] = []
    summary_rows: List[List[object]] = []
    multipart_indices: set[int] = set()

    for group in groups:
        mp_label = "是" if group.is_possible_multipart else "否"
        summary_rows.append(
            [
                group.canonical_code,
                group.count,
                mp_label,
                ", ".join(sorted({r.part for r in group.records if r.part})),
                ", ".join(sorted({r.source_name for r in group.records if r.source_name})),
            ]
        )
        for rec in group.records:
            if group.is_possible_multipart:
                multipart_indices.add(len(detail_rows))
            detail_rows.append(
                [
                    group.canonical_code,
                    group.count,
                    mp_label,
                    rec.prefix,
                    rec.number,
                    rec.part,
                    rec.tags_str,
                    rec.file_name or rec.raw_text,
                    rec.full_path,
                    rec.source_name,
                    rec.created_at,
                    rec.note,
                ]
            )

    _write_styled_sheet(
        ws_detail, detail_headers, detail_rows, highlight_row_indices=multipart_indices
    )

    ws_summary = wb.create_sheet("重复分组汇总")
    _write_styled_sheet(
        ws_summary,
        ["标准编码", "重复次数", "疑似分集", "包含分集", "涉及来源"],
        summary_rows,
    )

    return _save_workbook_safely(wb, target)


def export_duplicate_groups_csv(
    groups: Sequence[DuplicateGroup],
    output_path: str | Path,
) -> Path:
    """Export duplicate groups to a UTF-8-SIG ``.csv`` file."""
    target = Path(output_path)
    headers = [
        "标准编码",
        "重复次数",
        "疑似分集",
        "分集",
        "标签",
        "原始文件名",
        "完整路径",
        "来源",
        "录入时间",
    ]
    rows: List[List[object]] = []
    for group in groups:
        mp_label = "是" if group.is_possible_multipart else "否"
        for rec in group.records:
            rows.append(
                [
                    group.canonical_code,
                    group.count,
                    mp_label,
                    rec.part,
                    rec.tags_str,
                    rec.file_name or rec.raw_text,
                    rec.full_path,
                    rec.source_name,
                    rec.created_at,
                ]
            )
    return _write_csv_safely(target, headers, rows)


def _parse_result_row(item: ParseResult, source_label: str = "") -> List[object]:
    return [
        item.canonical_code,
        item.prefix,
        item.number,
        item.part,
        item.tags_str,
        item.file_name or item.raw_text,
        item.full_path,
        source_label or item.source_name,
        item.matched_rule,
        item.raw_text,
    ]


_PARSE_HEADERS = [
    "标准编码",
    "前缀分类",
    "数字编号",
    "分集",
    "标签",
    "原始文件名",
    "完整路径",
    "来源",
    "识别规则",
    "原始文本",
]


def export_compare_result_excel(
    result: CompareResult,
    output_path: str | Path,
) -> Path:
    """Export a DB comparison or A-vs-B cross-comparison result to styled ``.xlsx``."""
    _require_openpyxl()
    target = Path(output_path)
    wb = Workbook()

    if result.mode == "ab":
        ws_inter = wb.active
        ws_inter.title = "交集(重复)"
        inter_rows: List[List[object]] = []
        a_by_code: Dict[str, List[ParseResult]] = {}
        b_by_code: Dict[str, List[ParseResult]] = {}
        for item in result.intersection_a:
            a_by_code.setdefault(item.canonical_code.strip().upper(), []).append(item)
        for item in result.intersection_b:
            b_by_code.setdefault(item.canonical_code.strip().upper(), []).append(item)

        for code in result.intersection_codes:
            a_names = " | ".join(
                (i.file_name or i.raw_text) for i in a_by_code.get(code, [])
            )
            b_names = " | ".join(
                (i.file_name or i.raw_text) for i in b_by_code.get(code, [])
            )
            inter_rows.append(
                [
                    code,
                    len(a_by_code.get(code, [])),
                    a_names,
                    len(b_by_code.get(code, [])),
                    b_names,
                ]
            )
        _write_styled_sheet(
            ws_inter,
            [
                "标准编码",
                f"{result.source_a_name or '来源A'}数量",
                f"{result.source_a_name or '来源A'}条目",
                f"{result.source_b_name or '来源B'}数量",
                f"{result.source_b_name or '来源B'}条目",
            ],
            inter_rows,
        )

        ws_a = wb.create_sheet("仅A有")
        _write_styled_sheet(
            ws_a,
            _PARSE_HEADERS,
            [_parse_result_row(i, result.source_a_name) for i in result.only_in_a],
        )

        ws_b = wb.create_sheet("仅B有")
        _write_styled_sheet(
            ws_b,
            _PARSE_HEADERS,
            [_parse_result_row(i, result.source_b_name) for i in result.only_in_b],
        )

        ws_unrec = wb.create_sheet("未识别")
        unrec_rows = [
            [result.source_a_name or "来源A", i.line_number, i.raw_text, i.note]
            for i in result.unrecognized_a
        ] + [
            [result.source_b_name or "来源B", i.line_number, i.raw_text, i.note]
            for i in result.unrecognized_b
        ]
        _write_styled_sheet(ws_unrec, ["来源", "行号", "原始文本", "说明"], unrec_rows)

        ws_sum = wb.create_sheet("比对汇总")
        _write_styled_sheet(
            ws_sum,
            ["统计项", "数值"],
            [
                ["来源A", result.source_a_name or "来源A"],
                ["来源B", result.source_b_name or "来源B"],
                ["交集编码数", len(result.intersection_codes)],
                ["仅A有条目数", len(result.only_in_a)],
                ["仅B有条目数", len(result.only_in_b)],
                ["A未识别数", len(result.unrecognized_a)],
                ["B未识别数", len(result.unrecognized_b)],
            ],
        )
    else:
        ws_matched = wb.active
        ws_matched.title = "库内已存在"
        matched_rows: List[List[object]] = []
        for item, db_recs in result.matched_in_db:
            matched_rows.append(
                [
                    item.canonical_code,
                    item.part,
                    item.tags_str,
                    item.file_name or item.raw_text,
                    len(db_recs),
                    " | ".join((r.file_name or r.raw_text) for r in db_recs),
                    " | ".join(sorted({r.source_name for r in db_recs if r.source_name})),
                ]
            )
        _write_styled_sheet(
            ws_matched,
            [
                "标准编码",
                "待查分集",
                "待查标签",
                "待查原始文件名",
                "库内匹配数",
                "库内已有文件名",
                "库内来源",
            ],
            matched_rows,
        )

        ws_new = wb.create_sheet("未找到(新数据)")
        _write_styled_sheet(
            ws_new,
            _PARSE_HEADERS,
            [_parse_result_row(i, result.source_a_name) for i in result.not_in_db],
        )

        ws_unrec = wb.create_sheet("未识别")
        _write_styled_sheet(
            ws_unrec,
            ["行号", "原始文本", "文件名", "说明"],
            [
                [i.line_number, i.raw_text, i.file_name, i.note]
                for i in result.unrecognized_a
            ],
        )

        ws_sum = wb.create_sheet("比对汇总")
        _write_styled_sheet(
            ws_sum,
            ["统计项", "数值"],
            [
                ["比对来源", result.source_a_name or "快速比对"],
                ["库内已存在数", len(result.matched_in_db)],
                ["未找到(新数据)数", len(result.not_in_db)],
                ["未识别数", len(result.unrecognized_a)],
            ],
        )

    return _save_workbook_safely(wb, target)


def export_compare_result_csv(
    result: CompareResult,
    output_path: str | Path,
) -> Path:
    """Export comparison results to a flat UTF-8-SIG ``.csv`` file."""
    target = Path(output_path)
    headers = ["分类", "标准编码", "分集", "标签", "原始文件名", "来源", "备注"]
    rows: List[List[object]] = []

    if result.mode == "ab":
        for item in result.intersection_a:
            rows.append(
                [
                    "交集(A)",
                    item.canonical_code,
                    item.part,
                    item.tags_str,
                    item.file_name or item.raw_text,
                    result.source_a_name or item.source_name,
                    "",
                ]
            )
        for item in result.intersection_b:
            rows.append(
                [
                    "交集(B)",
                    item.canonical_code,
                    item.part,
                    item.tags_str,
                    item.file_name or item.raw_text,
                    result.source_b_name or item.source_name,
                    "",
                ]
            )
        for item in result.only_in_a:
            rows.append(
                [
                    "仅A有",
                    item.canonical_code,
                    item.part,
                    item.tags_str,
                    item.file_name or item.raw_text,
                    result.source_a_name or item.source_name,
                    "",
                ]
            )
        for item in result.only_in_b:
            rows.append(
                [
                    "仅B有",
                    item.canonical_code,
                    item.part,
                    item.tags_str,
                    item.file_name or item.raw_text,
                    result.source_b_name or item.source_name,
                    "",
                ]
            )
        for item in result.unrecognized_a:
            rows.append(
                ["未识别(A)", "", "", "", item.raw_text, result.source_a_name, item.note]
            )
        for item in result.unrecognized_b:
            rows.append(
                ["未识别(B)", "", "", "", item.raw_text, result.source_b_name, item.note]
            )
    else:
        for item, db_recs in result.matched_in_db:
            rows.append(
                [
                    "库内已存在",
                    item.canonical_code,
                    item.part,
                    item.tags_str,
                    item.file_name or item.raw_text,
                    result.source_a_name or item.source_name,
                    f"库内匹配 {len(db_recs)} 条",
                ]
            )
        for item in result.not_in_db:
            rows.append(
                [
                    "未找到(新数据)",
                    item.canonical_code,
                    item.part,
                    item.tags_str,
                    item.file_name or item.raw_text,
                    result.source_a_name or item.source_name,
                    "",
                ]
            )
        for item in result.unrecognized_a:
            rows.append(
                ["未识别", "", "", "", item.raw_text, result.source_a_name, item.note]
            )

    return _write_csv_safely(target, headers, rows)
