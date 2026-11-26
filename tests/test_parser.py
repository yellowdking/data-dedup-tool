"""Unit tests for multi-pattern code parsing, normalization, and custom rule engine.

All test cases, fixtures, and descriptions use strictly neutral synthetic examples.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.models import CustomRule, ScannedItem
from src.core.normalizer import (
    clean_text,
    extract_part,
    extract_tags,
    is_companion_extension,
    is_false_positive,
    normalize_serial_number,
    normalize_width,
)
from src.core.parser import CodeParser, parse_code
from src.core.rules import RuleRegistry


@pytest.fixture
def parser() -> CodeParser:
    return CodeParser()


# ---------------------------------------------------------------------------
# 1. Standard alphanumeric & over-padded serial normalization
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected_code, expected_prefix",
    [
        ("ABC-123.mp4", "ABC-123", "ABC"),
        ("ABCD-001.mkv", "ABCD-001", "ABCD"),
        ("START-001.mp4", "START-001", "START"),
        ("ABC123.mp4", "ABC-123", "ABC"),
        ("ABC-000123.mp4", "ABC-123", "ABC"),
        ("CAT-00001.mp4", "CAT-001", "CAT"),
        ("DOC_456.mkv", "DOC-456", "DOC"),
        ("ITEM 123.mp4", "ITEM-123", "ITEM"),
        ("SER-1234.mp4", "SER-1234", "SER"),
        ("MD-0123.mp4", "MD-0123", "MD"),
        ("MSD-012.mp4", "MSD-012", "MSD"),
        ("XK-8123.mp4", "XK-8123", "XK"),
        ("TMW-123.mp4", "TMW-123", "TMW"),
        ("PRJ-0042.mkv", "PRJ-0042", "PRJ"),
        ("CAT.001.1080p.mp4", "CAT-001", "CAT"),
        ("ＡＢＣ－１２３.mp4", "ABC-123", "ABC"),  # Full-width characters
        ("ABC—123.mp4", "ABC-123", "ABC"),  # Unicode em-dash
        ("abc-123.mp4", "ABC-123", "ABC"),
    ],
)
def test_standard_codes(parser: CodeParser, raw: str, expected_code: str, expected_prefix: str) -> None:
    res = parser.parse_single(raw)
    assert res.recognized is True
    assert res.canonical_code == expected_code
    assert res.prefix == expected_prefix
    assert res.confidence >= 0.85


# ---------------------------------------------------------------------------
# 2. Raw compact platform IDs -> canonical code normalization
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected_code, expected_prefix",
    [
        ("118abc00123.mp4", "ABC-123", "ABC"),
        ("h_1133abc00456.mp4", "ABC-456", "ABC"),
        ("1alpha00123.mkv", "ALPHA-123", "ALPHA"),
        ("55beta00123.mp4", "BETA-123", "BETA"),
        ("4k2_cat00123.mp4", "CAT-123", "CAT"),
        ("cat00001pl.mp4", "CAT-001", "CAT"),
        ("delta00123.mp4", "DELTA-123", "DELTA"),
        ("h_086cvdx00456.mkv", "CVDX-456", "CVDX"),
        ("n_1428abc00123.mp4", "ABC-123", "ABC"),
        ("cat00123hhb.mp4", "CAT-123", "CAT"),
        ("abw00001r.mp4", "ABW-001", "ABW"),
        ("site01.com@118abc00123_1.mp4", "ABC-123", "ABC"),
        ("13dsvr01234.mp4", "DSVR-1234", "DSVR"),
        ("24id00123.mp4", "ID-123", "ID"),
    ],
)
def test_raw_compact_ids(parser: CodeParser, raw: str, expected_code: str, expected_prefix: str) -> None:
    res = parser.parse_single(raw)
    assert res.recognized is True
    assert res.canonical_code == expected_code
    assert res.prefix == expected_prefix


def test_raw_compact_4k2_extracts_4k_tag(parser: CodeParser) -> None:
    res = parser.parse_single("4k2_cat00123.mp4")
    assert res.canonical_code == "CAT-123"
    assert "4K" in res.tags


def test_raw_compact_vr_suffix_extracts_vr_tag(parser: CodeParser) -> None:
    res = parser.parse_single("13dsvr00123.mp4")
    assert res.canonical_code == "DSVR-123"
    assert "VR" in res.tags


# ---------------------------------------------------------------------------
# 3. Composite prefix codes (including dot-separated variants)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected_code",
    [
        ("FC2-PPV-1234567.mp4", "FC2-PPV-1234567"),
        ("FC2.PPV.1234567.mp4", "FC2-PPV-1234567"),
        ("FC2PPV1234567.mkv", "FC2-PPV-1234567"),
        ("FC2 1234567.mp4", "FC2-PPV-1234567"),
        ("fc2_ppv_1234567.mp4", "FC2-PPV-1234567"),
        ("FC2-1234567.mp4", "FC2-PPV-1234567"),
        ("fc2ppv-987654.mp4", "FC2-PPV-987654"),
        ("[site.com] FC2-PPV-3456789 归档条目.mp4", "FC2-PPV-3456789"),
    ],
)
def test_composite_code_variants(parser: CodeParser, raw: str, expected_code: str) -> None:
    res = parser.parse_single(raw)
    assert res.recognized is True
    assert res.canonical_code == expected_code
    assert res.prefix == "FC2-PPV"


# ---------------------------------------------------------------------------
# 4. Number + Letter series prefixes (separated, compact, and zero-padded)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected_code, expected_prefix",
    [
        ("300AAA-123.mp4", "300AAA-123", "300AAA"),
        ("259BBBB-1234.mp4", "259BBBB-1234", "259BBBB"),
        ("200CCCC-2345.mp4", "200CCCC-2345", "200CCCC"),
        ("390DDD-123.mp4", "390DDD-123", "390DDD"),
        ("261EEE-123.mp4", "261EEE-123", "261EEE"),
        ("420FFF-123.mp4", "420FFF-123", "420FFF"),
        ("435GGG-123.mp4", "435GGG-123", "435GGG"),
        ("546HHHHH-123.mp4", "546HHHHH-123", "546HHHHH"),
        ("328IIII-123.mp4", "328IIII-123", "328IIII"),
        ("91CM-123.mp4", "91CM-123", "91CM"),
        ("1000PRO-1234.mp4", "1000PRO-1234", "1000PRO"),
        ("300AAA123.mp4", "300AAA-123", "300AAA"),
        ("200CCCC2345.mkv", "200CCCC-2345", "200CCCC"),
        # Zero-padded compact 3-digit series (must preserve 3-digit series prefix!)
        ("300aaa00123.mp4", "300AAA-123", "300AAA"),
        ("259bbbb01234.mp4", "259BBBB-1234", "259BBBB"),
        ("200cccc02345.mp4", "200CCCC-2345", "200CCCC"),
    ],
)
def test_num_alpha_series(parser: CodeParser, raw: str, expected_code: str, expected_prefix: str) -> None:
    res = parser.parse_single(raw)
    assert res.recognized is True
    assert res.canonical_code == expected_code
    assert res.prefix == expected_prefix


# ---------------------------------------------------------------------------
# 5. Mixed letter-number & multi-segment prefixes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected_code",
    [
        ("T28-123.mp4", "T28-123"),
        ("S2M-001.mp4", "S2M-001"),
        ("S2MBD-001.mkv", "S2MBD-001"),
        ("MKD-S123.mp4", "MKD-S123"),
        ("CWPBD-123.mp4", "CWPBD-123"),
        ("MCBD-12.mp4", "MCBD-12"),
        ("LAFBD-12.mp4", "LAFBD-12"),
        ("SKYHD-123.mp4", "SKYHD-123"),
        ("CW3D2DBD-12.mp4", "CW3D2DBD-12"),
        ("B20-123.mp4", "B20-123"),
        ("K8-1234.mp4", "K8-1234"),
        ("ABC-NS-001.mp4", "ABC-NS-001"),
        ("H4610-KI1234.mp4", "H4610-KI1234"),
        ("H0930-ORI1234.mp4", "H0930-ORI1234"),
        ("C0930-KI1234.mp4", "C0930-KI1234"),
        ("SERIES-4017-1234.mp4", "SERIES-4017-1234"),
        ("SerIES4236-1048.mp4", "SERIES-4236-1048"),
    ],
)
def test_mixed_and_three_segment_codes(parser: CodeParser, raw: str, expected_code: str) -> None:
    res = parser.parse_single(raw)
    assert res.recognized is True
    assert res.canonical_code == expected_code


# ---------------------------------------------------------------------------
# 6. Date-indexed & prefixed sequence codes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected_code",
    [
        ("010124-001.mp4", "010124-001"),
        ("010124_001.mp4", "010124_001"),
        ("srcA-010124_001.mp4", "010124_001"),
        ("257138_mirror_srcA_080521_001.mp4", "080521_001"),
        ("srcB-010124-001.mp4", "010124-001"),
        ("srcC-010124_01.mp4", "010124_01"),
        ("alpha-1234.mp4", "ALPHA-1234"),
        ("ALPHA_1234.mkv", "ALPHA-1234"),
        ("node99.cc@alpha_hd_2636_full.mp4", "ALPHA-2636"),
    ],
)
def test_date_indexed_and_prefixed_series(parser: CodeParser, raw: str, expected_code: str) -> None:
    res = parser.parse_single(raw)
    assert res.recognized is True
    assert res.canonical_code == expected_code


# ---------------------------------------------------------------------------
# 7. Short prefix codes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected_code",
    [
        ("n0123.mp4", "N0123"),
        ("n1234.mp4", "N1234"),
        ("k0123.mp4", "K0123"),
        ("k1234.mp4", "K1234"),
        ("cz001.mp4", "CZ-001"),
        ("red-123.mp4", "RED-123"),
        ("se123.mp4", "SE-123"),
        ("BatchSeries-n1287.mp4", "N1287"),
    ],
)
def test_short_prefix_codes(parser: CodeParser, raw: str, expected_code: str) -> None:
    res = parser.parse_single(raw)
    assert res.recognized is True
    assert res.canonical_code == expected_code


# ---------------------------------------------------------------------------
# 8. Messy domain/site wrappers & Chinese titles
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected_code",
    [
        ("portal800.com@CAT-123-C.mp4", "CAT-123"),
        ("mirror9.com@DOC-888_4K.mkv", "DOC-888"),
        ("[Sample.la]ABC-123 示例中文标题 [1080P].mp4", "ABC-123"),
        ("【整理归档】www.foo.com-PRJ-456 中文标题说明.mp4", "PRJ-456"),
        ("22-demo.me-MOD-012-UC.mkv", "MOD-012"),
        ("https://example.com/files/DEF-789.mp4", "DEF-789"),
        ("【2024-05-01】[4K][H265] PRJ-0042 归档文件 (2.35 GB).mkv", "PRJ-0042"),
        ("root_ABC-001.mp4", "ABC-001"),
        ("copy_DOC-456.mkv", "DOC-456"),
        ("file_n0123.mp4", "N0123"),
        ("backup_118abc00123.mp4", "ABC-123"),
    ],
)
def test_messy_wrappers_and_titles(parser: CodeParser, raw: str, expected_code: str) -> None:
    res = parser.parse_single(raw)
    assert res.recognized is True
    assert res.canonical_code == expected_code


# ---------------------------------------------------------------------------
# 9. Tag & multi-part extraction (separated from canonical code)
# ---------------------------------------------------------------------------
def test_tags_subtitle_and_resolution(parser: CodeParser) -> None:
    res = parser.parse_single("ABC-123-C [4K].mp4")
    assert res.canonical_code == "ABC-123"
    assert "4K" in res.tags
    assert "字幕" in res.tags


def test_tags_special_and_subtitle_uc(parser: CodeParser) -> None:
    res = parser.parse_single("ABC-123-UC.mp4")
    assert res.canonical_code == "ABC-123"
    assert "特殊版" in res.tags
    assert "字幕" in res.tags


def test_tags_special_u_only(parser: CodeParser) -> None:
    res = parser.parse_single("ABC-123-U.mkv")
    assert res.canonical_code == "ABC-123"
    assert "特殊版" in res.tags


def test_tags_8k_and_vr(parser: CodeParser) -> None:
    res = parser.parse_single("LABVR-123 [8K][VR][中文字幕].mp4")
    assert res.canonical_code == "LABVR-123"
    assert "8K" in res.tags
    assert "VR" in res.tags
    assert "字幕" in res.tags


@pytest.mark.parametrize(
    "raw, expected_code, expected_part",
    [
        ("ABC-123-CD1.mp4", "ABC-123", "CD1"),
        ("ABC-123_CD2.mp4", "ABC-123", "CD2"),
        ("ABC-123 Part1.mkv", "ABC-123", "Part1"),
        ("ABC-123_Part2.mkv", "ABC-123", "Part2"),
        ("ABC-123-A.mp4", "ABC-123", "A"),
        ("ABC-123-B.mp4", "ABC-123", "B"),
        ("ABC-123_1.mp4", "ABC-123", "Part1"),
        ("ABC-123_2.mp4", "ABC-123", "Part2"),
        ("118abc00123_2.mp4", "ABC-123", "Part2"),
        ("ABC-123-C-CD2.mp4", "ABC-123", "CD2"),
        ("ABC-123 上集.mp4", "ABC-123", "Part1"),
        ("ABC-123 下集.mp4", "ABC-123", "Part2"),
    ],
)
def test_multipart_extraction(
    parser: CodeParser, raw: str, expected_code: str, expected_part: str
) -> None:
    res = parser.parse_single(raw)
    assert res.canonical_code == expected_code
    assert res.part == expected_part
    assert res.is_multipart is True


# ---------------------------------------------------------------------------
# 10. Multi-code-per-line & parent-folder fallback
# ---------------------------------------------------------------------------
def test_multiple_codes_on_single_line(parser: CodeParser) -> None:
    results = parser.parse_line("ABC-123, DEF-456 / FC2-PPV-1234567")
    codes = [r.canonical_code for r in results]
    assert codes == ["ABC-123", "DEF-456", "FC2-PPV-1234567"]


def test_duplicate_code_in_same_line_deduped(parser: CodeParser) -> None:
    results = parser.parse_line("[ABC-123] ABC-123 标题说明.mp4")
    assert len(results) == 1
    assert results[0].canonical_code == "ABC-123"


def test_parent_folder_fallback_from_path(parser: CodeParser) -> None:
    res = parser.parse_single("ABC-123/1.mp4")
    assert res.recognized is True
    assert res.canonical_code == "ABC-123"
    assert res.from_parent is True
    assert res.note == "父目录回退识别"


def test_parent_folder_fallback_preserves_file_part(parser: CodeParser) -> None:
    item = ScannedItem(
        raw_text="cd2.mp4",
        file_name="cd2.mp4",
        parent_dir="CAT-001 [4K]",
        full_path=r"D:\Archive\CAT-001 [4K]\cd2.mp4",
        file_ext=".mp4",
    )
    results = parser.parse_item(item)
    assert len(results) == 1
    assert results[0].canonical_code == "CAT-001"
    assert results[0].from_parent is True
    assert results[0].part == "CD2"
    assert "4K" in results[0].tags


def test_grandparent_fallback_skips_generic_dir(parser: CodeParser) -> None:
    item = ScannedItem(
        raw_text="00001.m2ts",
        file_name="00001.m2ts",
        parent_dir="STREAM",
        full_path=r"D:\Archive\PRJ-998\STREAM\00001.m2ts",
        file_ext=".m2ts",
    )
    results = parser.parse_item(item)
    assert len(results) == 1
    assert results[0].canonical_code == "PRJ-998"
    assert results[0].from_parent is True


# ---------------------------------------------------------------------------
# 11. Strict false-positive blacklist (codecs, color spaces, system tokens)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "noise_line",
    [
        "H264.mp4",
        "H265_1080P_60FPS.mkv",
        "X264_HEVC_AVC_AAC_FLAC.mp4",
        "HDR10_BT2020_60FPS.mkv",
        "BT709_REC709_REC2020.mp4",
        "AVC1_HVC1_HEV1_VP09.mkv",
        "2160P_720P_480P.avi",
        "WIN10_WIN11_X64.iso",
        "UTF8_GBK_MD5_SHA1_SHA256.txt",
        "https://www.example.com/index.html",
        "2024-05-12 归档备份说明.mp4",
        "IMG_1234.mp4",
        "DSC_0001.mp4",
        "CD1.mp4",
        "Part2.mkv",
        "纯中文无编码名称.mp4",
    ],
)
def test_false_positive_rejection(parser: CodeParser, noise_line: str) -> None:
    res = parser.parse_single(noise_line)
    assert res.recognized is False
    assert res.canonical_code == ""


def test_is_false_positive_helper() -> None:
    assert is_false_positive("H264", "") is True
    assert is_false_positive("BT", "2020", "BT2020") is True
    assert is_false_positive("REC", "709", "REC709") is True
    assert is_false_positive("WIN", "10", "WIN10") is True
    assert is_false_positive("SHA", "256", "SHA256") is True
    assert is_false_positive("ABC", "123", "ABC-123") is False


def test_companion_extension_helper() -> None:
    for ext in (".jpg", ".png", ".nfo", ".srt", ".ass", ".ssa", ".txt", ".url", ".torrent", ".ini", ".db"):
        assert is_companion_extension(ext) is True
    for ext in (".mp4", ".mkv", ".avi", ".iso"):
        assert is_companion_extension(ext) is False


# ---------------------------------------------------------------------------
# 12. Custom regex rule registry (add, priority override, JSON persistence)
# ---------------------------------------------------------------------------
def test_custom_rule_overrides_builtin_and_persists(tmp_path: Path) -> None:
    rules_file = tmp_path / "custom_rules.json"
    registry = RuleRegistry(rules_file)

    ok, err = registry.validate_pattern(r"(?P<prefix>CUST)#(?P<number>\d{4})")
    assert ok is True and err == ""

    bad_ok, bad_err = registry.validate_pattern(r"[unclosed")
    assert bad_ok is False and "错误" in bad_err

    rule = CustomRule(
        id="custom_hash_code",
        name="井号自定义编码",
        pattern=r"(?P<prefix>CUST)#(?P<number>\d{4})",
        format_template="{prefix}-{number}",
        priority=1,
        enabled=True,
    )
    registry.add_rule(rule)
    registry.set_rule_enabled("builtin_short_prefix", False)
    registry.save_custom_rules()

    reloaded = RuleRegistry(rules_file)
    assert len(reloaded.get_custom_rules()) == 1
    assert reloaded.is_builtin_enabled("builtin_short_prefix") is False

    custom_parser = CodeParser(reloaded)
    res = custom_parser.parse_single("归档_CUST#0088_4K.mp4")
    assert res.recognized is True
    assert res.canonical_code == "CUST-0088"
    assert res.matched_rule == "custom_hash_code"
    assert "4K" in res.tags

    disabled_res = custom_parser.parse_single("n0123.mp4")
    assert disabled_res.recognized is False

    assert reloaded.delete_rule("custom_hash_code") is True
    assert len(reloaded.get_custom_rules()) == 0


def test_normalizer_helpers() -> None:
    assert normalize_width("ＡＢＣ－００１") == "ABC-001"
    assert normalize_width("【ＡＢＣ－００１＃／（）＿】") == "[ABC-001#/()_]"
    assert normalize_serial_number("000123") == "123"
    assert normalize_serial_number("00001", from_raw_cid=True) == "001"
    assert normalize_serial_number("0123") == "0123"
    assert "portal800" not in clean_text("portal800.com@ABC-123.mp4").lower()
    assert extract_part("ABC-123_CD2.mp4") == "CD2"
    assert "字幕" in extract_tags("ABC-123 中文字幕.mp4")
    assert parse_code("ABC-123.mp4").canonical_code == "ABC-123"


# ---------------------------------------------------------------------------
# 13. v1.1.0 Hardening: full-width normalization, composite #/No., adjacent CJK,
#     and office/Excel false-positive guards
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected_code",
    [
        ("ＡＢＣ－００１.mp4", "ABC-001"),
        ("ＡＢＣ－００１．ｍｐ４", "ABC-001"),
        ("ＦＣ２－ＰＰＶ－１２３４５６７.mp4", "FC2-PPV-1234567"),
        ("ＦＣ２　ＰＰＶ＃１２３４５６７.mkv", "FC2-PPV-1234567"),
        ("【归档】ＰＲＪ＿００４２（４Ｋ）.mp4", "PRJ-0042"),
        ("（精选）ＤＯＣ－８８８／完整版.mkv", "DOC-888"),
    ],
)
def test_v11_fullwidth_normalization_and_matching(
    parser: CodeParser, raw: str, expected_code: str
) -> None:
    res = parser.parse_single(raw)
    assert res.recognized is True
    assert res.canonical_code == expected_code


def test_v11_fullwidth_extension_and_tag(parser: CodeParser) -> None:
    res = parser.parse_single("【归档】ＰＲＪ＿００４２（４Ｋ）．ｍｐ４")
    assert res.canonical_code == "PRJ-0042"
    assert res.file_ext == ".mp4"
    assert "4K" in res.tags


@pytest.mark.parametrize(
    "raw, expected_code",
    [
        ("FC2-PPV#1234567.mp4", "FC2-PPV-1234567"),
        ("FC2 PPV #1234567.mp4", "FC2-PPV-1234567"),
        ("FC2 PPV # 1234567.mkv", "FC2-PPV-1234567"),
        ("FC2-PPV-No.1234567.mp4", "FC2-PPV-1234567"),
        ("FC2 PPV No. 1234567.mp4", "FC2-PPV-1234567"),
        ("FC2#1234567.mp4", "FC2-PPV-1234567"),
        ("FC2-PPV №1234567.mp4", "FC2-PPV-1234567"),
    ],
)
def test_v11_composite_hash_and_no_separators(
    parser: CodeParser, raw: str, expected_code: str
) -> None:
    res = parser.parse_single(raw)
    assert res.recognized is True
    assert res.canonical_code == expected_code
    assert res.prefix == "FC2-PPV"


@pytest.mark.parametrize(
    "raw, expected_code, expected_tag, expected_part",
    [
        ("ABC-123中字.mp4", "ABC-123", "字幕", ""),
        ("测试前缀ABC-123高清版.mkv", "ABC-123", "", ""),
        ("【收藏】XYZ-0098完整版.mp4", "XYZ-0098", "", ""),
        ("项目清单PRJ-0042中文字幕版.mkv", "PRJ-0042", "字幕", ""),
        ("归档ABC-123-C中文标题.mp4", "ABC-123", "字幕", ""),
        ("归档ABC-123-UC完整版.mp4", "ABC-123", "特殊版", ""),
        ("归档ABC-123-CD1高清版.mp4", "ABC-123", "", "CD1"),
        ("归档ABC-123-A中文.mp4", "ABC-123", "", "A"),
        ("测试FC2-PPV#1234567中字.mp4", "FC2-PPV-1234567", "字幕", ""),
        ("前缀300AAA-123高清4K版.mkv", "300AAA-123", "4K", ""),
    ],
)
def test_v11_adjacent_cjk_boundaries_tags_and_parts(
    parser: CodeParser,
    raw: str,
    expected_code: str,
    expected_tag: str,
    expected_part: str,
) -> None:
    res = parser.parse_single(raw)
    assert res.recognized is True
    assert res.canonical_code == expected_code
    if expected_tag:
        assert expected_tag in res.tags
    if expected_part:
        assert res.part == expected_part


@pytest.mark.parametrize(
    "office_noise",
    [
        "2024-05-12",
        "2026-11-26",
        "2025/01/15",
        "2024.05.12",
        "2026-11-26T14:30:22Z",
        "20240512",
        "2024年05月12日",
        "14:30:22",
        "09:05",
        "138-1234-5678",
        "13812345678",
        "+86-138-1234-5678",
        "010-88886666",
        "400-800-1234",
        "¥128.00",
        "￥1,280.50",
        "$99.99",
        "USD 128.00",
        "CNY 256.00",
        "RMB128",
        "No.001",
        "TEL-123456",
        "FAX-8888",
        "alice123@company.com",
    ],
)
def test_v11_office_date_price_phone_false_positive_rejection(
    parser: CodeParser, office_noise: str
) -> None:
    res = parser.parse_single(office_noise)
    assert res.recognized is False
    assert res.canonical_code == ""


def test_v11_mixed_office_row_keeps_real_code_only(parser: CodeParser) -> None:
    row_text = "2026-11-26 | ABC-123中字 | ¥128.00 | 138-1234-5678 | alice123@company.com"
    results = parser.parse_line(row_text)
    assert [r.canonical_code for r in results] == ["ABC-123"]
    assert "字幕" in results[0].tags
