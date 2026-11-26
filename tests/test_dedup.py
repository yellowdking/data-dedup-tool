"""Unit tests for deduplication preview, comparison, smart cleanup, and exporters."""

from __future__ import annotations

import csv
from pathlib import Path

import openpyxl
import pytest

from src.core.database import DatabaseRepository
from src.core.dedup import (
    STRATEGY_KEEP_EARLIEST,
    STRATEGY_KEEP_HIGHEST_TAG_SCORE,
    STRATEGY_KEEP_LATEST,
    STRATEGY_KEEP_LONGEST_NAME,
    commit_preview,
    compare_two_sources,
    compare_with_db,
    database_records_to_parse_results,
    execute_duplicate_cleanup,
    plan_duplicate_cleanup,
    preview_import,
)
from src.core.exporter import (
    ExportError,
    export_codes_txt,
    export_compare_result_csv,
    export_compare_result_excel,
    export_duplicate_groups_csv,
    export_duplicate_groups_excel,
    export_records_csv,
    export_records_excel,
)
from src.core.models import DataRecord, ParseResult


def _parsed(
    code: str = "",
    *,
    file_name: str = "",
    raw_text: str = "",
    source_name: str = "清单.txt",
    tags: list[str] | None = None,
    part: str = "",
    line_number: int = 1,
    file_size: int = 0,
    recognized: bool | None = None,
) -> ParseResult:
    prefix, number = ("", "")
    if "-" in code:
        prefix, number = code.split("-", 1)
    is_rec = bool(code) if recognized is None else recognized
    return ParseResult(
        canonical_code=code,
        prefix=prefix,
        number=number,
        raw_code=code,
        raw_text=raw_text or file_name or (f"{code}.mp4" if code else "无法识别的文本行"),
        file_name=file_name or (f"{code}.mp4" if code else ""),
        source_name=source_name,
        source_type="txt",
        line_number=line_number,
        file_size=file_size,
        tags=list(tags or []),
        part=part,
        matched_rule="standard_alpha_num" if is_rec else "",
        confidence=0.95 if is_rec else 0.0,
        recognized=is_rec,
    )


def _record(
    code: str,
    *,
    file_name: str = "",
    tags: list[str] | None = None,
    part: str = "",
    file_size: int = 0,
    created_at: str = "",
    source_name: str = "主库.txt",
) -> DataRecord:
    prefix, number = code.split("-", 1) if "-" in code else (code, "")
    return DataRecord(
        canonical_code=code,
        prefix=prefix,
        number=number,
        raw_code=code,
        raw_text=file_name or f"{code}.mp4",
        file_name=file_name or f"{code}.mp4",
        source_name=source_name,
        tags=list(tags or []),
        part=part,
        file_size=file_size,
        created_at=created_at,
    )


# ----------------------------------------------------------------------
# 1. Dry-run import preview & commit
# ----------------------------------------------------------------------


