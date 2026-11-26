"""Headless PySide6 (QT_QPA_PLATFORM=offscreen) UI smoke & regression test suite.

Covers:
- AppContext lifecycle, DB switching, theme switching, companion extension lookup
- MainWindow initialization, version sync, tab navigation, theme switch, safe shutdown
- RecordTableModel, PreviewTableModel (inline manual fix + sort), DynamicTableModel (sort/NaN/filter/reset)
- ImportView threaded preview, queue deduplication, cancellation reset & db_switched invalidation
- CompareView companion-file checkbox independence from global default & non-mutating DB check
- TablesView async file loading, filter reset on switch, dedup, merge & snapshot invalidation
- SettingsView & RuleEditorDialog regex validator (ReDoS, empty-match, clean_text preview)
- CLI `python -m src.app --smoke-test` end-to-end subprocess check
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

# Must be set before any QtGui/QtWidgets import so tests run cleanly in CI/headless shells.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import QApplication

from src.core.config import ConfigManager
from src.core.models import DataRecord, ParseResult
from src.core.table_data import DataTable
from src.ui.app_context import AppContext
from src.ui.main_window import APP_VERSION, MainWindow
from src.ui.table_models import PreviewTableModel, RecordTableModel
from src.ui.table_preview_model import DynamicTableModel
from src.ui.view_compare import CompareView
from src.ui.view_import import ImportView
from src.ui.view_settings import RuleEditorDialog, SettingsView, _validate_custom_regex_safe
from src.ui.view_tables import TablesView
from src.ui.widgets import WorkerThread


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(["pytest-ui-smoke", "-platform", "offscreen"])
    assert isinstance(app, QApplication)
    return app


def _wait_until(
    predicate: Callable[[], bool],
    timeout_s: float = 10.0,
    poll_interval: float = 0.015,
) -> bool:
    """Pump Qt events until ``predicate()`` returns True or ``timeout_s`` elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            QApplication.processEvents()
            return True
        time.sleep(poll_interval)
    QApplication.processEvents()
    return bool(predicate())


@pytest.fixture
def isolated_ctx(qapp: QApplication, tmp_path: Path):
    """Provide an isolated AppContext backed by a temporary data directory."""
    data_dir = tmp_path / "app_data"
    cfg_mgr = ConfigManager(data_dir=data_dir)
    ctx = AppContext(config_manager=cfg_mgr)
    try:
        yield ctx
    finally:
        ctx.close()
        QApplication.processEvents()


# ======================================================================
# 1. AppContext & MainWindow
# ======================================================================


def test_app_context_lifecycle_and_settings(isolated_ctx: AppContext, tmp_path: Path) -> None:
    ctx = isolated_ctx
    assert Path(ctx.db.db_path).is_file()

    # Companion extensions: global toggle off vs forced local checkbox override
    ctx.config.ignore_companion_files = False
    assert ctx.get_companion_extensions(respect_global_toggle=True) == set()
    forced_exts = ctx.get_companion_extensions(respect_global_toggle=False)
    assert ".jpg" in forced_exts
    assert ".nfo" in forced_exts

    # Theme signal & persistence
    seen_themes: list[str] = []
    ctx.theme_changed.connect(seen_themes.append)
    ctx.set_theme("dark")
    ctx.set_theme("light")
    assert seen_themes == ["dark", "light"]

    # Create & switch database
    db2_path = tmp_path / "second.db"
    switched_events: list[str] = []
    ctx.db_switched.connect(switched_events.append)
    ctx.create_database(db2_path)
    assert Path(ctx.db.db_path).resolve() == db2_path.resolve()
    assert len(switched_events) == 1

    # Sync DB backup preferences
    ctx.config.auto_backup = False
    ctx.config.backup_keep_count = 3
    ctx.sync_db_backup_settings()
    assert ctx.db.auto_backup is False
    assert ctx.db.backup_keep_count == 3

    # Idempotent close
    ctx.close()
    ctx.close()


