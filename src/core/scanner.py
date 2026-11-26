"""Multi-encoding TXT/tree/Everything/dir/CSV/TSV, Word (.docx), Excel (.xlsx/.xlsm),
and local folder recursive scanner.

Produces structured `ScannedItem` lists with parent directory or document-location
reconstruction and companion-file classification. All terminology is strictly neutral.
"""

from __future__ import annotations

import csv
import io
import os
import re
from pathlib import Path
from typing import Callable, Collection, Optional, Sequence, Union

from .io_utils import (
    normalize_cell_text,
    normalize_companion_extensions,
    read_text_auto_encoding,
)
from .models import ScannedItem
from .normalizer import split_known_extension

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
# Match 3-dash (`├───`/`└───`) or longer before 2-dash (`├──`/`└──`) cleanly via `{2,}`.
_RE_TREE_BRANCH = re.compile(r"^([│|\s]*)(?:[├└][─━-]{2,}|\+---|\\---)\s*(.+)$")
# Match leaf lines with vertical bars (including space-prefixed `    │   file.mp4`) or 2+ spaces.
_RE_TREE_LEAF = re.compile(r"^([\s│|]*[│|][\s│|]*|\s{2,})([^\s│|├└+\\].*)$")
_RE_TREE_HEADER = re.compile(
    r"^(?:Folder PATH listing|Volume serial number|卷\s+.+?\s+的文件夹\s*PATH\s*列表|卷序列号为)",
    re.IGNORECASE,
)
_RE_DRIVE_ROOT = re.compile(r"^[A-Za-z]:[\\/.]*$")

# Windows `dir` listing patterns (supports ISO/CJK YYYY/MM/DD, US MM/DD/YYYY AM/PM, EU DD.MM.YYYY, and 上午/下午).
_RE_DIR_HEADER_EN = re.compile(r"^\s*Directory of\s+(.+?)\s*$", re.IGNORECASE)
_RE_DIR_HEADER_CN = re.compile(r"^\s*(.+?)\s*的目录\s*$")
_RE_DIR_ENTRY = re.compile(
    r"^\s*(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})\s+"
    r"(?:(?:上午|下午)\s*)?\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?\s+"
    r"(<DIR>|[\d,.\u00a0]+)\s+(.+?)\s*$",
    re.IGNORECASE,
)
_RE_DIR_SUMMARY = re.compile(
    r"^\s*(?:\d+\s+(?:File\(s\)|Dir\(s\))|\d+\s+个文件|\d+\s+个目录|"
    r"Total Files Listed|所列文件总数|Volume in drive|驱动器\s+.+?\s+中的卷|Volume Serial Number|卷的序列号)",
    re.IGNORECASE,
)

# Separators, numeric size tokens, slash dates, and Word table row locators.
_RE_PURE_SEPARATOR = re.compile(r"^[\s\-_=*#~│├└+|\\/.]+$")
_RE_SIZE_ONLY = re.compile(r"^\d+(?:,\d{3})*$")
_RE_SLASH_DATE = re.compile(r"(?<![A-Za-z0-9])(?:19|20)\d{2}/\d{1,2}/\d{1,2}(?![A-Za-z0-9])")
_RE_WORD_TABLE_ROW_LOC = re.compile(r"^(表格\d+(?:\.\d+)*\s+行\d+)(?:\s*列\d+)?$")