class TestImportPreviewAndCommit:
    def test_preview_categorizes_all_buckets_without_mutating_db(self, tmp_path: Path) -> None:
        with DatabaseRepository(tmp_path / "preview.db") as db:
            db.add_records(
                [
                    _record("ABC-001", file_name="ABC-001.mp4"),
                    _record("PRJ-100", file_name="PRJ-100-CD1.mp4", part="CD1"),
                ]
            )

            incoming = [
                _parsed("NEW-001", file_name="NEW-001.mp4"),
                _parsed("NEW-001", file_name="NEW-001_copy.mp4"),  # batch duplicate
                _parsed("ABC-001", file_name="ABC-001_dup.mp4"),  # db duplicate
                _parsed("PRJ-100", file_name="PRJ-100-CD2.mp4", part="CD2"),  # multipart vs DB
                _parsed("PRJ-100", file_name="PRJ-100-CD1_again.mp4", part="CD1"),  # db duplicate (same part)
                _parsed("MOD-200", file_name="MOD-200-CD1.mp4", part="CD1"),  # new
                _parsed("MOD-200", file_name="MOD-200-CD2.mp4", part="CD2"),  # multipart inside batch
                _parsed("", raw_text="随机备注无编码", recognized=False),  # unrecognized
            ]

            preview = preview_import(incoming, db, skipped_companions=3)

            # DB untouched
            assert db.count_records() == 2

            assert [i.canonical_code for i in preview.new_items] == ["NEW-001", "MOD-200"]
            assert [i.canonical_code for i, _ in preview.db_duplicates] == ["ABC-001", "PRJ-100"]
            assert [i.canonical_code for i in preview.batch_duplicates] == ["NEW-001"]
            assert [(i.canonical_code, i.part) for i in preview.possible_multiparts] == [
                ("PRJ-100", "CD2"),
                ("MOD-200", "CD2"),
            ]
            assert len(preview.unrecognized) == 1
            assert preview.skipped_companions == 3
            assert preview.total_scanned == len(incoming) + 3

            # Commit new + multiparts (default)
            saved, batch = commit_preview(preview, db, source_name="新批次.txt")
            assert len(saved) == 4  # NEW-001, MOD-200(CD1), PRJ-100(CD2), MOD-200(CD2)
            assert batch.added_count == 4
            assert batch.duplicate_count == 3  # 2 db + 1 batch
            assert batch.unrecognized_count == 1
            assert db.count_records() == 6

    def test_commit_with_manual_fixed_extras_and_all_flags(self, tmp_path: Path) -> None:
        with DatabaseRepository(tmp_path / "commit_all.db") as db:
            db.add_record(_record("ABC-001"))
            incoming = [
                _parsed("ABC-001", file_name="ABC-001_v2.mp4"),
                _parsed("NEW-010"),
                _parsed("NEW-010"),
            ]
            preview = preview_import(incoming, db)
            fixed_item = _parsed("FIX-777", file_name="手工修正项.mp4")

            saved, _ = commit_preview(
                preview,
                db,
                include_multiparts=True,
                include_db_duplicates=True,
                include_batch_duplicates=True,
                extra_items=[fixed_item],
            )
            assert len(saved) == 4
            assert db.count_records() == 5

    def test_preview_batch_duplicate_of_new_multipart_part_not_misclassified_as_db_dup(
        self, tmp_path: Path
    ) -> None:
        with DatabaseRepository(tmp_path / "mp_batch_dup.db") as db:
            db.add_record(_record("MP-001", file_name="MP-001-CD1.mp4", part="CD1"))

            incoming = [
                _parsed("MP-001", file_name="MP-001-CD2_a.mp4", part="CD2"),
                _parsed("MP-001", file_name="MP-001-CD2_b.mp4", part="CD2"),
                _parsed("MP-001", file_name="MP-001-CD1_copy.mp4", part="CD1"),
            ]
            preview = preview_import(incoming, db)

            assert [(i.canonical_code, i.file_name) for i in preview.possible_multiparts] == [
                ("MP-001", "MP-001-CD2_a.mp4")
            ]
            assert [(i.canonical_code, i.file_name) for i in preview.batch_duplicates] == [
                ("MP-001", "MP-001-CD2_b.mp4")
            ]
            assert [(i.canonical_code, i.file_name) for i, _ in preview.db_duplicates] == [
                ("MP-001", "MP-001-CD1_copy.mp4")
            ]

    def test_commit_preview_infers_source_type_and_db_batch_dup_split(
        self, tmp_path: Path
    ) -> None:
        with DatabaseRepository(tmp_path / "infer_st.db") as db:
            db.add_record(_record("ABC-001", file_name="ABC-001.mp4"))

            item_folder = _parsed("NEW-101", file_name="NEW-101.mp4")
            item_folder.source_type = "folder"
            dup1 = _parsed("ABC-001", file_name="ABC-001_copy1.mp4")
            dup1.source_type = "folder"
            dup2 = _parsed("ABC-001", file_name="ABC-001_copy2.mp4")
            dup2.source_type = "folder"

            preview = preview_import([item_folder, dup1, dup2], db)
            assert len(preview.db_duplicates) == 1
            assert len(preview.batch_duplicates) == 1

            saved, batch = commit_preview(
                preview,
                db,
                include_db_duplicates=True,
                include_batch_duplicates=False,
            )
            assert batch.source_type == "folder"
            # NEW-101 + only 1 copy of ABC-001 (batch duplicate excluded)
            assert len(saved) == 2


# ----------------------------------------------------------------------
# 2. Pure DB compare & 3. A-vs-B cross-comparison
# ----------------------------------------------------------------------