def test_main_window_init_theme_switch_and_safe_shutdown(isolated_ctx: AppContext) -> None:
    assert APP_VERSION.startswith("v1.2.")

    win = MainWindow(isolated_ctx)
    try:
        assert "v1.2." in win.windowTitle()

        # Navigate across all 6 views
        for idx in range(6):
            win.switch_view(idx)
            QApplication.processEvents()

        # Switch themes light -> dark -> light
        isolated_ctx.set_theme("dark")
        QApplication.processEvents()
        assert isolated_ctx.config.theme == "dark"

        isolated_ctx.set_theme("light")
        QApplication.processEvents()
        assert isolated_ctx.config.theme == "light"

        # Invoke closeEvent / shutdown cleanly (and confirm idempotency)
        win.close()
        QApplication.processEvents()
        win.shutdown()
        assert win._is_shut_down is True
    finally:
        win.deleteLater()
        QApplication.processEvents()


# ======================================================================
# 2. Virtualized Table Models
# ======================================================================


def test_record_table_model_sort_and_theme(qapp: QApplication) -> None:
    model = RecordTableModel(theme="light")
    recs = [
        DataRecord(id=1, canonical_code="ZZZ-002", prefix="ZZZ", number="002", file_name="zzz.mp4"),
        DataRecord(id=2, canonical_code="AAA-001", prefix="AAA", number="001", file_name="aaa.mp4"),
        DataRecord(id=3, canonical_code="AAA-001", prefix="AAA", number="001", file_name="aaa_copy.mp4"),
    ]
    model.set_records(recs)
    assert model.rowCount() == 3
    assert model.columnCount() == len(RecordTableModel.COLUMNS)

    # Sort ascending by canonical_code (col 0)
    model.sort(0, Qt.SortOrder.AscendingOrder)
    assert model.data(model.index(0, 0)) == "AAA-001"
    assert model.data(model.index(2, 0)) == "ZZZ-002"

    # Sort descending
    model.sort(0, Qt.SortOrder.DescendingOrder)
    assert model.data(model.index(0, 0)) == "ZZZ-002"

    # Theme update emits cleanly
    model.set_theme("dark")
    fg = model.data(model.index(2, 0), Qt.ItemDataRole.ForegroundRole)
    assert fg is not None


def test_preview_table_model_inline_fix_and_sort(qapp: QApplication) -> None:
    model = PreviewTableModel(mode="unrecognized", theme="light")
    r1 = ParseResult(raw_text="weird_file_b.mp4", file_name="weird_file_b.mp4", recognized=False)
    r2 = ParseResult(raw_text="weird_file_a.mp4", file_name="weird_file_a.mp4", recognized=False)
    model.set_rows([r1, r2])
    assert model.rowCount() == 2

    fixed_signals: list[tuple[int, str]] = []
    model.code_manually_fixed.connect(lambda row, code: fixed_signals.append((row, code)))

    # Inline edit with multi-hyphen code
    idx = model.index(0, 0)
    ok = model.setData(idx, "prj-sub-0042", Qt.ItemDataRole.EditRole)
    assert ok is True
    assert r1.recognized is True
    assert r1.canonical_code == "PRJ-SUB-0042"
    assert r1.prefix == "PRJ-SUB"
    assert r1.number == "0042"
    assert fixed_signals == [(0, "PRJ-SUB-0042")]

    # Clearing manual code resets recognized state cleanly
    ok_clear = model.setData(idx, "", Qt.ItemDataRole.EditRole)
    assert ok_clear is True
    assert r1.recognized is False
    assert r1.canonical_code == ""
    assert r1.prefix == ""
    assert r1.number == ""

    # Sort works without raising
    model.sort(1, Qt.SortOrder.AscendingOrder)
    assert model.rowCount() == 2


