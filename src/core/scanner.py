"""Multi-encoding TXT/tree/Everything/dir/CSV/TSV, Word (.docx), Excel (.xlsx/.xlsm),
and local folder recursive scanner.

Produces structured `ScannedItem` lists with parent directory or document-location
reconstruction and companion-file classification. All terminology is strictly neutral.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import math
import os
import re
from pathlib import Path
from typing import Callable, Collection, Optional, Sequence, Union

from .models import ScannedItem
from .normalizer import is_companion_extension, split_known_extension


# Supported document extensions accepted by `scan_document_file`.
SUPPORTED_DOCUMENT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".txt",
        ".log",
        ".csv",
        ".tsv",
        ".efu",
        ".docx",
        ".xlsx",
        ".xlsm",
    }
)

_RE_FLOAT_INT_STR = re.compile(r"^(-?\d+)\.0+$")


# Encodings tried in order when decoding unknown TXT/CSV files on Windows.
_CANDIDATE_ENCODINGS: tuple[str, ...] = (
    "utf-8-sig",
    "utf-8",
    "gb18030",
    "big5",
    "cp932",
    "utf-16",
)

# Directories skipped during local filesystem scans.
_IGNORED_SYSTEM_DIRS: frozenset[str] = frozenset(
    {
        "$recycle.bin",
        "system volume information",
        ".git",
        ".svn",
        "__pycache__",
        ".venv",
    }
)

# Tree-drawing detection and parsing patterns.
_RE_TREE_BRANCH = re.compile(r"^([│|\s]*)(?:[├└][─━-]+\s*|\+---\\?|\+---|\+---|\\---)(.+)$")
_RE_TREE_BRANCH_ALT = re.compile(r"^([│|\s]*)(?:├──|└──|├───|└───|\+---|\\---)\s*(.+)$")
_RE_TREE_LEAF = re.compile(r"^([│|][\s│|]*|\s{2,})([^\s│|├└+\\].*)$")
_RE_TREE_HEADER = re.compile(
    r"^(?:Folder PATH listing|Volume serial number|卷\s+.+?\s+的文件夹\s*PATH\s*列表|卷序列号为)",
    re.IGNORECASE,
)
_RE_DRIVE_ROOT = re.compile(r"^[A-Za-z]:[\\/.]*$")

# Windows `dir` listing patterns.
_RE_DIR_HEADER_EN = re.compile(r"^\s*Directory of\s+(.+?)\s*$", re.IGNORECASE)
_RE_DIR_HEADER_CN = re.compile(r"^\s*(.+?)\s*的目录\s*$")
_RE_DIR_ENTRY = re.compile(
    r"^\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?\s+"
    r"(<DIR>|[\d,]+)\s+(.+?)\s*$",
    re.IGNORECASE,
)
_RE_DIR_SUMMARY = re.compile(
    r"^\s*(?:\d+\s+(?:File\(s\)|Dir\(s\))|\d+\s+个文件|\d+\s+个目录|"
    r"Total Files Listed|所列文件总数|Volume in drive|驱动器\s+.+?\s+中的卷|Volume Serial Number|卷的序列号)",
    re.IGNORECASE,
)

# Everything / custom tab-separated lines (path\tsize\tdate) or size suffix.
_RE_PURE_SEPARATOR = re.compile(r"^[\s\-_=*#~│├└+|\\/.]+$")
_RE_SIZE_ONLY = re.compile(r"^\d+(?:,\d{3})*$")


def read_text_auto_encoding(file_path: Union[str, Path]) -> tuple[str, str]:
    """Read a text file by auto-detecting BOM and trying common Windows/Asian encodings."""
    path = Path(file_path)
    raw_bytes = path.read_bytes()
    if not raw_bytes:
        return "", "utf-8"

    # Explicit BOM checks first.
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


def detect_input_format(lines: Sequence[str], file_ext: str = "") -> str:
    """Heuristically classify text lines into `tree`, `dir`, `csv`, or `txt`."""
    ext_low = file_ext.lower()
    non_empty = [ln for ln in lines[:80] if ln.strip()]
    if not non_empty:
        return "txt"

    if ext_low in {".csv", ".efu"}:
        return "csv"

    tree_hits = sum(
        1
        for ln in non_empty
        if _RE_TREE_BRANCH_ALT.match(ln)
        or _RE_TREE_HEADER.match(ln.strip())
        or ln.lstrip().startswith(("├──", "└──", "│   ", "+---", "\\---"))
    )
    if tree_hits >= 2:
        return "tree"

    dir_hits = sum(
        1
        for ln in non_empty
        if _RE_DIR_HEADER_EN.match(ln) or _RE_DIR_HEADER_CN.match(ln) or _RE_DIR_ENTRY.match(ln)
    )
    if dir_hits >= 2:
        return "dir"

    # Detect Everything CSV / EFU pasted text by header line.
    first_low = non_empty[0].strip().lower()
    if first_low.startswith(("filename,", '"filename",', "path,", "name,")):
        return "csv"

    return "txt"


def _extract_path_components(path_str: str) -> tuple[str, str, str]:
    """Extract `(file_name, parent_dir, full_path)` from a path-like string."""
    s = path_str.strip().strip('"').strip("'")
    if not s:
        return "", "", ""

    # Determine if `s` is genuinely a path rather than a multi-code line like `ABC-123 / DEF-456`.
    is_win_path = bool(re.match(r"^[A-Za-z]:[\\/]", s)) or s.startswith("\\\\") or ("\\" in s and " \\ " not in s)
    is_posix_path = s.startswith("/") or (
        "/" in s
        and " / " not in s
        and not re.match(r"^https?://", s, re.IGNORECASE)
        and bool(split_known_extension(s.rsplit("/", 1)[-1])[1] or s.count("/") >= 2)
    )

    if is_win_path or is_posix_path:
        norm = s.replace("\\", "/").rstrip("/")
        parts = [p for p in norm.split("/") if p]
        if not parts:
            return s, "", s
        file_name = parts[-1]
        parent_dir = ""
        if len(parts) >= 2 and not parts[-2].endswith(":"):
            parent_dir = parts[-2]
        return file_name, parent_dir, s

    return s, "", ""


def _make_item(
    raw_text: str,
    file_name: str,
    parent_dir: str,
    full_path: str,
    source_name: str,
    source_type: str,
    line_number: int,
    file_size: int = 0,
    companion_extensions: Optional[Collection[str]] = None,
) -> ScannedItem:
    _, ext = split_known_extension(file_name)
    return ScannedItem(
        raw_text=raw_text,
        file_name=file_name,
        parent_dir=parent_dir,
        full_path=full_path,
        source_name=source_name,
        source_type=source_type,
        line_number=line_number,
        file_size=file_size,
        file_ext=ext,
        is_companion=is_companion_extension(ext, companion_extensions),
    )


def _scan_tree_lines(
    lines: Sequence[str],
    source_name: str,
    companion_extensions: Optional[Collection[str]] = None,
) -> list[ScannedItem]:
    """Parse Windows `tree /f`, `tree /f /a`, and Unix `tree` outputs with parent reconstruction."""
    # Pre-tokenize each meaningful tree line into (line_no, raw, depth, name, has_branch).
    tokens: list[tuple[int, str, int, str, bool]] = []
    root_prefix = ""

    for idx, raw_line in enumerate(lines, start=1):
        line = raw_line.rstrip("\r\n")
        stripped = line.strip()
        if not stripped or _RE_PURE_SEPARATOR.match(stripped):
            continue
        if _RE_TREE_HEADER.match(stripped):
            continue
        if _RE_DRIVE_ROOT.match(stripped) or stripped == ".":
            root_prefix = stripped.rstrip("\\/")
            continue
        # Capture bare root path like `D:\ARCHIVE` before tree branches start.
        if not tokens and re.match(r"^[A-Za-z]:\\", stripped):
            root_prefix = stripped.rstrip("\\/")
            continue

        m_branch = _RE_TREE_BRANCH_ALT.match(line)
        if m_branch:
            prefix_str, name = m_branch.group(1), m_branch.group(2).strip()
            depth = len(prefix_str)
            if name:
                tokens.append((idx, stripped, depth, name, True))
            continue

        m_leaf = _RE_TREE_LEAF.match(line)
        if m_leaf:
            prefix_str, name = m_leaf.group(1), m_leaf.group(2).strip()
            depth = len(prefix_str)
            if name:
                tokens.append((idx, stripped, depth, name, False))
            continue

        # Fallback: unindented top-level entry.
        tokens.append((idx, stripped, -1, stripped, False))

    items: list[ScannedItem] = []
    # Directory stack holds tuples of `(depth, dir_name)`.
    dir_stack: list[tuple[int, str]] = []

    for i, (line_no, raw, depth, name, has_branch) in enumerate(tokens):
        # Pop directories that are at the same or deeper level than current node.
        while dir_stack and dir_stack[-1][0] >= depth:
            dir_stack.pop()

        # Peek at next token to check whether this node has children (i.e. is a directory).
        has_children = (i + 1 < len(tokens)) and (tokens[i + 1][2] > depth)
        _, ext = split_known_extension(name)

        if has_children and not ext:
            dir_stack.append((depth, name))
            continue

        parent_dir = dir_stack[-1][1] if dir_stack else ""
        path_parts = ([root_prefix] if root_prefix else []) + [d for _, d in dir_stack] + [name]
        full_path = "\\".join(path_parts) if root_prefix or dir_stack else name

        items.append(
            _make_item(
                raw_text=raw,
                file_name=name,
                parent_dir=parent_dir,
                full_path=full_path,
                source_name=source_name,
                source_type="tree",
                line_number=line_no,
                companion_extensions=companion_extensions,
            )
        )

    return items


def _scan_dir_lines(
    lines: Sequence[str],
    source_name: str,
    companion_extensions: Optional[Collection[str]] = None,
) -> list[ScannedItem]:
    """Parse Windows `dir` / `dir /s` text listings."""
    items: list[ScannedItem] = []
    current_dir = ""

    for idx, raw_line in enumerate(lines, start=1):
        line = raw_line.rstrip("\r\n")
        stripped = line.strip()
        if not stripped or _RE_PURE_SEPARATOR.match(stripped) or _RE_DIR_SUMMARY.match(stripped):
            continue

        m_hdr = _RE_DIR_HEADER_EN.match(line) or _RE_DIR_HEADER_CN.match(line)
        if m_hdr:
            current_dir = m_hdr.group(1).strip().rstrip("\\/")
            continue

        m_entry = _RE_DIR_ENTRY.match(line)
        if m_entry:
            size_or_dir = m_entry.group(1).strip()
            name = m_entry.group(2).strip()
            if name in {".", ".."}:
                continue
            if size_or_dir.upper() == "<DIR>":
                # Keep directory entry only if it might carry a code on its own.
                continue
            file_size = 0
            digits = size_or_dir.replace(",", "")
            if digits.isdigit():
                file_size = int(digits)
            parent_dir = Path(current_dir).name if current_dir else ""
            full_path = f"{current_dir}\\{name}" if current_dir else name
            items.append(
                _make_item(
                    raw_text=stripped,
                    file_name=name,
                    parent_dir=parent_dir,
                    full_path=full_path,
                    source_name=source_name,
                    source_type="txt",
                    line_number=idx,
                    file_size=file_size,
                    companion_extensions=companion_extensions,
                )
            )

    return items


def _scan_csv_text(
    text: str,
    source_name: str,
    companion_extensions: Optional[Collection[str]] = None,
) -> list[ScannedItem]:
    """Parse CSV and Everything EFU file lists."""
    items: list[ScannedItem] = []
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return items

    name_col = 0
    size_col: Optional[int] = None
    start_row = 0

    header = [c.strip().lower() for c in rows[0]]
    name_candidates = {"filename", "file", "name", "path", "full_path", "文件名", "路径", "编码", "原始文件名"}
    size_candidates = {"size", "file_size", "大小", "文件大小"}

    header_matched = False
    for i, col in enumerate(header):
        if col in name_candidates:
            name_col = i
            header_matched = True
        elif col in size_candidates:
            size_col = i
            header_matched = True
    if header_matched:
        start_row = 1

    for row_idx, row in enumerate(rows[start_row:], start=start_row + 1):
        if not row or name_col >= len(row):
            continue
        cell = row[name_col].strip()
        if not cell or _RE_PURE_SEPARATOR.match(cell):
            continue
        file_size = 0
        if size_col is not None and size_col < len(row):
            raw_sz = row[size_col].strip().replace(",", "")
            if raw_sz.isdigit():
                file_size = int(raw_sz)
        file_name, parent_dir, full_path = _extract_path_components(cell)
        items.append(
            _make_item(
                raw_text=cell,
                file_name=file_name,
                parent_dir=parent_dir,
                full_path=full_path,
                source_name=source_name,
                source_type="csv",
                line_number=row_idx,
                file_size=file_size,
                companion_extensions=companion_extensions,
            )
        )

    return items


def _scan_plain_lines(
    lines: Sequence[str],
    source_name: str,
    source_type: str,
    companion_extensions: Optional[Collection[str]] = None,
) -> list[ScannedItem]:
    """Parse plain text lists, full file paths, and tab-separated Everything exports."""
    items: list[ScannedItem] = []

    for idx, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped or _RE_PURE_SEPARATOR.match(stripped):
            continue

        file_size = 0
        target = stripped

        # Handle tab-separated Everything/Excel copy-paste (e.g. `D:\Dir\ABC-123.mp4\t123456\t2024-01-01`).
        if "\t" in stripped:
            cols = [c.strip() for c in stripped.split("\t") if c.strip()]
            if cols:
                target = cols[0]
                for extra in cols[1:]:
                    cleaned_sz = extra.replace(",", "")
                    if _RE_SIZE_ONLY.match(extra) and cleaned_sz.isdigit():
                        file_size = int(cleaned_sz)
                        break

        file_name, parent_dir, full_path = _extract_path_components(target)
        items.append(
            _make_item(
                raw_text=stripped,
                file_name=file_name,
                parent_dir=parent_dir,
                full_path=full_path,
                source_name=source_name,
                source_type=source_type,
                line_number=idx,
                file_size=file_size,
                companion_extensions=companion_extensions,
            )
        )

    return items


def scan_text(
    text: str,
    source_name: str = "手动粘贴",
    source_type: str = "paste",
    file_ext: str = "",
    companion_extensions: Optional[Collection[str]] = None,
) -> list[ScannedItem]:
    """Auto-detect format and scan text content into `ScannedItem` entries."""
    if not text or not text.strip():
        return []
    lines = text.splitlines()
    fmt = detect_input_format(lines, file_ext=file_ext)
    if fmt == "tree":
        return _scan_tree_lines(lines, source_name=source_name, companion_extensions=companion_extensions)
    if fmt == "dir":
        return _scan_dir_lines(lines, source_name=source_name, companion_extensions=companion_extensions)
    if fmt == "csv":
        return _scan_csv_text(text, source_name=source_name, companion_extensions=companion_extensions)
    return _scan_plain_lines(
        lines,
        source_name=source_name,
        source_type=source_type,
        companion_extensions=companion_extensions,
    )


def scan_file(
    file_path: Union[str, Path],
    companion_extensions: Optional[Collection[str]] = None,
) -> list[ScannedItem]:
    """Read a TXT/CSV/EFU/tree/dir listing file with automatic encoding detection."""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"找不到文件: {path}")
    content, _ = read_text_auto_encoding(path)
    return scan_text(
        content,
        source_name=path.name,
        source_type="csv" if path.suffix.lower() in {".csv", ".efu"} else "txt",
        file_ext=path.suffix.lower(),
        companion_extensions=companion_extensions,
    )


def normalize_cell_text(value: object) -> str:
    """Convert an Excel/Word cell value into a clean string.

    - `None` -> `""`
    - `bool` -> `"TRUE"` / `"FALSE"`
    - integer-valued `float` (e.g. `1234567.0`) -> `"1234567"` (preserving real decimals like `3.14`)
    - `datetime` / `date` -> ISO date/datetime string
    - stringified float-int (e.g. `"1234567.0"`) -> `"1234567"`
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        if value.is_integer():
            return str(int(value))
        return format(value, "g")
    if isinstance(value, _dt.datetime):
        if value.hour == 0 and value.minute == 0 and value.second == 0:
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, _dt.date):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text:
        return ""
    m = _RE_FLOAT_INT_STR.match(text)
    if m:
        return m.group(1)
    return text


