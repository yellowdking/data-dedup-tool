"""Unit tests for TXT/tree/Everything/dir/CSV scanning, encoding detection, and folder scanning.

All test names and sample data use strictly neutral synthetic examples.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.parser import CodeParser
from src.core.scanner import (
    detect_input_format,
    normalize_cell_text,
    read_text_auto_encoding,
    scan_document_file,
    scan_excel_file,
    scan_file,
    scan_files,
    scan_folder,
    scan_text,
    scan_word_file,
)


def test_read_text_auto_encoding_utf8_gbk_utf16(tmp_path: Path) -> None:
    sample = "数据清单：ABC-001 中文标题.mp4\nPRJ-002 测试条目.mkv\n"

    p_utf8 = tmp_path / "list_utf8.txt"
    p_utf8.write_bytes(sample.encode("utf-8"))
    text, enc = read_text_auto_encoding(p_utf8)
    assert "ABC-001" in text and "中文标题" in text
    assert enc == "utf-8"

    p_bom = tmp_path / "list_bom.txt"
    p_bom.write_bytes(sample.encode("utf-8-sig"))
    text, enc = read_text_auto_encoding(p_bom)
    assert text.startswith("数据清单")
    assert enc == "utf-8-sig"

    p_gbk = tmp_path / "list_gbk.txt"
    p_gbk.write_bytes(sample.encode("gb18030"))
    text, enc = read_text_auto_encoding(p_gbk)
    assert "中文标题" in text and "PRJ-002" in text
    assert enc == "gb18030"

    p_utf16 = tmp_path / "list_utf16.txt"
    p_utf16.write_bytes(sample.encode("utf-16"))
    text, enc = read_text_auto_encoding(p_utf16)
    assert "PRJ-002" in text
    assert enc == "utf-16"


def test_windows_unicode_tree_with_parent_fallback_and_companions() -> None:
    tree_text = """卷 DATA 的文件夹 PATH 列表
卷序列号为 1234-5678
D:\\ARCHIVE
│   root_ABC-001.mp4
├───CAT-123
│   │   1.mp4
│   │   cover.jpg
│   │   info.nfo
│   └───CD2
│           video.mkv
└───FC2-PPV-1234567
        fc2ppv1234567.mp4
        sub.srt
"""
    assert detect_input_format(tree_text.splitlines()) == "tree"

    items = scan_text(tree_text, source_name="tree.txt", source_type="txt")
    parser = CodeParser()
    results = parser.parse_items(items, ignore_companions=True)

    codes = [r.canonical_code for r in results]
    assert "ABC-001" in codes
    assert codes.count("CAT-123") == 2  # 1.mp4 (parent) + CD2/video.mkv (grandparent)
    assert "FC2-PPV-1234567" in codes
    names = [r.file_name for r in results]
    assert "cover.jpg" not in names
    assert "info.nfo" not in names
    assert "sub.srt" not in names


def test_windows_ascii_tree_format() -> None:
    ascii_tree = """Folder PATH listing for volume DATA
Volume serial number is ABCD-1234
E:\\ARCHIVE
+---300AAA-123
|       part1.mp4
|       thumb.png
\\---ABC-NS-001
        main.mkv
"""
    assert detect_input_format(ascii_tree.splitlines()) == "tree"
    items = scan_text(ascii_tree, source_name="ascii_tree.txt")
    results = CodeParser().parse_items(items, ignore_companions=True)

    assert len(results) == 2
    assert results[0].canonical_code == "300AAA-123"
    assert results[0].from_parent is True
    assert results[0].part == "Part1"
    assert results[1].canonical_code == "ABC-NS-001"
    assert results[1].from_parent is True


def test_windows_dir_listing_format() -> None:
    dir_text = """ Volume in drive D is DATA
 Volume Serial Number is 1234-5678

 Directory of D:\\Archive\\PRJ-123

2024/05/10  14:30    <DIR>          .
2024/05/10  14:30    <DIR>          ..
2024/05/10  14:31     1,234,567,890 1.mp4
2024/05/10  14:31            45,678 poster.jpg
               2 File(s)  1,234,613,568 bytes

 Directory of D:\\Archive\\Mixed