def test_dynamic_table_model_sort_nan_filter_and_reset(qapp: QApplication) -> None:
    model = DynamicTableModel(theme="light")
    headers = ["编号", "金额", "备注"]
    rows = [
        ["0010", "1,200.5", "alpha"],
        ["0002", "NaN", "beta"],
        ["0001", "25", "gamma"],
        ["0003", "inf", "delta"],
        ["0004", ",123", "epsilon"],
        ["0005", "", "blank"],
    ]
    model.set_data(headers, rows)
    assert model.rowCount() == 6

    # Sort numeric column containing finite numbers, commas, NaN, inf, malformed comma, blank
    model.sort(1, Qt.SortOrder.AscendingOrder)
    sorted_vals = [model.data(model.index(r, 1)) for r in range(model.rowCount())]
    # Finite numbers (25, 1,200.5) must sort first in numeric order, blank last
    assert sorted_vals[0] == "25"
    assert sorted_vals[1] == "1,200.5"
    assert sorted_vals[-1] == ""

    # Leading-zero IDs sort lexicographically without numeric corruption
    model.sort(0, Qt.SortOrder.AscendingOrder)
    assert model.data(model.index(0, 0)) == "0001"
    assert model.data(model.index(5, 0)) == "0010"

    # Substring filtering
    model.set_filter("gamma")
    assert model.rowCount() == 1
    assert model.data(model.index(0, 2)) == "gamma"

    # Switching data with reset_filter=True MUST clear stale filter query (P0 fix)
    model.set_data(["列A"], [["全新内容"]])
    assert model.filter_query() == ""
    assert model.rowCount() == 1
    assert model.data(model.index(0, 0)) == "全新内容"

    # Theme update + clear
    model.set_theme("dark")
    model.clear()
    assert model.rowCount() == 0
    assert model.filter_query() == ""


# ======================================================================
# 3. WorkerThread Cancellation Contract
# ======================================================================


def test_worker_thread_cancel_emits_cancelled_only(qapp: QApplication) -> None:
    events: list[str] = []

    def _slow_task() -> str:
        for _ in range(40):
            if worker.is_cancelled:
                return "aborted"
            time.sleep(0.01)
        return "done"

    worker = WorkerThread(_slow_task, auto_delete=False)
    worker.succeeded.connect(lambda _: events.append("succeeded"))
    worker.failed.connect(lambda _: events.append("failed"))
    worker.cancelled.connect(lambda: events.append("cancelled"))

    worker.start()
    time.sleep(0.03)
    worker.cancel()
    assert _wait_until(lambda: not worker.isRunning(), timeout_s=5.0)
    assert events == ["cancelled"]
    worker.deleteLater()
    QApplication.processEvents()


# ======================================================================
# 4. ImportView Preview & Cancel
# ======================================================================


def test_import_view_preview_queue_and_cancel(isolated_ctx: AppContext, tmp_path: Path) -> None:
    # Seed 1 record in DB so preview classifies both new and db_dup
    isolated_ctx.db.add_records(
        [
            DataRecord(
                canonical_code="ABC-001",
                prefix="ABC",
                number="001",
                raw_text="ABC-001.mp4",
                file_name="ABC-001.mp4",
                source_type="txt",
                source_name="seed.txt",
            )
        ]
    )

    sample_txt = tmp_path / "import_list.txt"
    sample_txt.write_text(
        "ABC-001.mp4\nDEF-002.mkv\nDEF-002_copy.mkv\nrandom_unrecognized_note\n",
        encoding="utf-8",
    )

    view = ImportView(isolated_ctx)
    try:
        view._on_paths_added([str(sample_txt)])
        assert _wait_until(lambda: view._current_preview is not None, timeout_s=10.0)

        prev = view._current_preview
        assert prev is not None
        assert len(prev.new_items) == 1
        assert prev.new_items[0].canonical_code == "DEF-002"
        assert len(prev.db_duplicates) == 1
        assert len(prev.batch_duplicates) == 1
        assert len(prev.unrecognized) == 1
        assert view._new_model.rowCount() == 1
        assert view._db_dup_model.rowCount() == 1

        # Re-adding the same file deduplicates queue cleanly
        view._on_paths_added([str(sample_txt)])
        assert len(view._queued_paths) == 1

        # Test cancel resets busy UI state cleanly (never leaves buttons stuck)
        big_txt = tmp_path / "many_lines.txt"
        big_txt.write_text(
            "\n".join(f"PRJ-{i:04d}.mp4" for i in range(2000)),
            encoding="utf-8",
        )
        view._clear_all()
        view._queued_paths = [str(big_txt)]
        view._start_scan_worker()
        view._cancel_worker()
        assert _wait_until(
            lambda: not view._is_worker_running(view._worker) and not view._progress_bar.isVisible(),
            timeout_s=10.0,
        )
        assert view._btn_files.isEnabled() is True

        # Switching DB invalidates stale preview
        db2 = tmp_path / "other.db"
        isolated_ctx.create_database(db2)
        QApplication.processEvents()
        assert view._current_preview is None
    finally:
        view.shutdown()
        view.deleteLater()
        QApplication.processEvents()