def _emit_doc_text_lines(
    raw_text: str,
    *,
    file_path: Path,
    location: str,
    source_type: str,
    line_number: int,
    companion_extensions: Optional[Collection[str]] = None,
) -> list[ScannedItem]:
    """Split a normalized document text block (handling embedded newlines) into ScannedItems."""
    if not raw_text:
        return []
    source_badge = f"{file_path.name} [{location}]" if location else file_path.name
    items: list[ScannedItem] = []
    for sub_line in raw_text.splitlines():
        cleaned = normalize_cell_text(sub_line)
        if not cleaned or _RE_PURE_SEPARATOR.match(cleaned):
            continue
        file_name, parent_dir, full_path = _extract_path_components(cleaned)
        items.append(
            _make_item(
                raw_text=cleaned,
                file_name=file_name,
                parent_dir=parent_dir,
                full_path=full_path,
                source_name=source_badge,
                source_type=source_type,
                line_number=line_number,
                companion_extensions=companion_extensions,
            )
        )
    return items


def _coerce_row_cells_to_items(
    cells: Sequence[str],
    *,
    file_path: Path,
    location: str,
    source_type: str,
    row_number: int,
    companion_extensions: Optional[Collection[str]] = None,
) -> list[ScannedItem]:
    """Convert non-empty normalized cells from a single spreadsheet/table row into ScannedItems.

    - Single non-empty cell: splits embedded newlines (`\\n`) into individual items so pasted
      multi-line lists or paths inside one cell scan cleanly.
    - Multiple non-empty cells: flattens internal newlines and joins row cells with `" | "`
      so row-level codes and tags stay associated without creating noisy unrecognized rows
      for auxiliary date/price/index columns.
    """
    non_empty = [c for c in cells if c and not _RE_PURE_SEPARATOR.match(c)]
    if not non_empty:
        return []
    if len(non_empty) == 1:
        return _emit_doc_text_lines(
            non_empty[0],
            file_path=file_path,
            location=location,
            source_type=source_type,
            line_number=row_number,
            companion_extensions=companion_extensions,
        )

    parts: list[str] = []
    for cell in non_empty:
        sub_lines = [normalize_cell_text(ln) for ln in cell.splitlines()]
        sub_lines = [ln for ln in sub_lines if ln and not _RE_PURE_SEPARATOR.match(ln)]
        parts.extend(sub_lines)
    if not parts:
        return []
    combined = " | ".join(parts)
    source_badge = f"{file_path.name} [{location}]" if location else file_path.name
    return [
        _make_item(
            raw_text=combined,
            file_name=combined,
            parent_dir="",
            full_path="",
            source_name=source_badge,
            source_type=source_type,
            line_number=row_number,
            companion_extensions=companion_extensions,
        )
    ]