class TestComparisonEngine:
    def test_compare_with_db_does_not_mutate(self, tmp_path: Path) -> None:
        with DatabaseRepository(tmp_path / "cmp.db") as db:
            db.add_records([_record("ABC-001"), _record("ABC-001", file_name="ABC-001_2.mp4")])
            items = [
                _parsed("ABC-001", file_name="待查_ABC-001.mp4"),
                _parsed("NEW-888", file_name="待查_NEW-888.mp4"),
                _parsed("", raw_text="无效行", recognized=False),
            ]
            res = compare_with_db(items, db, source_name="粘贴清单")

            assert res.mode == "db"
            assert db.count_records() == 2  # unchanged
            assert len(res.matched_in_db) == 1
            assert res.matched_in_db[0][0].canonical_code == "ABC-001"
            assert len(res.matched_in_db[0][1]) == 2
            assert [i.canonical_code for i in res.not_in_db] == ["NEW-888"]
            assert len(res.unrecognized_a) == 1

    def test_compare_two_sources_ab(self) -> None:
        list_a = [
            _parsed("ABC-001", file_name="A_ABC-001.mp4"),
            _parsed("DOC-002", file_name="A_DOC-002.mp4"),
            _parsed("ONLY-A1", file_name="ONLY-A1.mp4"),
            _parsed("", raw_text="A未识别", recognized=False),
        ]
        list_b = [
            _parsed("DOC-002", file_name="B_DOC-002.mp4"),
            _parsed("ABC-001", file_name="B_ABC-001.mp4"),
            _parsed("ONLY-B1", file_name="ONLY-B1.mp4"),
            _parsed("", raw_text="B未识别", recognized=False),
        ]
        res = compare_two_sources(list_a, list_b, source_a_name="磁盘A.txt", source_b_name="磁盘B.txt")

        assert res.mode == "ab"
        assert res.intersection_codes == ["ABC-001", "DOC-002"]
        assert len(res.intersection_a) == 2
        assert len(res.intersection_b) == 2
        assert [i.canonical_code for i in res.only_in_a] == ["ONLY-A1"]
        assert [i.canonical_code for i in res.only_in_b] == ["ONLY-B1"]
        assert len(res.unrecognized_a) == 1
        assert len(res.unrecognized_b) == 1


# ----------------------------------------------------------------------
# 4. Smart duplicate cleanup selection strategies & multipart protection
# ----------------------------------------------------------------------