# ======================================================================
# 5. CompareView Companion Checkbox & Non-Mutating DB Check
# ======================================================================


def test_compare_view_companion_checkbox_overrides_global_default(isolated_ctx: AppContext) -> None:
    isolated_ctx.db.add_records(
        [
            DataRecord(
                canonical_code="ABC-100",
                prefix="ABC",
                number="100",
                raw_text="ABC-100.mp4",
                file_name="ABC-100.mp4",
                source_type="txt",
                source_name="seed.txt",
            )
        ]
    )
    # Global setting OFF, but local checkbox ON in CompareView -> MUST still filter companions!
    isolated_ctx.config.ignore_companion_files = False

    view = CompareView(isolated_ctx)
    try:
        view._chk_db_ignore_comp.setChecked(True)
        view._db_input.setPlainText(
            "ABC-100.mp4\nXYZ-200.mkv\ncover.jpg\nreadme.txt\ninfo.nfo\n"
        )
        view.run_db_compare()
        assert _wait_until(lambda: view._db_compare_result is not None, timeout_s=10.0)

        res = view._db_compare_result
        assert res is not None
        assert len(res.matched_in_db) == 1
        assert len(res.not_in_db) == 1
        assert res.not_in_db[0].canonical_code == "XYZ-200"
        # Companion files were filtered instead of leaking into unrecognized_a
        assert len(res.unrecognized_a) == 0

        # Non-mutating guarantee: DB still has only the 1 seeded record
        assert isolated_ctx.db.get_stats().total_records == 1
    finally:
        view.shutdown()
        view.deleteLater()
        QApplication.processEvents()


# ======================================================================
# 6. TablesView Load, Filter Reset, Dedup, Merge & Snapshot Invalidation
# ======================================================================