2024/05/11  09:00       987,654,321 118abc00123.mkv
               1 File(s)    987,654,321 bytes
"""
    assert detect_input_format(dir_text.splitlines()) == "dir"
    items = scan_text(dir_text, source_name="dir.txt")
    assert len(items) == 3

    results = CodeParser().parse_items(items, ignore_companions=True)
    assert len(results) == 2
    assert results[0].canonical_code == "PRJ-123"
    assert results[0].from_parent is True
    assert results[0].file_size == 1234567890
    assert results[1].canonical_code == "ABC-123"
    assert results[1].file_size == 987654321


def test_everything_csv_efu_and_tsv_formats(tmp_path: Path) -> None:
    efu_content = '''Filename,Size,Date Modified,Date Created,Attributes
"D:\\Files\\ABC-001\\ABC-001_4K.mp4",2500000000,133500000000000000,133500000000000000,32
"D:\\Files\\ABC-001\\ABC-001.nfo",2048,133500000000000000,133500000000000000,32
"D:\\Files\\ALPHA_1234.mkv",1500000000,133500000000000000,133500000000000000,32
'''
    efu_path = tmp_path / "export.efu"
    efu_path.write_text(efu_content, encoding="utf-8")

    items = scan_file(efu_path)
    assert len(items) == 3
    assert items[1].is_companion is True

    results = CodeParser().parse_items(items, ignore_companions=True)
    assert [r.canonical_code for r in results] == ["ABC-001", "ALPHA-1234"]
    assert results[0].file_size == 2500000000
    assert "4K" in results[0].tags

    tsv_text = "D:\\Data\\T28-123.mp4\t1,200,000\t2024-05-01\nD:\\Data\\010124_001.mkv\t800,000\t2024-05-02\n"
    tsv_results = CodeParser().parse_text(tsv_text)
    assert [r.canonical_code for r in tsv_results] == ["T28-123", "010124_001"]
    assert tsv_results[0].file_size == 1200000


def test_scan_files_multi_file_and_progress(tmp_path: Path) -> None:
    f1 = tmp_path / "batch1.txt"
    f2 = tmp_path / "batch2.txt"
    f1.write_text("ABC-101.mp4\nDEF-202.mkv\n", encoding="utf-8")
    f2.write_text("GHI-303.mp4\n", encoding="gbk")

    progress_events: list[tuple[int, int, str]] = []
    items = scan_files(
        [f1, f2],
        progress_callback=lambda cur, tot, name: progress_events.append((cur, tot, name)),
    )
    assert len(items) == 3
    assert progress_events == [(1, 2, "batch1.txt"), (2, 2, "batch2.txt")]


def test_scan_folder_recursive_and_companion_filter(tmp_path: Path) -> None:
    sub_coded = tmp_path / "DOC-777"
    sub_coded.mkdir()
    (sub_coded / "1.mp4").write_bytes(b"0" * 100)
    (sub_coded / "cover.jpg").write_bytes(b"1" * 20)
    (sub_coded / "subtitle.srt").write_text("1\n00:00:01 --> 00:00:02\nhi\n", encoding="utf-8")

    (tmp_path / "portal800.com@CAT-999-C.mkv").write_bytes(b"2" * 50)
    (tmp_path / "readme.txt").write_text("notes", encoding="utf-8")

    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "ABC-999.mp4").write_bytes(b"x")

    items = scan_folder(tmp_path, recursive=True)
    assert len(items) == 5

    parser = CodeParser()
    results = parser.parse_items(items, ignore_companions=True)
    codes = sorted(r.canonical_code for r in results)
    assert codes == ["CAT-999", "DOC-777"]

    top_items = scan_folder(tmp_path, recursive=False)
    top_results = parser.parse_items(top_items, ignore_companions=True)
    assert [r.canonical_code for r in top_results] == ["CAT-999"]


def test_scan_folder_and_file_error_handling(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        scan_file(tmp_path / "missing.txt")
    with pytest.raises(NotADirectoryError):
        scan_folder(tmp_path / "missing_dir")


# ---------------------------------------------------------------------------
# v1.1.0 Word (.docx), Excel (.xlsx), TSV & unified scan_document_file tests
# ---------------------------------------------------------------------------
def test_normalize_cell_text_float_int_and_decimals() -> None:
    assert normalize_cell_text(None) == ""
    assert normalize_cell_text(True) == "TRUE"
    assert normalize_cell_text(1234567) == "1234567"
    assert normalize_cell_text(1234567.0) == "1234567"
    assert normalize_cell_text("1234567.0") == "1234567"
    assert normalize_cell_text(3.14) == "3.14"
    assert normalize_cell_text("  ABC-001  ") == "ABC-001"


def test_scan_excel_file_multi_sheet_multiline_merged_and_float_ids(tmp_path: Path) -> None:
    import openpyxl

    xlsx_path = tmp_path / "清单数据.xlsx"
    wb = openpyxl.Workbook()

    ws1 = wb.active
    ws1.title = "清单A"
    ws1.append(["序号", "项目编码", "说明", "更新日期", "金额"])
    ws1.append([1.0, "ＡＢＣ－００１", "示例条目 4K", "2026-11-26", "¥128.00"])
    ws1.append([2.0, "PRJ-0042中字", "常规记录", "2025/01/15", "USD 99.00"])
    ws1.append([1234567.0, "DEF-101.mp4\nGHI-202.mkv", "多行单元格", "14:30:22", "138-1234-5678"])
    ws1.append(["", "MERG-009", None, None, None])
    ws1.merge_cells("B5:D5")
    ws1.append(["", "2026-11-26", "纯日期价格行", "20240512", "¥256.00"])

    ws2 = wb.create_sheet("清单B")
    ws2.append(["FC2-PPV#7654321"])
    ws2.append(["300AAA-123.mp4\n300BBB-456.mkv"])

    wb.save(xlsx_path)
    wb.close()

    items_all = scan_excel_file(xlsx_path)
    assert any("1234567" in it.raw_text and "1234567.0" not in it.raw_text for it in items_all)
    assert any("[清单A 行2]" in it.source_name for it in items_all)
    assert any("[清单B 行1]" in it.source_name for it in items_all)

    results = CodeParser().parse_items(items_all, ignore_companions=True)
    recognized_codes = [r.canonical_code for r in results if r.recognized]
    assert "ABC-001" in recognized_codes
    assert "PRJ-0042" in recognized_codes
    assert "DEF-101" in recognized_codes
    assert "GHI-202" in recognized_codes
    assert recognized_codes.count("MERG-009") == 1  # Merged cell not duplicated
    assert "FC2-PPV-7654321" in recognized_codes
    assert "300AAA-123" in recognized_codes
    assert "300BBB-456" in recognized_codes
    # Ensure pure date/price/phone row produced no false positive code.
    assert not any(c.startswith(("2026", "2025", "2024", "USD", "138")) for c in recognized_codes)

    # Sheet filter test
    items_b = scan_excel_file(xlsx_path, sheet_names=["清单B"])
    codes_b = [r.canonical_code for r in CodeParser().parse_items(items_b) if r.recognized]
    assert codes_b == ["FC2-PPV-7654321", "300AAA-123", "300BBB-456"]


def test_scan_excel_single_cell_multiline_paths_preserve_parent_fallback(tmp_path: Path) -> None:
    import openpyxl

    xlsx_path = tmp_path / "paths.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "路径表"
    ws.append([r"D:\Archive\CAT-001\1.mp4" + "\n" + r"D:\Archive\CAT-002\1.mp4"])
    wb.save(xlsx_path)
    wb.close()

    items = scan_excel_file(xlsx_path)
    assert len(items) == 2
    results = CodeParser().parse_items(items, ignore_companions=True)
    assert [r.canonical_code for r in results] == ["CAT-001", "CAT-002"]
    assert all(r.from_parent for r in results)


def test_scan_word_file_paragraphs_tables_merged_and_headers(tmp_path: Path) -> None:
    import docx

    docx_path = tmp_path / "归档报告.docx"
    doc = docx.Document()
    doc.sections[0].header.paragraphs[0].text = "页眉编号 TOP-001"
    doc.add_paragraph("ＡＢＣ－００１.mp4")
    doc.add_paragraph("测试前缀XYZ-0098中字.mkv")
    doc.add_paragraph("2026-11-26 ¥299.00 138-1234-5678")

    tbl = doc.add_table(rows=3, cols=2)
    tbl.cell(0, 0).text = "编码"
    tbl.cell(0, 1).text = "备注"
    merged_cell = tbl.cell(1, 0).merge(tbl.cell(1, 1))
    merged_cell.text = "MERG-777 合并单元格"
    tbl.cell(2, 0).text = "TAB-002\nTAB-003"
    tbl.cell(2, 1).text = "FC2 PPV #1234567"

    doc.save(docx_path)

    items = scan_word_file(docx_path)
    assert any("归档报告.docx" in it.source_name for it in items)
    assert any("表格1" in it.source_name for it in items)

    results = CodeParser().parse_items(items, ignore_companions=True)
    recognized_codes = [r.canonical_code for r in results if r.recognized]
    assert "TOP-001" in recognized_codes
    assert "ABC-001" in recognized_codes
    assert "XYZ-0098" in recognized_codes
    assert recognized_codes.count("MERG-777") == 1  # Merged cell not duplicated
    assert "TAB-002" in recognized_codes
    assert "TAB-003" in recognized_codes
    assert "FC2-PPV-1234567" in recognized_codes


def test_scan_document_file_unified_dispatcher_and_scan_files(tmp_path: Path) -> None:
    import docx
    import openpyxl

    p_txt = tmp_path / "list.txt"
    p_txt.write_text("TXT-001.mp4\n", encoding="utf-8")

    p_log = tmp_path / "scan.log"
    p_log.write_text("LOG-002.mkv\n", encoding="utf-8")

    p_csv = tmp_path / "items.csv"
    p_csv.write_text("文件名,大小\nCSV-003.mp4,1024\n", encoding="utf-8-sig")

    p_tsv = tmp_path / "items.tsv"
    p_tsv.write_text("序号\t编码\t备注\n1\tTSV-004\t说明\n", encoding="utf-8")

    p_docx = tmp_path / "doc.docx"
    d = docx.Document()
    d.add_paragraph("DOCX-005.mp4")
    d.save(p_docx)

    p_xlsx = tmp_path / "book.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(["XLSX-006"])
    wb.save(p_xlsx)
    wb.close()

    # Test single unified dispatcher for each format
    for fp, expected in [
        (p_txt, "TXT-001"),
        (p_log, "LOG-002"),
        (p_csv, "CSV-003"),
        (p_tsv, "TSV-004"),
        (p_docx, "DOCX-005"),
        (p_xlsx, "XLSX-006"),
    ]:
        scanned = scan_document_file(fp)
        codes = [r.canonical_code for r in CodeParser().parse_items(scanned) if r.recognized]
        assert expected in codes, f"Expected {expected} from {fp.name}, got {codes}"

    # Test scan_files with mixed document formats
    all_items = scan_files([p_txt, p_log, p_csv, p_tsv, p_docx, p_xlsx])
    all_codes = {r.canonical_code for r in CodeParser().parse_items(all_items) if r.recognized}
    assert {"TXT-001", "LOG-002", "CSV-003", "TSV-004", "DOCX-005", "XLSX-006"}.issubset(all_codes)


def test_scan_document_file_legacy_and_missing_errors(tmp_path: Path) -> None:
    legacy_doc = tmp_path / "old.doc"
    legacy_doc.write_bytes(b"legacy")
    with pytest.raises(ValueError, match=r"\.docx"):
        scan_document_file(legacy_doc)

    legacy_xls = tmp_path / "old.xls"
    legacy_xls.write_bytes(b"legacy")
    with pytest.raises(ValueError, match=r"\.xlsx"):
        scan_document_file(legacy_xls)

    with pytest.raises(FileNotFoundError):
        scan_word_file(tmp_path / "missing.docx")
    with pytest.raises(FileNotFoundError):
        scan_excel_file(tmp_path / "missing.xlsx")
    with pytest.raises(FileNotFoundError):
        scan_document_file(tmp_path / "missing.xlsx")


def test_word_and_excel_no_false_parent_fallback_from_host_folder_or_sheet(tmp_path: Path) -> None:
    """Host folder names (e.g. PRJ-2026_folder / dedup_qa_v110) and sheet names must never
    cause unrecognized header/date/price rows in Word/Excel files to inherit false codes.
    """
    import docx
    import openpyxl

    coded_dir = tmp_path / "dedup_qa_v110_PRJ-2026"
    coded_dir.mkdir()

    # 1. Excel inside coded folder with coded sheet name
    xlsx_path = coded_dir / "2026Q3_资产清单.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "PRJ-2026"
    ws.append(["记录编号", "原始文件名", "归档分类", "更新日期", "金额"])
    ws.append(["", "纯中文无编码说明", "办公文档", "2026-11-26", "¥128.00"])
    ws.append(["1", "ABC-001.mp4", "归档", "2026-11-26", "¥0.00"])
    ws.append([r"D:\Archive\CAT-009\1.mp4"])  # Genuine in-cell path SHOULD still fall back
    wb.save(xlsx_path)
    wb.close()

    excel_items = scan_excel_file(xlsx_path)
    excel_results = CodeParser().parse_items(excel_items, ignore_companions=True)
    excel_recognized = [r.canonical_code for r in excel_results if r.recognized]
    excel_unrec = [r for r in excel_results if not r.recognized]

    assert excel_recognized == ["ABC-001", "CAT-009"]
    assert len(excel_unrec) == 2  # Header row + pure Chinese/date/price row stay unrecognized
    assert not any("PRJ-2026" in c or "V110" in c for c in excel_recognized)

    # 2. Word inside coded folder
    docx_path = coded_dir / "归档汇总说明.docx"
    doc = docx.Document()
    doc.add_paragraph("2026年第三季度归档汇总说明")
    doc.add_paragraph("2026-11-26 ¥299.00 138-1234-5678")
    doc.add_paragraph("DEF-002.mkv")
    tbl = doc.add_table(rows=2, cols=2)
    tbl.cell(0, 0).text = "编码"
    tbl.cell(0, 1).text = "备注"
    tbl.cell(1, 0).text = "GHI-003"
    tbl.cell(1, 1).text = "正式归档"
    doc.save(docx_path)

    word_items = scan_word_file(docx_path)
    word_results = CodeParser().parse_items(word_items, ignore_companions=True)
    word_recognized = [r.canonical_code for r in word_results if r.recognized]
    word_unrec = [r for r in word_results if not r.recognized]

    assert word_recognized == ["DEF-002", "GHI-003"]
    assert len(word_unrec) == 3  # Title paragraph + date/price paragraph + table header row
    assert not any("PRJ-2026" in c or "V110" in c for c in word_recognized)


# ---------------------------------------------------------------------------
# v1.2.0 Hardening: P0/P1 scanner, tree, CSV/TSV, dir, encoding & scandir tests
# ---------------------------------------------------------------------------
def test_v12_companion_filter_only_drops_single_file_items(tmp_path: Path) -> None:
    import docx
    import openpyxl

    # 1. Excel multi-column row ending in `cover.jpg` must NOT be dropped.
    xlsx_path = tmp_path / "with_cover.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["ABC-123", "主视频", "cover.jpg"])
    ws.append(["cover.jpg"])  # Standalone companion filename in single cell IS filtered.
    wb.save(xlsx_path)
    wb.close()

    excel_results = CodeParser().parse_items(scan_excel_file(xlsx_path), ignore_companions=True)
    assert [r.canonical_code for r in excel_results if r.recognized] == ["ABC-123"]

    # 2. Word paragraph ending in `readme.txt` must NOT be dropped.
    docx_path = tmp_path / "with_readme.docx"
    doc = docx.Document()
    doc.add_paragraph("编号 ABC-123 说明见 readme.txt")
    doc.save(docx_path)
    word_results = CodeParser().parse_items(scan_word_file(docx_path), ignore_companions=True)
    assert [r.canonical_code for r in word_results if r.recognized] == ["ABC-123"]

    # 3. Multi-token TXT line ending in `.html` URL must NOT be dropped.
    txt_results = CodeParser().parse_text(
        "ABC-123 详情 https://example.com/page.html\ncover.jpg\n.DS_Store\n",
        ignore_companions=True,
    )
    assert [r.canonical_code for r in txt_results if r.recognized] == ["ABC-123"]
    assert len(txt_results) == 1


def test_v12_multicolumn_csv_and_excel_pasted_tsv() -> None:
    parser = CodeParser()

    # CSV without recognized filename header scans all non-size columns.
    csv_no_hdr = "序号,资产代号,大小\n1,ABC-123,1024\n2,DEF-456,2048\n"
    items_1 = scan_text(csv_no_hdr, file_ext=".csv")
    res_1 = parser.parse_items(items_1)
    assert [r.canonical_code for r in res_1] == ["ABC-123", "DEF-456"]
    assert res_1[0].file_size == 1024

    # CSV with both `文件名` and `路径` prioritizes `文件名` over `路径`.
    csv_both = "文件名,路径,大小\nABC-123.mp4,D:\\Videos,1024\n"
    items_2 = scan_text(csv_both, file_ext=".csv")
    res_2 = parser.parse_items(items_2)
    assert [r.canonical_code for r in res_2] == ["ABC-123"]

    # Excel copy-paste TSV scans all columns instead of only cols[0].
    pasted_tsv = "1\tABC-123\t4K 中文字幕\n2\tDEF-456\t1080P\n"
    res_tsv = parser.parse_text(pasted_tsv)
    assert [r.canonical_code for r in res_tsv] == ["ABC-123", "DEF-456"]
    assert "4K" in res_tsv[0].tags and "字幕" in res_tsv[0].tags


def test_v12_tree_last_branch_space_prefixed_bar_and_no_stray_dash() -> None:
    tree_text = """D:\\ARCHIVE
