"""Shared lightweight I/O, encoding detection, cell formatting, and extension constants.

Single source of truth imported by `config.py`, `normalizer.py`, `scanner.py`,
`doc_reader.py`, and exporters to eliminate duplication and circular imports.
All terminology is strictly neutral.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Collection, Optional, Union

# Canonical list of companion/sidecar file extensions (single source of truth).
DEFAULT_COMPANION_EXTENSIONS_LIST: tuple[str, ...] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".webp",
    ".tiff",
    ".tif",
    ".ico",
    ".svg",
    ".nfo",
    ".srt",
    ".ass",
    ".ssa",
    ".sub",
    ".idx",
    ".vtt",
    ".sup",
    ".lrc",
    ".txt",
    ".log",
    ".url",
    ".lnk",
    ".torrent",
    ".ini",
    ".cfg",
    ".db",
    ".sqlite",
    ".bak",
    ".json",
    ".xml",
    ".md",
    ".html",
    ".htm",
    ".mht",
    ".sfv",
    ".md5",
    ".sha1",
    ".sha256",
    ".par2",
    ".ds_store",
    ".thumbs",
    ".m3u",
    ".m3u8",
    ".cue",
)

DEFAULT_COMPANION_EXTENSIONS: frozenset[str] = frozenset(DEFAULT_COMPANION_EXTENSIONS_LIST)

# Legacy CJK encodings evaluated when UTF-8 decoding fails.
_LEGACY_CJK_ENCODINGS: tuple[str, ...] = ("gb18030", "cp932", "big5")

_RE_FLOAT_INT_STR = re.compile(r"^(-?\d+)\.0+$")


def normalize_companion_extensions(
    companion_exts: Optional[Collection[str]] = None,
) -> frozenset[str]:
    """Normalize a collection of extensions to lowercase dot-prefixed frozenset."""
    if companion_exts is None:
        return DEFAULT_COMPANION_EXTENSIONS
    if isinstance(companion_exts, frozenset) and all(
        e.startswith(".") and e == e.lower() for e in companion_exts
    ):
        return companion_exts
    result: set[str] = set()
    for raw in companion_exts:
        item = str(raw).strip().lower()
        if not item:
            continue
        result.add(item if item.startswith(".") else f".{item}")
    return frozenset(result)


def _score_decoded_text(text: str, encoding: str) -> float:
    """Heuristically score a decoded legacy CJK text sample to distinguish Shift-JIS vs GB18030 vs Big5."""
    sample = text[:8192]
    score = 0.0
    non_ascii_count = 0

    for ch in sample:
        cp = ord(ch)
        if cp < 0x80:
            continue
        non_ascii_count += 1

        # Penalize C1 control characters and Private Use Area strongly.
        if (0x80 <= cp <= 0x9F) or (0xE000 <= cp <= 0xF8FF) or cp == 0xFFFD:
            score -= 10.0
            continue

        if encoding == "cp932":
            # Genuine Japanese text uses Hiragana/Katakana frequently and rarely half-width katakana
            # (whereas GBK Chinese decoded as cp932 produces dense half-width katakana).
            if 0x3040 <= cp <= 0x30FF:
                score += 4.5
            elif 0xFF61 <= cp <= 0xFF9F:
                score -= 5.0
            elif 0x3000 <= cp <= 0x303F or 0xFF01 <= cp <= 0xFF5E:
                score += 1.5
            elif 0x4E00 <= cp <= 0x9FFF:
                score += 1.2
            else:
                score -= 2.0
        elif encoding == "gb18030":
            # Common Simplified Chinese & punctuation live in GB2312; Shift-JIS decoded as GB18030
            # maps to rare GBK-extension characters outside GB2312.
            try:
                ch.encode("gb2312")
                in_gb2312 = True
            except UnicodeEncodeError:
                in_gb2312 = False
            if in_gb2312:
                score += 2.5
            elif 0x3040 <= cp <= 0x30FF:
                score += 1.0
            else:
                score -= 2.5
        elif encoding == "big5":
            # Traditional Chinese common ideographs & full-width punctuation.
            if 0x3000 <= cp <= 0x303F or 0xFF01 <= cp <= 0xFF5E:
                score += 1.5
            elif 0x4E00 <= cp <= 0x9FFF:
                try:
                    raw_b5 = ch.encode("big5")
                    # Big5 Level 1 common characters: 0xA440 - 0xC67E.
                    if len(raw_b5) == 2 and 0xA4 <= raw_b5[0] <= 0xC6:
                        score += 2.0
                    else:
                        score -= 1.0
                except UnicodeEncodeError:
                    score -= 2.0
            else:
                score -= 2.0

    if non_ascii_count == 0:
        return 0.0
    # Slight tie-breaker favoring gb18030 for ambiguous pure-Hanzi Simplified Windows files.
    if encoding == "gb18030":
        score += 0.25
    return score


def decode_bytes_auto(raw_bytes: bytes) -> tuple[str, str]:
    """Decode raw bytes safely with BOM detection (UTF-32/UTF-16/UTF-8) and CJK disambiguation.

    Never raises `UnicodeDecodeError`; corrupt BOM files safely fall back.
    Returns `(decoded_text, detected_encoding_label)`.
    """
    if not raw_bytes:
        return "", "utf-8"

    # 1. Explicit BOM checks (check 4-byte UTF-32 BOMs BEFORE 2-byte UTF-16 BOMs!).
    if raw_bytes.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        try:
            return raw_bytes.decode("utf-32"), "utf-32"
        except UnicodeDecodeError:
            return raw_bytes.decode("utf-32", errors="replace"), "utf-32-replace"

    if raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw_bytes.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            return raw_bytes.decode("utf-16", errors="replace"), "utf-16-replace"

    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        try:
            return raw_bytes.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            # Corrupt byte inside a UTF-8-BOM file: fall back to lossy utf-8-sig directly
            # rather than misinterpreting BOM bytes as legacy CJK characters.
            return raw_bytes.decode("utf-8-sig", errors="replace"), "utf-8-replace"

    # 2. Strict UTF-8 (no BOM).
    try:
        return raw_bytes.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass

    # 3. Try legacy CJK encodings and disambiguate when multiple succeed (e.g. Shift-JIS vs GB18030).
    candidates: list[tuple[float, str, str]] = []
    for enc in _LEGACY_CJK_ENCODINGS:
        try:
            decoded = raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
        candidates.append((_score_decoded_text(decoded, enc), enc, decoded))

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        _, best_enc, best_text = candidates[0]
        return best_text, best_enc

    # 4. Try UTF-16 without BOM if even length and NUL bytes indicate UTF-16.
    if len(raw_bytes) >= 4 and len(raw_bytes) % 2 == 0 and b"\x00" in raw_bytes[:64]:
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                decoded = raw_bytes.decode(enc)
                if "\x00" not in decoded[:256]:
                    return decoded, enc
            except UnicodeDecodeError:
                continue

    # 5. Final lossy UTF-8 fallback (strip corrupt UTF-16/UTF-32 BOMs if present).
    if raw_bytes.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return raw_bytes.decode("utf-32", errors="replace"), "utf-32-replace"
    if raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw_bytes.decode("utf-16", errors="replace"), "utf-16-replace"
    return raw_bytes.decode("utf-8", errors="replace"), "utf-8-replace"


def read_text_auto_encoding(
    file_path: Union[str, Path],
    max_bytes: Optional[int] = None,
) -> tuple[str, str]:
    """Read a text file with automatic BOM and multi-encoding detection.

    If `max_bytes` is specified and the file exceeds `max_bytes`, raises `ValueError`.
    """
    path = Path(file_path)
    if max_bytes is not None:
        size = path.stat().st_size
        if size > max_bytes:
            raise ValueError(
                f"文件体积过大 ({size / (1024 * 1024):.1f} MB)，超过安全上限 ({max_bytes / (1024 * 1024):.1f} MB): {path.name}"
            )
    raw_bytes = path.read_bytes()
    return decode_bytes_auto(raw_bytes)


def format_cell_value(value: Any, *, strip_float_int_str: bool = False) -> str:
    """Convert an Excel/Word/Python cell value into a clean display string.

    - `None` -> `""`
    - `bool` -> `"TRUE"` / `"FALSE"`
    - `int` -> `str(value)`
    - whole-number `float` (e.g. `1234567.0`) -> `"1234567"`; real decimals (e.g. `1234567.5`, `3.14`)
      are preserved without scientific notation (`str(value)` instead of `format(value, "g")`)
    - `Decimal` -> normalized non-scientific string (whole numbers stripped of `.0`)
    - `datetime` / `date` / `time` -> ISO string
    - normalizes CRLF/CR to LF and strips outer whitespace
    - when `strip_float_int_str=True`, stringified float-ints like `"1234567.0"` become `"1234567"`
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
        return str(value)
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite():
            return ""
        if value == value.to_integral_value():
            return str(int(value))
        return format(value.normalize(), "f")
    if isinstance(value, _dt.datetime):
        if (
            value.hour == 0
            and value.minute == 0
            and value.second == 0
            and value.microsecond == 0
        ):
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, _dt.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, _dt.time):
        return value.strftime("%H:%M:%S")

    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    if strip_float_int_str:
        m = _RE_FLOAT_INT_STR.match(text)
        if m:
            return m.group(1)
    return text


def normalize_cell_text(value: Any) -> str:
    """Scanner-oriented cell formatter (strips stringified float-int suffixes like `'1234567.0'`)."""
    return format_cell_value(value, strip_float_int_str=True)