# CSV / TSV column header candidates (tier 1 filename/code columns beat tier 2 path columns).
_PRIMARY_NAME_HEADERS: frozenset[str] = frozenset(
    {"filename", "file", "name", "文件名", "原始文件名", "编码", "项目编码", "编号", "代码", "code"}
)
_PATH_NAME_HEADERS: frozenset[str] = frozenset({"path", "full_path", "路径", "文件路径"})
_SIZE_HEADERS: frozenset[str] = frozenset({"size", "file_size", "大小", "文件大小"})


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
        if _RE_TREE_BRANCH.match(ln)
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
    """Extract `(file_name, parent_dir, full_path)` from a path-like string.

    Avoids misclassifying slash-separated codes (`ABC-123/DEF-456/GHI-789`),
    calendar dates (`ABC-123 2024/05/12 DEF-456`), or mid-line URLs as POSIX paths.
    """
    s = path_str.strip().strip('"').strip("'")
    if not s:
        return "", "", ""

    s_low = s.lower()
    if "http://" in s_low or "https://" in s_low:
        return s, "", ""

    is_win_path = (
        bool(re.match(r"^[A-Za-z]:[\\/]", s))
        or s.startswith("\\\\")
        or s.startswith("//")
        or ("\\" in s and " \\ " not in s)
    )
    is_posix_path = False
    if not is_win_path:
        if s.startswith("/") and " / " not in s:
            is_posix_path = True
        elif "/" in s and " / " not in s and not _RE_SLASH_DATE.search(s):
            if s.startswith(("./", "../")):
                is_posix_path = True
            else:
                last_seg = s.rsplit("/", 1)[-1]
                _, ext = split_known_extension(last_seg)
                if ext:
                    is_posix_path = True

    if is_win_path or is_posix_path:
        norm = s.replace("\\", "/").rstrip("/")
        # Strip leading UNC //server/share so share name does not become parent_dir.
        if s.startswith(("\\\\", "//")):
            unc_parts = [p for p in norm.split("/") if p]
            if len(unc_parts) <= 2:
                return s, "", s
            rel_parts = unc_parts[2:]
            file_name = rel_parts[-1]
            parent_dir = rel_parts[-2] if len(rel_parts) >= 2 else ""
            return file_name, parent_dir, s

        parts = [p for p in norm.split("/") if p]
        if not parts:
            return s, "", s
        file_name = parts[-1]
        parent_dir = ""
        if len(parts) >= 2 and not parts[-2].endswith(":"):
            parent_dir = parts[-2]
        return file_name, parent_dir, s

    return s, "", ""


def _is_single_file_candidate(file_name: str, full_path: str) -> bool:
    """Return True if a non-filesystem text item looks like a single filename or file path."""
    if full_path:
        return True
    s = file_name.strip()
    if not s or " | " in s or "://" in s:
        return False
    return not any(ch.isspace() for ch in s)


def _make_item(
    raw_text: str,
    file_name: str,
    parent_dir: str,
    full_path: str,
    source_name: str,
    source_type: str,
    line_number: int,
    file_size: int = 0,
    companion_set: Optional[frozenset[str]] = None,
    companion_extensions: Optional[Collection[str]] = None,
    is_file_entry: Optional[bool] = None,
) -> ScannedItem:
    comp_set = (
        companion_set
        if companion_set is not None
        else normalize_companion_extensions(companion_extensions)
    )
    if is_file_entry is None:
        is_file_entry = _is_single_file_candidate(file_name, full_path)

    ext = ""
    is_companion = False
    if is_file_entry:
        _, ext = split_known_extension(file_name)
        is_companion = bool(ext and ext in comp_set)

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
        is_companion=is_companion,
    )


def _scan_tree_lines(
    lines: Sequence[str],
    source_name: str,
    companion_extensions: Optional[Collection[str]] = None,
) -> list[ScannedItem]:
    """Parse Windows `tree /f`, `tree /f /a`, and Unix `tree` outputs with parent reconstruction."""
    comp_set = normalize_companion_extensions(companion_extensions)
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

        m_branch = _RE_TREE_BRANCH.match(line)
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

    for i, (line_no, raw, depth, name, _has_branch) in enumerate(tokens):
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
                companion_set=comp_set,
                is_file_entry=True,
            )
        )

    return items


def _scan_dir_lines(
    lines: Sequence[str],
    source_name: str,
    companion_extensions: Optional[Collection[str]] = None,
) -> list[ScannedItem]:
    """Parse Windows `dir` / `dir /s` text listings (CN, US AM/PM, and EU locales)."""
    comp_set = normalize_companion_extensions(companion_extensions)
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
                continue
            file_size = 0
            digits = re.sub(r"[,.\u00a0\s]", "", size_or_dir)
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
                    companion_set=comp_set,
                    is_file_entry=True,
                )
            )

    return items