class TestSmartDuplicateCleanup:
    def test_all_four_strategies(self, tmp_path: Path) -> None:
        with DatabaseRepository(tmp_path / "cleanup.db") as db:
            r1 = db.add_record(
                _record(
                    "ABC-001",
                    file_name="ABC-001.mp4",
                    tags=["HD"],
                    created_at="2026-01-01 10:00:00",
                )
            )
            r2 = db.add_record(
                _record(
                    "ABC-001",
                    file_name="ABC-001_4K_字幕版_详细归档名称.mp4",
                    tags=["4K", "字幕"],
                    created_at="2026-01-02 10:00:00",
                )
            )
            r3 = db.add_record(
                _record(
                    "ABC-001",
                    file_name="ABC-001_latest_ultra_long_descriptive_filename_extra.mp4",
                    tags=[],
                    created_at="2026-01-03 10:00:00",
                )
            )

            groups = db.get_duplicate_groups()

            # 1. keep_earliest -> keep r1
            p_early = plan_duplicate_cleanup(groups, strategy=STRATEGY_KEEP_EARLIEST)
            assert p_early.keep_ids == [r1.id]
            assert set(p_early.delete_ids) == {r2.id, r3.id}

            # 2. keep_latest -> keep r3
            p_late = plan_duplicate_cleanup(groups, strategy=STRATEGY_KEEP_LATEST)
            assert p_late.keep_ids == [r3.id]
            assert set(p_late.delete_ids) == {r1.id, r2.id}

            # 3. keep_highest_tag_score -> keep r2 (4K+字幕 = 9)
            p_tag = plan_duplicate_cleanup(groups, strategy=STRATEGY_KEEP_HIGHEST_TAG_SCORE)
            assert p_tag.keep_ids == [r2.id]
            assert set(p_tag.delete_ids) == {r1.id, r3.id}

            # 4. keep_longest_name -> keep r3
            p_long = plan_duplicate_cleanup(groups, strategy=STRATEGY_KEEP_LONGEST_NAME)
            assert p_long.keep_ids == [r3.id]
            assert set(p_long.delete_ids) == {r1.id, r2.id}

    def test_multipart_protection_and_execution_with_backup(self, tmp_path: Path) -> None:
        with DatabaseRepository(tmp_path / "mp_cleanup.db", auto_backup=True) as db:
            # Pure multipart pair: CD1 + CD2 -> must be completely protected
            cd1 = db.add_record(_record("PRJ-100", file_name="PRJ-100-CD1.mp4", part="CD1"))
            cd2 = db.add_record(_record("PRJ-100", file_name="PRJ-100-CD2.mp4", part="CD2"))

            # Mixed group: two copies of CD1 (one 4K, one plain) + one CD2
            mix_cd1_low = db.add_record(_record("MIX-200", file_name="MIX-200-CD1.mp4", part="CD1"))
            mix_cd1_4k = db.add_record(
                _record("MIX-200", file_name="MIX-200-CD1_4K.mp4", part="CD1", tags=["4K"])
            )
            mix_cd2 = db.add_record(_record("MIX-200", file_name="MIX-200-CD2.mp4", part="CD2"))

            groups = db.get_duplicate_groups()

            plan_protected = plan_duplicate_cleanup(
                groups,
                strategy=STRATEGY_KEEP_HIGHEST_TAG_SCORE,
                protect_multipart=True,
            )
            assert "PRJ-100" in plan_protected.protected_groups
            assert cd1.id in plan_protected.keep_ids
            assert cd2.id in plan_protected.keep_ids
            assert mix_cd1_4k.id in plan_protected.keep_ids
            assert mix_cd2.id in plan_protected.keep_ids
            assert plan_protected.delete_ids == [mix_cd1_low.id]

            # Without protection, even CD1/CD2 gets reduced to 1 record per code.
            plan_unprotected = plan_duplicate_cleanup(
                groups,
                strategy=STRATEGY_KEEP_HIGHEST_TAG_SCORE,
                protect_multipart=False,
            )
            assert len(plan_unprotected.delete_ids) == 3

            # Execute protected cleanup and verify automatic backup created
            deleted_count = execute_duplicate_cleanup(db, plan_protected.delete_ids, create_backup=True)
            assert deleted_count == 1
            assert db.count_records() == 4
            assert len(db.list_backups()) == 1


# ----------------------------------------------------------------------
# 5. Exporters (.xlsx, .csv UTF-8-SIG, .txt) & locked-file handling
# ----------------------------------------------------------------------


