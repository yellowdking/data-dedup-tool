"""Comprehensive unit tests for `table_data.py` and `doc_reader.py`.

All fixtures and test strings are 100% neutral and general-purpose.
"""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

import docx
import openpyxl
import pytest

from src.core.database import DatabaseRepository
from src.core.doc_reader import (
    extract_excel_text_items,
    extract_word_text_items,
    list_excel_sheets,
    load_tables_from_file,
    read_csv_table,
    read_database_as_table,
    read_excel_tables,
    read_txt_as_table,
    read_word_tables,
)
from src.core.models import DataRecord
from src.core.parser import CodeParser
from src.core.table_data import (
    DataTable,
    TableColumnInfo,
    TableDedupConfig,
    TableDedupReport,
    TableDuplicateGroup,
    TableMergeConfig,
    TableMergeResult,
    TableRowRef,
)


# ---------------------------------------------------------------------------
# table_data.py model tests
# ---------------------------------------------------------------------------


def test_data_table_helpers_and_clone_rename():
    tbl = DataTable(
        table_id="",
        name="清单A",
        headers=["编号", "名称", "备注"],
        rows=[
            ["ABC-101", "项目文档一", "4K"],
            ["PRJ-202", "项目文档二"],  # shorter row handled gracefully
            ["ABC-101", "", "归档"],
        ],
        source_type="excel",
        metadata={"tag": "v1"},
    )
    assert tbl.table_id.startswith("tbl_")
    assert tbl.created_at
    assert tbl.row_count == 3
    assert tbl.col_count == 3
    assert tbl.get_cell(1, 2) == ""
    assert tbl.get_cell(99, 0, "fallback") == "fallback"
    assert tbl.get_column_index("名称") == 1
    assert tbl.get_column_index("不存在") == -1

    dicts = tbl.to_dicts()
    assert len(dicts) == 3
    assert dicts[1] == {"编号": "PRJ-202", "名称": "项目文档二", "备注": ""}

    col_info = tbl.get_column_info()
    assert len(col_info) == 3
    assert isinstance(col_info[0], TableColumnInfo)
    assert col_info[0].non_empty_count == 3
    assert col_info[0].unique_count == 2
    assert col_info[1].non_empty_count == 2

    cloned = tbl.clone(new_name="清单A_副本")
    assert cloned.table_id != tbl.table_id
    assert cloned.name == "清单A_副本"
    cloned.rows[0][0] = "MODIFIED"
    cloned.metadata["tag"] = "v2"
    assert tbl.rows[0][0] == "ABC-101"
    assert tbl.metadata["tag"] == "v1"

    tbl.rename("  新清单名称  ")
    assert tbl.name == "新清单名称"
    tbl.rename("   ")
    assert tbl.name == "新清单名称"


def test_table_dedup_and_merge_dataclasses():
    ref1 = TableRowRef(
        table_id="t1",
        table_name="表1",
        row_index=1,
        key_value="ABC-101",
        row_data=["ABC-101", "说明"],
        headers=["编号", "说明"],
        parsed_code="ABC-101",
        tags="4K",
        part="CD1",
    )
    ref2 = TableRowRef(
        table_id="t2",
        table_name="表2",
        row_index=3,
        key_value="ABC-101",
        row_data=["ABC-101"],
        headers=["编号", "说明"],
    )
    assert ref1.to_dict() == {"编号": "ABC-101", "说明": "说明"}
    assert ref2.to_dict() == {"编号": "ABC-101", "说明": ""}

    grp = TableDuplicateGroup(key_value="ABC-101", is_cross_table=True, rows=[ref1, ref2])
    assert grp.table_names == ["表1", "表2"]
    assert grp.table_count == 2
    assert grp.total_rows == 2

    report = TableDedupReport(config=TableDedupConfig(), table_names=["表1", "表2"])
    assert report.config.mode == "smart_code"
    assert report.cross_table_groups == []

    m_cfg = TableMergeConfig()
    assert m_cfg.mode == "union_dedup"
    merged_tbl = DataTable(table_id="m1", name="合并结果", headers=["编号"], rows=[["ABC-101"]])
    m_res = TableMergeResult(merged_table=merged_tbl, dropped_rows=[ref2], stats={"dropped": 1})
    assert m_res.merged_table.row_count == 1
    assert len(m_res.dropped_rows) == 1