def _detect_tabular_columns(header: Sequence[str]) -> tuple[Optional[int], Optional[int], bool]:
    """Detect `(name_col, size_col, header_matched)` with tier-1 filename priority over path."""
    name_col: Optional[int] = None
    path_col: Optional[int] = None
    size_col: Optional[int] = None
    header_matched = False

    for i, col in enumerate(header):
        if col in _PRIMARY_NAME_HEADERS:
            if name_col is None:
                name_col = i
            header_matched = True
        elif col in _PATH_NAME_HEADERS:
            if path_col is None:
                path_col = i
            header_matched = True
        elif col in _SIZE_HEADERS:
            if size_col is None:
                size_col = i
            header_matched = True

    if name_col is None:
        name_col = path_col
    return name_col, size_col, header_matched


def _scan_csv_text(
    text: str,
    source_name: str,
    companion_extensions: Optional[Collection[str]] = None,
) -> list[ScannedItem]:
    """Parse CSV and Everything EFU file lists without dropping codes in multi-column tables."""
    comp_set = normalize_companion_extensions(companion_extensions)
    items: list[ScannedItem] = []
    reader = csv.reader(io.StringIO(text))
    first_row = next(reader, None)
    if first_row is None:
        return items

    header = [c.strip().lower() for c in first_row]
    name_col, size_col, header_matched = _detect_tabular_columns(header)

    def _process_row(row_idx: int, row: list[str]) -> None:
        if not row:
            return
        file_size = 0
        if size_col is not None and size_col < len(row):
            raw_sz = row[size_col].strip().replace(",", "")
            if raw_sz.isdigit():
                file_size = int(raw_sz)

        if name_col is not None and name_col < len(row):
            cell = row[name_col].strip()
            if not cell or _RE_PURE_SEPARATOR.match(cell):
                return
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
                    companion_set=comp_set,
                )
            )
            return

        # No filename/path header matched: scan ALL columns (excluding numeric size column).
        non_empty: list[str] = []
        for col_idx, raw_cell in enumerate(row):
            if size_col is not None and col_idx == size_col:
                continue
            cleaned_cell = normalize_cell_text(raw_cell)
            if cleaned_cell and not _RE_PURE_SEPARATOR.match(cleaned_cell):
                non_empty.append(cleaned_cell)
        if not non_empty:
            return
        if len(non_empty) == 1:
            cell = non_empty[0]
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
                    companion_set=comp_set,
                )
            )
            return

        # Check if col 0 is an Everything-style full path with trailing numeric size.
        f0, p0, fp0 = _extract_path_components(non_empty[0])
        if fp0:
            if not file_size:
                for extra in non_empty[1:]:
                    cleaned_sz = extra.replace(",", "")
                    if _RE_SIZE_ONLY.match(extra) and cleaned_sz.isdigit():
                        file_size = int(cleaned_sz)
                        break
            items.append(
                _make_item(
                    raw_text=",".join(non_empty),
                    file_name=f0,
                    parent_dir=p0,
                    full_path=fp0,
                    source_name=source_name,
                    source_type="csv",
                    line_number=row_idx,
                    file_size=file_size,
                    companion_set=comp_set,
                    is_file_entry=True,
                )
            )
            return

        combined = " | ".join(non_empty)
        items.append(
            _make_item(
                raw_text=combined,
                file_name=combined,
                parent_dir="",
                full_path="",
                source_name=source_name,
                source_type="csv",
                line_number=row_idx,
                file_size=file_size,
                companion_set=comp_set,
                is_file_entry=False,
            )
        )

    if not header_matched:
        _process_row(1, first_row)
    for row_idx, row in enumerate(reader, start=2):
        _process_row(row_idx, row)

    return items