class TestExporters:
    def test_export_records_excel_csv_txt(self, tmp_path: Path) -> None:
        with DatabaseRepository(tmp_path / "exp.db") as db:
            db.add_records(
                [
                    _record("ABC-001", file_name="ABC-001_4K.mp4", tags=["4K", "字幕"]),
                    _record("ABC-001", file_name="ABC-001_dup.mp4", tags=["HD"]),
                    _record("PRJ-050", file_name="PRJ-050-CD1.mp4", part="CD1"),
                ]
            )
            records = db.query_records(sort_by="id", sort_desc=False)
            stats = db.get_stats()
            counts = db.get_code_counts()

            xlsx_path = tmp_path / "records.xlsx"
            export_records_excel(records, xlsx_path, code_counts=counts, stats=stats)
            assert xlsx_path.is_file()

            wb = openpyxl.load_workbook(xlsx_path)
            assert wb.sheetnames == ["数据清单", "汇总统计"]
            ws = wb["数据清单"]
            assert ws.cell(1, 1).value == "标准编码"
            assert ws.cell(2, 1).value == "ABC-001"
            assert ws.cell(2, 4).value == 2  # 重复次数
            assert ws.freeze_panes == "A2"
            assert ws.auto_filter.ref == "A1:N4"
            wb.close()

            csv_path = tmp_path / "records.csv"
            export_records_csv(records, csv_path, code_counts=counts)
            raw_bytes = csv_path.read_bytes()
            assert raw_bytes.startswith(b"\xef\xbb\xbf")  # UTF-8-SIG BOM
            with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
            assert len(rows) == 4
            assert rows[0][0] == "标准编码"

            txt_path = tmp_path / "codes.txt"
            export_codes_txt(records, txt_path, unique=True, sort_codes=True)
            assert txt_path.read_text(encoding="utf-8").splitlines() == ["ABC-001", "PRJ-050"]

    def test_export_duplicate_groups_and_compare_results(self, tmp_path: Path) -> None:
        with DatabaseRepository(tmp_path / "exp2.db") as db:
            db.add_records(
                [
                    _record("ABC-001", file_name="ABC-001_1.mp4"),
                    _record("ABC-001", file_name="ABC-001_2.mp4"),
                    _record("PRJ-200", file_name="PRJ-200-CD1.mp4", part="CD1"),
                    _record("PRJ-200", file_name="PRJ-200-CD2.mp4", part="CD2"),
                ]
            )
            groups = db.get_duplicate_groups()

            dup_xlsx = tmp_path / "dups.xlsx"
            export_duplicate_groups_excel(groups, dup_xlsx)
            wb = openpyxl.load_workbook(dup_xlsx)
            assert wb.sheetnames == ["重复项明细", "重复分组汇总"]
            assert wb["重复分组汇总"].max_row == 3
            wb.close()

            dup_csv = tmp_path / "dups.csv"
            export_duplicate_groups_csv(groups, dup_csv)
            assert dup_csv.read_bytes().startswith(b"\xef\xbb\xbf")

            # DB comparison export
            cmp_db = compare_with_db(
                [_parsed("ABC-001"), _parsed("NEW-111"), _parsed("", recognized=False)],
                db,
                source_name="新列表.txt",
            )
            cmp_db_xlsx = tmp_path / "cmp_db.xlsx"
            export_compare_result_excel(cmp_db, cmp_db_xlsx)
            wb = openpyxl.load_workbook(cmp_db_xlsx)
            assert wb.sheetnames == ["库内已存在", "未找到(新数据)", "未识别", "比对汇总"]
            wb.close()

            cmp_db_csv = tmp_path / "cmp_db.csv"
            export_compare_result_csv(cmp_db, cmp_db_csv)
            assert cmp_db_csv.is_file()

            # A-vs-B comparison export
            cmp_ab = compare_two_sources(
                [_parsed("ABC-001"), _parsed("ONLY-A")],
                [_parsed("ABC-001"), _parsed("ONLY-B")],
                source_a_name="A.txt",
                source_b_name="B.txt",
            )
            cmp_ab_xlsx = tmp_path / "cmp_ab.xlsx"
            export_compare_result_excel(cmp_ab, cmp_ab_xlsx)
            wb = openpyxl.load_workbook(cmp_ab_xlsx)
            assert wb.sheetnames == [
                "覆盖率总览",
                "目标清单逐条核对",
                "缺失清单(仅目标B有)",
                "已包含对照(A∩B)",
                "仅基准A独有",
            ]
            wb.close()

            cmp_ab_csv = tmp_path / "cmp_ab.csv"
            export_compare_result_csv(cmp_ab, cmp_ab_csv)
            assert cmp_ab_csv.is_file()

    def test_locked_target_raises_friendly_export_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        locked_file = tmp_path / "locked_report.xlsx"
        locked_file.write_bytes(b"placeholder")

        real_open = open

        def fake_open(path, mode="r", *args, **kwargs):
            if Path(path) == locked_file and "r+b" in mode:
                raise PermissionError(13, "Permission denied", str(path))
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", fake_open)

        with pytest.raises(ExportError) as exc_info:
            export_records_excel([_record("ABC-001")], locked_file)
        assert "Excel" in str(exc_info.value) or "WPS" in str(exc_info.value)

        with pytest.raises(ExportError) as exc_csv:
            export_records_csv([_record("ABC-001")], locked_file)
        assert "占用" in str(exc_csv.value)

        with pytest.raises(ExportError) as exc_txt:
            export_codes_txt([_record("ABC-001")], locked_file)
        assert "占用" in str(exc_txt.value)

    def test_excel_export_sanitizes_illegal_control_characters(self, tmp_path: Path) -> None:
        dirty_record = _record(
            "ABC-001",
            file_name="ABC-001\x01dirty\x0bname\x1f.mp4",
            source_name="清单\x02.txt",
        )
        dirty_record.note = "备注\x00含控制字符\x0c"

        out_path = tmp_path / "sanitized.xlsx"
        export_records_excel([dirty_record], out_path)

        wb = openpyxl.load_workbook(out_path)
        ws = wb["数据清单"]
        assert ws.cell(2, 7).value == "ABC-001dirtyname.mp4"
        assert ws.cell(2, 9).value == "清单.txt"
        assert ws.cell(2, 14).value == "备注含控制字符"
        wb.close()


