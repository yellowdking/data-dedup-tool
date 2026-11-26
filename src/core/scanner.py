"""Multi-encoding TXT/tree/Everything/dir/CSV reader and local folder recursive scanner.

Produces structured `ScannedItem` lists with parent directory reconstruction and
companion-file classification. All terminology is strictly neutral.
"""

from __future__ import annotations

import csv
import io
import os
import re
from pathlib import Path
from typing import Callable, Collection, Optional, Sequence, Union

from .models import ScannedItem
from .normalizer import is_companion_extension, split_known_extension


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


def scan_files(
    file_paths: Sequence[Union[str, Path]],
    companion_extensions: Optional[Collection[str]] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> list[ScannedItem]:
    """Scan multiple text/CSV/tree files sequentially with progress and cancellation hooks."""
    all_items: list[ScannedItem] = []
    total = len(file_paths)
    for idx, fp in enumerate(file_paths, start=1):
        if cancel_check and cancel_check():
            break
        p = Path(fp)
        all_items.extend(scan_file(p, companion_extensions=companion_extensions))
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
