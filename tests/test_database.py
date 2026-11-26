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
    get_app_base_dir,
    get_resource_path,
    get_user_data_dir,
    is_frozen,
    normalize_extension,
)
from src.core.database import DatabaseError, DatabaseRepository
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