# ----------------------------------------------------------------------
# 6. v1.3.0 Dual-List Coverage & Missing-Item Comparison
# ----------------------------------------------------------------------


class TestDualListCoverageComparison:
    def test_smart_code_checklist_order_tags_names_and_full_coverage(self) -> None:
        list_a = [
            _parsed("PRJ-002", file_name="PRJ-002_4K.mp4", tags=["4K"]),
            _parsed("PRJ-002", file_name="PRJ-002_字幕版.mkv", tags=["字幕"]),
            _parsed("PRJ-001", file_name="PRJ-001_FHD.mp4", tags=["FHD"]),
            _parsed("EXTRA-999", file_name="EXTRA-999.mp4"),
        ]
        # Target List B deliberately unordered (PRJ-003 -> PRJ-002 -> PRJ-001 -> dup PRJ-002)
        list_b = [
            _parsed("PRJ-003", raw_text="#1 PRJ-003 缺失项示例"),
            _parsed("PRJ-002", raw_text="#2 PRJ-002 命中两份"),
            _parsed("PRJ-001", raw_text="#3 PRJ-001 命中一份"),
            _parsed("PRJ-002", raw_text="#4 PRJ-002 重复出现不重复计数"),
            _parsed("", raw_text="=== 无效说明行 ===", recognized=False),
        ]

        res = compare_two_sources(list_a, list_b, "基准库A", "目标榜单B")

        assert res.mode == "ab"
        assert res.match_mode == "smart_code"
        assert res.source_a_name == "基准库A"
        assert res.source_b_name == "目标榜单B"
        assert res.unique_a_count == 3
        assert res.unique_b_count == 3
        assert res.hit_b_count == 2
        assert res.missing_b_count == 1
        assert pytest.approx(res.coverage_rate, rel=1e-4) == (200.0 / 3.0)
        assert res.is_fully_covered is False
        # Exact Target List B order preserved!
        assert res.missing_b_codes == ["PRJ-003"]
        assert res.matched_b_codes == ["PRJ-002", "PRJ-001"]
        assert [e.order_index for e in res.target_checklist] == [1, 2, 3]
        assert [e.canonical_code for e in res.target_checklist] == ["PRJ-003", "PRJ-002", "PRJ-001"]
        assert [e.status for e in res.target_checklist] == ["missing", "matched", "matched"]

        # Matched entry carries A's count, filenames, combined tags, and items
        entry_2 = res.target_checklist[1]
        assert entry_2.matched_a_count == 2
        assert entry_2.matched_a_names == ["PRJ-002_4K.mp4", "PRJ-002_字幕版.mkv"]
        assert entry_2.matched_a_tags == "4K,字幕"
        assert len(entry_2.matched_a_items) == 2

        # 100% coverage verdict
        res_full = compare_two_sources(list_a, [_parsed("PRJ-002"), _parsed("PRJ-001")])
        assert res_full.coverage_rate == 100.0
        assert res_full.is_fully_covered is True
        assert res_full.missing_b_count == 0

        # Empty target list is NOT fully covered
        res_empty = compare_two_sources(list_a, [])
        assert res_empty.coverage_rate == 0.0
        assert res_empty.is_fully_covered is False

    def test_exact_line_mode_strings_and_parse_results(self) -> None:
        base_lines = ["  财务部-张三  ", "研发部-李四", "Alpha Project", "  "]
        target_lines = ["alpha project", "运营部-王五", "财务部-张三", "ALPHA PROJECT"]

        res_ci = compare_two_sources(
            base_lines,
            target_lines,
            "现有人员",
            "应到人员",
            match_mode="exact_line",
            ignore_case=True,
            strip_whitespace=True,
        )
        assert res_ci.match_mode == "exact_line"
        assert res_ci.unique_a_count == 3
        assert res_ci.unique_b_count == 3
        assert res_ci.hit_b_count == 2
        assert res_ci.missing_b_count == 1
        assert res_ci.matched_b_codes == ["alpha project", "财务部-张三"]
        assert res_ci.missing_b_codes == ["运营部-王五"]
        assert len(res_ci.unrecognized_a) == 1

        # Case-sensitive exact_line mode
        res_cs = compare_two_sources(
            ["Alpha", "Beta"],
            ["alpha", "Beta"],
            match_mode="exact_line",
            ignore_case=False,
        )
        assert res_cs.matched_b_codes == ["Beta"]
        assert res_cs.missing_b_codes == ["alpha"]

        # ParseResult inputs with unrecognized plain text are compared without mutation
        pr_a = ParseResult(raw_text="普通办公条目A", recognized=False)
        pr_b = ParseResult(raw_text="普通办公条目A", recognized=False)
        res_pr = compare_two_sources([pr_a], [pr_b], match_mode="exact_line")
        assert res_pr.is_fully_covered is True
        assert pr_a.recognized is False
        assert pr_a.canonical_code == ""

        with pytest.raises(ValueError, match="不支持的比对模式"):
            compare_two_sources([], [], match_mode="invalid_mode")

    def test_5000_db_vs_250_ranked_target_coverage_and_speed(self, tmp_path: Path) -> None:
        import time

        from src.core.parser import CodeParser

        with DatabaseRepository(tmp_path / "bench5000.db") as db:
            records = []
            # 220 codes that will match Target List B (plus 4,780 other library items = 5,000 total)
            for i in range(1, 221):
                tags = ["4K", "字幕"] if i % 2 == 0 else ["FHD"]
                records.append(
                    _record(
                        f"CAT-{i:03d}",
                        file_name=f"archive_CAT-{i:03d}_{'4K_中字' if i % 2 == 0 else 'FHD'}.mp4",
                        tags=tags,
                        source_name="主归档库",
                    )
                )
            for j in range(1, 4781):
                records.append(
                    _record(
                        f"LIB-{j:04d}",
                        file_name=f"LIB-{j:04d}.mkv",
                        source_name="主归档库",
                    )
                )
            db.add_records(records)
            assert db.count_records() == 5000

            t_load0 = time.perf_counter()
            items_a = database_records_to_parse_results(db)
            t_load = time.perf_counter() - t_load0
            assert len(items_a) == 5000
            assert t_load < 0.5  # Fast SQLite-to-ParseResult without regex parsing

        # Build 250-item ranked target list with heavy ranking noise: 220 present + 30 missing
        parser = CodeParser()
        raw_target_lines = []
        for rank in range(1, 251):
            code = f"CAT-{rank:03d}" if rank <= 220 else f"MISS-{rank:03d}"
            if rank % 5 == 0:
                line = f"#{rank} TOP250 No.{rank} {code} 年度精选条目"
            elif rank % 5 == 1:
                line = f"{rank:03d}. {code} 归档说明"
            elif rank % 5 == 2:
                line = f"Rank {rank} PAGE12 {code} 标题文本"
            elif rank % 5 == 3:
                line = f"第{rank}名 NO.{rank:03d} {code}"
            else:
                line = f"Top {rank:02d}: ITEM01 {code}"
            raw_target_lines.append(line)

        items_b = []
        for idx, line in enumerate(raw_target_lines, start=1):
            parsed = parser.parse_line(line, source_name="目标TOP250.txt", line_number=idx)
            # Zero ranking-token false positives: every line yields exactly 1 real code
            assert len(parsed) == 1, f"Line {idx} ({line!r}) produced {[p.canonical_code for p in parsed]}"
            items_b.extend(parsed)

        t_cmp0 = time.perf_counter()
        res = compare_two_sources(items_a, items_b, "当前总库(5000条)", "目标TOP250.txt")
        t_cmp = time.perf_counter() - t_cmp0

        assert t_cmp < 1.0, f"5000-vs-250 compare took too long: {t_cmp:.3f}s"
        assert res.unique_a_count == 5000
        assert res.unique_b_count == 250
        assert res.hit_b_count == 220
        assert res.missing_b_count == 30
        assert pytest.approx(res.coverage_rate, abs=1e-6) == 88.0
        assert res.is_fully_covered is False
        assert len(res.target_checklist) == 250
        assert [e.order_index for e in res.target_checklist] == list(range(1, 251))
        assert res.missing_b_codes == [f"MISS-{r:03d}" for r in range(221, 251)]
        assert res.target_checklist[1].canonical_code == "CAT-002"
        assert res.target_checklist[1].matched_a_tags == "4K,字幕"
        assert "archive_CAT-002_4K_中字.mp4" in res.target_checklist[1].matched_a_names
