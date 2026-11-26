"""Comprehensive unit and integration tests for table_engine and table_exporter.

All sample data, table names, headers, and assertions are strictly neutral.
"""

from __future__ import annotations

import csv
from pathlib import Path
from unittest.mock import patch

import docx
import openpyxl
import pytest

from src.core.database import DatabaseRepository
from src.core.doc_reader import read_database_as_table
from src.core.exporter import ExportError
from src.core.models import DataRecord
from src.core.parser import CodeParser
from src.core.table_data import (
    DataTable,
    TableDedupConfig,
    TableMergeConfig,
)
from src.core.table_engine import (
    import_table_to_database,
    merge_tables,
    run_multi_table_dedup,
)
from src.core.table_exporter import (
    export_dedup_report_excel,
    export_dedup_report_word,
    export_multiple_tables_workbook,
    export_table_csv,
    export_table_excel,
    export_table_txt,
    export_table_word,
    sanitize_sheet_name,
    sanitize_xml_text,
)


# ----------------------------------------------------------------------
# Fixtures & neutral test helpers
# ----------------------------------------------------------------------


def _make_smart_tables() -> list[DataTable]:
    t1 = DataTable(
        table_id="t1",
        name="清单A",
        headers=["文件名", "备注", "容量"],
        rows=[
            ["abc00001.mp4", "初始存档", "2.1GB"],
            ["[portal.com]DEF-002-HD.mkv", "普通版", "1.4GB"],
            ["ABC-001_duplicate.mkv", "表内重复", ""],
            ["GHI-003.mp4", "仅A独有", "1.8GB"],
            ["纯说明无编码文本", "无法识别", ""],
        ],
        source_type="excel",
    )
    t2 = DataTable(
        table_id="t2",
        name="清单B",
        headers=["原始条目", "标签说明", "来源目录"],
        rows=[
            ["[site]ABC-001-4K-中文字幕.mkv", "高画质版", "D:/archive"],
            ["def-002_CD1.mp4", "分集1", "D:/archive"],
            ["JKL-004.mp4", "B与C共有", "D:/archive"],
            ["MNO-005.mp4", "仅B独有", "D:/archive"],
        ],
        source_type="word",
    )
    t3 = DataTable(
        table_id="t3",
        name="清单C",
        headers=["编号", "描述"],
        rows=[
            ["ABC_001", "第三份清单命中"],
            ["JKL00004.mp4", "B与C共有"],
            ["PQR-006", "仅C独有"],
            ["PQR-006", "C表内重复"],
        ],
        source_type="csv",
    )
    return [t1, t2, t3]


# ----------------------------------------------------------------------
# 1. Multi-table dedup tests
# ----------------------------------------------------------------------


