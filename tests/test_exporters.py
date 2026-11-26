"""Comprehensive unit and integration tests for exporter.py and table_exporter.py.

Covers:
- Formula injection neutralization in Excel (.xlsx) and CSV (.csv)
- Sheet name sanitization (reserved "History", edge apostrophes after 31-char slice, uniqueness)
- Excel hard limits (32,767-char cell cap, 1,048,576 rows, 16,384 cols) & XML sanitization
- Empty-header handling without silent cell drops across Excel, CSV, Word, and TXT
- CSV/TSV newline & NUL hygiene + export_table_txt column_index bounds checks
- Atomic writes, directory-as-target errors, mkdir OSError handling, early lock checks,
  and temp-file cleanup on non-OSError failures
- High-speed Word (.docx) 1,000-row export, OOXML roundtrip fidelity (text, header shading,
  repeating header, zebra striping, borders), progress_callback & cancel_check,
  and multi-sheet / Word dedup report truncation notices
"""

from __future__ import annotations

import csv
import time
from pathlib import Path

import docx
import openpyxl
import pytest

import src.core.exporter as exporter_mod
import src.core.table_exporter as table_exporter_mod
from src.core.exporter import (
    EXCEL_MAX_CELL_CHARS,
    EXCEL_MAX_COLS,
    EXCEL_MAX_ROWS,
    ExportError,
    _atomic_write_file,
    _sanitize_csv_value,
    _sanitize_excel_value,
    _write_styled_sheet,
    export_codes_txt,
    export_compare_result_csv,
    export_compare_result_excel,
    export_duplicate_groups_csv,
    export_duplicate_groups_excel,
    export_records_csv,
    export_records_excel,
    neutralize_formula_text,
    sanitize_xml_text,
)
from src.core.models import (
    CompareResult,
    DataRecord,
    DuplicateGroup,
    LibraryStats,
    ParseResult,
)
from src.core.table_data import (
    DataTable,
    TableDedupConfig,
    TableDedupReport,
    TableDuplicateGroup,
    TableRowRef,
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
)


def _rec(
    code: str = "ABC-001",
    *,
    file_name: str = "ABC-001.mp4",
    source_name: str = "list.txt",
    note: str = "",
) -> DataRecord:
    prefix, _, num = code.partition("-")
    return DataRecord(
        canonical_code=code,
        prefix=prefix,
        number=num,
        raw_code=code,
        raw_text=file_name,
        file_name=file_name,
        full_path=f"D:/media/{file_name}",
        source_name=source_name,
        source_type="txt",
        matched_rule="standard",
        confidence=0.95,
        created_at="2026-01-01 10:00:00",
        note=note,
    )