def _extract_location_and_line(entry: dict, fallback_idx: int) -> tuple[str, int]:
    """Extract location badge and row/line number from a doc_reader dict entry."""
    loc = str(
        entry.get("location")
        or entry.get("locator")
        or entry.get("source_location")
        or ""
    ).strip()
    if not loc:
        sheet = str(entry.get("sheet_name") or entry.get("sheet") or "").strip()
        row = entry.get("row_index") or entry.get("row") or entry.get("line_number")
        if sheet and row:
            loc = f"{sheet} 行{row}"
        elif sheet:
            loc = sheet
    raw_line = (
        entry.get("line_number")
        or entry.get("row_index")
        or entry.get("row")
        or entry.get("index")
        or fallback_idx
    )
    try:
        line_no = int(raw_line)
    except (TypeError, ValueError):
        line_no = fallback_idx
    return loc, line_no


_RE_WORD_TABLE_ROW_LOC = re.compile(r"^(表格\d+(?:\.\d+)*\s+行\d+)(?:\s*列\d+)?$")


def scan_word_file(
    file_path: Union[str, Path],
    companion_extensions: Optional[Collection[str]] = None,
) -> list[ScannedItem]:
    """Scan a Word `.docx` document (paragraphs, tables, headers/footers) into `ScannedItem`s."""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"找不到文件: {path}")
    ext = path.suffix.lower()
    if ext == ".doc":
        raise ValueError("暂不支持旧版 .doc 格式，请先用 Word 或 WPS 另存为 .docx 后再导入。")

    # Prefer shared `doc_reader.extract_word_text_items` when available.
    doc_entries: Optional[list[dict]] = None
    try:
        from .doc_reader import extract_word_text_items  # type: ignore[import-not-found]

        doc_entries = extract_word_text_items(path)
    except ImportError:
        doc_entries = None

    items: list[ScannedItem] = []
    if doc_entries is not None:
        # Group consecutive cells belonging to the same Word table row (e.g. "表格1 行2列1", "表格1 行2列2")
        # into a single row badge ("表格1 行2") so codes and tags stay associated without column noise.
        pending_row_loc: Optional[str] = None
        pending_row_line: int = 0
        pending_row_cells: list[str] = []

        def _flush_pending_row() -> None:
            nonlocal pending_row_loc, pending_row_line, pending_row_cells
            if pending_row_loc is not None and pending_row_cells:
                items.extend(
                    _coerce_row_cells_to_items(
                        pending_row_cells,
                        file_path=path,
                        location=pending_row_loc,
                        source_type="word",
                        row_number=pending_row_line,
                        companion_extensions=companion_extensions,
                    )
                )
            pending_row_loc = None
            pending_row_line = 0
            pending_row_cells = []

        for idx, entry in enumerate(doc_entries, start=1):
            if not isinstance(entry, dict):
                continue
            raw_val = entry.get("text")
            if raw_val is None:
                raw_val = entry.get("content", "")
            text = normalize_cell_text(raw_val)
            if not text:
                continue
            loc, line_no = _extract_location_and_line(entry, idx)
            kind = str(entry.get("kind", "")).lower()
            m_tbl = _RE_WORD_TABLE_ROW_LOC.match(loc)
            if kind == "table_cell" or m_tbl:
                row_loc = m_tbl.group(1) if m_tbl else loc
                if pending_row_loc == row_loc:
                    pending_row_cells.append(text)
                else:
                    _flush_pending_row()
                    pending_row_loc = row_loc
                    pending_row_line = line_no
                    pending_row_cells = [text]
            else:
                _flush_pending_row()
                items.extend(
                    _emit_doc_text_lines(
                        text,
                        file_path=path,
                        location=loc,
                        source_type="word",
                        line_number=line_no,
                        companion_extensions=companion_extensions,
                    )
                )
        _flush_pending_row()
        return items

    # Built-in fallback using python-docx directly.
    try:
        import docx  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError("缺少 python-docx 依赖，无法读取 Word (.docx) 文件。") from exc

    document = docx.Document(str(path))
    seq = 0

    # Headers and footers (deduplicated across linked sections).
    seen_hf: set[str] = set()
    for sec_idx, section in enumerate(document.sections, start=1):
        for hf_label, hf_obj in (("页眉", section.header), ("页脚", section.footer)):
            if hf_obj is None:
                continue
            for p in hf_obj.paragraphs:
                txt = normalize_cell_text(p.text)
                if not txt or txt in seen_hf:
                    continue
                seen_hf.add(txt)
                seq += 1
                items.extend(
                    _emit_doc_text_lines(
                        txt,
                        file_path=path,
                        location=f"{hf_label}{sec_idx}",
                        source_type="word",
                        line_number=seq,
                        companion_extensions=companion_extensions,
                    )
                )

    # Body paragraphs.
    for para_idx, para in enumerate(document.paragraphs, start=1):
        txt = normalize_cell_text(para.text)
        if not txt:
            continue
        items.extend(
            _emit_doc_text_lines(
                txt,
                file_path=path,
                location=f"段落{para_idx}",
                source_type="word",
                line_number=para_idx,
                companion_extensions=companion_extensions,
            )
        )

    # Embedded tables (deduplicating merged cells via underlying XML element identity).
    for tbl_idx, table in enumerate(document.tables, start=1):
        seen_tc: set[int] = set()
        for row_idx, row in enumerate(table.rows, start=1):
            row_cells: list[str] = []
            for cell in row.cells:
                tc_id = id(cell._tc)
                if tc_id in seen_tc:
                    continue
                seen_tc.add(tc_id)
                cell_txt = normalize_cell_text(cell.text)
                if cell_txt:
                    row_cells.append(cell_txt)
            items.extend(
                _coerce_row_cells_to_items(
                    row_cells,
                    file_path=path,
                    location=f"表格{tbl_idx} 行{row_idx}",
                    source_type="word",
                    row_number=row_idx,
                    companion_extensions=companion_extensions,
                )
            )

    return items