class TestMultiTableDedupSmartCode:
    def test_cross_intra_intersection_exclusives_and_unrecognized(self) -> None:
        tables = _make_smart_tables()
        parser = CodeParser()
        cfg = TableDedupConfig(mode="smart_code")

        report = run_multi_table_dedup(tables, cfg, parser_engine=parser)

        assert report.table_names == ["清单A", "清单B", "清单C"]
        assert report.total_rows == 13
        # Distinct canonical codes: ABC-001, DEF-002, GHI-003, JKL-004, MNO-005, PQR-006
        assert report.unique_key_count == 6
        assert len(report.unrecognized_rows) == 1
        assert report.unrecognized_rows[0].table_id == "t1"
        assert report.unrecognized_rows[0].row_index == 5

        # Cross-table groups sorted by table_count desc, total_rows desc
        cross_keys = [g.key_value for g in report.cross_table_groups]
        assert cross_keys == ["ABC-001", "DEF-002", "JKL-004"]

        abc_group = report.cross_table_groups[0]
        assert abc_group.is_cross_table is True
        assert abc_group.table_count == 3
        assert abc_group.total_rows == 4  # 2 in t1 + 1 in t2 + 1 in t3
        assert abc_group.table_names == ["清单A", "清单B", "清单C"]

        # Tags & part preserved on TableRowRef
        t2_abc_ref = next(r for r in abc_group.rows if r.table_id == "t2")
        assert t2_abc_ref.parsed_code == "ABC-001"
        assert "4K" in t2_abc_ref.tags
        assert "字幕" in t2_abc_ref.tags

        def_group = report.cross_table_groups[1]
        t2_def_ref = next(r for r in def_group.rows if r.table_id == "t2")
        assert t2_def_ref.part == "CD1"

        # Intra-table duplicates: ABC-001 in t1 (2 rows) and PQR-006 in t3 (2 rows)
        intra_pairs = {(g.table_names[0], g.key_value, g.total_rows) for g in report.intra_table_groups}
        assert intra_pairs == {("清单A", "ABC-001", 2), ("清单C", "PQR-006", 2)}
        assert all(not g.is_cross_table and g.table_count == 1 for g in report.intra_table_groups)

        # Full intersection across all 3 tables: only ABC-001
        assert report.intersection_keys == ["ABC-001"]
        assert len(report.intersection_rows) == 4

        # Exclusive rows per table
        assert [r.key_value for r in report.unique_by_table["t1"]] == ["GHI-003"]
        assert [r.key_value for r in report.unique_by_table["t2"]] == ["MNO-005"]
        assert [r.key_value for r in report.unique_by_table["t3"]] == ["PQR-006", "PQR-006"]

        # Per-table stats
        s1 = report.table_stats["t1"]
        assert s1["total_rows"] == 5
        assert s1["unique_keys"] == 3
        assert s1["cross_dup_rows"] == 3  # 2 ABC-001 + 1 DEF-002
        assert s1["intra_dup_rows"] == 2
        assert s1["exclusive_rows"] == 1
        assert s1["unrecognized_rows"] == 1

    def test_smart_code_restricted_column_no_fallback(self) -> None:
        t1 = DataTable(
            table_id="a",
            name="表A",
            headers=["标题", "隐藏编码列"],
            rows=[
                ["普通文本无编码", "XYZ-100"],
                ["XYZ-100", "其他"],
            ],
        )
        cfg = TableDedupConfig(
            mode="smart_code",
            key_columns={"a": ["标题"]},
            scan_all_columns_for_code=False,
        )
        report = run_multi_table_dedup([t1], cfg)
        assert report.unique_key_count == 1
        assert len(report.unrecognized_rows) == 1
        assert report.unrecognized_rows[0].row_index == 1

    def test_include_flags_and_single_table(self) -> None:
        t1 = DataTable(
            table_id="only",
            name="单表",
            headers=["编码"],
            rows=[["AAA-001"], ["AAA-001"], ["BBB-002"]],
        )
        cfg = TableDedupConfig(
            mode="smart_code",
            include_intra_table=False,
            include_cross_table=False,
        )
        report = run_multi_table_dedup([t1], cfg)
        assert report.cross_table_groups == []
        assert report.intra_table_groups == []
        assert report.intersection_keys == []
        assert len(report.unique_by_table["only"]) == 3

    def test_empty_tables_list(self) -> None:
        report = run_multi_table_dedup([], TableDedupConfig())
        assert report.total_rows == 0
        assert report.unique_key_count == 0
        assert report.cross_table_groups == []


class TestMultiTableDedupColumnValue:
    def test_single_and_composite_key_with_normalization_options(self) -> None:
        t1 = DataTable(
            table_id="emp1",
            name="一月名单",
            headers=["部门", "姓名", "工号"],
            rows=[
                ["研发部", " Alice ", "E-001"],
                ["研发部", "Bob", "E-002"],
                ["财务部", "Carol", "E-003"],
                ["", "", ""],  # blank key row -> ignored from dupes
            ],
        )
        t2 = DataTable(
            table_id="emp2",
            name="二月名单",
            headers=["所属部门", "成员姓名", "员工编号"],
            rows=[
                ["研发部", "alice", "E_001"],
                ["市场部", "Bob", "E-004"],
                ["财务部", "carol", "E-003"],
            ],
        )

        # Composite key (部门 + 姓名): Alice & Carol match across tables, Bob differs by 部门
        cfg_comp = TableDedupConfig(
            mode="column_value",
            key_columns={
                "emp1": ["部门", "姓名"],
                "emp2": ["所属部门", "成员姓名"],
            },
            ignore_case=True,
            strip_whitespace=True,
        )
        rep = run_multi_table_dedup([t1, t2], cfg_comp)
        assert sorted(g.key_value for g in rep.cross_table_groups) == [
            "研发部 | alice",
            "财务部 | carol",
        ]
        assert rep.intersection_keys == ["研发部 | alice", "财务部 | carol"]
        assert len(rep.unrecognized_rows) == 1

        # Case-sensitive check: "Alice" != "alice"
        cfg_case = TableDedupConfig(
            mode="column_value",
            key_columns={"emp1": ["姓名"], "emp2": ["成员姓名"]},
            ignore_case=False,
            strip_whitespace=True,
        )
        rep_case = run_multi_table_dedup([t1, t2], cfg_case)
        assert [g.key_value for g in rep_case.cross_table_groups] == ["Bob"]

        # ignore_punctuation=True: "E-001" == "E_001"
        cfg_punct = TableDedupConfig(
            mode="column_value",
            key_columns={"emp1": ["工号"], "emp2": ["员工编号"]},
            ignore_case=True,
            strip_whitespace=True,
            ignore_punctuation=True,
        )
        rep_punct = run_multi_table_dedup([t1, t2], cfg_punct)
        assert sorted(g.key_value for g in rep_punct.cross_table_groups) == ["e001", "e003"]

    def test_composite_key_no_separator_collision_and_same_name_tables(self) -> None:
        t1 = DataTable(
            table_id="sep1",
            name="export.csv",
            headers=["A", "B"],
            rows=[["foo | bar", "baz"], ["same", "val"]],
        )
        t2 = DataTable(
            table_id="sep2",
            name="export.csv",
            headers=["A", "B"],
            rows=[["foo", "bar | baz"], ["same", "val"]],
        )
        cfg = TableDedupConfig(mode="column_value", key_columns={"*": ["A", "B"]})
        rep = run_multi_table_dedup([t1, t2], cfg)
        # Only ("same", "val") matches; ("foo | bar", "baz") != ("foo", "bar | baz")!
        assert [g.key_value for g in rep.cross_table_groups] == ["same | val"]
        assert rep.cross_table_groups[0].table_count == 2
        assert len(rep.cross_table_groups[0].table_names) == 2

        # Union dedup also keeps both distinct composite tuples
        m_res = merge_tables(
            [t1, t2],
            TableMergeConfig(
                mode="union_dedup",
                dedup_mode="column_value",
                key_columns={"*": ["A", "B"]},
                add_source_column=False,
            ),
        )
        assert m_res.merged_table.row_count == 3
        assert len(m_res.dropped_rows) == 1

        # Smart code with invalid explicit column + scan_all_columns_for_code=False must NOT leak
        t3 = DataTable(table_id="x", name="表X", headers=["描述"], rows=[["ABC-001"]])
        rep_strict = run_multi_table_dedup(
            [t3],
            TableDedupConfig(
                mode="smart_code",
                key_columns={"x": ["不存在列"]},
                scan_all_columns_for_code=False,
            ),
        )
        assert rep_strict.unique_key_count == 0
        assert len(rep_strict.unrecognized_rows) == 1