def _make_report(
    *,
    cross_groups: int = 2,
    intra_groups: int = 2,
    rows_per_group: int = 2,
    include_unrec: bool = True,
) -> TableDedupReport:
    cross_list: list[TableDuplicateGroup] = []
    for g in range(cross_groups):
        key = f"CROSS-{g:03d}"
        refs = [
            TableRowRef(
                table_id=f"t{r % 2}",
                table_name=f"表{(r % 2) + 1}",
                row_index=g * rows_per_group + r + 1,
                key_value=key,
                row_data=[key, f"标题_{g}_{r}", "=CMD|'/C calc'!A0"],
                headers=["编码", "标题", "备注"],
                parsed_code=key,
            )
            for r in range(rows_per_group)
        ]
        cross_list.append(
            TableDuplicateGroup(
                key_value=key,
                is_cross_table=True,
                table_names=["表1", "表2"],
                table_count=2,
                total_rows=len(refs),
                rows=refs,
            )
        )

    intra_list: list[TableDuplicateGroup] = []
    for g in range(intra_groups):
        key = f"INTRA-{g:03d}"
        refs = [
            TableRowRef(
                table_id="t0",
                table_name="表1",
                row_index=r + 1,
                key_value=key,
                row_data=[key, f"重复项_{g}_{r}"],
                headers=["编码", "名称"],
                parsed_code=key,
            )
            for r in range(2)
        ]
        intra_list.append(
            TableDuplicateGroup(
                key_value=key,
                is_cross_table=False,
                table_names=["表1"],
                table_count=1,
                total_rows=len(refs),
                rows=refs,
            )
        )

    unrec = (
        [
            TableRowRef(
                table_id="t0",
                table_name="表1",
                row_index=99,
                key_value="",
                row_data=["无编码文本", "说明"],
                headers=["编码", "名称"],
            )
        ]
        if include_unrec
        else []
    )

    return TableDedupReport(
        config=TableDedupConfig(mode="smart_code"),
        table_names=["表1", "表2"],
        table_stats={
            "t0": {
                "table_name": "表1",
                "source_type": "excel",
                "total_rows": 20,
                "unique_keys": 15,
                "cross_dup_rows": cross_groups,
                "intra_dup_rows": intra_groups * 2,
                "exclusive_rows": 5,
                "unrecognized_rows": len(unrec),
            },
            "t1": {
                "table_name": "表2",
                "source_type": "csv",
                "total_rows": 18,
                "unique_keys": 14,
                "cross_dup_rows": cross_groups,
                "intra_dup_rows": 0,
                "exclusive_rows": 4,
                "unrecognized_rows": 0,
            },
        },
        cross_table_groups=cross_list,
        intra_table_groups=intra_list,
        intersection_keys=[g.key_value for g in cross_list],
        intersection_rows=[g.rows[0] for g in cross_list],
        unique_by_table={
            "表1": [
                TableRowRef(
                    table_id="t0",
                    table_name="表1",
                    row_index=10,
                    key_value="UNIQ-001",
                    row_data=["UNIQ-001", "独有项"],
                    headers=["编码", "名称"],
                    parsed_code="UNIQ-001",
                )
            ]
        },
        total_rows=38,
        unique_key_count=20,
        unrecognized_rows=unrec,
    )


# ----------------------------------------------------------------------
# 1. Formula injection protection (Excel & CSV)
# ----------------------------------------------------------------------


