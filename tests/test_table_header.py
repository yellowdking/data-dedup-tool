"""Headless PySide6 tests for TableHeaderManager, TableLayoutState & TableCopyHelper.

Covers:
- TableLayoutState JSON round-trip, clamping, fault tolerance & legacy config compatibility
- TableHeaderManager drag-reorder persistence, column hide/show + >=1 visible / lock guard
- Forward-compatible known_cols schema evolution & obsolete-column pruning
- Fast O(60-row) sampled auto-fit, reset to defaults & global table_layouts_reset signal
- Dynamic table synchronization without config.json pollution
- TableCopyHelper order-preserving deduplication & clipboard formats (newline/space/comma/regex/tsv)
  respecting dragged visual column order and hidden columns
"""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QAbstractTableModel, QItemSelectionModel, QModelIndex, Qt
from PySide6.QtWidgets import QApplication, QMenu, QTableView

from src.core.config import AppConfig, ConfigManager, TableLayoutState
from src.core.models import DataRecord
from src.ui.app_context import AppContext
from src.ui.delegates import ROLE_COPY_TEXT
from src.ui.table_copy import (
    TableCopyHelper,
    deduplicate_preserve_order,
    format_codes_for_clipboard,
    format_rows_as_tsv,
)
from src.ui.compare_ab_models import (
    ChecklistTableModel,
    MatchedDetailTableModel,
    MissingOnlyTableModel,
    OnlyInATableModel,
)
from src.ui.table_header import ColumnSpec, TableHeaderManager
from src.ui.table_models import (
    CompareTableModel,
    DuplicateTableModel,
    PreviewTableModel,
    RecordTableModel,
)
from src.ui.table_preview_model import DynamicTableModel


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(["pytest-table-header", "-platform", "offscreen"])
    assert isinstance(app, QApplication)
    return app


@pytest.fixture
def isolated_ctx(qapp: QApplication, tmp_path: Path):
    cfg_mgr = ConfigManager(data_dir=tmp_path / "hdr_data")
    ctx = AppContext(config_manager=cfg_mgr)
    try:
        yield ctx
    finally:
        ctx.close()
        QApplication.processEvents()


