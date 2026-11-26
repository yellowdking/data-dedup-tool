"""Text pre-cleaning, noise/false-positive filtering, tag/part extraction, and canonical normalization.

All identifiers, labels, and documentation use strictly neutral terminology.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Collection, Optional


# Extensions typically representing sidecar/companion files rather than primary items.
DEFAULT_COMPANION_EXTENSIONS: frozenset[str] = frozenset(
    {
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
        ".sfv",
        ".md5",
        ".sha1",
        ".sha256",
        ".ds_store",
        ".thumbs",
        ".m3u",
        ".m3u8",
        ".cue",
    }
)

# Extensions representing primary media or archive files (stripped before parsing).
PRIMARY_FILE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp4",
        ".mkv",
        ".avi",
        ".wmv",
        ".mov",
        ".flv",
        ".ts",
        ".m2ts",
        ".rmvb",
        ".rm",
        ".webm",
        ".mpg",
        ".mpeg",
        ".m4v",
        ".iso",
        ".vob",
        ".3gp",
        ".f4v",
        ".divx",
        ".asf",
        ".ogm",
        ".strm",
        ".zip",
        ".rar",
        ".7z",
        ".tar",
        ".gz",
    }
)

ALL_KNOWN_EXTENSIONS: frozenset[str] = DEFAULT_COMPANION_EXTENSIONS | PRIMARY_FILE_EXTENSIONS

# Exact technical tokens that must never be recognized as catalog codes.
FALSE_POSITIVE_CODES: frozenset[str] = frozenset(
    {
        "H264",
        "H265",
        "H-264",
        "H-265",
        "X264",
        "X265",
        "X-264",
        "X-265",
        "HEVC",
        "AVC",
        "AVC1",
        "HVC1",
        "HEV1",
        "AV1",
        "AV01",
        "VP8",
        "VP9",
        "VP09",
        "MP4V",
        "MP4A",
        "MPEG2",
        "MPEG4",
        "MPEG-2",
        "MPEG-4",
        "DIVX",
        "XVID",
        "AAC",
        "AAC2",
        "AC3",
        "EAC3",
        "DTS",
        "DTS5",
        "DTS51",
        "DTS71",
        "DD5",
        "DDP51",
        "DDP20",
        "FLAC",
        "BT2020",
        "BT-2020",
        "BT709",
        "BT-709",
        "BT601",
        "BT-601",
        "REC2020",
        "REC-2020",
        "REC709",
        "REC-709",
        "REC601",
        "HLG10",
        "PQ10",
        "MP3",
        "MP4",
        "MKV",
        "AVI",
        "WMV",
        "MOV",
        "M4V",
        "M2TS",
        "TS",
        "ISO",
        "FLV",
        "WEBM",
        "RMVB",
        "1080P",
        "2160P",
        "4320P",
        "1440P",
        "720P",
        "540P",
        "480P",
        "360P",
        "240P",
        "1080I",
        "720I",
        "60FPS",
        "30FPS",
        "24FPS",
        "25FPS",
        "50FPS",
        "120FPS",
        "10BIT",
        "8BIT",
        "12BIT",
        "HDR10",
        "4K",
        "8K",
        "2K",
        "4K60",
        "4K30",
        "FHD",
        "UHD",
        "QHD",
        "HD",
        "SD",
        "WIN7",
        "WIN8",
        "WIN10",
        "WIN11",
        "WIN32",
        "WIN64",
        "WINXP",
        "WIN95",
        "WIN98",
        "WIN-10",
        "WIN-11",
        "X86",
        "X64",
        "ARM64",
        "UTF8",
        "UTF-8",
        "UTF16",
        "UTF-16",
        "UTF32",
        "UTF-32",
        "GBK",
        "GB2312",
        "GB18030",
        "ASCII",
        "MD5",
        "SHA1",
        "SHA-1",
        "SHA256",
        "SHA-256",
        "SHA512",
        "SHA-512",
        "CRC32",
        "HTTP",
        "HTTPS",
        "WWW",
        "COM",
        "NET",
        "ORG",
        "CD1",
        "CD2",
        "CD3",
        "CD4",
        "PART1",
        "PART2",
        "PART3",
        "DISC1",
        "DISC2",
        "DISK1",
        "DISK2",
    }
)

# Prefixes that represent technical identifiers, timestamps, camera names, or split markers.
FALSE_POSITIVE_PREFIXES: frozenset[str] = frozenset(
    {
        "H",
        "X",
        "V",
        "H264",
        "H265",
        "X264",
        "X265",
        "MPEG",
        "MP",
        "UTF",
        "GB",
        "SHA",
        "CRC",
        "WIN",
        "HTTP",
        "HTTPS",
        "WWW",
        "FPS",
        "HDR",
        "AAC",
        "BIT",
        "KHZ",
        "KBPS",
        "MBPS",
        "CD",
        "PART",
        "PT",
        "DISC",
        "DISK",
        "EP",
        "VOL",
        "VER",
        "REV",
        "IMG",
        "DSC",
        "DCIM",
        "VID",
        "MOV",
        "WEBRIP",
        "WEBDL",
        "BDRIP",
        "DVDRIP",
        "HDRIP",
        "BLURAY",
        "HDTV",
        "REMUX",
        "HEVC",
        "AVC",
        "FLAC",
    }
)

# Segment tokens that cannot act as middle/leading parts of a 3-segment code.
NON_SEGMENT_TOKENS: frozenset[str] = frozenset(
    {
        "HD",
        "FHD",
        "UHD",
        "QHD",
        "SD",
        "VR",
        "4K",
        "8K",
        "CD",
        "PT",
        "PART",
        "DISC",
        "DISK",
        "CH",
        "CHS",
        "CHT",
        "UC",
        "SUB",
        "SUBS",
        "RAW",
        "FULL",
        "LEAK",
        "NEW",
        "OLD",
        "TOP",
        "VIP",
        "WEB",
        "COM",
        "NET",
        "ORG",
        "MP4",
        "MKV",
        "AVI",
        "WMV",
        "ROOT",
        "FILE",
        "COPY",
        "TEST",
        "DATA",
        "ITEM",
        "CLIP",
        "BEST",
        "FREE",
        "SITE",
        "MAIN",
        "DEMO",
        "TEMP",
        "WORK",
        "LIST",
        "CODE",
        "VIDEO",
        "MOVIE",
        "MEDIA",
        "HOME",
        "USER",
        "SAVE",
    }
)

# Common top-level domains used to strip site/domain watermarks cleanly.
_TLD_PATTERN = (
    r"com|net|org|cc|me|la|tv|xyz|top|vip|club|info|co|pw|ru|io|app|pro|"
    r"site|online|live|link|fun|icu|cn|jp|tw|hk|uk|us|de|fr|nl|to|gg|ws|bz|su|in|work"
)

_RE_URL_HOST = re.compile(r"https?://[^\s/]+(?:/|$)", re.IGNORECASE)
_RE_DOMAIN_AT = re.compile(
    rf"(?:^|[\s\[\(【（])[a-z0-9](?:[a-z0-9-]{{0,40}}[a-z0-9])?"
    rf"(?:\.[a-z0-9-]+)*\.(?:{_TLD_PATTERN})@",
    re.IGNORECASE,
)
_RE_DOMAIN_PREFIX = re.compile(
    rf"(?<![a-z0-9])(?:www\.)?[a-z0-9](?:[a-z0-9-]{{0,40}}[a-z0-9])?"
    rf"(?:\.[a-z0-9-]+)*\.(?:{_TLD_PATTERN})(?![a-z0-9])[-_.\s@]*",
    re.IGNORECASE,
)
_RE_FILE_SIZE = re.compile(
    r"(?<![A-Za-z0-9])\d+(?:\.\d+)?\s*(?:TB|GB|MB|KB|BYTES)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_RE_ISO_DATE = re.compile(
    r"(?<![A-Za-z0-9])(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?(?![A-Za-z0-9])"
)
_RE_TECH_NOISE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"H\.?264|H\.?265|X\.?264|X\.?265|HEVC|AVC1?|HVC1|HEV1|AV0?1|VP0?[89]|MP4[VA]|MPEG[24]|DIVX|XVID|"
    r"BT\.?(?:2020|709|601)|REC\.?(?:2020|709|601)|HLG10|PQ10|"
    r"AAC2?\.?0?|AC3|EAC3|DTS(?:-?HD|5\.?1|7\.?1)?|FLAC|DDP?[25]\.?[01]|"
    r"4320P|2160P|1440P|1080P|1080I|720P|720I|540P|480P|360P|240P|"
    r"120FPS|60FPS|50FPS|30FPS|25FPS|24FPS|12BIT|10BIT|8BIT|HDR10\+?|SDR|"
    r"WEB-?DL|WEBRIP|BDRIP|BLURAY|BLU-RAY|DVDRIP|HDRIP|HDTV|REMUX|"
    r"WIN(?:11|10|8|7|64|32|XP)|UTF-?(?:8|16|32)|GB(?:K|2312|18030)|"
    r"MD5|SHA-?(?:1|256|512)|CRC32|4K60|4K30|UHD|FHD|QHD|"
    r"FULL(?:\s*HD)?|SAMPLE|TRAILER|PREVIEW"
    r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_RE_STANDALONE_QUALITY = re.compile(
    r"(?:^|[\s\[\(【（\-_])(?:8K|4K|2K|HD|SD)(?=$|[\s\]\)】）\-_])",
    re.IGNORECASE,
)
_RE_DASH_VARIANTS = re.compile(r"[‐‑‒–—―−－]")
_RE_BRACKETS = re.compile(r"[\[\]【】\(\)（）\{\}《》<>「」『』]")
_RE_MULTI_SPACE = re.compile(r"\s+")

# Tag detection patterns (all emitted tag names are strictly neutral).
_RE_TAG_8K = re.compile(r"(?<![A-Za-z0-9])(?:8K|4320P)(?![A-Za-z0-9])", re.IGNORECASE)
_RE_TAG_4K = re.compile(
    r"(?<![A-Za-z0-9])(?:4K|4K2|2160P|UHD)(?![A-Za-z0-9])|(?<![A-Za-z0-9])4K2_",
    re.IGNORECASE,
)
_RE_TAG_FHD = re.compile(r"(?<![A-Za-z0-9])(?:FHD|1080P)(?![A-Za-z0-9])", re.IGNORECASE)
_RE_TAG_HD = re.compile(r"(?<![A-Za-z0-9])(?:720P|HD)(?![A-Za-z0-9])", re.IGNORECASE)
_RE_TAG_VR = re.compile(r"(?<![A-Za-z0-9])VR(?![A-Za-z0-9])", re.IGNORECASE)
_RE_TAG_SUB_CN = re.compile(r"中文字幕|双语字幕|内嵌字幕|外挂字幕|硬字幕|字幕版|中字|简中|繁中")
_RE_TAG_SUB_EN = re.compile(
    r"(?:^|[-_.\s\[\(【（])(?:CH|CHS|CHT|SUB|SUBS|ZH-?CN)(?=$|[-_.\s\]\)】）])",
    re.IGNORECASE,
)
_RE_TAG_SPECIAL_TOKEN = re.compile(
    r"(?:^|[-_.\s\[\(【（])(?:UC|UNC[A-Z]*|LEAK|SPECIAL|RAW-?ORIG)(?=$|[-_.\s\]\)】）])",
    re.IGNORECASE,
)
_RE_SUFFIX_UC = re.compile(r"^[-_.\s]*(?:UC)(?=$|[-_.\s\]\)】）])", re.IGNORECASE)
_RE_SUFFIX_C = re.compile(r"^[-_.\s]*(?:CH|CHS|CHT|C)(?=$|[-_.\s\]\)】）])", re.IGNORECASE)
_RE_SUFFIX_U = re.compile(r"^[-_.\s]*(?:U|UNC[A-Z]*|LEAK)(?=$|[-_.\s\]\)】）])", re.IGNORECASE)

# Multi-part extraction patterns.
_RE_PART_CD = re.compile(r"(?<![A-Za-z0-9])CD[-_\s]?0?([1-9]\d?)(?![A-Za-z0-9])", re.IGNORECASE)
_RE_PART_WORD = re.compile(
    r"(?<![A-Za-z0-9])(?:PART|PT)[-_\s]?0?([1-9]\d?)(?![A-Za-z0-9])", re.IGNORECASE
)
_RE_PART_DISC = re.compile(
    r"(?<![A-Za-z0-9])(?:DISC|DISK)[-_\s]?0?([1-9]\d?)(?![A-Za-z0-9])", re.IGNORECASE
)
_RE_PART_CN_NUM = re.compile(r"第\s*0?([1-9]\d?)\s*[集部段卷话]")
_RE_PART_CN_UPPER = re.compile(r"(?:上集|上部|上卷|（上）|\(上\)|\[上\]|【上】)")
_RE_PART_CN_MIDDLE = re.compile(r"(?:中集|中部|中卷|（中）|\(中\)|\[中\]|【中】)")
_RE_PART_CN_LOWER = re.compile(r"(?:下集|下部|下卷|（下）|\(下\)|\[下\]|【下】)")
_RE_SUFFIX_LETTER_PART = re.compile(
    r"^(?:[-_.\s]+([ABDEF])|([ABDEF]))(?=$|[-_.\s\]\)】）])", re.IGNORECASE
)
_RE_SUFFIX_NUM_PART = re.compile(
    r"^(?:[-_]([1-9])(?=$|[-_.\s\]\)】）]))"
)
_RE_SUFFIX_COMBINED_PART = re.compile(
    r"(?:^|[-_.\s])(?:(?:CD|PART|PT|DISC|DISK)[-_\s]?0?([1-9]\d?)|([ABDEF]))(?=$|[-_.\s\]\)】）])",
    re.IGNORECASE,
)


def normalize_width(text: str) -> str:
    """Convert full-width ASCII variants and Unicode dashes to standard ASCII."""
    if not text:
        return ""
    chars: list[str] = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            chars.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - 0xFEE0))
        else:
            chars.append(ch)
    normalized = "".join(chars)
    normalized = unicodedata.normalize("NFC", normalized)
    normalized = _RE_DASH_VARIANTS.sub("-", normalized)
    return normalized


def split_known_extension(text: str) -> tuple[str, str]:
    """Split off a trailing known file extension if present.

    Avoids mangling non-extension dots such as version numbers or dotted dates.
    Returns `(stem, lowercase_ext_with_dot)`.
    """
    stripped = text.strip()
    if not stripped or "." not in stripped:
        return stripped, ""
    dot_idx = stripped.rfind(".")
    if dot_idx <= 0:
        return stripped, ""
    ext = stripped[dot_idx:].lower()
    if ext in ALL_KNOWN_EXTENSIONS:
        return stripped[:dot_idx], ext
    return stripped, ""


def is_companion_extension(
    ext: str,
    companion_exts: Optional[Collection[str]] = None,
) -> bool:
    """Return True if the given extension is treated as a companion/sidecar file."""
    if not ext:
        return False
    norm = ext.lower() if ext.startswith(".") else f".{ext.lower()}"
    if companion_exts is None:
        return norm in DEFAULT_COMPANION_EXTENSIONS
    normalized_set = {
        e.lower() if e.startswith(".") else f".{e.lower()}" for e in companion_exts
    }
    return norm in normalized_set


def is_false_positive(prefix: str, number: str, raw_token: str = "") -> bool:
    """Determine whether a candidate `(prefix, number)` is technical noise."""
    p_up = prefix.upper().strip("-_ ")
    n_up = number.upper().strip("-_ ")
    raw_up = raw_token.upper().strip()
    compact = f"{p_up}{n_up}"
    hyphen = f"{p_up}-{n_up}"

    if raw_up in FALSE_POSITIVE_CODES or compact in FALSE_POSITIVE_CODES or hyphen in FALSE_POSITIVE_CODES:
        return True
    if p_up in FALSE_POSITIVE_PREFIXES:
        return True
    # Catch leading digits + tech suffix like 60FPS, 10BIT, 1080P.
    alpha_tail = re.sub(r"^\d+", "", p_up)
    if alpha_tail and alpha_tail in {"FPS", "BIT", "KHZ", "KBPS", "MBPS", "GB", "MB", "KB", "TB"}:
        return True
    return False


def normalize_serial_number(number: str, from_raw_cid: bool = False) -> str:
    """Normalize serial number zero-padding consistently.

    - Raw compact vendor IDs (e.g. `00123`, `00001`) strip excess zeros down to >=3 digits.
    - Explicit codes with over-padded 5+ digit leading zeros (e.g. `000123`) strip to >=3 digits.
    - 2-digit (`12`), 3-digit (`001`), and 4-digit (`0123`, `1234`) explicit serials are preserved.
    """
    digits = number.strip()
    if not digits.isdigit():
        return digits.upper()
    if from_raw_cid:
        return str(int(digits)).zfill(3)
    if len(digits) >= 5 and digits.startswith("0"):
        return str(int(digits)).zfill(3)
    return digits


def is_plausible_six_digit_date(six_digits: str) -> bool:
    """Validate whether a 6-digit string is a plausible MMDDYY or YYMMDD date index."""
    if len(six_digits) != 6 or not six_digits.isdigit():
        return False
    a, b, c = int(six_digits[0:2]), int(six_digits[2:4]), int(six_digits[4:6])
    # MMDDYY: month 01-12, day 01-31
    mmddyy = (1 <= a <= 12) and (1 <= b <= 31)
    # YYMMDD: month 01-12, day 01-31
    yymmdd = (1 <= b <= 12) and (1 <= c <= 31)
    return mmddyy or yymmdd


def extract_tags(text: str, suffix: str = "", prefix: str = "") -> list[str]:
    """Extract normalized neutral version/feature tags from raw text and matched suffix."""
    tags: list[str] = []

    def _add(tag: str) -> None:
        if tag not in tags:
            tags.append(tag)

    norm_text = normalize_width(text)
    norm_suffix = normalize_width(suffix)
    prefix_up = prefix.upper()

    if _RE_TAG_8K.search(norm_text):
        _add("8K")
    elif _RE_TAG_4K.search(norm_text):
        _add("4K")
    elif _RE_TAG_FHD.search(norm_text):
        _add("FHD")
    elif _RE_TAG_HD.search(norm_text) and not prefix_up.endswith("HD"):
        _add("HD")

    if _RE_TAG_VR.search(norm_text) or (len(prefix_up) >= 4 and prefix_up.endswith("VR")):
        _add("VR")

    # Subtitle & special-edition markers from full text and immediate suffix.
    if _RE_TAG_SUB_CN.search(norm_text) or _RE_TAG_SUB_EN.search(norm_text):
        _add("字幕")

    if _RE_TAG_SPECIAL_TOKEN.search(norm_text):
        _add("特殊版")
        if re.search(r"(?:^|[-_.\s\[\(【（])UC(?=$|[-_.\s\]\)】）])", norm_text, re.IGNORECASE):
            _add("字幕")

    if norm_suffix:
        # Check combined suffix patterns such as -UC, -C, -CH, -U, -C-CD1, _CD1_C.
        if _RE_SUFFIX_UC.search(norm_suffix):
            _add("特殊版")
            _add("字幕")
        else:
            if _RE_SUFFIX_C.search(norm_suffix):
                _add("字幕")
            if _RE_SUFFIX_U.search(norm_suffix):
                _add("特殊版")
        # Also catch trailing -C / -UC after part token (e.g. ABC-123_CD1-C).
        if re.search(r"[-_.](?:UC)(?=$|[-_.\s\]\)】）])", norm_suffix, re.IGNORECASE):
            _add("特殊版")
            _add("字幕")
        elif re.search(r"[-_.](?:CH|CHS|CHT|C)(?=$|[-_.\s\]\)】）])", norm_suffix, re.IGNORECASE):
            _add("字幕")
        elif re.search(r"[-_.](?:U)(?=$|[-_.\s\]\)】）])", norm_suffix, re.IGNORECASE):
            _add("特殊版")

    return tags


def extract_part(text: str, suffix: str = "") -> str:
    """Extract normalized multi-part indicator (e.g. CD1, Part2, A, B) if present."""
    norm_text = normalize_width(text)
    norm_suffix = normalize_width(suffix)

    # Prefer explicit part tokens in suffix first, then anywhere in text.
    for target in (norm_suffix, norm_text):
        if not target:
            continue
        m = _RE_PART_CD.search(target)
        if m:
            return f"CD{int(m.group(1))}"
        m = _RE_PART_WORD.search(target)
        if m:
            return f"Part{int(m.group(1))}"
        m = _RE_PART_DISC.search(target)
        if m:
            return f"Disc{int(m.group(1))}"

    # Check Chinese part indicators.
    m = _RE_PART_CN_NUM.search(norm_text)
    if m:
        return f"Part{int(m.group(1))}"
    if _RE_PART_CN_UPPER.search(norm_text):
        return "Part1"
    if _RE_PART_CN_MIDDLE.search(norm_text):
        return "Part2"
    if _RE_PART_CN_LOWER.search(norm_text):
        return "Part2"

    # Immediate suffix after the matched canonical code (strip leading -C/-UC/-U tag first).
    if norm_suffix:
        trimmed = norm_suffix
        trimmed = re.sub(
            r"^[-_.\s]*(?:UC|CH|CHS|CHT|C|U)(?=[-_.\s]|$)",
            "",
            trimmed,
            flags=re.IGNORECASE,
        )
        m = _RE_SUFFIX_LETTER_PART.search(trimmed)
        if m:
            letter = (m.group(1) or m.group(2)).upper()
            return letter
        m = _RE_SUFFIX_NUM_PART.search(trimmed)
        if m:
            return f"Part{int(m.group(1))}"
        m = _RE_SUFFIX_COMBINED_PART.search(trimmed)
        if m:
            if m.group(1):
                return f"Part{int(m.group(1))}"
            if m.group(2):
                return m.group(2).upper()

    return ""


def clean_text(text: str) -> str:
    """Run the pre-cleaning pipeline while preserving catalog code structure.

    Steps:
    1. Strip known file extension
    2. Normalize full-width characters and Unicode dash variants
    3. Strip URLs, domain/site `@` prefixes, and site domain watermarks
    4. Strip file size tokens and 4-digit-year timestamps
    5. Strip codec/resolution/technical noise tokens
    6. Convert wrapper brackets to spaces and collapse whitespace
    """
    if not text:
        return ""
    stem, _ = split_known_extension(text)
    cleaned = normalize_width(stem)

    # Strip Windows dir timestamp+size prefix if still attached.
    cleaned = re.sub(
        r"^\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?\s+(?:<DIR>|[\d,]+)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    # Strip scheme+host URLs and domain@ prefixes (e.g. portal.com@, mirror.net@).
    cleaned = _RE_URL_HOST.sub(" ", cleaned)
    cleaned = _RE_DOMAIN_AT.sub(" ", cleaned)
    cleaned = _RE_DOMAIN_PREFIX.sub(" ", cleaned)

    # Strip file size markers (e.g. 1.45 GB) and 4-digit-year dates (e.g. 2024-05-01).
    cleaned = _RE_FILE_SIZE.sub(" ", cleaned)
    cleaned = _RE_ISO_DATE.sub(" ", cleaned)

    # Strip technical noise tokens (H264, 1080P, 60FPS, WIN10, UTF8, etc.).
    cleaned = _RE_TECH_NOISE.sub(" ", cleaned)
    cleaned = _RE_STANDALONE_QUALITY.sub(" ", cleaned)

    # Replace brackets with spaces so codes inside [ABC-123] or 【PRJ-001】 stay intact.
    cleaned = _RE_BRACKETS.sub(" ", cleaned)

    # Clean stray leading/trailing punctuation and collapse whitespace.
    cleaned = _RE_MULTI_SPACE.sub(" ", cleaned).strip(" -_.,;:@|")
    return cleaned