def scan_excel_file(
    file_path: Union[str, Path],
    sheet_names: Optional[Sequence[str]] = None,
    companion_extensions: Optional[Collection[str]] = None,
) -> list[ScannedItem]:
    """Scan an Excel `.xlsx`/`.xlsm` workbook into `ScannedItem`s.

    Handles multiple sheets, multi-line cells (`\\n`), merged cell ranges, and
    float-formatted numeric IDs (`1234567.0` -> `1234567`).
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"找不到文件: {path}")
    ext = path.suffix.lower()
    if ext == ".xls":
        raise ValueError("暂不支持旧版 .xls 格式，请先用 Excel 或 WPS 另存为 .xlsx 后再导入。")

    # Prefer shared `doc_reader.extract_excel_text_items` when available.
    excel_entries: Optional[list[dict]] = None
    try:
        from .doc_reader import extract_excel_text_items  # type: ignore[import-not-found]

        excel_entries = extract_excel_text_items(path, sheet_names=sheet_names)
    except ImportError:
        excel_entries = None

    items: list[ScannedItem] = []
    if excel_entries is not None:
        # When `doc_reader.extract_excel_text_items` provides `cells`, each entry is already a
        # complete row or pre-split single-cell multiline item — emit directly without re-merging.
        # Otherwise, group consecutive cell-level entries sharing `(location, line_no)`.
        pending_loc: Optional[str] = None
        pending_line: int = 0
        pending_cells: list[str] = []

        def _flush_pending() -> None:
            nonlocal pending_loc, pending_line, pending_cells
            if pending_loc is not None and pending_cells:
                items.extend(
                    _coerce_row_cells_to_items(
                        pending_cells,
                        file_path=path,
                        location=pending_loc,
                        source_type="excel",
                        row_number=pending_line,
                        companion_extensions=companion_extensions,
                    )
                )
            pending_loc = None
            pending_line = 0
            pending_cells = []

        for idx, entry in enumerate(excel_entries, start=1):
            if not isinstance(entry, dict):
                continue
            loc, line_no = _extract_location_and_line(entry, idx)
            raw_cells = entry.get("cells")
            if isinstance(raw_cells, (list, tuple)):
                _flush_pending()
                norm_cells = [normalize_cell_text(c) for c in raw_cells]
                norm_cells = [c for c in norm_cells if c]
                if not norm_cells:
                    continue
                items.extend(
                    _coerce_row_cells_to_items(
                        norm_cells,
                        file_path=path,
                        location=loc,
                        source_type="excel",
                        row_number=line_no,
                        companion_extensions=companion_extensions,
                    )
                )
                continue

            raw_val = entry.get("text")
            if raw_val is None:
                raw_val = entry.get("content", "")
            text = normalize_cell_text(raw_val)
            if not text:
                continue
            if pending_loc == loc and pending_line == line_no:
                pending_cells.append(text)
            else:
                _flush_pending()
                pending_loc = loc
                pending_line = line_no
                pending_cells = [text]

        _flush_pending()
        return items

    # Built-in fallback using openpyxl directly.
    try:
        import openpyxl  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError("缺少 openpyxl 依赖，无法读取 Excel (.xlsx) 文件。") from exc

    wb = openpyxl.load_workbook(str(path), data_only=True, read_only=False)
    try:
        target_sheets = list(sheet_names) if sheet_names is not None else list(wb.sheetnames)
        for sheet_name in target_sheets:
            if sheet_name not in wb.sheetnames:
                continue
            ws = wb[sheet_name]
            # Track non-origin coordinates of merged ranges so merged strings are never duplicated.
            merged_skip: set[tuple[int, int]] = set()
            for merged_range in ws.merged_cells.ranges:
                for r in range(merged_range.min_row, merged_range.max_row + 1):
                    for c in range(merged_range.min_col, merged_range.max_col + 1):
                        if r != merged_range.min_row or c != merged_range.min_col:
                            merged_skip.add((r, c))

            for row_idx, row in enumerate(ws.iter_rows(values_only=False), start=1):
                row_cells: list[str] = []
                for col_idx, cell in enumerate(row, start=1):
                    if (row_idx, col_idx) in merged_skip:
                        continue
                    cell_txt = normalize_cell_text(cell.value)
                    if cell_txt:
                        row_cells.append(cell_txt)
                items.extend(
                    _coerce_row_cells_to_items(
                        row_cells,
                        file_path=path,
                        location=f"{sheet_name} 行{row_idx}",
                        source_type="excel",
                        row_number=row_idx,
                        companion_extensions=companion_extensions,
                    )
                )
    finally:
        wb.close()

    return items


def _scan_tsv_text(
    text: str,
    source_name: str,
    companion_extensions: Optional[Collection[str]] = None,
) -> list[ScannedItem]:
    """Parse tab-separated `.tsv` files cleanly (supporting both Everything TSV and table TSV)."""
    items: list[ScannedItem] = []
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    rows = list(reader)
    if not rows:
        return items

    header = [c.strip().lower() for c in rows[0]]
    name_candidates = {"filename", "file", "name", "path", "full_path", "文件名", "路径", "编码", "原始文件名"}
    size_candidates = {"size", "file_size", "大小", "文件大小"}

    name_col: Optional[int] = None
    size_col: Optional[int] = None
    start_row = 0
    for i, col in enumerate(header):
        if col in name_candidates and name_col is None:
            name_col = i
            start_row = 1
        elif col in size_candidates and size_col is None:
            size_col = i
            start_row = 1

    for row_idx, row in enumerate(rows[start_row:], start=start_row + 1):
        cells = [normalize_cell_text(c) for c in row]
        non_empty = [c for c in cells if c and not _RE_PURE_SEPARATOR.match(c)]
        if not non_empty:
            continue

        file_size = 0
        if size_col is not None and size_col < len(cells):
            raw_sz = cells[size_col].replace(",", "")
            if raw_sz.isdigit():
                file_size = int(raw_sz)

        if name_col is not None and name_col < len(cells) and cells[name_col]:
            target = cells[name_col]
            file_name, parent_dir, full_path = _extract_path_components(target)
            items.append(
                _make_item(
                    raw_text=target,
                    file_name=file_name,
                    parent_dir=parent_dir,
                    full_path=full_path,
                    source_name=source_name,
                    source_type="tsv",
                    line_number=row_idx,
                    file_size=file_size,
                    companion_extensions=companion_extensions,
                )
            )
            continue

        # No explicit header match: preserve Everything-style path\tsize rows or join multi-column rows.
        first_cell = non_empty[0]
        _, parent_dir, full_path = _extract_path_components(first_cell)
        if full_path:
            for extra in non_empty[1:]:
                cleaned_sz = extra.replace(",", "")
                if _RE_SIZE_ONLY.match(extra) and cleaned_sz.isdigit():
                    file_size = int(cleaned_sz)
                    break
            file_name, parent_dir, full_path = _extract_path_components(first_cell)
            items.append(
                _make_item(
                    raw_text="\t".join(non_empty),
                    file_name=file_name,
                    parent_dir=parent_dir,
                    full_path=full_path,
                    source_name=source_name,
                    source_type="tsv",
                    line_number=row_idx,
                    file_size=file_size,
                    companion_extensions=companion_extensions,
                )
            )
        else:
            combined = " | ".join(non_empty) if len(non_empty) > 1 else first_cell
            items.append(
                _make_item(
                    raw_text=combined,
                    file_name=combined,
                    parent_dir="",
                    full_path="",
                    source_name=source_name,
                    source_type="tsv",
                    line_number=row_idx,
                    file_size=file_size,
                    companion_extensions=companion_extensions,
                )
            )

    return items


def scan_document_file(
    file_path: Union[str, Path],
    sheet_names: Optional[Sequence[str]] = None,
    companion_extensions: Optional[Collection[str]] = None,
) -> list[ScannedItem]:
    """Unified file scanner dispatching `.txt`, `.log`, `.csv`, `.tsv`, `.efu`, `.docx`, `.xlsx`, `.xlsm`."""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"找不到文件: {path}")

    ext = path.suffix.lower()
    if ext in {".docx", ".doc"}:
        return scan_word_file(path, companion_extensions=companion_extensions)
    if ext in {".xlsx", ".xlsm", ".xls"}:
        return scan_excel_file(
            path,
            sheet_names=sheet_names,
            companion_extensions=companion_extensions,
        )
    if ext == ".tsv":
        content, _ = read_text_auto_encoding(path)
        return _scan_tsv_text(
            content,
            source_name=path.name,
            companion_extensions=companion_extensions,
        )
    return scan_file(path, companion_extensions=companion_extensions)


def scan_files(
    file_paths: Sequence[Union[str, Path]],
    companion_extensions: Optional[Collection[str]] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> list[ScannedItem]:
    """Scan multiple text/CSV/TSV/Word/Excel/tree files sequentially with progress and cancellation hooks."""
    all_items: list[ScannedItem] = []
    total = len(file_paths)
    for idx, fp in enumerate(file_paths, start=1):
        if cancel_check and cancel_check():
            break
        p = Path(fp)
        all_items.extend(scan_document_file(p, companion_extensions=companion_extensions))
        if progress_callback:
            progress_callback(idx, total, p.name)
    return all_items


def scan_folder(
    folder_path: Union[str, Path],
    recursive: bool = True,
    companion_extensions: Optional[Collection[str]] = None,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> list[ScannedItem]:
    """Recursively scan a local directory and emit a `ScannedItem` per discovered file."""
    root = Path(folder_path)
    if not root.is_dir():
        raise NotADirectoryError(f"找不到目录: {root}")

    items: list[ScannedItem] = []
    source_name = str(root)
    count = 0

    def _on_walk_error(_err: OSError) -> None:
        # Silently skip unreadable system folders instead of aborting the whole scan.
        return

    for dirpath, dirnames, filenames in os.walk(root, onerror=_on_walk_error):
        if cancel_check and cancel_check():
            break

        # Prune ignored system directories in place.
        dirnames[:] = [
            d for d in dirnames if d.lower() not in _IGNORED_SYSTEM_DIRS
        ]
        if not recursive:
            dirnames.clear()

        current_dir = Path(dirpath)
        parent_name = current_dir.name

        for fname in filenames:
            if cancel_check and cancel_check():
                break
            full = current_dir / fname
            try:
                file_size = full.stat().st_size
            except OSError:
                file_size = 0

            _, ext = split_known_extension(fname)
            if not ext and "." in fname:
                ext = full.suffix.lower()

            items.append(
                ScannedItem(
                    raw_text=fname,
                    file_name=fname,
                    parent_dir=parent_name,
                    full_path=str(full),
                    source_name=source_name,
                    source_type="folder",
                    line_number=0,
                    file_size=file_size,
                    file_ext=ext,
                    is_companion=is_companion_extension(ext, companion_extensions),
                )
            )
            count += 1
            if progress_callback and count % 50 == 0:
                progress_callback(count, fname)

    if progress_callback:
        progress_callback(count, root.name)
    return items