class _SampleTableModel(QAbstractTableModel):
    """Minimal sortable QAbstractTableModel with ROLE_COPY_TEXT on column 1."""

    def __init__(self, headers: list[str], rows: list[list[str]], copy_col: int = 1) -> None:
        super().__init__()
        self._headers = list(headers)
        self._rows = [list(r) for r in rows]
        self._copy_col = copy_col
        self.sort_calls: list[tuple[int, Qt.SortOrder]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._headers)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            if 0 <= section < len(self._headers):
                return self._headers[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        val = self._rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return val
        if role == ROLE_COPY_TEXT and index.column() == self._copy_col:
            return val
        return None

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        if column < 0:
            return
        self.sort_calls.append((column, order))
        self.layoutAboutToBeChanged.emit()
        reverse = order == Qt.SortOrder.DescendingOrder
        self._rows.sort(key=lambda r: r[column], reverse=reverse)
        self.layoutChanged.emit()


def _default_specs() -> list[ColumnSpec]:
    return [
        ColumnSpec("check", "选", default_width=40, default_visible=True, min_width=36, sortable=False),
        ColumnSpec("code", "标准编码", default_width=140, default_visible=True, min_width=90, lock_visible=True),
        ColumnSpec("file_name", "原始文件名", default_width=240, default_visible=True, min_width=120, stretch_weight=1.0),
        ColumnSpec("source", "来源", default_width=130, default_visible=True, min_width=80),
        ColumnSpec("tags", "版本标签", default_width=110, default_visible=False, min_width=70, category="advanced"),
        ColumnSpec("rule", "匹配规则", default_width=120, default_visible=False, min_width=80, category="advanced"),
    ]


def _make_table(
    specs: list[ColumnSpec],
    rows: list[list[str]] | None = None,
    *,
    copy_col: int = 1,
) -> tuple[QTableView, _SampleTableModel]:
    headers = [s.label for s in specs]
    data_rows = rows if rows is not None else [
        ["", "ABC-001", "alpha_file.mp4", "batch_a.txt", "4K", "rule_std"],
        ["", "ABC-001", "alpha_copy.mp4", "batch_b.txt", "HD", "rule_std"],
        ["", "DEF-002", "beta_item.mkv", "batch_a.txt", "-", "rule_std"],
        ["", "PRJ-0042", "gamma_doc.docx", "archive.docx", "V2", "rule_prj"],
    ]
    model = _SampleTableModel(headers, data_rows, copy_col=copy_col)
    view = QTableView()
    view.resize(900, 360)
    view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
    view.setModel(model)
    return view, model


# ======================================================================
# 1. TableLayoutState & ConfigManager Persistence
# ======================================================================


def test_table_layout_state_json_roundtrip_and_fault_tolerance(tmp_path: Path) -> None:
    raw = {
        "visible_cols": ["code", "file_name", "code", "", 123],
        "column_order": ["file_name", "code", "source", "file_name"],
        "column_widths": {
            "code": 180,
            "too_small": 5,
            "too_wide": 9999,
            "bad_bool": True,
            "bad_str": "abc",
            "": 100,
        },
        "sort_col": " file_name ",
        "sort_desc": "true",
        "known_cols": ["code", "file_name", "source"],
    }
    state = TableLayoutState.from_dict(raw)
    assert state.visible_cols == ["code", "file_name"]
    assert state.column_order == ["file_name", "code", "source"]
    assert state.column_widths == {"code": 180, "too_small": 36, "too_wide": 1600}
    assert state.sort_col == "file_name"
    assert state.sort_desc is True
    assert state.known_cols == ["code", "file_name", "source"]

    # Round-trip through ConfigManager disk JSON
    data_dir = tmp_path / "cfg_rt"
    mgr = ConfigManager(data_dir=data_dir)
    mgr.set_table_layout("demo_table", state)
    mgr.config.duplicates_view_mode = "grouped"
    mgr.config.show_advanced_fields = True
    mgr.save()

    mgr2 = ConfigManager(data_dir=data_dir)
    loaded = mgr2.get_table_layout("demo_table")
    assert loaded is not None
    assert loaded.to_dict() == state.to_dict()
    assert mgr2.config.duplicates_view_mode == "grouped"
    assert mgr2.config.show_advanced_fields is True

    # Reset single vs all layouts
    mgr2.set_table_layout("other_table", TableLayoutState(visible_cols=["a"]))
    mgr2.reset_table_layouts("demo_table", save=True)
    assert mgr2.get_table_layout("demo_table") is None
    assert mgr2.get_table_layout("other_table") is not None
    mgr2.reset_table_layouts(save=True)
    assert mgr2.config.table_layouts == {}


def test_legacy_v13_config_defaults_gracefully(tmp_path: Path) -> None:
    data_dir = tmp_path / "legacy_cfg"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.json").write_text(
        json.dumps({"theme": "dark", "auto_backup": False}),
        encoding="utf-8",
    )
    mgr = ConfigManager(data_dir=data_dir)
    assert mgr.config.theme == "dark"
    assert mgr.config.auto_backup is False
    assert mgr.config.table_layouts == {}
    assert mgr.config.duplicates_view_mode == "flat"
    assert mgr.config.show_advanced_fields is False

    # Invalid duplicates_view_mode falls back cleanly to "flat"
    bad_cfg = AppConfig.from_dict({"duplicates_view_mode": "tree_invalid", "table_layouts": "bad"})
    assert bad_cfg.duplicates_view_mode == "flat"
    assert bad_cfg.table_layouts == {}


# ======================================================================
# 2. TableHeaderManager: Drag-Reorder, Guards, Evolution & Reset
# ======================================================================


