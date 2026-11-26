"""Unit tests for configuration management and SQLite multi-library repository."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from src.core.config import (
    DEFAULT_COMPANION_EXTENSIONS,
    AppConfig,
    ConfigManager,
    WindowGeometry,
    get_app_base_dir,
    get_resource_path,
    get_user_data_dir,
    is_frozen,
    normalize_extension,
)
from src.core.database import SCHEMA_VERSION, DatabaseError, DatabaseRepository
from src.core.io_utils import DEFAULT_COMPANION_EXTENSIONS as IO_COMPANION_EXTS
from src.core.models import DataRecord, ImportBatch


def _make_record(
    code: str,
    *,
    prefix: str = "",
    number: str = "",
    file_name: str = "",
    source_name: str = "清单A.txt",
    source_type: str = "txt",
    tags: list[str] | None = None,
    part: str = "",
    file_size: int = 0,
    created_at: str = "",
    note: str = "",
) -> DataRecord:
    if not prefix and "-" in code:
        prefix, number = code.split("-", 1)
    return DataRecord(
        canonical_code=code,
        prefix=prefix,
        number=number,
        raw_code=code,
        raw_text=file_name or f"{code}.mp4",
        file_name=file_name or f"{code}.mp4",
        full_path=f"D:/archive/{file_name or code + '.mp4'}",
        source_name=source_name,
        source_type=source_type,
        file_size=file_size,
        file_ext=".mp4",
        tags=list(tags or []),
        part=part,
        matched_rule="standard_alpha_num",
        confidence=0.95,
        created_at=created_at,
        note=note,
    )


# ----------------------------------------------------------------------
# ConfigManager tests
# ----------------------------------------------------------------------


class TestConfigManager:
    def test_defaults_save_and_reload(self, tmp_path: Path) -> None:
        mgr = ConfigManager(data_dir=tmp_path)
        assert mgr.config.theme == "light"
        assert mgr.config.ignore_companion_files is True
        assert ".jpg" in mgr.config.companion_extensions
        assert Path(mgr.config.current_db_path).name == "default.db"
        assert Path(mgr.config.custom_rules_path).name == "custom_rules.json"

        mgr.config.theme = "dark"
        mgr.update_window_geometry(width=1440, height=900, x=100, y=50, maximized=True)
        mgr.set_companion_extensions(["JPG", ".nfo", "jpg", "  .SRT "])
        mgr.save()

        reloaded = ConfigManager(data_dir=tmp_path)
        assert reloaded.config.theme == "dark"
        assert reloaded.config.window_geometry.width == 1440
        assert reloaded.config.window_geometry.height == 900
        assert reloaded.config.window_geometry.maximized is True
        assert reloaded.config.companion_extensions == [".jpg", ".nfo", ".srt"]

    def test_recent_dbs_and_existing_filter(self, tmp_path: Path) -> None:
        mgr = ConfigManager(data_dir=tmp_path)
        mgr.config.max_recent_dbs = 3

        db1 = tmp_path / "lib1.db"
        db2 = tmp_path / "lib2.db"
        db3 = tmp_path / "lib3.db"
        db4 = tmp_path / "lib4.db"
        db1.write_text("", encoding="utf-8")
        db3.write_text("", encoding="utf-8")

        for db in (db1, db2, db3, db4):
            mgr.add_recent_db(db)

        recent = mgr.get_recent_dbs()
        assert len(recent) == 3
        assert Path(recent[0]) == db4.resolve()
        assert Path(mgr.config.current_db_path) == db4.resolve()

        # Re-adding moves existing item to the top without duplicating.
        mgr.add_recent_db(db3)
        assert Path(mgr.get_recent_dbs()[0]) == db3.resolve()
        assert len(mgr.get_recent_dbs()) == 3

        existing = mgr.get_recent_dbs(existing_only=True)
        assert [Path(p) for p in existing] == [db3.resolve()]

        mgr.remove_recent_db(db3)
        assert db3.resolve() not in [Path(p) for p in mgr.get_recent_dbs()]

    def test_corrupt_json_recovers_gracefully(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{broken json!!!", encoding="utf-8")

        mgr = ConfigManager(config_path=cfg_file, data_dir=tmp_path)
        assert isinstance(mgr.config, AppConfig)
        assert mgr.config.companion_extensions == DEFAULT_COMPANION_EXTENSIONS
        assert (tmp_path / "config.corrupt.json").exists()

    def test_frozen_path_resolution(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_exe = tmp_path / "dist" / "data_dedup.exe"
        fake_exe.parent.mkdir(parents=True)
        fake_exe.write_bytes(b"")
        fake_meipass = tmp_path / "meipass"
        (fake_meipass / "src" / "resources").mkdir(parents=True)
        icon = fake_meipass / "src" / "resources" / "app.ico"
        icon.write_bytes(b"ICO")

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))
        monkeypatch.setattr(sys, "_MEIPASS", str(fake_meipass), raising=False)

        assert is_frozen() is True
        assert get_app_base_dir() == fake_exe.parent.resolve()
        assert get_resource_path("src/resources/app.ico") == icon

        # Portable mode flag puts user_data right next to executable.
        (fake_exe.parent / "portable.flag").write_text("1", encoding="utf-8")
        assert get_user_data_dir() == fake_exe.parent.resolve() / "user_data"

    def test_normalize_extension_and_disabled_companion_set(self, tmp_path: Path) -> None:
        assert normalize_extension(" NFO ") == ".nfo"
        assert normalize_extension(".JPG") == ".jpg"
        assert normalize_extension("   ") == ""

        mgr = ConfigManager(data_dir=tmp_path)
        assert ".jpg" in mgr.get_companion_extension_set()
        mgr.config.ignore_companion_files = False
        assert mgr.get_companion_extension_set() == set()

    def test_single_bad_field_preserves_other_settings_and_guards_bools(
        self, tmp_path: Path
    ) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "theme": "dark",
                    "max_recent_dbs": "not-a-number",
                    "backup_keep_count": True,
                    "default_dedup_strategy": "unknown_strategy",
                    "window_geometry": {
                        "width": "broken",
                        "height": 920,
                        "x": True,
                        "y": False,
                        "maximized": "true",
                        "qt_geometry_b64": "QUJD",
                    },
                }
            ),
            encoding="utf-8",
        )

        mgr = ConfigManager(config_path=cfg_file, data_dir=tmp_path)
        assert not (tmp_path / "config.corrupt.json").exists()
        assert mgr.config.theme == "dark"
        assert mgr.config.max_recent_dbs == 10
        assert mgr.config.backup_keep_count == 5
        assert mgr.config.default_dedup_strategy == "keep_highest_tag_score"
        assert mgr.config.window_geometry.width == 1280
        assert mgr.config.window_geometry.height == 920
        assert mgr.config.window_geometry.x is None
        assert mgr.config.window_geometry.y is None
        assert mgr.config.window_geometry.maximized is True
        assert mgr.config.window_geometry.qt_geometry_b64 == "QUJD"

        # Invalid theme falls back cleanly
        cfg_invalid = AppConfig.from_dict({"theme": "neon", "window_geometry": "bad"})
        assert cfg_invalid.theme == "light"
        assert isinstance(cfg_invalid.window_geometry, WindowGeometry)

    def test_portable_mode_relative_paths_and_explicit_config_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Passing explicit config_path without data_dir must NOT call get_user_data_dir()
        called = False

        def _fail_user_dir(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("get_user_data_dir should not be called")

        monkeypatch.setattr("src.core.config.get_user_data_dir", _fail_user_dir)
        explicit_cfg = tmp_path / "isolated" / "custom.json"
        mgr_iso = ConfigManager(config_path=explicit_cfg)
        assert called is False
        assert mgr_iso.data_dir == explicit_cfg.parent.resolve()

        # Portable mode: stores relative paths for files inside data_dir
        drive_e = tmp_path / "drive_e" / "app"
        data_e = drive_e / "user_data"
        data_e.mkdir(parents=True)
        (drive_e / "portable.flag").write_text("1", encoding="utf-8")

        mgr = ConfigManager(data_dir=data_e)
        ext_db = tmp_path / "external_shared.db"
        mgr.add_recent_db(ext_db, set_current=False)
        mgr.add_recent_db(data_e / "databases" / "default.db", set_current=True)
        mgr.save()

        raw_saved = json.loads((data_e / "config.json").read_text(encoding="utf-8"))
        assert raw_saved["current_db_path"] == "databases/default.db"
        assert raw_saved["custom_rules_path"] == "custom_rules.json"
        assert raw_saved["recent_db_paths"][0] == "databases/default.db"
        assert Path(raw_saved["recent_db_paths"][1]) == ext_db.resolve()

        # Simulate moving portable folder to a new drive/location
        drive_f = tmp_path / "drive_f" / "app"
        data_f = drive_f / "user_data"
        data_f.mkdir(parents=True)
        (drive_f / "portable.flag").write_text("1", encoding="utf-8")
        (data_f / "config.json").write_text(
            (data_e / "config.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        moved = ConfigManager(data_dir=data_f)
        assert Path(moved.config.current_db_path) == (data_f / "databases" / "default.db").resolve()
        assert Path(moved.config.custom_rules_path) == (data_f / "custom_rules.json").resolve()

    def test_reset_to_defaults_non_frozen_paths_and_single_source_extensions(
        self, tmp_path: Path
    ) -> None:
        assert set(DEFAULT_COMPANION_EXTENSIONS) == set(IO_COMPANION_EXTS)
        assert get_resource_path("pyproject.toml") == get_app_base_dir() / "pyproject.toml"

        mgr = ConfigManager(data_dir=tmp_path)
        mgr.config.theme = "dark"
        mgr.update_window_geometry(qt_geometry_b64="BASE64DATA")
        mgr.save()
        reloaded = ConfigManager(data_dir=tmp_path)
        assert reloaded.config.window_geometry.qt_geometry_b64 == "BASE64DATA"

        reloaded.reset_to_defaults()
        assert reloaded.config.theme == "light"
        assert reloaded.config.window_geometry.qt_geometry_b64 == ""
        assert Path(reloaded.config.current_db_path).parent == (tmp_path / "databases").resolve()


# ----------------------------------------------------------------------
# DatabaseRepository tests
# ----------------------------------------------------------------------


class TestDatabaseRepository:
    def test_wal_mode_and_multi_library_switching(self, tmp_path: Path) -> None:
        db_a = tmp_path / "library_a.db"
        db_b = tmp_path / "library_b.db"

        with DatabaseRepository(db_a) as repo:
            # Verify WAL journal mode is active on disk DB.
            conn = sqlite3.connect(str(db_a))
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            conn.close()
            assert str(mode).lower() == "wal"

            repo.add_record(_make_record("ABC-001"))
            assert repo.count_records() == 1

            # Create and switch to library B.
            repo.create_database(db_b)
            assert Path(repo.db_path) == db_b.resolve()
            assert repo.count_records() == 0

            repo.add_record(_make_record("PRJ-100"))
            assert repo.count_records() == 1

            # Creating existing DB without overwrite fails cleanly.
            with pytest.raises(DatabaseError):
                repo.create_database(db_a, overwrite=False)

            # Switch back to library A and verify isolation.
            repo.switch_database(db_a)
            assert repo.count_records() == 1
            assert repo.find_by_code("ABC-001")[0].canonical_code == "ABC-001"
            assert repo.find_by_code("PRJ-100") == []

            # Overwrite library B cleanly.
            repo.create_database(db_b, overwrite=True)
            assert repo.count_records() == 0

    def test_crud_batch_insert_and_chunked_lookup(self, tmp_path: Path) -> None:
        db_path = tmp_path / "main.db"
        with DatabaseRepository(db_path) as repo:
            batch = ImportBatch(
                source_name="批次目录.txt",
                source_type="txt",
                total_count=5,
                added_count=3,
                duplicate_count=1,
                unrecognized_count=1,
            )
            recs = [
                _make_record("ABC-001", tags=["4K", "字幕"], file_size=5000),
                _make_record("ABC-002", tags=["HD"], file_size=2000),
                _make_record("DOC-105", tags=["特殊版"], note="已核对"),
            ]
            saved, saved_batch = repo.add_records(recs, batch=batch)
            assert saved_batch is not None and saved_batch.id is not None
            assert all(r.id is not None and r.batch_id == saved_batch.id for r in saved)

            batches = repo.get_batches()
            assert len(batches) == 1
            assert batches[0].source_name == "批次目录.txt"
            assert repo.get_batch(saved_batch.id).added_count == 3

            # Update record
            target = repo.get_record(saved[0].id)
            assert target is not None
            target.note = "高优先级归档"
            target.tags = ["8K", "字幕"]
            assert repo.update_record(target) is True

            refetched = repo.get_record(saved[0].id)
            assert refetched is not None
            assert refetched.note == "高优先级归档"
            assert refetched.tags == ["8K", "字幕"]

            # Chunked lookup with > 900 codes
            many_codes = [f"BULK-{i:04d}" for i in range(950)]
            repo.add_records([_make_record(c) for c in many_codes[:5]])
            found_map = repo.find_by_codes(many_codes)
            assert len(found_map) == 5
            assert "BULK-0000" in found_map

            # Chunked get_records_by_ids
            fetched_ids = repo.get_records_by_ids([r.id for r in saved])
            assert [r.canonical_code for r in fetched_ids] == ["ABC-001", "ABC-002", "DOC-105"]

    def test_search_filter_sort_duplicates_and_stats(self, tmp_path: Path) -> None:
        db_path = tmp_path / "query.db"
        with DatabaseRepository(db_path) as repo:
            repo.add_records(
                [
                    _make_record("ABC-001", file_name="ABC-001_4K.mp4", tags=["4K"], source_name="盘A.txt"),
                    _make_record("ABC-001", file_name="ABC-001_HD.mp4", tags=["HD"], source_name="盘B.txt"),
                    _make_record("ABC-001", file_name="ABC-001_backup.mp4", tags=["字幕"], source_name="盘C.txt"),
                    _make_record("PRJ-200", file_name="PRJ-200-CD1.mp4", part="CD1", source_name="盘A.txt"),
                    _make_record("PRJ-200", file_name="PRJ-200-CD2.mp4", part="CD2", source_name="盘A.txt"),
                    _make_record("DOC-999", file_name="DOC-999_unique.mp4", note="特殊备注", source_name="盘B.txt"),
                ]
            )

            assert repo.count_records() == 6
            assert repo.count_records(filter_mode="duplicates") == 5
            assert repo.count_records(filter_mode="unique") == 1
            assert repo.count_records(filter_mode="multipart") == 2
            assert repo.count_records(prefix="ABC") == 3
            assert repo.count_records(source_name="盘B.txt") == 2
            assert repo.count_records(tag="4K") == 1
            assert repo.count_records(search="特殊备注") == 1
            assert repo.count_records(search="prj-200") == 2

            # Sort by duplicate_count descending puts ABC-001 (count 3) first.
            sorted_recs = repo.query_records(sort_by="duplicate_count", sort_desc=True)
            assert sorted_recs[0].canonical_code == "ABC-001"
            assert sorted_recs[-1].canonical_code == "DOC-999"

            # Pagination
            page = repo.query_records(sort_by="id", sort_desc=False, limit=2, offset=2)
            assert len(page) == 2

            # Duplicate groups
            all_groups = repo.get_duplicate_groups(include_multipart=True)
            assert len(all_groups) == 2
            by_code = {g.canonical_code: g for g in all_groups}
            assert by_code["ABC-001"].count == 3
            assert by_code["ABC-001"].is_possible_multipart is False
            assert by_code["PRJ-200"].count == 2
            assert by_code["PRJ-200"].is_possible_multipart is True

            non_mp_groups = repo.get_duplicate_groups(include_multipart=False)
            assert [g.canonical_code for g in non_mp_groups] == ["ABC-001"]

            # Stats
            stats = repo.get_stats()
            assert stats.total_records == 6
            assert stats.unique_codes == 3
            assert stats.duplicate_groups == 2
            assert stats.duplicate_records == 3  # (3-1) + (2-1)
            assert stats.possible_multipart_groups == 1
            assert stats.prefix_counts == {"ABC": 3, "PRJ": 2, "DOC": 1}
            assert repo.get_distinct_prefixes() == ["ABC", "PRJ", "DOC"]
            assert repo.get_distinct_sources() == ["盘A.txt", "盘B.txt", "盘C.txt"]

    def test_auto_rolling_backup_and_restore(self, tmp_path: Path) -> None:
        db_path = tmp_path / "backup_test.db"
        with DatabaseRepository(db_path, auto_backup=True, backup_keep_count=2) as repo:
            saved, _ = repo.add_records(
                [
                    _make_record("ITEM-001"),
                    _make_record("ITEM-002"),
                    _make_record("ITEM-003"),
                ]
            )

            # First delete creates backup #1 (contains all 3 records)
            deleted = repo.delete_records([saved[0].id])
            assert deleted == 1
            backups_after_1 = repo.list_backups()
            assert len(backups_after_1) == 1
            first_backup = backups_after_1[0]

            # Two more destructive ops trigger rolling prune to keep_count=2
            repo.delete_record(saved[1].id)
            repo.clear_all_records()
            assert repo.count_records() == 0

            backups_after_3 = repo.list_backups()
            assert len(backups_after_3) == 2
            assert not first_backup.exists()

            # Restore from oldest remaining backup (which had 2 records before second delete)
            oldest_remaining = backups_after_3[-1]
            repo.restore_backup(oldest_remaining)
            assert repo.count_records() == 2

    def test_delete_by_batch(self, tmp_path: Path) -> None:
        db_path = tmp_path / "batch_del.db"
        with DatabaseRepository(db_path, auto_backup=False) as repo:
            _, b1 = repo.add_records(
                [_make_record("A-001"), _make_record("A-002")],
                batch=ImportBatch(source_name="b1.txt", total_count=2),
            )
            _, b2 = repo.add_records(
                [_make_record("B-001")],
                batch=ImportBatch(source_name="b2.txt", total_count=1),
            )
            assert b1 is not None and b1.id is not None
            assert b2 is not None and b2.id is not None

            removed = repo.delete_by_batch(b1.id)
            assert removed == 2
            assert repo.count_records() == 1
            assert repo.get_batch(b1.id) is None
            assert repo.get_batch(b2.id) is not None

    def test_backup_isolation_prefix_collision_and_brackets(self, tmp_path: Path) -> None:
        db_short = tmp_path / "work.db"
        db_long = tmp_path / "work_2026.db"
        db_bracket = tmp_path / "lib_[1].db"

        with DatabaseRepository(db_long, backup_keep_count=3) as repo_long:
            repo_long.add_record(_make_record("LONG-001"))
            bak_long = repo_long.create_backup(reason="manual")
            assert bak_long is not None and bak_long.is_file()

        with DatabaseRepository(db_short, backup_keep_count=1) as repo_short:
            repo_short.add_record(_make_record("SHORT-001"))
            repo_short.create_backup(reason="first")
            repo_short.create_backup(reason="second")

            # work.db must NEVER see or delete work_2026.db's backup!
            assert bak_long.exists()
            short_baks = repo_short.list_backups()
            assert len(short_baks) == 1
            assert all(b.name.startswith("work_20") is False for b in short_baks) or all(
                b != bak_long for b in short_baks
            )

        with DatabaseRepository(db_bracket, backup_keep_count=1) as repo_br:
            repo_br.add_record(_make_record("BR-001"))
            b1 = repo_br.create_backup(reason="one")
            b2 = repo_br.create_backup(reason="two")
            assert b1 is not None and b2 is not None
            listed = repo_br.list_backups()
            assert len(listed) == 1
            assert listed[0] == b2
            assert not b1.exists()

    def test_restore_rejects_corrupt_bak_and_works_under_wal_reader(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "live.db"
        with DatabaseRepository(db_path, auto_backup=True) as repo:
            repo.add_records([_make_record("KEEP-001"), _make_record("KEEP-002")])
            good_bak = repo.create_backup(reason="good")
            assert good_bak is not None

            # Corrupt garbage file must be rejected WITHOUT damaging live DB
            bad_bak = tmp_path / "corrupt.db.bak"
            bad_bak.write_bytes(b"NOT A SQLITE FILE")
            with pytest.raises(DatabaseError):
                repo.restore_backup(bad_bak)
            assert repo.count_records() == 2

            # Valid SQLite file missing required tables must also be rejected
            empty_sqlite = tmp_path / "empty_schema.db.bak"
            c = sqlite3.connect(str(empty_sqlite))
            c.execute("CREATE TABLE other (x INT)")
            c.commit()
            c.close()
            with pytest.raises(DatabaseError):
                repo.restore_backup(empty_sqlite)
            assert repo.count_records() == 2

            # Mutate live DB, hold a concurrent WAL reader, then restore cleanly
            repo.add_record(_make_record("EXTRA-003"))
            assert repo.count_records() == 3

            reader = sqlite3.connect(str(db_path))
            try:
                assert reader.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 3
                repo.restore_backup(good_bak)
                assert repo.count_records() == 2
            finally:
                reader.close()

    def test_no_redundant_backup_on_noop_deletes_and_remove_db_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "noop.db"
        with DatabaseRepository(db_path, auto_backup=True) as repo:
            assert repo.delete_records([]) == 0
            assert repo.delete_records([99999]) == 0
            assert repo.delete_by_batch(99999) == 0
            assert repo.clear_all_records() == 0
            assert repo.list_backups() == []

        # _remove_db_files must surface OSError as DatabaseError (never silently swallow)
        wal_path = Path(f"{db_path}-wal")
        wal_path.write_bytes(b"locked")

        orig_unlink = Path.unlink

        def _deny_wal(self: Path, *args, **kwargs):
            if self.name.endswith("-wal"):
                raise PermissionError("file locked")
            return orig_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _deny_wal)
        with pytest.raises(DatabaseError, match="无法删除数据库文件"):
            DatabaseRepository._remove_db_files(db_path)

    def test_like_escape_multipart_consistency_empty_code_sort_and_schema_guard(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "edge.db"
        with DatabaseRepository(db_path, auto_backup=False) as repo:
            repo.add_records(
                [
                    _make_record("ABC-001", file_name="ABC-001.mp4", tags=["4K"]),
                    _make_record("ABC-002", file_name="ABC_001.mp4", tags=["4_"]),
                    _make_record("PCT-001", file_name="100%real.mp4"),
                    _make_record("PCT-002", file_name="1000real.mp4"),
                    # Singleton with part -> NOT a multipart group!
                    _make_record("SOLO-001", file_name="SOLO-001-CD1.mp4", part="CD1"),
                    # Same-part duplicates -> NOT a multipart group!
                    _make_record("SAME-001", file_name="SAME-001-CD1-a.mp4", part="CD1"),
                    _make_record("SAME-001", file_name="SAME-001-CD1-b.mp4", part="CD1"),
                    # True multipart group (CD1 + CD2)
                    _make_record("MP-001", file_name="MP-001-CD1.mp4", part="CD1"),
                    _make_record("MP-001", file_name="MP-001-CD2.mp4", part="CD2"),
                    # 3 empty canonical_code rows must NOT outrank real duplicates
                    _make_record("", file_name="unrec1.txt"),
                    _make_record("", file_name="unrec2.txt"),
                    _make_record("", file_name="unrec3.txt"),
                ]
            )

            # Escaped LIKE wildcards
            assert [r.file_name for r in repo.query_records(search="ABC_001")] == ["ABC_001.mp4"]
            assert [r.file_name for r in repo.query_records(search="100%")] == ["100%real.mp4"]
            assert [r.canonical_code for r in repo.query_records(tag="4_")] == ["ABC-002"]

            # Multipart filter aligns strictly with get_stats()
            stats = repo.get_stats()
            assert stats.possible_multipart_groups == 1
            mp_rows = repo.query_records(filter_mode="multipart")
            assert {r.canonical_code for r in mp_rows} == {"MP-001"}
            assert len(mp_rows) == 2

            # sort_by="duplicate_count" DESC puts real duplicates before empty canonical_code rows
            ordered = repo.query_records(sort_by="duplicate_count", sort_desc=True)
            assert ordered[0].canonical_code in ("MP-001", "SAME-001")
            assert ordered[-1].canonical_code == ""

        # Schema downgrade protection
        future_db = tmp_path / "future.db"
        conn = sqlite3.connect(str(future_db))
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
            (str(int(SCHEMA_VERSION) + 5),),
        )
        conn.commit()
        conn.close()
        with pytest.raises(DatabaseError, match="高于当前程序支持版本"):
            DatabaseRepository(future_db)

    def test_edge_error_paths_chunked_ids_and_batch_filter(self, tmp_path: Path) -> None:
        with DatabaseRepository(tmp_path / "misc.db", auto_backup=False) as repo:
            # update_record edge cases
            with pytest.raises(ValueError):
                repo.update_record(_make_record("NO-ID"))
            missing = _make_record("MISSING-1")
            missing.id = 999999
            assert repo.update_record(missing) is False

            # > 800 IDs chunked get_records_by_ids + batch_id filtering
            saved, b1 = repo.add_records(
                [_make_record(f"ID-{i:04d}") for i in range(850)],
                batch=ImportBatch(source_name="big.txt", total_count=850),
            )
            assert b1 is not None and b1.id is not None
            fetched = repo.get_records_by_ids([r.id for r in saved if r.id is not None])
            assert len(fetched) == 850
            assert repo.count_records(batch_id=b1.id) == 850
            assert repo.count_records(batch_id=b1.id + 999) == 0

        # Closed DB raises DatabaseError
        with pytest.raises(DatabaseError, match="已关闭"):
            repo.count_records()