# ---------------------------------------------------------------------------
# Excel (.xlsx / .xlsm / .xls) tests
# ---------------------------------------------------------------------------


def test_excel_multi_sheet_normalization_and_headers(tmp_path: Path):
    xlsx_path = tmp_path / "inventory.xlsx"
    wb = openpyxl.Workbook()

    ws1 = wb.active
    ws1.title = "主清单"
    # Leading blank row to test trimming
    ws1.append([None, None, None, None, None])
    # Header with empty and duplicate titles + trailing blank column
    ws1.append(["编号", None, "编号", "数值", "日期", "编号", None])
    ws1.append([12345.0, "ABC-101.mp4", 3.14, True, date(2026, 11, 26), "备注A", None])
    ws1.append(
        [1234567.0, "PRJ-0042_4K.mkv", -99.5, False, datetime(2026, 11, 26, 14, 30, 5), "备注B", None]
    )
    ws1.append([None, None, None, None, None, None, None])  # internal blank row
    ws1.append([700.0, "DEV-008.mp4", 0.25, time(9, 15, 0), datetime(2026, 5, 1, 0, 0, 0), "备注C", None])
    ws1.append([None, None, None, None, None, None, None])  # trailing blank row

    ws2 = wb.create_sheet("空工作表")
    ws2.append([None, ""])

    ws3 = wb.create_sheet("分表二")
    ws3.append(["分类", "项目码"])
    ws3.append(["批次甲", "MOD-001"])
    ws3.append([None, "MOD-002"])
    ws3.merge_cells("A2:A3")  # vertical merge in data rows

    wb.save(xlsx_path)

    sheets = list_excel_sheets(xlsx_path)
    assert sheets == ["主清单", "空工作表", "分表二"]

    tables = read_excel_tables(xlsx_path)
    # Blank sheet is skipped cleanly
    assert len(tables) == 2

    t1 = tables[0]
    assert t1.source_type == "excel"
    assert t1.sheet_name == "主清单"
    assert t1.name == "inventory - 主清单"
    assert t1.headers == ["编号", "列2", "编号_2", "数值", "日期", "编号_3"]
    assert t1.row_count == 3
    assert t1.rows[0] == ["12345", "ABC-101.mp4", "3.14", "TRUE", "2026-11-26", "备注A"]
    assert t1.rows[1] == [
        "1234567",
        "PRJ-0042_4K.mkv",
        "-99.5",
        "FALSE",
        "2026-11-26 14:30:05",
        "备注B",
    ]
    assert t1.rows[2] == ["700", "DEV-008.mp4", "0.25", "09:15:00", "2026-05-01", "备注C"]

    t2 = tables[1]
    assert t2.sheet_name == "分表二"
    assert t2.headers == ["分类", "项目码"]
    # Vertical merge propagates "批次甲" to row 2
    assert t2.rows == [["批次甲", "MOD-001"], ["批次甲", "MOD-002"]]

    # Filtering specific sheets & has_header=False
    filtered = read_excel_tables(xlsx_path, sheet_names=["分表二"], has_header=False)
    assert len(filtered) == 1
    assert filtered[0].headers == ["列1", "列2"]
    assert filtered[0].row_count == 3

    with pytest.raises(ValueError, match="未找到指定工作表"):
        read_excel_tables(xlsx_path, sheet_names=["不存在的表"])