def _scan_plain_lines(
    lines: Sequence[str],
    source_name: str,
    source_type: str,
    companion_extensions: Optional[Collection[str]] = None,
) -> list[ScannedItem]:
    """Parse plain text lists, full file paths, and tab-separated Everything/Excel pastes."""
    comp_set = normalize_companion_extensions(companion_extensions)
    items: list[ScannedItem] = []

    for idx, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped or _RE_PURE_SEPARATOR.match(stripped):
            continue

        file_size = 0

        # Handle tab-separated Everything exports AND Excel copy-paste rows cleanly.
        if "\t" in stripped:
            cols = [c.strip() for c in stripped.split("\t") if c.strip()]
            if not cols:
                continue
            for extra in cols[1:]:
                cleaned_sz = extra.replace(",", "")
                if _RE_SIZE_ONLY.match(extra) and cleaned_sz.isdigit():
                    file_size = int(cleaned_sz)
                    break

            f0, p0, fp0 = _extract_path_components(cols[0])
            if fp0:
                items.append(
                    _make_item(
                        raw_text=stripped,
                        file_name=f0,
                        parent_dir=p0,
                        full_path=fp0,
                        source_name=source_name,
                        source_type=source_type,
                        line_number=idx,
                        file_size=file_size,
                        companion_set=comp_set,
                        is_file_entry=True,
                    )
                )
                continue

            if len(cols) == 1:
                items.append(
                    _make_item(
                        raw_text=stripped,
                        file_name=f0,
                        parent_dir=p0,
                        full_path=fp0,
                        source_name=source_name,
                        source_type=source_type,
                        line_number=idx,
                        file_size=file_size,
                        companion_set=comp_set,
                    )
                )
                continue

            # Multi-column pasted TSV (e.g. Excel rows `1\tABC-123\t4K 中文字幕`):
            # preserve ALL columns joined by ` | ` so codes & tags in any column are captured.
            path_parent = ""
            path_full = ""
            for col in cols:
                _, p_c, fp_c = _extract_path_components(col)
                if fp_c:
                    path_parent = p_c
                    path_full = fp_c
                    break
            combined = " | ".join(cols)
            items.append(
                _make_item(
                    raw_text=stripped,
                    file_name=combined,
                    parent_dir=path_parent,
                    full_path=path_full,
                    source_name=source_name,
                    source_type=source_type,
                    line_number=idx,
                    file_size=file_size,
                    companion_set=comp_set,
                    is_file_entry=False,
                )
            )
            continue

        file_name, parent_dir, full_path = _extract_path_components(stripped)
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
                companion_set=comp_set,
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