def test_tables_view_load_dedup_merge_and_snapshot_invalidation(
    isolated_ctx: AppContext,
    tmp_path: Path,
) -> None:
    csv_a = tmp_path / "table_a.csv"
    csv_b = tmp_path / "table_b.csv"
    csv_a.write_text(
        "编码,名称,备注\nABC-001,项目一,来自A\nDEF-002,项目二,仅A有\n",
        encoding="utf-8-sig",
    )
    csv_b.write_text(
        "编码,名称,备注\nABC-001,项目一,来自B\nGHI-003,项目三,仅B有\n",
        encoding="utf-8-sig",
    )

    view = TablesView(isolated_ctx)
    try:
        # 1. Load both CSV files asynchronously (non-interactive)
        view.load_files([str(csv_a), str(csv_b)], interactive=False)
        assert _wait_until(
            lambda: len(view._tables) == 2 and not view._is_worker_running(view._load_worker),
            timeout_s=10.0,
        )

        # 2. Apply preview search filter on table A, then switch to table B -> filter MUST reset
        view._preview_search.setText("仅A有")
        view._apply_preview_filter()
        assert view._preview_model.rowCount() == 1

        second_id = view._tables[1].table_id
        view._select_table_by_id(second_id)
        QApplication.processEvents()
        assert view._preview_model.filter_query() == ""
        assert view._preview_model.rowCount() == 2

        # 3. Run multi-table dedup (column_value on "编码" or smart_code)
        view.run_dedup()
        assert _wait_until(
            lambda: view._last_dedup_report is not None and not view._is_worker_running(view._dedup_worker),
            timeout_s=10.0,
        )
        assert view._last_dedup_report is not None
        assert view._last_dedup_snapshot is not None
        assert len(view._last_dedup_report.cross_table_groups) == 1
        assert view._last_dedup_report.cross_table_groups[0].key_value == "ABC-001"

        # 4. Run multi-table merge (union_dedup default)
        view.run_merge()
        assert _wait_until(
            lambda: view._last_merge_result is not None and not view._is_worker_running(view._merge_worker),
            timeout_s=10.0,
        )
        assert view._last_merge_result is not None
        assert view._last_merge_snapshot is not None
        assert view._last_merge_result.merged_table.row_count == 3
        assert len(view._last_merge_result.dropped_rows) == 1

        # 5. Snapshot invalidation: removing one participating table MUST invalidate BOTH reports
        view._table_list.clearSelection()
        view._table_list.item(1).setSelected(True)
        view._remove_selected_table()
        QApplication.processEvents()
        assert len(view._tables) == 1
        assert view._last_dedup_report is None
        assert view._last_dedup_snapshot is None
        assert view._last_merge_result is None
        assert view._last_merge_snapshot is None
    finally:
        view.shutdown()
        view.deleteLater()
        QApplication.processEvents()


# ======================================================================
# 7. SettingsView & RuleEditorDialog Validator
# ======================================================================


def test_settings_view_and_rule_editor_validator(isolated_ctx: AppContext) -> None:
    # Direct validator safety checks
    ok, _ = _validate_custom_regex_safe(r"(?<![A-Za-z0-9])(PRJ)[-_]?(\d{4})(?![0-9])")
    assert ok is True

    ok_empty, err_empty = _validate_custom_regex_safe(r"a*")
    assert ok_empty is False
    assert err_empty

    ok_redos, err_redos = _validate_custom_regex_safe(r"(a+)+$")
    assert ok_redos is False
    assert err_redos

    ok_bad, err_bad = _validate_custom_regex_safe(r"([unclosed")
    assert ok_bad is False
    assert err_bad

    # RuleEditorDialog live preview runs against clean_text(sample)
    dlg = RuleEditorDialog()
    try:
        dlg._name.setText("内部项目编码")
        dlg._pattern.setText(r"(?<![A-Za-z0-9])(PRJ)[-_]?(\d{4})(?![0-9])")
        dlg._template.setText("{1}-{2}")
        dlg._example.setText("[Archive] PRJ_0042_4K.mkv")
        dlg._live_validate()
        assert "PRJ-0042" in dlg._validation_label.text()

        # ReDoS pattern displays error marker
        dlg._pattern.setText(r"(a+)+")
        dlg._live_validate()
        assert "❌" in dlg._validation_label.text()
    finally:
        dlg.close()
        dlg.deleteLater()
        QApplication.processEvents()

    # SettingsView boots, switches theme, and shuts down cleanly
    view = SettingsView(isolated_ctx)
    try:
        view.apply_theme("dark")
        view.apply_theme("light")
        QApplication.processEvents()
    finally:
        view.shutdown()
        view.deleteLater()
        QApplication.processEvents()


# ======================================================================
# 8. CLI `python -m src.app --smoke-test`
# ======================================================================


def test_app_cli_smoke_test_subprocess(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parent.parent
    data_dir = tmp_path / "cli_smoke_data"
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    result = subprocess.run(
        [sys.executable, "-m", "src.app", "--smoke-test", "--data-dir", str(data_dir)],
        cwd=str(project_root),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    combined = (result.stdout or "") + (result.stderr or "")
    assert "sqlite" in combined.lower() or "smoke" in combined.lower()