def test_extract_excel_text_items_locators_and_multiline(tmp_path: Path):
    xlsx_path = tmp_path / "scan_source.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "档案表"
    ws.append(["编码", "说明"])
    ws.append(["ABC-101.mp4", "高清归档"])
    ws.append([None, None])  # blank row 3
    ws.append(["PRJ-001\nPRJ-002"])  # single-cell multiline row 4
    ws.append([1234567.0, "备注"])  # row 5 with float-integer
    wb.save(xlsx_path)

    items = extract_excel_text_items(xlsx_path)
    assert len(items) == 5
    assert items[0]["location"] == "档案表 行1"
    assert items[1]["location"] == "档案表 行2"
    assert items[1]["text"] == "ABC-101.mp4 | 高清归档"
    assert items[1]["cells"] == ["ABC-101.mp4", "高清归档"]
    assert items[2]["location"] == "档案表 行4"
    assert items[2]["text"] == "PRJ-001"
    assert items[3]["location"] == "档案表 行4"
    assert items[3]["text"] == "PRJ-002"
    assert items[4]["location"] == "档案表 行5"
    assert items[4]["text"] == "1234567 | 备注"


def test_legacy_xls_and_corrupt_xlsx_errors(tmp_path: Path):
    xls_path = tmp_path / "legacy.xls"
    xls_path.write_bytes(b"fake xls content")
    with pytest.raises(ValueError, match=r"\.xls.*\.xlsx"):
        read_excel_tables(xls_path)
    with pytest.raises(ValueError, match=r"\.xls.*\.xlsx"):
        load_tables_from_file(xls_path)

    bad_xlsx = tmp_path / "broken.xlsx"
    bad_xlsx.write_bytes(b"not a valid zip workbook")
    with pytest.raises(ValueError, match="无法读取 Excel 文件"):
        read_excel_tables(bad_xlsx)

    with pytest.raises(FileNotFoundError):
        read_excel_tables(tmp_path / "missing.xlsx")


# ---------------------------------------------------------------------------
# Word (.docx / .doc) tests
# ---------------------------------------------------------------------------


def test_word_tables_merged_cells_paragraphs_and_headers_footers(tmp_path: Path):
    docx_path = tmp_path / "report.docx"
    doc = docx.Document()

    # Header and footer
    sec = doc.sections[0]
    sec.header.paragraphs[0].text = "归档页眉 HDR-001"
    sec.footer.paragraphs[0].text = "归档页脚 FTR-999"

    # Body paragraphs
    doc.add_paragraph("第一段说明：ABC-101.mp4")
    doc.add_paragraph("")
    doc.add_paragraph("第二段说明：PRJ-202.mkv")

    # Table 1 with horizontal & vertical merges
    t1 = doc.add_table(rows=4, cols=3)
    t1.cell(0, 0).text = "分类"
    t1.cell(0, 1).text = "编码"
    t1.cell(0, 2).text = "备注"

    t1.cell(1, 0).text = "组一"
    t1.cell(1, 1).text = "DEV-101"
    t1.cell(1, 2).text = "初始版本"

    t1.cell(2, 0).text = ""
    t1.cell(2, 1).text = "DEV-102"
    t1.cell(2, 2).text = "修订版本"
    # Vertical merge (row 1..2, col 0)
    t1.cell(1, 0).merge(t1.cell(2, 0))

    # Horizontal merge on row 3 (col 1..2)
    t1.cell(3, 0).text = "组二"
    t1.cell(3, 1).text = "合并说明"
    t1.cell(3, 1).merge(t1.cell(3, 2))

    # Table 2: single-row table (should preserve row as data)
    t2 = doc.add_table(rows=1, cols=2)
    t2.cell(0, 0).text = "SOLO-001"
    t2.cell(0, 1).text = "单行记录"

    doc.save(docx_path)

    # Default: extract embedded tables only (since tables exist)
    tables = read_word_tables(docx_path)
    assert len(tables) == 2
    assert tables[0].name == "report - 表格1"
    assert tables[0].source_type == "word"
    assert tables[0].headers == ["分类", "编码", "备注"]
    assert tables[0].rows == [
        ["组一", "DEV-101", "初始版本"],
        ["组一", "DEV-102", "修订版本"],
        ["组二", "合并说明", ""],
    ]
    assert tables[1].headers == ["列1", "列2"]
    assert tables[1].rows == [["SOLO-001", "单行记录"]]

    # With include_paragraphs_as_table=True
    tables_with_paras = read_word_tables(docx_path, include_paragraphs_as_table=True)
    assert len(tables_with_paras) == 3
    para_tbl = tables_with_paras[-1]
    assert para_tbl.headers == ["段落内容"]
    assert para_tbl.rows == [["第一段说明：ABC-101.mp4"], ["第二段说明：PRJ-202.mkv"]]

    # extract_word_text_items: paragraphs + deduplicated merged table cells + header/footer
    text_items = extract_word_text_items(docx_path)
    texts = [it["text"] for it in text_items]
    locs = {it["text"]: it["location"] for it in text_items}

    assert "第一段说明：ABC-101.mp4" in texts
    assert locs["第一段说明：ABC-101.mp4"] == "正文 段落1"
    assert "DEV-101" in texts
    assert locs["DEV-101"] == "表格1 行2列2"
    # Merged cells must appear only ONCE each!
    assert texts.count("组一") == 1
    assert texts.count("合并说明") == 1
    assert "归档页眉 HDR-001" in texts
    assert "页眉" in locs["归档页眉 HDR-001"]
    assert "归档页脚 FTR-999" in texts
    assert "页脚" in locs["归档页脚 FTR-999"]