def _emit_doc_text_lines(
    raw_text: str,
    *,
    file_path: Path,
    location: str,
    source_type: str,
    line_number: int,
    companion_set: frozenset[str],
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
                companion_set=companion_set,
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
    companion_set: frozenset[str],
) -> list[ScannedItem]:
    """Convert non-empty normalized cells from a single spreadsheet/table row into ScannedItems.

    - Single non-empty cell: splits embedded newlines (`\\n`) into individual items so pasted
      multi-line lists or paths inside one cell scan cleanly.
    - Multiple non-empty cells: flattens internal newlines and joins row cells with `" | "`
      (never flagged as companion even if the trailing cell is `cover.jpg` or `readme.txt`).
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
            companion_set=companion_set,
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
            companion_set=companion_set,
            is_file_entry=False,
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

    from .doc_reader import extract_word_text_items

    comp_set = normalize_companion_extensions(companion_extensions)
    doc_entries = extract_word_text_items(path)

    items: list[ScannedItem] = []
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
                    companion_set=comp_set,
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
                    companion_set=comp_set,
                )
            )
    _flush_pending_row()
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

    from .doc_reader import extract_excel_text_items

    comp_set = normalize_companion_extensions(companion_extensions)
    excel_entries = extract_excel_text_items(path, sheet_names=sheet_names)

    items: list[ScannedItem] = []
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
                    companion_set=comp_set,
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
                    companion_set=comp_set,
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


def _scan_tsv_text(
    text: str,
    source_name: str,
    companion_extensions: Optional[Collection[str]] = None,
) -> list[ScannedItem]:
    """Parse tab-separated `.tsv` files cleanly (supporting both Everything TSV and table TSV)."""
    comp_set = normalize_companion_extensions(companion_extensions)
    items: list[ScannedItem] = []
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    first_row = next(reader, None)
    if first_row is None:
        return items

    header = [c.strip().lower() for c in first_row]
    name_col, size_col, header_matched = _detect_tabular_columns(header)

    def _process_row(row_idx: int, row: list[str]) -> None:
        cells = [normalize_cell_text(c) for c in row]
        non_empty = [c for c in cells if c and not _RE_PURE_SEPARATOR.match(c)]
        if not non_empty:
            return

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
                    companion_set=comp_set,
                )
            )
            return

        # No explicit header match: preserve Everything-style path\tsize rows or join multi-column rows.
        first_cell = non_empty[0]
        file_name, parent_dir, full_path = _extract_path_components(first_cell)
        if full_path:
            if not file_size:
                for extra in non_empty[1:]:
                    cleaned_sz = extra.replace(",", "")
                    if _RE_SIZE_ONLY.match(extra) and cleaned_sz.isdigit():
                        file_size = int(cleaned_sz)
                        break
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
                    companion_set=comp_set,
                    is_file_entry=True,
                )
            )
            return

        if len(non_empty) == 1:
            items.append(
                _make_item(
                    raw_text=first_cell,
                    file_name=first_cell,
                    parent_dir="",
                    full_path="",
                    source_name=source_name,
                    source_type="tsv",
                    line_number=row_idx,
                    file_size=file_size,
                    companion_set=comp_set,
                )
            )
            return

        combined = " | ".join(non_empty)
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
                companion_set=comp_set,
                is_file_entry=False,
            )
        )

    if not header_matched:
        _process_row(1, first_row)
    for row_idx, row in enumerate(reader, start=2):
        _process_row(row_idx, row)

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
    """Recursively scan a local directory using `os.scandir()` and emit a `ScannedItem` per file."""
    root = Path(folder_path)
    if not root.is_dir():
        raise NotADirectoryError(f"找不到目录: {root}")

    comp_set = normalize_companion_extensions(companion_extensions)
    items: list[ScannedItem] = []
    source_name = str(root)
    count = 0

    # Iterative DFS stack of `(dir_path, is_root)`.
    stack: list[tuple[Path, bool]] = [(root, True)]

    while stack:
        if cancel_check and cancel_check():
            break
        current_dir, is_root = stack.pop()
        parent_name = "" if is_root else current_dir.name
        subdirs: list[Path] = []

        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    if cancel_check and cancel_check():
                        break
                    name = entry.name
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if recursive and name.lower() not in _IGNORED_SYSTEM_DIRS:
                                subdirs.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue

                    try:
                        file_size = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        file_size = 0

                    _, ext = split_known_extension(name)
                    if not ext:
                        if name.startswith(".") and "." not in name[1:]:
                            ext = name.lower()
                        elif "." in name:
                            ext = Path(name).suffix.lower()

                    items.append(
                        ScannedItem(
                            raw_text=name,
                            file_name=name,
                            parent_dir=parent_name,
                            full_path=entry.path,
                            source_name=source_name,
                            source_type="folder",
                            line_number=0,
                            file_size=file_size,
                            file_ext=ext,
                            is_companion=bool(ext and ext in comp_set),
                        )
                    )
                    count += 1
                    if progress_callback and count % 50 == 0:
                        progress_callback(count, name)
        except OSError:
            # Silently skip unreadable system folders instead of aborting the whole scan.
            continue

        # Push subdirs in reverse sorted order for deterministic traversal.
        if subdirs:
            subdirs.sort(key=lambda p: p.name.lower(), reverse=True)
            for sub in subdirs:
                stack.append((sub, False))

    if progress_callback:
        progress_callback(count, root.name)
    return items