└───PARENT-001
    │   1.mp4
    └───SUB
            2.mp4
"""
    items = scan_text(tree_text, source_name="tree.txt")
    assert [it.file_name for it in items] == ["1.mp4", "2.mp4"]
    assert items[0].parent_dir == "PARENT-001"
    assert not items[0].parent_dir.startswith("─")
    assert items[1].parent_dir == "SUB"

    results = CodeParser().parse_items(items, ignore_companions=True)
    assert [r.canonical_code for r in results] == ["PARENT-001", "PARENT-001"]
    assert all(r.from_parent for r in results)


def test_v12_slash_codes_and_dates_not_misclassified_as_posix_paths() -> None:
    parser = CodeParser()
    r1 = parser.parse_text("ABC-123/DEF-456/GHI-789")
    assert [r.canonical_code for r in r1] == ["ABC-123", "DEF-456", "GHI-789"]

    r2 = parser.parse_text("ABC-123 2024/05/12 DEF-456")
    assert [r.canonical_code for r in r2] == ["ABC-123", "DEF-456"]

    r3 = parser.parse_text("ABC-123 2024/05/12/extra")
    assert [r.canonical_code for r in r3] == ["ABC-123"]


def test_v12_encoding_utf32_shift_jis_big5_and_corrupt_bom(tmp_path: Path) -> None:
    # UTF-32LE BOM
    p_u32 = tmp_path / "u32.txt"
    p_u32.write_bytes("ABC-123 中文清单.mp4\n".encode("utf-32"))
    txt_u32, enc_u32 = read_text_auto_encoding(p_u32)
    assert enc_u32 == "utf-32"
    assert "\x00" not in txt_u32
    assert CodeParser().parse_text(txt_u32)[0].canonical_code == "ABC-123"

    # Shift-JIS (cp932) must NOT be mis-decoded as GB18030 mojibake
    p_sjis = tmp_path / "sjis.txt"
    jp_sample = "日本語タイトル ABC-123 テスト.mp4\n"
    p_sjis.write_bytes(jp_sample.encode("cp932"))
    txt_sjis, enc_sjis = read_text_auto_encoding(p_sjis)
    assert enc_sjis == "cp932"
    assert "日本語タイトル" in txt_sjis and "テスト" in txt_sjis

    # Corrupt UTF-8 BOM & corrupt UTF-16 BOM must never crash
    p_bad_u8 = tmp_path / "bad_u8.txt"
    p_bad_u8.write_bytes(b"\xef\xbb\xbfABC-123\xff.mp4")
    txt_bad_u8, _ = read_text_auto_encoding(p_bad_u8)
    assert "ABC-123" in txt_bad_u8

    p_bad_u16 = tmp_path / "bad_u16.txt"
    p_bad_u16.write_bytes(b"\xff\xfeA\x00\xd8")
    txt_bad_u16, enc_bad_u16 = read_text_auto_encoding(p_bad_u16)
    assert enc_bad_u16 == "utf-16-replace"
    assert txt_bad_u16.startswith("A")


def test_v12_windows_dir_us_eu_and_chinese_ampm_formats() -> None:
    us_dir = """ Directory of D:\\Archive\\PRJ-123