# ----------------------------------------------------------------------
# 2. Multi-table merge tests (union_all / union_dedup / join)
# ----------------------------------------------------------------------


class TestMergeTables:
    def test_union_all_by_header_and_by_position(self) -> None:
        t1 = DataTable(
            table_id="u1",
            name="表1",
            headers=["编码", "名称", "规格"],
            rows=[["ABC-001", "项目一", "A级"]],
        )
        t2 = DataTable(
            table_id="u2",
            name="表2",
            headers=["名称", "编码", "备注"],
            rows=[["项目二", "DEF-002", "补充"]],
        )

        # by_header preserves order and aligns columns by name
        res_hdr = merge_tables(
            [t1, t2],
            TableMergeConfig(
                mode="union_all",
                align_mode="by_header",
                add_source_column=True,
                add_row_index_column=True,
                add_extracted_code_column=True,
            ),
        )
        mt = res_hdr.merged_table
        assert mt.headers == ["编码", "名称", "规格", "备注", "_来源表", "_原行号", "_标准编码"]
        assert mt.rows[0] == ["ABC-001", "项目一", "A级", "", "表1", "1", "ABC-001"]
        assert mt.rows[1] == ["DEF-002", "项目二", "", "补充", "表2", "1", "DEF-002"]
        assert res_hdr.dropped_rows == []
        assert res_hdr.stats["output_rows"] == 2

        # by_position aligns strictly by column index
        res_pos = merge_tables(
            [t1, t2],
            TableMergeConfig(
                mode="union_all",
                align_mode="by_position",
                add_source_column=False,
            ),
        )
        assert res_pos.merged_table.headers == ["编码", "名称", "规格"]
        assert res_pos.merged_table.rows[1] == ["项目二", "DEF-002", "补充"]

    @pytest.mark.parametrize(
        "strategy,expected_title,expected_note",
        [
            ("first", "ABC-001.mp4", ""),
            ("last", "ABC-001-HD.mp4", "末条备注"),
            ("most_complete", "ABC-001-完整.mp4", "字段齐全"),
            ("highest_tag_score", "ABC-001-4K-中字.mkv", "高画质"),
        ],
    )
    def test_union_dedup_keep_strategies(
        self, strategy: str, expected_title: str, expected_note: str
    ) -> None:
        t1 = DataTable(
            table_id="d1",
            name="源1",
            headers=["文件名", "备注", "分类", "作者"],
            rows=[
                ["ABC-001.mp4", "", "", ""],
                ["ABC-001-完整.mp4", "字段齐全", "A类", "张三"],
                ["纯文本行1", "保留不误删", "", ""],
            ],
        )
        t2 = DataTable(
            table_id="d2",
            name="源2",
            headers=["文件名", "备注", "分类", "作者"],
            rows=[
                ["ABC-001-4K-中字.mkv", "高画质", "", ""],
                ["ABC-001-HD.mp4", "末条备注", "", ""],
                ["纯文本行2", "也应保留", "", ""],
            ],
        )

        res = merge_tables(
            [t1, t2],
            TableMergeConfig(
                mode="union_dedup",
                dedup_mode="smart_code",
                keep_strategy=strategy,
                add_source_column=True,
                add_extracted_code_column=True,
            ),
        )
        mt = res.merged_table
        # 1 deduplicated ABC-001 + 2 unrecognized rows preserved
        assert mt.row_count == 3
        assert len(res.dropped_rows) == 3
        assert mt.rows[0][0] == expected_title
        assert mt.rows[0][1] == expected_note
        assert mt.rows[0][-1] == "ABC-001"
        assert res.stats["unrecognized_kept"] == 2

    def test_union_dedup_coalesce_fills_empty_cells(self) -> None:
        t1 = DataTable(
            table_id="c1",
            name="主清单",
            headers=["编码", "标题", "容量", "备注"],
            rows=[
                ["abc00001.mp4", "作品一号", "", ""],
                ["XYZ-009", "独立条目", "1GB", "原备注"],
            ],
        )
        t2 = DataTable(
            table_id="c2",
            name="补充清单1",
            headers=["编码", "标题", "容量", "备注"],
            rows=[
                ["ABC-001", "会被忽略的标题", "4.2GB", ""],
            ],
        )
        t3 = DataTable(
            table_id="c3",
            name="补充清单2",
            headers=["编码", "标题", "容量", "备注"],
            rows=[
                ["[site]ABC-001.mkv", "", "9.9GB", "来自清单2的备注"],
            ],
        )

        res = merge_tables(
            [t1, t2, t3],
            TableMergeConfig(
                mode="union_dedup",
                dedup_mode="smart_code",
                keep_strategy="coalesce",
                add_source_column=True,
            ),
        )
        mt = res.merged_table
        assert mt.row_count == 2
        assert len(res.dropped_rows) == 2
        # Row 0 coalesced from t1 (标题), t2 (容量), t3 (备注)
        assert mt.rows[0][:4] == ["abc00001.mp4", "作品一号", "4.2GB", "来自清单2的备注"]
        assert "主清单" in mt.rows[0][4]
        assert "补充清单1" in mt.rows[0][4]
        assert "补充清单2" in mt.rows[0][4]

    def test_union_dedup_column_value_mode(self) -> None:
        t1 = DataTable(
            table_id="cv1",
            name="表1",
            headers=["订单号", "金额", "备注"],
            rows=[["ORD-100", "50", ""], ["ORD-200", "80", "OK"]],
        )
        t2 = DataTable(
            table_id="cv2",
            name="表2",
            headers=["订单号", "金额", "备注"],
            rows=[["ord-100", "50", "已核对"]],
        )
        res = merge_tables(
            [t1, t2],
            TableMergeConfig(
                mode="union_dedup",
                dedup_mode="column_value",
                key_columns={"*": ["订单号"]},
                keep_strategy="coalesce",
                add_source_column=False,
            ),
        )
        assert res.merged_table.rows == [
            ["ORD-100", "50", "已核对"],
            ["ORD-200", "80", "OK"],
        ]
        assert len(res.dropped_rows) == 1

    def test_join_left_inner_outer_and_column_collision(self) -> None:
        t_main = DataTable(
            table_id="main",
            name="主表",
            headers=["编码", "名称", "状态"],
            rows=[
                ["abc00001.mp4", "条目1", "正常"],
                ["DEF-002", "条目2", "待检"],
                ["无编码行", "未知", "草稿"],
            ],
        )
        t_extra = DataTable(
            table_id="extra",
            name="扩展表",
            headers=["文件", "状态", "评分"],
            rows=[
                ["[site]ABC-001-4K.mkv", "已归档", "9.5"],
                ["GHI-003.mp4", "新增", "8.0"],
                ["另一个无编码行", "空", "0"],
            ],
        )

        # LEFT join anchored on t_main (passed second to verify primary_table_id works)
        res_left = merge_tables(
            [t_extra, t_main],
            TableMergeConfig(
                mode="join",
                join_type="left",
                dedup_mode="smart_code",
                primary_table_id="main",
                add_source_column=False,
            ),
        )
        assert res_left.merged_table.headers == ["编码", "名称", "状态", "文件", "状态(扩展表)", "评分"]
        assert res_left.merged_table.row_count == 3
        assert res_left.merged_table.rows[0] == [
            "abc00001.mp4",
            "条目1",
            "正常",
            "[site]ABC-001-4K.mkv",
            "已归档",
            "9.5",
        ]
        assert res_left.merged_table.rows[1] == ["DEF-002", "条目2", "待检", "", "", ""]
        # Empty-key rows must NOT match each other
        assert res_left.merged_table.rows[2] == ["无编码行", "未知", "草稿", "", "", ""]

        # INNER join
        res_inner = merge_tables(
            [t_main, t_extra],
            TableMergeConfig(
                mode="join",
                join_type="inner",
                dedup_mode="smart_code",
                primary_table_id="main",
                add_source_column=True,
                add_extracted_code_column=True,
            ),
        )
        assert res_inner.merged_table.row_count == 1
        assert res_inner.merged_table.rows[0][-2:] == ["主表 + 扩展表", "ABC-001"]
        assert len(res_inner.dropped_rows) == 4  # 2 unmatched left + 2 unmatched right

        # OUTER join
        res_outer = merge_tables(
            [t_main, t_extra],
            TableMergeConfig(
                mode="join",
                join_type="outer",
                dedup_mode="smart_code",
                primary_table_id="main",
                add_source_column=False,
            ),
        )
        assert res_outer.merged_table.row_count == 5
        # Last 2 rows are right-only rows from t_extra with blank left cells
        assert res_outer.merged_table.rows[3] == ["", "", "", "GHI-003.mp4", "新增", "8.0"]
        assert res_outer.merged_table.rows[4] == ["", "", "", "另一个无编码行", "空", "0"]

    def test_merge_empty_and_invalid_config(self) -> None:
        empty_res = merge_tables([], TableMergeConfig())
        assert empty_res.merged_table.row_count == 0
        assert empty_res.stats["input_tables"] == 0

        with pytest.raises(ValueError):
            merge_tables([], TableMergeConfig(mode="bad_mode"))
        with pytest.raises(ValueError):
            merge_tables([], TableMergeConfig(mode="union_dedup", keep_strategy="bad"))

    def test_merge_strict_smart_code_column_ignore_punct_and_coalesce_provenance(
        self,
    ) -> None:
        t1 = DataTable(
            table_id="s1",
            name="表A",
            headers=["主编码", "备注"],
            rows=[["ABC-001", ""], ["", "参考 ABC-001"]],
        )
        # scan_all_columns_for_code=False must NOT scan 备注 when 主编码 is blank
        res_strict = merge_tables(
            [t1],
            TableMergeConfig(
                mode="union_dedup",
                dedup_mode="smart_code",
                key_columns={"s1": ["主编码"]},
                scan_all_columns_for_code=False,
                add_source_column=False,
            ),
        )
        assert res_strict.merged_table.row_count == 2
        assert len(res_strict.dropped_rows) == 0

        # ignore_punctuation=True + paired coalesce provenance in _原行号
        p1 = DataTable(table_id="p1", name="表A", headers=["工号", "部门"], rows=[["E-001", ""]])
        p2 = DataTable(table_id="p2", name="表B", headers=["工号", "部门"], rows=[["E_001", "研发"]])
        res_p = merge_tables(
            [p1, p2],
            TableMergeConfig(
                mode="union_dedup",
                dedup_mode="column_value",
                key_columns={"*": ["工号"]},
                ignore_punctuation=True,
                keep_strategy="coalesce",
                add_source_column=True,
                add_row_index_column=True,
            ),
        )
        assert res_p.merged_table.row_count == 1
        assert res_p.merged_table.rows[0] == ["E-001", "研发", "表A / 表B", "表A#1 / 表B#1"]

    def test_join_outer_row_index_3table_inner_dropped_and_cartesian_guards(self) -> None:
        # 1. Outer join preserves right-side _原行号
        left = DataTable(table_id="l", name="左表", headers=["K", "L"], rows=[["a", "1"]])
        right = DataTable(
            table_id="r", name="右表", headers=["K", "R"], rows=[["a", "10"], ["b", "20"]]
        )
        res_out = merge_tables(
            [left, right],
            TableMergeConfig(
                mode="join",
                join_type="outer",
                dedup_mode="column_value",
                key_columns={"*": ["K"]},
                add_source_column=True,
                add_row_index_column=True,
            ),
        )
        assert res_out.merged_table.row_count == 2
        # Row 1 (right-only "b") must keep right row index "2", not ""
        assert res_out.merged_table.rows[1][-2:] == ["右表", "2"]

        # 2. 3-table inner join with Cartesian intermediate rows that fail Table 3
        t1 = DataTable(
            table_id="j1", name="T1", headers=["K", "V1"], rows=[["x", "a"], ["y", "b"]]
        )
        t2 = DataTable(
            table_id="j2",
            name="T2",
            headers=["K", "V2"],
            rows=[["x", "1"], ["x", "2"], ["y", "3"]],
        )
        t3 = DataTable(table_id="j3", name="T3", headers=["K", "V3"], rows=[["y", "ok"]])
        res_3 = merge_tables(
            [t1, t2, t3],
            TableMergeConfig(
                mode="join",
                join_type="inner",
                dedup_mode="column_value",
                key_columns={"*": ["K"]},
                add_source_column=False,
            ),
        )
        assert res_3.merged_table.row_count == 1
        # Dropped must contain T1#1 ("x"), T2#1 ("x"), T2#2 ("x") — each exactly ONCE!
        dropped_keys = [(r.table_id, r.row_index) for r in res_3.dropped_rows]
        assert dropped_keys == [("j1", 1), ("j2", 1), ("j2", 2)]

        # 3. join_match_mode="first" vs max_join_rows explosion guard
        m1 = DataTable(table_id="m1", name="M1", headers=["K"], rows=[["dup"]] * 20)
        m2 = DataTable(table_id="m2", name="M2", headers=["K"], rows=[["dup"]] * 20)
        with pytest.raises(ValueError, match="安全上限|笛卡尔积"):
            merge_tables(
                [m1, m2],
                TableMergeConfig(
                    mode="join",
                    join_type="inner",
                    dedup_mode="column_value",
                    key_columns={"*": ["K"]},
                    max_join_rows=100,
                ),
            )

        res_first = merge_tables(
            [m1, m2],
            TableMergeConfig(
                mode="join",
                join_type="left",
                dedup_mode="column_value",
                key_columns={"*": ["K"]},
                join_match_mode="first",
                max_join_rows=100,
            ),
        )
        assert res_first.merged_table.row_count == 20