def test_word_paragraphs_only_document(tmp_path: Path):
    docx_path = tmp_path / "notes_only.docx"
    doc = docx.Document()
    doc.add_paragraph("ABC-301")
    doc.add_paragraph("ABC-302")
    doc.save(docx_path)

    tables = read_word_tables(docx_path)
    assert len(tables) == 1
    assert tables[0].headers == ["段落内容"]
    assert tables[0].rows == [["ABC-301"], ["ABC-302"]]


def test_legacy_doc_and_corrupt_docx_errors(tmp_path: Path):
    doc_path = tmp_path / "legacy.doc"
    doc_path.write_bytes(b"fake doc binary")
    with pytest.raises(ValueError, match=r"\.doc.*\.docx"):
        read_word_tables(doc_path)
    with pytest.raises(ValueError, match=r"\.doc.*\.docx"):
        extract_word_text_items(doc_path)
    with pytest.raises(ValueError, match=r"\.doc.*\.docx"):
        load_tables_from_file(doc_path)

    bad_docx = tmp_path / "broken.docx"
    bad_docx.write_bytes(b"corrupted content")
    with pytest.raises(ValueError, match="无法读取 Word 文件"):
        read_word_tables(bad_docx)


# ---------------------------------------------------------------------------
# CSV / TSV encoding & delimiter tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("encoding", "delim", "suffix"),
    [
        ("utf-8-sig", ",", ".csv"),
        ("utf-8", ";", ".csv"),
        ("gb18030", ",", ".csv"),
        ("utf-16", "\t", ".tsv"),
    ],
)
def test_csv_tsv_encodings_and_delimiters(tmp_path: Path, encoding: str, delim: str, suffix: str):
    file_path = tmp_path / f"sample_{encoding}{suffix}"
    content = (
        f"编号{delim}名称{delim}编号\n"
        f"ABC-101{delim}测试项目一{delim}附加A\n"
        f"PRJ-202{delim}测试项目二{delim}附加B\n"
    )
    file_path.write_bytes(content.encode(encoding))

    tbl = read_csv_table(file_path)
    assert tbl.source_type == "csv"
    assert tbl.headers == ["编号", "名称", "编号_2"]
    assert tbl.row_count == 2
    assert tbl.rows[0] == ["ABC-101", "测试项目一", "附加A"]
    assert tbl.rows[1] == ["PRJ-202", "测试项目二", "附加B"]


# ---------------------------------------------------------------------------
# TXT-to-table & DB-to-table & unified dispatcher tests
# ---------------------------------------------------------------------------