05/10/2024  02:30 PM     1,234,567,890 1.mp4
05/10/2024  02:31 PM            12,345 cover.jpg
"""
    assert detect_input_format(us_dir.splitlines()) == "dir"
    res_us = CodeParser().parse_items(scan_text(us_dir), ignore_companions=True)
    assert len(res_us) == 1
    assert res_us[0].canonical_code == "PRJ-123"
    assert res_us[0].file_size == 1234567890

    cn_dir = """ D:\\Archive\\CAT-456 的目录

2024/05/10  下午 02:30         987,654 1.mp4
2024/05/10  上午 09:05     <DIR>       ..
"""
    assert detect_input_format(cn_dir.splitlines()) == "dir"
    res_cn = CodeParser().parse_items(scan_text(cn_dir), ignore_companions=True)
    assert len(res_cn) == 1
    assert res_cn[0].canonical_code == "CAT-456"
    assert res_cn[0].file_size == 987654

    eu_dir = """ Directory of D:\\Archive\\DOC-789

10.05.2024  14:30       555,000 1.mkv
10.05.2024  14:31         1,024 note.txt
"""
    assert detect_input_format(eu_dir.splitlines()) == "dir"
    res_eu = CodeParser().parse_items(scan_text(eu_dir), ignore_companions=True)
    assert [r.canonical_code for r in res_eu] == ["DOC-789"]


def test_v12_scan_folder_dotfiles_above_root_clamp_and_cancel(tmp_path: Path) -> None:
    # Above-root folder named `Backup-2024` must NOT leak onto unrecognized files inside `inbox`.
    above = tmp_path / "Backup-2024" / "inbox"
    above.mkdir(parents=True)
    (above / "random.mp4").write_bytes(b"1234")
    (above / ".DS_Store").write_bytes(b"meta")
    sub = above / "PRJ-555"
    sub.mkdir()
    (sub / "1.mp4").write_bytes(b"5678")

    items = scan_folder(above, recursive=True)
    ds = [it for it in items if it.file_name == ".DS_Store"]
    assert len(ds) == 1 and ds[0].is_companion is True

    results = CodeParser().parse_items(items, ignore_companions=True)
    by_name = {r.file_name: r for r in results}
    assert ".DS_Store" not in by_name
    assert by_name["random.mp4"].recognized is False
    assert by_name["1.mp4"].canonical_code == "PRJ-555"

    # Verify cancel_check stops scan_folder and scan_files cleanly.
    cancelled_items = scan_folder(above, recursive=True, cancel_check=lambda: True)
    assert cancelled_items == []

    # Verify normalize_cell_text avoids scientific notation on large decimals.
    assert normalize_cell_text(1234567.5) == "1234567.5"