# ----------------------------------------------------------------------
# 3. Database import roundtrip tests
# ----------------------------------------------------------------------


class TestImportTableToDatabase:
    def test_import_roundtrip_and_dedup_handling(self) -> None:
        tbl = DataTable(
            table_id="imp1",
            name="合并结果表",
            headers=["标准编码", "原始文件名", "标签", "分集", "备注"],
            rows=[
                ["ABC-001", "ABC-001-4K.mkv", "4K,字幕", "", "首条"],
                ["ABC-001", "ABC-001-dup.mkv", "", "", "批内重复"],
                ["DEF-002", "DEF-002_CD1.mp4", "", "CD1", "分集1"],
                ["DEF-002", "DEF-002_CD2.mp4", "", "CD2", "分集2"],
                ["无有效编码", "说明.txt", "", "", "未识别"],
            ],
            source_type="merged",
        )
        parser = CodeParser()
        with DatabaseRepository(":memory:") as db:
            summary1 = import_table_to_database(
                tbl, db, parser_engine=parser, source_name="工作台导入"
            )
            assert summary1["total"] == 5
            assert summary1["added"] == 3  # ABC-001 + DEF-002 CD1 + DEF-002 CD2
            assert summary1["skipped"] == 1  # batch duplicate ABC-001
            assert summary1["batch_duplicates"] == 1
            assert summary1["multiparts_added"] == 1
            assert summary1["unrecognized"] == 1
            assert summary1["batch_id"] is not None

            records = db.query_records(sort_by="id", sort_desc=False)
            assert len(records) == 3
            assert records[0].canonical_code == "ABC-001"
            assert "4K" in records[0].tags
            assert "字幕" in records[0].tags
            assert records[0].note == "首条"
            assert records[0].source_name == "工作台导入"

            # Re-importing the exact same table skips all existing codes
            summary2 = import_table_to_database(tbl, db, parser_engine=parser)
            assert summary2["added"] == 0
            assert summary2["skipped"] == 4
            assert summary2["db_duplicates"] >= 3
            assert summary2["batch_id"] is None

            # Force import with skip_existing=False
            summary3 = import_table_to_database(
                tbl, db, parser_engine=parser, skip_existing=False
            )
            assert summary3["added"] == 4
            assert summary3["skipped"] == 0

    def test_db_table_db_roundtrip_explicit_part_and_code_column_validation(self) -> None:
        parser = CodeParser()
        with DatabaseRepository(":memory:") as db1, DatabaseRepository(":memory:") as db2:
            db1.add_record(
                DataRecord(
                    canonical_code="ABC-001",
                    prefix="ABC",
                    number="001",
                    file_name="clean_title_CD1.mp4",
                    part="CD2",
                    source_name="原库",
                )
            )
            exported_tbl = read_database_as_table(db1, name="原库快照")
            summary = import_table_to_database(exported_tbl, db2, parser_engine=parser)
            assert summary["added"] == 1
            rec = db2.query_records()[0]
            # "原始名称" preserves file_name, and explicit "分集" ("CD2") overrides heuristic "CD1"!
            assert rec.file_name == "clean_title_CD1.mp4"
            assert rec.part == "CD2"

            # Explicit non-existent code_column raises ValueError
            with pytest.raises(ValueError, match="不存在指定的编码列"):
                import_table_to_database(exported_tbl, db2, code_column="NO_SUCH_COL")
            with pytest.raises(ValueError, match="超出范围"):
                import_table_to_database(exported_tbl, db2, code_column=99)

            # Empty table returns zero summary cleanly
            empty_tbl = DataTable(table_id="e0", name="空表", headers=["编码"], rows=[])
            assert import_table_to_database(empty_tbl, db2)["total"] == 0

            # fallback_scan_all=True vs False with explicit code_column
            mixed_tbl = DataTable(
                table_id="fb",
                name="回退表",
                headers=["主编码", "备选列"],
                rows=[["", "XYZ-777.mp4"]],
            )
            s_strict = import_table_to_database(
                mixed_tbl, db2, parser_engine=parser, code_column="主编码", fallback_scan_all=False
            )
            assert s_strict["added"] == 0 and s_strict["unrecognized"] == 1

            s_fallback = import_table_to_database(
                mixed_tbl, db2, parser_engine=parser, code_column=0, fallback_scan_all=True
            )
            assert s_fallback["added"] == 1


