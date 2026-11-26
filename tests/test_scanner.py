"""Unit tests for TXT/tree/Everything/dir/CSV scanning, encoding detection, and folder scanning.

All test names and sample data use strictly neutral synthetic examples.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.parser import CodeParser
from src.core.scanner import (
    detect_input_format,
    read_text_auto_encoding,
    scan_file,
    scan_files,
    scan_folder,
    scan_text,
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