class TestFormulaInjectionProtection:
    @pytest.mark.parametrize(
        "payload",
        [
            "=CMD|'/C calc'!A0",
            "=SUM(A1:A10)",
            "+CMD|'/C calc'!A0",
            "-1+1+cmd|' /C calc'!A0",
            "@SUM(1,2)",
            "\t=1+1",
            "\r=1+1",
        ],
    )
    def test_neutralize_formula_text_escapes_dangerous_prefixes(self, payload: str) -> None:
        safe = neutralize_formula_text(payload)
        assert safe.startswith("'")
        assert safe == "'" + payload

    @pytest.mark.parametrize("safe_text", ["-123.45", "+12", "-0", "+3.14", "ABC-001", "普通文本", ""])
    def test_neutralize_formula_text_preserves_signed_numbers_and_normal_strings(
        self, safe_text: str
    ) -> None:
        assert neutralize_formula_text(safe_text) == safe_text

    def test_excel_formula_injection_never_emits_formula_cells(self, tmp_path: Path) -> None:
        payloads = [
            "=CMD|'/C calc'!A0",
            "+SUM(A1:A2)",
            "-1+1",
            "@SUM(1,2)",
            "\t=1+1",
            "\r=1+1",
        ]
        tbl = DataTable(
            table_id="f1",
            name="公式注入测试",
            headers=["=HEADER_INJECT", "安全列", "纯数字字符串"],
            rows=[
                [payload, "正常值", "-123.45"]
                for payload in payloads
            ] + [["+42", "ABC-001", "-0.5"]],
        )
        out = tmp_path / "formula_safe.xlsx"
        export_table_excel(tbl, out)

        wb = openpyxl.load_workbook(out, data_only=False)
        ws = wb.active
        # Header must be neutralized and string type
        hdr_cell = ws.cell(1, 1)
        assert hdr_cell.data_type == "s"
        assert hdr_cell.value == "'=HEADER_INJECT"

        # Every payload cell must be string ('s'), NEVER formula ('f')
        for idx, payload in enumerate(payloads, start=2):
            cell = ws.cell(idx, 1)
            assert cell.data_type == "s", f"Payload {payload!r} emitted data_type={cell.data_type!r}"
            assert str(cell.value).startswith("'")

        # Pure signed number strings remain untouched and non-formula
        assert ws.cell(2, 3).value == "-123.45"
        assert ws.cell(2, 3).data_type == "s"
        assert ws.cell(len(payloads) + 2, 1).value == "+42"
        wb.close()

    def test_export_records_excel_and_csv_neutralize_formulas(self, tmp_path: Path) -> None:
        rec = _rec(
            "ABC-001",
            file_name="=CMD|'/C calc'!A0",
            source_name="+evil_source",
            note="@SUM(1,2)\r\nsecond_line\x00",
        )
        xlsx_out = tmp_path / "rec_formula.xlsx"
        export_records_excel([rec], xlsx_out)
        wb = openpyxl.load_workbook(xlsx_out, data_only=False)
        ws = wb["数据清单"]
        assert ws.cell(2, 7).data_type == "s"
        assert ws.cell(2, 7).value == "'=CMD|'/C calc'!A0"
        assert ws.cell(2, 9).value == "'+evil_source"
        assert ws.cell(2, 14).data_type == "s"
        assert ws.cell(2, 14).value.startswith("'@SUM(1,2)")
        assert "\x00" not in ws.cell(2, 14).value
        wb.close()

        csv_out = tmp_path / "rec_formula.csv"
        export_records_csv([rec], csv_out)
        raw = csv_out.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert b"\x00" not in raw
        with open(csv_out, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 2  # multiline note collapsed so CSV stays 2 rows
        assert rows[1][6] == "'=CMD|'/C calc'!A0"
        assert rows[1][8] == "'+evil_source"
        assert rows[1][13] == "'@SUM(1,2) second_line"

    def test_table_csv_neutralizes_formulas_and_preserves_signed_numbers(
        self, tmp_path: Path
    ) -> None:
        tbl = DataTable(
            table_id="fc",
            name="CSV安全",
            headers=["=H1", "数值", "文本"],
            rows=[
                ["=CMD|'/C calc'!A0", "-123.45", "第一行\r\n第二行\x00\ufffe"],
                ["\r=1+1", "+99", "\t=2+2"],
            ],
        )
        out = tmp_path / "safe.csv"
        export_table_csv(tbl, out)
        with open(out, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        assert rows[0][0] == "'=H1"
        assert rows[1] == ["'=CMD|'/C calc'!A0", "-123.45", "第一行 第二行"]
        assert rows[2][0].startswith("'")
        assert "\r" not in rows[2][0]
        assert rows[2][1] == "+99"
        assert rows[2][2].startswith("'")


# ----------------------------------------------------------------------
# 2. Sheet name sanitization
# ----------------------------------------------------------------------


class TestSheetNameSanitization:
    @pytest.mark.parametrize("name", ["History", "history", "HISTORY", "  'History'  "])
    def test_reserved_history_name_is_renamed(self, name: str) -> None:
        res = sanitize_sheet_name(name)
        assert res.lower() != "history"
        assert len(res) <= 31

    def test_reserved_history_collision_deduplication(self) -> None:
        used: set[str] = set()
        s1 = sanitize_sheet_name("History", used)
        s2 = sanitize_sheet_name("HISTORY", used)
        s3 = sanitize_sheet_name("History_1", used)
        assert len({s1.lower(), s2.lower(), s3.lower()}) == 3
        for s in (s1, s2, s3):
            assert s.lower() != "history"
            assert len(s) <= 31

    def test_trailing_apostrophe_after_31_char_slice_is_stripped(self) -> None:
        # 30 'A's + apostrophe at index 30 (the 31st char) + extra
        tricky = "A" * 30 + "'extra"
        s = sanitize_sheet_name(tricky)
        assert len(s) <= 31
        assert not s.startswith("'")
        assert not s.endswith("'")
        assert s == "A" * 30

        # Multiple collisions on the same tricky prefix
        used: set[str] = set()
        names = [sanitize_sheet_name("A" * 29 + "''extra", used) for _ in range(4)]
        assert len({n.lower() for n in names}) == 4
        for n in names:
            assert 1 <= len(n) <= 31
            assert not n.startswith("'")
            assert not n.endswith("'")

    def test_apostrophe_only_and_whitespace_names_fallback(self) -> None:
        assert sanitize_sheet_name("'''") == "工作表"
        assert sanitize_sheet_name("  '  ' ") == "工作表"
        assert sanitize_sheet_name("'hello'") == "hello"

    def test_multiple_tables_workbook_accepts_history_and_apostrophe_names(
        self, tmp_path: Path
    ) -> None:
        tables = [
            DataTable(table_id="t1", name="History", headers=["A"], rows=[["1"]]),
            DataTable(table_id="t2", name="HISTORY", headers=["B"], rows=[["2"]]),
            DataTable(table_id="t3", name="X" * 30 + "'tail", headers=["C"], rows=[["3"]]),
        ]
        out = tmp_path / "sheets.xlsx"
        export_multiple_tables_workbook(tables, out)
        wb = openpyxl.load_workbook(out)
        assert len(wb.sheetnames) == 3
        for title in wb.sheetnames:
            assert title.lower() != "history"
            assert not title.startswith("'")
            assert not title.endswith("'")
            assert len(title) <= 31
        wb.close()


# ----------------------------------------------------------------------
# 3. Excel cell cap, XML cleanup, dimension limits, empty headers
# ----------------------------------------------------------------------


class TestExcelLimitsAndEmptyHeaders:
    def test_unified_illegal_xml_regex_strips_fffe_ffff(self) -> None:
        dirty = "ok\x00\x01\x0b\x0c\x1f\ufffe\uffffend"
        assert sanitize_xml_text(dirty) == "okend"
        assert _sanitize_excel_value(dirty) == "okend"
        assert _sanitize_csv_value(dirty) == "okend"

    def test_32767_char_cell_cap_with_ellipsis(self, tmp_path: Path) -> None:
        huge = "X" * 40_000
        sanitized = _sanitize_excel_value(huge)
        assert isinstance(sanitized, str)
        assert len(sanitized) == EXCEL_MAX_CELL_CHARS
        assert sanitized.endswith("...")

        # Formula-prefixed huge string also respects 32,767 cap after leading apostrophe
        huge_formula = "=" + "Y" * 40_000
        sanitized_formula = _sanitize_excel_value(huge_formula)
        assert isinstance(sanitized_formula, str)
        assert len(sanitized_formula) == EXCEL_MAX_CELL_CHARS
        assert sanitized_formula.startswith("'=")
        assert sanitized_formula.endswith("...")

        tbl = DataTable(table_id="cap", name="长单元格", headers=["内容"], rows=[[huge]])
        out = tmp_path / "cap.xlsx"
        export_table_excel(tbl, out)
        wb = openpyxl.load_workbook(out)
        val = wb.active.cell(2, 1).value
        assert len(val) == EXCEL_MAX_CELL_CHARS
        assert val.endswith("...")
        wb.close()

    def test_excel_row_and_col_hard_limits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert EXCEL_MAX_ROWS == 1_048_576
        assert EXCEL_MAX_COLS == 16_384

        wb = openpyxl.Workbook()
        ws = wb.active

        monkeypatch.setattr(exporter_mod, "EXCEL_MAX_ROWS", 5)
        with pytest.raises(ExportError, match="最大行数限制"):
            _write_styled_sheet(ws, ["A"], [[i] for i in range(5)])  # 1 header + 5 rows = 6 > 5

        monkeypatch.setattr(exporter_mod, "EXCEL_MAX_ROWS", 1_048_576)
        monkeypatch.setattr(exporter_mod, "EXCEL_MAX_COLS", 3)
        with pytest.raises(ExportError, match="最大列数限制"):
            _write_styled_sheet(ws, ["A", "B", "C", "D"], [["1", "2", "3", "4"]])
        wb.close()

    def test_empty_headers_with_non_empty_rows_never_drop_cells(self, tmp_path: Path) -> None:
        tbl = DataTable(
            table_id="eh",
            name="无表头表",
            headers=[],
            rows=[["A1", "B1", "C1"], ["A2", "B2"]],
        )

        # Single Excel
        xlsx_out = tmp_path / "no_header.xlsx"
        export_table_excel(tbl, xlsx_out)
        wb = openpyxl.load_workbook(xlsx_out)
        ws = wb.active
        assert ws.max_row == 2
        assert ws.max_column == 3
        assert [ws.cell(1, c).value for c in range(1, 4)] == ["A1", "B1", "C1"]
        assert [ws.cell(2, c).value for c in range(1, 3)] == ["A2", "B2"]
        wb.close()

        # Multi-table Excel
        multi_out = tmp_path / "no_header_multi.xlsx"
        export_multiple_tables_workbook([tbl], multi_out)
        wb2 = openpyxl.load_workbook(multi_out)
        assert wb2.active.cell(1, 2).value == "B1"
        wb2.close()

        # Dedup report merged_table with empty headers
        report = _make_report(cross_groups=1, intra_groups=0, include_unrec=False)
        rep_out = tmp_path / "no_header_rep.xlsx"
        export_dedup_report_excel(report, rep_out, merged_table=tbl)
        wb3 = openpyxl.load_workbook(rep_out)
        assert wb3["合并去重结果"].cell(1, 3).value == "C1"
        wb3.close()

        # CSV
        csv_out = tmp_path / "no_header.csv"
        export_table_csv(tbl, csv_out)
        with open(csv_out, "r", encoding="utf-8-sig", newline="") as f:
            csv_rows = list(csv.reader(f))
        assert csv_rows == [["A1", "B1", "C1"], ["A2", "B2", ""]]

        # Word
        docx_out = tmp_path / "no_header.docx"
        export_table_word(tbl, docx_out)
        doc = docx.Document(str(docx_out))
        assert len(doc.tables) == 1
        assert len(doc.tables[0].rows) == 2
        assert doc.tables[0].rows[0].cells[2].text == "C1"

        # Word with both empty headers and empty rows
        empty_tbl = DataTable(table_id="emp", name="全空表", headers=[], rows=[])
        empty_docx = tmp_path / "completely_empty.docx"
        export_table_word(empty_tbl, empty_docx)
        doc_emp = docx.Document(str(empty_docx))
        assert len(doc_emp.tables) == 0
        assert any("无列数据" in p.text for p in doc_emp.paragraphs)


# ----------------------------------------------------------------------
# 4. TXT / TSV hygiene & column_index bounds
# ----------------------------------------------------------------------


class TestTxtHygieneAndBounds:
    def test_export_table_txt_strips_newlines_tabs_and_nul(self, tmp_path: Path) -> None:
        tbl = DataTable(
            table_id="txt1",
            name="TXT清理",
            headers=["列\t一", "列\n二"],
            rows=[
                ["行1\r\n换行\x00值", "带\t制表符\r内容"],
                ["正常\ufffe值", "第二行"],
            ],
        )
        full_out = tmp_path / "full.txt"
        export_table_txt(tbl, full_out)
        raw = full_out.read_text(encoding="utf-8")
        assert "\x00" not in raw
        assert "\ufffe" not in raw
        lines = raw.splitlines()
        assert len(lines) == 3  # header + 2 rows, no broken lines
        assert lines[0] == "列 一\t列 二"
        assert lines[1] == "行1 换行值\t带 制表符 内容"
        assert lines[2] == "正常值\t第二行"

        col_out = tmp_path / "col0.txt"
        export_table_txt(tbl, col_out, column_index=0)
        assert col_out.read_text(encoding="utf-8").splitlines() == ["行1 换行值", "正常值"]

    def test_export_table_txt_column_index_bounds_with_empty_headers(self, tmp_path: Path) -> None:
        tbl_no_hdr = DataTable(table_id="nh", name="无头", headers=[], rows=[["a", "b"]])
        ok_out = tmp_path / "ok.txt"
        export_table_txt(tbl_no_hdr, ok_out, column_index=1)
        assert ok_out.read_text(encoding="utf-8").splitlines() == ["b"]

        with pytest.raises(ValueError):
            export_table_txt(tbl_no_hdr, tmp_path / "oob.txt", column_index=2)
        with pytest.raises(ValueError):
            export_table_txt(tbl_no_hdr, tmp_path / "neg.txt", column_index=-1)

        tbl_empty = DataTable(table_id="e", name="空", headers=[], rows=[])
        with pytest.raises(ValueError):
            export_table_txt(tbl_empty, tmp_path / "emp.txt", column_index=0)

    def test_export_codes_txt_sanitizes_and_respects_flags(self, tmp_path: Path) -> None:
        out = tmp_path / "codes.txt"
        items = ["xyz-002\x00", ParseResult(canonical_code="abc-001"), _rec("XYZ-002"), "  "]
        export_codes_txt(items, out, unique=False, sort_codes=False)
        assert out.read_text(encoding="utf-8").splitlines() == ["XYZ-002", "ABC-001", "XYZ-002"]


# ----------------------------------------------------------------------
# 5. Atomic writes, directory-as-target, early lock checks, temp cleanup
# ----------------------------------------------------------------------


class TestAtomicWriteAndErrorHandling:
    def test_directory_as_target_gives_friendly_dir_error_not_locked_error(
        self, tmp_path: Path
    ) -> None:
        target_dir = tmp_path / "existing_folder"
        target_dir.mkdir()
        tbl = DataTable(table_id="d1", name="目录测试", headers=["A"], rows=[["1"]])
        rec = _rec("ABC-001")

        for fn in (
            lambda: export_records_excel([rec], target_dir),
            lambda: export_records_csv([rec], target_dir),
            lambda: export_codes_txt([rec], target_dir),
            lambda: export_table_excel(tbl, target_dir),
            lambda: export_table_csv(tbl, target_dir),
            lambda: export_table_word(tbl, target_dir),
            lambda: export_table_txt(tbl, target_dir),
        ):
            with pytest.raises(ExportError) as exc_info:
                fn()
            msg = str(exc_info.value)
            assert "文件夹" in msg
            assert "占用" not in msg

    def test_mkdir_oserror_converted_to_friendly_export_error(
        self, tmp_path: Path
    ) -> None:
        # Use an existing regular file as a fake parent directory so mkdir raises FileExistsError (OSError)
        blocking_file = tmp_path / "not_a_dir"
        blocking_file.write_text("hi", encoding="utf-8")
        bad_target = blocking_file / "nested" / "out.xlsx"
        tbl = DataTable(table_id="m", name="T", headers=["A"], rows=[["1"]])

        with pytest.raises(ExportError, match="无法创建导出目录"):
            export_table_excel(tbl, bad_target)

    def test_early_lock_check_fails_before_building_workbook_or_document(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        locked_xlsx = tmp_path / "locked.xlsx"
        locked_xlsx.write_bytes(b"x")
        locked_docx = tmp_path / "locked.docx"
        locked_docx.write_bytes(b"x")

        real_open = open

        def deny_locked(path, mode="r", *args, **kwargs):
            if Path(path) in (locked_xlsx, locked_docx) and "r+b" in mode:
                raise PermissionError(13, "Permission denied", str(path))
            return real_open(path, mode, *args, **kwargs)

        def fail_if_called(*args, **kwargs):
            raise AssertionError("Should fail fast on lock before constructing Workbook/Document!")

        monkeypatch.setattr("builtins.open", deny_locked)
        monkeypatch.setattr(exporter_mod, "Workbook", fail_if_called)
        monkeypatch.setattr(table_exporter_mod, "Workbook", fail_if_called)
        monkeypatch.setattr(table_exporter_mod.docx, "Document", fail_if_called)

        tbl = DataTable(table_id="lk", name="锁", headers=["A"], rows=[["1"]])
        with pytest.raises(ExportError, match="占用"):
            export_records_excel([_rec()], locked_xlsx)
        with pytest.raises(ExportError, match="占用"):
            export_table_excel(tbl, locked_xlsx)
        with pytest.raises(ExportError, match="占用"):
            export_table_word(tbl, locked_docx)

    def test_atomic_write_cleans_up_tmp_file_on_non_oserror(self, tmp_path: Path) -> None:
        target = tmp_path / "boom.xlsx"

        def failing_writer(tmp_path_arg: Path) -> None:
            tmp_path_arg.write_bytes(b"partial-data")
            raise RuntimeError("simulated mid-save failure")

        with pytest.raises(RuntimeError, match="simulated mid-save failure"):
            _atomic_write_file(target, failing_writer, tmp_suffix=".tmp.xlsx")

        assert not target.exists()
        leftovers = list(tmp_path.glob("*tmp*"))
        assert leftovers == []


# ----------------------------------------------------------------------
# 6. High-speed Word export, roundtrip fidelity, progress/cancel & reports
# ----------------------------------------------------------------------


class TestFastWordExportAndReports:
    def test_word_1000_row_fast_export_and_full_roundtrip(self, tmp_path: Path) -> None:
        headers = [f"列{c + 1}" for c in range(8)]
        rows = [
            [
                f"ROW-{r:04d}",
                f"中文标题 & <标签> #{r}",
                "第一行\n第二行" if r == 0 else f"说明_{r}",
                f"SRC_{r % 5}",
                "4K,字幕",
                f"CD{(r % 2) + 1}",
                "" if r % 10 == 0 else f"路径/子目录/{r}.mp4",
                f"备注\x00_{r}",
            ]
            for r in range(1000)
        ]
        tbl = DataTable(table_id="fast_w", name="千行Word性能表", headers=headers, rows=rows)
        out = tmp_path / "fast_1000.docx"

        progress_events: list[tuple[int, int]] = []
        t0 = time.perf_counter()
        export_table_word(
            tbl,
            out,
            title="千行性能测试",
            progress_callback=lambda cur, tot: progress_events.append((cur, tot)),
        )
        elapsed = time.perf_counter() - t0
        assert out.exists()
        assert elapsed < 3.0, f"1,000-row Word export took too long: {elapsed:.2f}s"
        assert progress_events and progress_events[-1] == (1000, 1000)

        # Re-open with python-docx and verify roundtrip fidelity
        doc = docx.Document(str(out))
        assert len(doc.tables) == 1
        word_tbl = doc.tables[0]
        assert len(word_tbl.rows) == 1001  # 1 header + 1000 data rows

        # Verify header row text, repeating header flag, and header shading
        hdr_row = word_tbl.rows[0]
        assert [c.text for c in hdr_row.cells] == headers
        hdr_xml = hdr_row._tr.xml
        assert "tblHeader" in hdr_xml
        assert 'w:fill="1F4E78"' in hdr_xml

        # Verify borders & cell margins on table
        tbl_pr_xml = word_tbl._tbl.tblPr.xml
        assert "tblBorders" in tbl_pr_xml
        assert "tblCellMar" in tbl_pr_xml

        # Verify first, second (zebra), middle, and last row text & shading
        r0 = word_tbl.rows[1]  # data_idx=0 (even -> no zebra shading)
        assert r0.cells[0].text == "ROW-0000"
        assert r0.cells[1].text == "中文标题 & <标签> #0"
        assert r0.cells[2].text == "第一行\n第二行"
        assert r0.cells[7].text == "备注_0"
        assert 'w:fill="F4F7FA"' not in r0._tr.xml

        r1 = word_tbl.rows[2]  # data_idx=1 (odd -> zebra shading)
        assert r1.cells[0].text == "ROW-0001"
        assert 'w:fill="F4F7FA"' in r1._tr.xml

        r_last = word_tbl.rows[1000]  # data_idx=999 (odd -> zebra shading)
        assert r_last.cells[0].text == "ROW-0999"
        assert r_last.cells[1].text == "中文标题 & <标签> #999"
        assert 'w:fill="F4F7FA"' in r_last._tr.xml

    def test_cancel_check_aborts_cleanly_without_leaving_files(self, tmp_path: Path) -> None:
        tbl = DataTable(
            table_id="cn",
            name="取消测试",
            headers=["A", "B"],
            rows=[[str(i), f"v{i}"] for i in range(600)],
        )
        docx_out = tmp_path / "cancelled.docx"
        with pytest.raises(ExportError, match="取消"):
            export_table_word(tbl, docx_out, cancel_check=lambda: True)
        assert not docx_out.exists()
        assert list(tmp_path.glob("*tmp*")) == []

        xlsx_out = tmp_path / "cancelled.xlsx"
        with pytest.raises(ExportError, match="取消"):
            export_table_excel(tbl, xlsx_out, cancel_check=lambda: True)
        assert not xlsx_out.exists()
        assert list(tmp_path.glob("*tmp*")) == []

    def test_dedup_report_word_zero_dups_and_intra_truncation_notice(
        self, tmp_path: Path
    ) -> None:
        # Case A: 0 duplicates
        empty_report = _make_report(cross_groups=0, intra_groups=0, include_unrec=False)
        out_zero = tmp_path / "zero_dups.docx"
        export_dedup_report_word(empty_report, out_zero)
        doc0 = docx.Document(str(out_zero))
        text0 = "\n".join(p.text for p in doc0.paragraphs)
        assert "未发现跨表重复数据" in text0
        assert "未发现表内重复数据" in text0
        assert "全表共有交集键：无" in text0

        # Case B: >200 intra_table_groups triggers clear truncation notice
        big_report = _make_report(cross_groups=3, intra_groups=215, rows_per_group=2)
        out_big = tmp_path / "big_intra.docx"
        progress_steps: list[tuple[int, int]] = []
        export_dedup_report_word(
            big_report,
            out_big,
            max_detail_rows=4,
            progress_callback=lambda c, t: progress_steps.append((c, t)),
        )
        assert progress_steps == [(1, 4), (2, 4), (3, 4), (4, 4)]

        doc_big = docx.Document(str(out_big))
        text_big = "\n".join(p.text for p in doc_big.paragraphs)
        assert "跨表重复明细共 6 条，本文档展示前 4 条" in text_big
        assert "表内重复分组共 215 组，本文档展示前 200 组" in text_big
        # 4th table is the intra-table summary: 1 header + 200 data rows
        assert len(doc_big.tables[3].rows) == 201

    def test_dedup_report_excel_and_compare_exports_with_formula_payloads(
        self, tmp_path: Path
    ) -> None:
        report = _make_report(cross_groups=2, intra_groups=1, include_unrec=True)
        merged = DataTable(
            table_id="mg",
            name="合并",
            headers=["编码", "备注"],
            rows=[["CROSS-000", "=CMD|'/C calc'!A0"]],
        )
        rep_xlsx = tmp_path / "report.xlsx"
        steps: list[tuple[int, int]] = []
        export_dedup_report_excel(
            report,
            rep_xlsx,
            merged_table=merged,
            progress_callback=lambda c, t: steps.append((c, t)),
        )
        assert steps[-1] == (7, 7)

        wb = openpyxl.load_workbook(rep_xlsx, data_only=False)
        assert len(wb.sheetnames) == 7
        ws_merged = wb["合并去重结果"]
        assert ws_merged.cell(2, 2).data_type == "s"
        assert ws_merged.cell(2, 2).value == "'=CMD|'/C calc'!A0"
        wb.close()

        # Compare & duplicate group exports with formula payloads
        grp = DuplicateGroup(
            canonical_code="ABC-001",
            records=[_rec("ABC-001", file_name="=EVIL()"), _rec("ABC-001", file_name="+1+2")],
        )
        dup_xlsx = tmp_path / "dup.xlsx"
        export_duplicate_groups_excel([grp], dup_xlsx)
        dup_csv = tmp_path / "dup.csv"
        export_duplicate_groups_csv([grp], dup_csv)

        wb_dup = openpyxl.load_workbook(dup_xlsx, data_only=False)
        assert wb_dup["重复项明细"].cell(2, 8).data_type == "s"
        assert wb_dup["重复项明细"].cell(2, 8).value == "'=EVIL()"
        wb_dup.close()

        cmp = CompareResult(
            mode="db",
            source_a_name="=SRC",
            matched_in_db=[(ParseResult(canonical_code="ABC-001", file_name="=A"), [_rec()])],
            not_in_db=[ParseResult(canonical_code="NEW-001", file_name="+B")],
        )
        cmp_xlsx = tmp_path / "cmp.xlsx"
        cmp_csv = tmp_path / "cmp.csv"
        export_compare_result_excel(cmp, cmp_xlsx)
        export_compare_result_csv(cmp, cmp_csv)
        with open(cmp_csv, "r", encoding="utf-8-sig", newline="") as f:
            cmp_rows = list(csv.reader(f))
        assert cmp_rows[1][4] == "'=A"
        assert cmp_rows[1][5] == "'=SRC"
        assert cmp_rows[2][4] == "'+B"

        # Records Excel with LibraryStats
        stats = LibraryStats(
            total_records=2,
            unique_codes=1,
            duplicate_groups=1,
            duplicate_records=1,
            possible_multipart_groups=0,
        )
        rec_xlsx = tmp_path / "rec_stats.xlsx"
        export_records_excel([_rec()], rec_xlsx, stats=stats)
        wb_rec = openpyxl.load_workbook(rec_xlsx)
        assert wb_rec.sheetnames == ["数据清单", "汇总统计"]
        wb_rec.close()