# ----------------------------------------------------------------------
# 4. Exporter tests (Excel, CSV, Word, TXT, Multi-sheet, Reports, Locks)
# ----------------------------------------------------------------------


class TestTableExporter:
    def test_sanitize_helpers(self) -> None:
        assert sanitize_xml_text("hello\x00world\x0b\x1f!") == "helloworld!"
        used: set[str] = set()
        s1 = sanitize_sheet_name("a/b:c*d?e[f]\\g" + "x" * 40, used)
        s2 = sanitize_sheet_name("A/B:C*D?E[F]\\G" + "X" * 40, used)
        assert len(s1) <= 31
        assert len(s2) <= 31
        assert s1.lower() != s2.lower()
        assert sanitize_sheet_name("///", set()) == "___"
        assert sanitize_sheet_name("", set()) == "工作表"

    def test_export_table_excel_with_highlight_and_xml_clean(self, tmp_path: Path) -> None:
        tbl = DataTable(
            table_id="e1",
            name="导出测试:表[1]",
            headers=["编码", "名称\x00清理"],
            rows=[
                ["ABC-001", "第一行\x0b值"],
                ["ABC-001", "重复编码行"],
                ["XYZ-002", "唯一行"],
            ],
        )
        out = tmp_path / "single.xlsx"
        saved = export_table_excel(tbl, out, highlight_duplicates_col="编码")
        assert saved == out
        assert out.exists()

        wb = openpyxl.load_workbook(out)
        ws = wb.active
        assert ws.freeze_panes == "A2"
        assert ws.cell(1, 2).value == "名称清理"
        assert ws.cell(2, 2).value == "第一行值"
        # Row 2 & 3 share ABC-001 -> amber highlight FFF3CD
        assert ws.cell(2, 1).fill.start_color.rgb.endswith("FFF3CD")
        assert ws.cell(3, 1).fill.start_color.rgb.endswith("FFF3CD")
        assert not ws.cell(4, 1).fill.start_color.rgb.endswith("FFF3CD")

    def test_export_table_csv_utf8_sig(self, tmp_path: Path) -> None:
        tbl = DataTable(
            table_id="c1",
            name="CSV表",
            headers=["编码", "说明"],
            rows=[["ABC-001", "中文,带逗号"]],
        )
        out = tmp_path / "out.csv"
        export_table_csv(tbl, out)
        raw_bytes = out.read_bytes()
        assert raw_bytes.startswith(b"\xef\xbb\xbf")  # UTF-8-SIG BOM
        with open(out, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        assert rows == [["编码", "说明"], ["ABC-001", "中文,带逗号"]]

    def test_export_table_word_and_truncation_notice(self, tmp_path: Path) -> None:
        tbl = DataTable(
            table_id="w1",
            name="Word导出表",
            headers=["序号", "编码", "内容"],
            rows=[[str(i), f"ABC-{i:03d}", f"记录\x00_{i}"] for i in range(1, 8)],
        )
        out = tmp_path / "table.docx"
        export_table_word(tbl, out, title="自定义导出标题", max_rows=4)
        assert out.exists()

        doc = docx.Document(str(out))
        all_paras = "\n".join(p.text for p in doc.paragraphs)
        assert "自定义导出标题" in all_paras
        assert "仅展示前 4 行" in all_paras
        assert len(doc.tables) == 1
        # 1 header row + 4 truncated data rows
        assert len(doc.tables[0].rows) == 5
        assert doc.tables[0].rows[1].cells[2].text == "记录_1"

    def test_export_table_txt_full_and_single_column(self, tmp_path: Path) -> None:
        tbl = DataTable(
            table_id="x1",
            name="TXT表",
            headers=["编码", "名称"],
            rows=[["ABC-001", "条目A"], ["DEF-002", "条目B"], ["", "空编码"]],
        )
        out_full = tmp_path / "full.txt"
        export_table_txt(tbl, out_full)
        assert out_full.read_text(encoding="utf-8").splitlines() == [
            "编码\t名称",
            "ABC-001\t条目A",
            "DEF-002\t条目B",
            "\t空编码",
        ]

        out_col = tmp_path / "col0.txt"
        export_table_txt(tbl, out_col, column_index=0)
        assert out_col.read_text(encoding="utf-8").splitlines() == ["ABC-001", "DEF-002"]

        with pytest.raises(ValueError):
            export_table_txt(tbl, tmp_path / "bad.txt", column_index=9)

    def test_export_multiple_tables_workbook(self, tmp_path: Path) -> None:
        long_name = "超长工作表名称_" + "数据" * 20
        t1 = DataTable(table_id="m1", name=long_name, headers=["A"], rows=[["1"]])
        t2 = DataTable(table_id="m2", name=long_name, headers=["B"], rows=[["2"]])
        t3 = DataTable(table_id="m3", name="含非法/字符:表?", headers=["C"], rows=[["3"]])
        out = tmp_path / "multi.xlsx"
        export_multiple_tables_workbook([t1, t2, t3], out)

        wb = openpyxl.load_workbook(out)
        assert len(wb.sheetnames) == 3
        assert all(len(s) <= 31 for s in wb.sheetnames)
        assert len({s.lower() for s in wb.sheetnames}) == 3

        # Empty list handled cleanly
        out_empty = tmp_path / "empty_multi.xlsx"
        export_multiple_tables_workbook([], out_empty)
        wb_empty = openpyxl.load_workbook(out_empty)
        assert wb_empty.sheetnames == ["空数据"]

    def test_export_dedup_report_excel_and_word(self, tmp_path: Path) -> None:
        tables = _make_smart_tables()
        report = run_multi_table_dedup(tables, TableDedupConfig(mode="smart_code"))
        merged = merge_tables(
            tables, TableMergeConfig(mode="union_dedup", keep_strategy="coalesce")
        ).merged_table

        xlsx_out = tmp_path / "dedup_report.xlsx"
        export_dedup_report_excel(report, xlsx_out, merged_table=merged)
        wb = openpyxl.load_workbook(xlsx_out)
        assert wb.sheetnames == [
            "查重总览",
            "跨表重复明细",
            "表内重复明细",
            "全表共有交集",
            "各表独有差集",
            "未识别行",
            "合并去重结果",
        ]
        ws_cross = wb["跨表重复明细"]
        assert ws_cross.max_row == 1 + sum(len(g.rows) for g in report.cross_table_groups)
        ws_merged = wb["合并去重结果"]
        assert ws_merged.max_row == 1 + merged.row_count

        docx_out = tmp_path / "dedup_report.docx"
        export_dedup_report_word(report, docx_out, max_detail_rows=3)
        doc = docx.Document(str(docx_out))
        full_text = "\n".join(p.text for p in doc.paragraphs)
        assert "多表查重报告" in full_text
        assert "全表共有交集键：ABC-001" in full_text
        assert "展示前 3 条" in full_text
        assert len(doc.tables) >= 4

    def test_locked_target_file_raises_friendly_export_error(self, tmp_path: Path) -> None:
        tbl = DataTable(table_id="lk", name="锁文件测试", headers=["列1"], rows=[["值1"]])
        locked_xlsx = tmp_path / "locked.xlsx"
        locked_xlsx.write_bytes(b"placeholder")
        locked_docx = tmp_path / "locked.docx"
        locked_docx.write_bytes(b"placeholder")
        locked_csv = tmp_path / "locked.csv"
        locked_csv.write_bytes(b"placeholder")

        real_open = open

        def _deny_target(file, *args, **kwargs):
            if Path(file) in (locked_xlsx, locked_docx, locked_csv):
                raise PermissionError(13, "Permission denied", str(file))
            return real_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=_deny_target):
            with pytest.raises(ExportError, match="占用|关闭后重试"):
                export_table_excel(tbl, locked_xlsx)
            with pytest.raises(ExportError, match="占用|关闭后重试"):
                export_table_word(tbl, locked_docx)
            with pytest.raises(ExportError, match="占用|关闭后重试"):
                export_table_csv(tbl, locked_csv)