def test_header_manager_defaults_drag_reorder_and_persistence_roundtrip(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "persist_hdr"
    specs = _default_specs()

    # Session 1: attach, verify defaults & no spurious col-0 sort, then mutate & flush
    ctx1 = AppContext(config_manager=ConfigManager(data_dir=data_dir))
    tv1, model1 = _make_table(specs)
    try:
        mgr1 = TableHeaderManager.attach(tv1, "sample_main", specs, ctx1)
        hdr1 = tv1.horizontalHeader()

        assert hdr1.sectionsMovable() is True
        assert hdr1.isFirstSectionMovable() is True
        assert hdr1.stretchLastSection() is False
        assert hdr1.sortIndicatorSection() == -1
        assert model1.sort_calls == []
        assert mgr1.visible_column_keys() == ["check", "code", "file_name", "source"]
        assert hdr1.isSectionHidden(4) is True  # tags hidden by default
        assert hdr1.isSectionHidden(5) is True  # rule hidden by default

        # Drag "source" (logical 3) before "code" (logical 1)
        hdr1.moveSection(hdr1.visualIndex(3), hdr1.visualIndex(1))
        # Show advanced column "tags" (logical 4) and resize "code" (logical 1)
        assert mgr1.set_column_visible(4, True) is True
        tv1.setColumnWidth(1, 210)
        # Sort descending by file_name (logical 2)
        tv1.sortByColumn(2, Qt.SortOrder.DescendingOrder)
        # Clicking non-sortable col 0 must NOT overwrite sort_col
        hdr1.setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        assert mgr1.current_sort() == (2, Qt.SortOrder.DescendingOrder)

        mgr1.flush()
        expected_order = mgr1.column_order_keys()
        expected_visible = mgr1.visible_column_keys()
        assert expected_visible[:3] == ["check", "source", "code"]
        assert "tags" in expected_visible
    finally:
        tv1.deleteLater()
        ctx1.close()
        QApplication.processEvents()

    # Session 2: fresh AppContext + QTableView reloads exact dragged order, widths & sort
    ctx2 = AppContext(config_manager=ConfigManager(data_dir=data_dir))
    tv2, model2 = _make_table(specs)
    try:
        mgr2 = TableHeaderManager.attach(tv2, "sample_main", specs, ctx2)
        hdr2 = tv2.horizontalHeader()

        assert mgr2.column_order_keys() == expected_order
        assert mgr2.visible_column_keys() == expected_visible
        assert hdr2.sectionSize(1) == 210
        assert hdr2.isSectionHidden(4) is False
        assert hdr2.isSectionHidden(5) is True
        assert mgr2.current_sort() == (2, Qt.SortOrder.DescendingOrder)
        assert model2.data(model2.index(0, 2)) == "gamma_doc.docx"
    finally:
        tv2.deleteLater()
        ctx2.close()
        QApplication.processEvents()


def test_header_manager_visibility_guards_and_context_menu(isolated_ctx: AppContext) -> None:
    # Case A: locked column cannot be hidden; menu separates basic vs 进阶字段
    specs = _default_specs()
    tv, _ = _make_table(specs)
    try:
        mgr = TableHeaderManager.attach(tv, "guard_locked", specs, isolated_ctx)
        assert mgr.set_column_visible(1, False) is False  # code is lock_visible=True
        assert mgr.set_column_visible(3, False) is True   # source can be hidden
        assert tv.horizontalHeader().isSectionHidden(3) is True

        # Re-showing a column whose size dropped to 0 restores default_width
        tv.setColumnWidth(3, 0)
        assert mgr.set_column_visible(3, True) is True
        assert tv.horizontalHeader().sectionSize(3) >= specs[3].min_width

        menu = mgr.build_column_menu(logical_section=1)
        texts = [a.text() for a in menu.actions() if a.text()]
        assert "进阶字段" in texts
        assert "自动调整当前列宽" in texts
        assert "恢复默认列与顺序" in texts

        code_act = next(a for a in menu.actions() if a.data() == "code")
        assert code_act.isChecked() is True
        assert code_act.isEnabled() is False
        assert "不可隐藏" in code_act.toolTip()
    finally:
        tv.deleteLater()
        QApplication.processEvents()

    # Case B: >=1 visible column guard when NO column is lock_visible
    unlocked_specs = [
        ColumnSpec("a", "列A", default_width=100, default_visible=True),
        ColumnSpec("b", "列B", default_width=100, default_visible=True),
    ]
    tv2, _ = _make_table(unlocked_specs, rows=[["1", "2"]], copy_col=0)
    try:
        mgr2 = TableHeaderManager.attach(tv2, "guard_last", unlocked_specs, isolated_ctx)
        assert mgr2.set_column_visible(0, False) is True
        # Only col 1 remains visible -> hiding col 1 MUST be blocked!
        assert mgr2.set_column_visible(1, False) is False
        assert mgr2.visible_column_keys() == ["b"]

        menu2 = mgr2.build_column_menu()
        b_act = next(a for a in menu2.actions() if a.data() == "b")
        assert b_act.isEnabled() is False
        assert "至少保留 1 个显示列" in b_act.toolTip()
    finally:
        tv2.deleteLater()
        QApplication.processEvents()


def test_header_manager_known_cols_schema_evolution(isolated_ctx: AppContext) -> None:
    # Persist an older schema where user explicitly hid "source" and had removed "legacy_col"
    old_state = TableLayoutState(
        visible_cols=["code", "file_name"],
        column_order=["file_name", "legacy_col", "code", "source"],
        column_widths={"code": 175, "file_name": 260, "legacy_col": 90},
        sort_col="legacy_col",
        sort_desc=True,
        known_cols=["code", "file_name", "source", "legacy_col"],
    )
    isolated_ctx.config_manager.set_table_layout("evolved_tbl", old_state)

    # Newer app version removes "legacy_col" and introduces two new columns
    v21_specs = [
        ColumnSpec("code", "标准编码", default_width=140, default_visible=True, lock_visible=True),
        ColumnSpec("file_name", "原始文件名", default_width=220, default_visible=True),
        ColumnSpec("source", "来源", default_width=120, default_visible=True),
        ColumnSpec("new_basic", "新基础列", default_width=110, default_visible=True),
        ColumnSpec("new_adv", "新进阶列", default_width=100, default_visible=False, category="advanced"),
    ]
    tv, _ = _make_table(
        v21_specs,
        rows=[["ABC-001", "demo.mp4", "src.txt", "ok", "adv"]],
        copy_col=0,
    )
    try:
        mgr = TableHeaderManager.attach(tv, "evolved_tbl", v21_specs, isolated_ctx)
        hdr = tv.horizontalHeader()

        # User's hidden choice for known column "source" is respected
        assert hdr.isSectionHidden(2) is True
        # Newly added basic column becomes visible automatically; new advanced column stays hidden
        assert hdr.isSectionHidden(3) is False
        assert hdr.isSectionHidden(4) is True
        # Dragged order preserved with new columns appended cleanly, obsolete key pruned
        assert mgr.visible_column_keys() == ["file_name", "code", "new_basic"]
        assert "legacy_col" not in mgr.column_order_keys()
        # Sort on removed legacy_col falls back cleanly to unsorted (-1)
        assert mgr.current_sort()[0] == -1
        assert hdr.sectionSize(0) == 175
    finally:
        tv.deleteLater()
        QApplication.processEvents()


def test_header_manager_autofit_reset_and_global_reset(isolated_ctx: AppContext) -> None:
    specs = _default_specs()
    many_rows = [
        ["", f"PRJ-{i:04d}", f"very_long_descriptive_filename_record_{i:04d}.mkv", "source.xlsx", "", ""]
        for i in range(250)
    ]
    tv, _ = _make_table(specs, rows=many_rows)
    try:
        mgr = TableHeaderManager.attach(tv, "reset_demo", specs, isolated_ctx)
        hdr = tv.horizontalHeader()

        # Sampled auto-fit works on 250+ rows and clamps within [min_width, 460]
        fitted_w = mgr.auto_fit_column(2)
        assert specs[2].min_width <= fitted_w <= 460
        assert mgr.auto_fit_column(4) == 0  # hidden column returns 0
        mgr.auto_fit_all()

        # Mutate order + visibility + sort, then reset_to_defaults
        hdr.moveSection(hdr.visualIndex(3), 0)
        mgr.set_column_visible(4, True)
        tv.sortByColumn(1, Qt.SortOrder.DescendingOrder)
        mgr.flush()
        assert isolated_ctx.config_manager.get_table_layout("reset_demo") is not None

        mgr.reset_to_defaults()
        assert mgr.visible_column_keys() == ["check", "code", "file_name", "source"]
        assert mgr.column_order_keys() == [s.key for s in specs]
        assert mgr.current_sort()[0] == -1
        assert isolated_ctx.config_manager.get_table_layout("reset_demo") is None

        # Mutate again and verify AppContext.reset_all_table_layouts() resets via signal
        hdr.moveSection(hdr.visualIndex(2), 0)
        mgr.set_column_visible(5, True)
        mgr.flush()
        assert "reset_demo" in isolated_ctx.config.table_layouts

        isolated_ctx.reset_all_table_layouts()
        QApplication.processEvents()
        assert mgr.visible_column_keys() == ["check", "code", "file_name", "source"]
        assert mgr.column_order_keys() == [s.key for s in specs]
        assert isolated_ctx.config.table_layouts == {}
    finally:
        tv.deleteLater()
        QApplication.processEvents()


def test_header_manager_dynamic_table_resync_without_config_pollution(
    isolated_ctx: AppContext,
) -> None:
    model = DynamicTableModel(theme="light")
    model.set_data(["编码", "名称", "备注"], [["ABC-001", "项目一", "ok"]])
    tv = QTableView()
    tv.resize(800, 320)
    tv.setModel(model)
    try:
        mgr = TableHeaderManager.attach_dynamic(tv, isolated_ctx)
        assert mgr.is_dynamic is True
        assert mgr.persist is False
        assert len(mgr.columns) == 3

        # Reorder and hide a column in session
        hdr = tv.horizontalHeader()
        hdr.moveSection(2, 0)
        assert mgr.set_column_visible(1, False) is True
        assert len(mgr.visible_logical_columns_in_visual_order()) == 2
        mgr.flush()
        assert isolated_ctx.config.table_layouts == {}  # never pollutes config.json!

        # Switch to a new dynamic table schema -> sync_dynamic_columns resets columns cleanly
        model.set_data(["列甲", "列乙", "列丙", "列丁"], [["1", "2", "3", "4"]])
        mgr.sync_dynamic_columns(auto_fit=True)
        assert len(mgr.columns) == 4
        assert [c.label for c in mgr.columns] == ["列甲", "列乙", "列丙", "列丁"]
        assert mgr.visible_logical_columns_in_visual_order() == [0, 1, 2, 3]
        assert isolated_ctx.config.table_layouts == {}
    finally:
        tv.deleteLater()
        QApplication.processEvents()


# ======================================================================
# 3. TableCopyHelper: Deduplication, Formats & Visual-Order TSV
# ======================================================================


def test_code_formatters_deduplicate_and_escape_regex() -> None:
    raw = [" ABC-001 ", "", "-", "（未识别）", "DEF-002", "ABC-001", "A.B+1", "（双击填写编码）"]
    assert deduplicate_preserve_order(raw) == ["ABC-001", "DEF-002", "A.B+1"]

    assert format_codes_for_clipboard(raw, "newline") == "ABC-001\nDEF-002\nA.B+1"
    assert format_codes_for_clipboard(raw, "space") == "ABC-001 DEF-002 A.B+1"
    assert format_codes_for_clipboard(raw, "comma") == "ABC-001, DEF-002, A.B+1"
    assert format_codes_for_clipboard(raw, "regex") == r"ABC-001|DEF-002|A\.B\+1"
    assert format_codes_for_clipboard(["", "-"], "space") == ""


def test_table_copy_helper_clipboard_formats_split_button_and_visual_order_tsv(
    isolated_ctx: AppContext,
) -> None:
    specs = _default_specs()
    tv, model = _make_table(specs)
    paths = [
        r"D:\archive\alpha_file.mp4",
        r"D:\archive\alpha_copy.mp4",
        r"D:\archive\beta_item.mkv",
        "",
    ]
    try:
        mgr = TableHeaderManager.attach(tv, "copy_demo", specs, isolated_ctx)
        helper = TableCopyHelper(
            tv,
            isolated_ctx,
            header_manager=mgr,
            code_column=1,
            name_column=2,
            path_extractor=lambda r: paths[r] if 0 <= r < len(paths) else "",
        )
        split_btn = helper.create_copy_split_button(primary_variant=True)

        # No selection -> split button shows ALL deduplicated codes (3 unique across 4 rows)
        assert "3" in split_btn.text()
        assert "▾" not in split_btn.text()
        assert split_btn.isEnabled() is True

        # Clicking primary action with no selection copies all 3 deduplicated codes
        split_btn.click()
        assert QApplication.clipboard().text() == "ABC-001\nDEF-002\nPRJ-0042"

        # Select rows 0, 1 (both ABC-001) and row 2 (DEF-002) -> 2 unique codes
        sel = tv.selectionModel()
        sel.select(
            model.index(0, 0),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )
        sel.select(
            model.index(1, 0),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )
        sel.select(
            model.index(2, 0),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )
        QApplication.processEvents()
        assert "2" in split_btn.text()
        assert "选中" in split_btn.text()

        # Test all code clipboard formats on selection
        assert helper.copy_codes("newline") == 2
        assert QApplication.clipboard().text() == "ABC-001\nDEF-002"

        assert helper.copy_codes("space") == 2
        assert QApplication.clipboard().text() == "ABC-001 DEF-002"

        assert helper.copy_codes("comma") == 2
        assert QApplication.clipboard().text() == "ABC-001, DEF-002"

        assert helper.copy_codes("regex") == 2
        assert QApplication.clipboard().text() == "ABC-001|DEF-002"

        # Filenames preserve all selected items (non-deduplicated)
        assert helper.copy_filenames() == 3
        assert QApplication.clipboard().text() == "alpha_file.mp4\nalpha_copy.mp4\nbeta_item.mkv"

        # Paths copy non-empty paths
        assert helper.copy_paths() == 3
        assert "alpha_copy.mp4" in QApplication.clipboard().text()

        # Drag-reorder columns: move "source" (col 3) to visual pos 1, show "tags" (col 4),
        # hide "file_name" (col 2), then verify TSV respects dragged visual order & skips "check"!
        hdr = tv.horizontalHeader()
        hdr.moveSection(hdr.visualIndex(3), 1)
        mgr.set_column_visible(4, True)
        mgr.set_column_visible(2, False)

        tsv_with_hdr = format_rows_as_tsv(tv, [0, 2], header_manager=mgr, include_headers=True)
        lines = tsv_with_hdr.splitlines()
        assert lines[0] == "来源\t标准编码\t版本标签"
        assert lines[1] == "batch_a.txt\tABC-001\t4K"
        assert lines[2] == "batch_a.txt\tDEF-002\t-"

        assert helper.copy_rows_tsv(rows=[0, 2]) == 2
        assert QApplication.clipboard().text() == "batch_a.txt\tABC-001\t4K\nbatch_a.txt\tDEF-002\t-"

        # Context menu populates clean emoji-free actions for multi-code selection
        ctx_menu = QMenu()
        helper.populate_context_menu(ctx_menu)
        action_texts = [a.text() for a in ctx_menu.actions()]
        assert any("复制选中编码 (2 个" in t for t in action_texts)
        assert any("空格分隔" in t for t in action_texts)
        assert any("TSV" in t for t in action_texts)
        assert all("▾" not in t and "📋" not in t for t in action_texts)
    finally:
        tv.deleteLater()
        QApplication.processEvents()


def test_record_table_model_preserves_unsorted_order_on_header_attach(
    isolated_ctx: AppContext,
) -> None:
    """Guard against Qt6 setSortingEnabled(True) -> model.sort(-1) spurious col-0 sort."""
    model = RecordTableModel(theme="light")
    model.set_records(
        [
            DataRecord(id=1, canonical_code="ZZZ-999", prefix="ZZZ", number="999", file_name="z.mp4"),
            DataRecord(id=2, canonical_code="AAA-001", prefix="AAA", number="001", file_name="a.mp4"),
        ]
    )
    tv = QTableView()
    tv.setModel(model)
    try:
        TableHeaderManager.attach(tv, "lib_unsorted_guard", RecordTableModel.COLUMN_SPECS, isolated_ctx)
        assert model.data(model.index(0, 0)) == "ZZZ-999"
        assert model.data(model.index(1, 0)) == "AAA-001"
    finally:
        tv.deleteLater()
        QApplication.processEvents()


def test_header_manager_reapply_sort_and_detach_replace(isolated_ctx: AppContext) -> None:
    specs = _default_specs()
    tv, model = _make_table(specs)
    try:
        mgr1 = TableHeaderManager.attach(tv, "reapply_demo", specs, isolated_ctx)
        tv.sortByColumn(2, Qt.SortOrder.DescendingOrder)
        assert mgr1.current_sort() == (2, Qt.SortOrder.DescendingOrder)

        # Simulate model reload resetting internal order, then reapply_sort restores descending
        model._rows.reverse()
        mgr1.reapply_sort()
        assert model.data(model.index(0, 2)) == "gamma_doc.docx"

        # Attaching a new manager cleanly detaches the previous one
        mgr2 = TableHeaderManager.attach(tv, "reapply_demo_2", specs, isolated_ctx)
        assert getattr(tv, "_table_header_manager") is mgr2
        mgr2.detach()
    finally:
        tv.deleteLater()
        QApplication.processEvents()


def test_table_copy_helper_empty_table_and_tsv_ctrl_c_mode(isolated_ctx: AppContext) -> None:
    specs = _default_specs()
    tv, model = _make_table(specs, rows=[])
    try:
        mgr = TableHeaderManager.attach(tv, "empty_copy", specs, isolated_ctx)
        helper = TableCopyHelper(
            tv,
            isolated_ctx,
            header_manager=mgr,
            code_column=1,
            name_column=2,
            default_ctrl_c_mode="tsv",
        )
        btn = helper.create_copy_split_button()
        assert btn.isEnabled() is False
        assert helper.copy_selected_or_all_codes() == 0
        assert helper.copy_filenames() == 0
        assert helper.copy_paths() == 0
        assert helper.copy_rows_tsv() == 0
    finally:
        tv.deleteLater()
        QApplication.processEvents()


def test_all_core_models_default_hide_advanced_metadata_columns() -> None:
    """Zero-noise v2.0.0 contract: tags/part/prefix/rule are hidden by default across views."""
    hidden_by_model = {
        "RecordTableModel": {s.key for s in RecordTableModel.COLUMN_SPECS if not s.default_visible},
        "DuplicateTableModel": {s.key for s in DuplicateTableModel.COLUMN_SPECS if not s.default_visible},
        "PreviewTableModel(new)": {s.key for s in PreviewTableModel.specs_for_mode("new") if not s.default_visible},
        "PreviewTableModel(db_dup)": {s.key for s in PreviewTableModel.specs_for_mode("db_dup") if not s.default_visible},
        "CompareTableModel(match)": {s.key for s in CompareTableModel.specs_for_mode(True) if not s.default_visible},
        "ChecklistTableModel": {s.key for s in ChecklistTableModel.COLUMN_SPECS if not s.default_visible},
        "MatchedDetailTableModel": {s.key for s in MatchedDetailTableModel.COLUMN_SPECS if not s.default_visible},
        "OnlyInATableModel": {s.key for s in OnlyInATableModel.COLUMN_SPECS if not s.default_visible},
    }
    assert {"prefix", "tags", "part", "rule", "record_id", "note"}.issubset(hidden_by_model["RecordTableModel"])
    assert {"status", "dup_type", "tags", "part", "prefix", "rule", "record_id"}.issubset(
        hidden_by_model["DuplicateTableModel"]
    )
    assert {"prefix", "tags", "part", "rule"}.issubset(hidden_by_model["PreviewTableModel(new)"])
    assert {"tags", "part"}.issubset(hidden_by_model["PreviewTableModel(db_dup)"])
    assert {"tags", "part"}.issubset(hidden_by_model["CompareTableModel(match)"])
    assert "matched_tags" in hidden_by_model["ChecklistTableModel"]
    assert "matched_tags" in hidden_by_model["MatchedDetailTableModel"]
    assert {"prefix", "tags", "part"}.issubset(hidden_by_model["OnlyInATableModel"])
    assert len(MissingOnlyTableModel.COLUMN_SPECS) == 4


def test_checklist_model_keeps_clean_filenames_and_optional_tag_col6(qapp: QApplication) -> None:
    from src.core.models import ChecklistEntry

    entry = ChecklistEntry(
        order_index=1,
        target_text="ABC-001 示例",
        canonical_code="ABC-001",
        status="matched",
        matched_a_count=1,
        matched_a_names=["ABC-001_clean.mp4"],
        matched_a_tags="4K / 字幕",
    )
    model = ChecklistTableModel(theme="light")
    model.set_entries([entry])
    assert model.columnCount() == 7
    # Col 5 stays clean filename without prepended [tags]; col 6 holds tags (hidden by default)
    assert model.data(model.index(0, 5)) == "ABC-001_clean.mp4"
    assert "[4K" not in model.data(model.index(0, 5))
    assert model.data(model.index(0, 6)) == "4K / 字幕"
    # Emoji-free ROLE_BADGE emitted on status column
    from src.ui.delegates import ROLE_BADGE

    badge = model.data(model.index(0, 1), ROLE_BADGE)
    assert badge is not None and badge[0] == "已包含" and badge[1] == "success"
    assert "✅" not in str(model.data(model.index(0, 1)))