def test_read_txt_as_table_plain_and_parsed(tmp_path: Path):
    txt_path = tmp_path / "listing.txt"
    txt_path.write_text(
        "ABC-101 [4K].mp4\nPRJ-0042-CD1.mkv\n普通说明行\n",
        encoding="utf-8",
    )

    plain_tbl = read_txt_as_table(txt_path, parser_engine=None)
    assert plain_tbl.source_type == "txt"
    assert plain_tbl.headers == ["原始文本", "来源"]
    assert plain_tbl.row_count == 3

    parsed_tbl = read_txt_as_table(txt_path, parser_engine=CodeParser())
    assert parsed_tbl.headers == [
        "标准编码",
        "前缀分类",
        "数字编号",
        "标签",
        "分集",
        "原始文本",
        "来源",
    ]
    assert parsed_tbl.row_count == 3
    assert parsed_tbl.rows[0][0] == "ABC-101"
    assert "4K" in parsed_tbl.rows[0][3]
    assert parsed_tbl.rows[1][0] == "PRJ-042" or parsed_tbl.rows[1][0] == "PRJ-0042"
    assert parsed_tbl.rows[1][4] == "CD1"
    assert parsed_tbl.rows[2][0] == ""


def test_read_database_as_table():
    with DatabaseRepository(":memory:") as repo:
        repo.add_records(
            [
                DataRecord(
                    canonical_code="ABC-101",
                    prefix="ABC",
                    number="101",
                    file_name="ABC-101.mp4",
                    source_name="批次1.txt",
                    source_type="txt",
                    tags=["4K", "字幕"],
                    part="CD1",
                    note="测试记录",
                ),
                DataRecord(
                    canonical_code="DEF-202",
                    prefix="DEF",
                    number="202",
                    file_name="DEF-202.mkv",
                    source_name="批次2.xlsx",
                    source_type="excel",
                ),
            ]
        )
        tbl = read_database_as_table(repo, name="主数据库")

    assert tbl.source_type == "database"
    assert tbl.name == "主数据库"
    assert tbl.row_count == 2
    assert "标准编码" in tbl.headers
    dicts = tbl.to_dicts()
    assert dicts[0]["标准编码"] == "ABC-101"
    assert dicts[0]["标签"] == "4K,字幕"
    assert dicts[0]["分集"] == "CD1"
    assert dicts[1]["标准编码"] == "DEF-202"


def test_load_tables_from_file_dispatcher(tmp_path: Path):
    xlsx_path = tmp_path / "a.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["编码"])
    ws.append(["ABC-001"])
    wb.save(xlsx_path)

    docx_path = tmp_path / "b.docx"
    doc = docx.Document()
    doc.add_paragraph("DEF-002")
    doc.save(docx_path)

    csv_path = tmp_path / "c.csv"
    csv_path.write_text("编码\nGHI-003\n", encoding="utf-8")

    txt_path = tmp_path / "d.log"
    txt_path.write_text("JKL-004\n", encoding="utf-8")

    unsupported = tmp_path / "e.pdf"
    unsupported.write_bytes(b"%PDF-1.4")

    assert len(load_tables_from_file(xlsx_path)) == 1
    assert len(load_tables_from_file(docx_path)) == 1
    assert len(load_tables_from_file(csv_path)) == 1
    assert len(load_tables_from_file(txt_path, parser_engine=CodeParser())) == 1

    with pytest.raises(ValueError, match="暂不支持的文件格式"):
        load_tables_from_file(unsupported)


def test_tree_txt_and_empty_csv_edge_cases(tmp_path: Path):
    tree_path = tmp_path / "archive_tree.txt"
    tree_path.write_text(
        "Folder PATH listing\n"
        "D:\\ARCHIVE\n"
        "├──ABC-101\n"
        "│   └──ABC-101_4K.mp4\n"
        "└──PRJ-202\n"
        "    └──PRJ-202.mkv\n",
        encoding="utf-8",
    )
    tbl = read_txt_as_table(tree_path, parser_engine=CodeParser())
    codes = [row[0] for row in tbl.rows]
    assert "ABC-101" in codes
    assert "PRJ-202" in codes

    empty_csv = tmp_path / "empty.csv"
    empty_csv.write_text("", encoding="utf-8")
    empty_tbl = read_csv_table(empty_csv)
    assert empty_tbl.headers == []
    assert empty_tbl.rows == []

