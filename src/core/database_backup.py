"""Rolling ``.bak`` backup, integrity validation, and WAL-safe restore mixin.

Extracted from ``src/core/database.py`` for modularity while keeping all public
symbols and import paths on ``src.core.database`` 100% backward-compatible.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

SCHEMA_VERSION = "1"
_SQLITE_BUSY_TIMEOUT_MS = 15000

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'txt',
    total_count INTEGER NOT NULL DEFAULT 0,
    added_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    unrecognized_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_code TEXT NOT NULL,
    prefix TEXT NOT NULL DEFAULT '',
    number TEXT NOT NULL DEFAULT '',
    raw_code TEXT NOT NULL DEFAULT '',
    raw_text TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL DEFAULT '',
    full_path TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'txt',
    file_size INTEGER NOT NULL DEFAULT 0,
    file_ext TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    part TEXT NOT NULL DEFAULT '',
    matched_rule TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    batch_id INTEGER REFERENCES import_batches(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_records_canonical_code
    ON records(canonical_code);
CREATE INDEX IF NOT EXISTS idx_records_prefix
    ON records(prefix);
CREATE INDEX IF NOT EXISTS idx_records_source_name
    ON records(source_name);
CREATE INDEX IF NOT EXISTS idx_records_batch_id
    ON records(batch_id);
CREATE INDEX IF NOT EXISTS idx_records_created_at
    ON records(created_at);
CREATE INDEX IF NOT EXISTS idx_records_part
    ON records(part);
CREATE INDEX IF NOT EXISTS idx_records_code_part
    ON records(canonical_code, part);
"""


class DatabaseError(Exception):
    """User-friendly database operation error."""


class DatabaseBackupMixin:
    """Mixin providing atomic rolling ``.bak`` backups and validated online restore."""

    _lock: threading.RLock
    _db_path: str
    _backup_keep_count: int

    @property
    def is_memory(self) -> bool:
        return self._db_path == ":memory:"

    def _require_conn(self) -> sqlite3.Connection:  # pragma: no cover - provided by host class
        raise NotImplementedError

    def _init_schema(self) -> None:  # pragma: no cover - provided by host class
        raise NotImplementedError

    @staticmethod
    def _backup_name_regex(db_stem: str) -> re.Pattern[str]:
        return re.compile(
            rf"^{re.escape(db_stem)}_\d{{8}}_\d{{6}}_\d{{6}}_.*\.db\.bak$"
        )

    @classmethod
    def _list_backups_in_dir(cls, backup_dir: Path, db_stem: str) -> List[Path]:
        if not backup_dir.is_dir():
            return []
        matcher = cls._backup_name_regex(db_stem)
        matched: List[Tuple[float, str, Path]] = []
        for entry in backup_dir.iterdir():
            if not entry.is_file() or not matcher.match(entry.name):
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            matched.append((mtime, entry.name, entry))
        matched.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in matched]

    def get_backup_dir(self) -> Optional[Path]:
        with self._lock:
            if self.is_memory:
                return None
            return Path(self._db_path).parent / "backups"

    def create_backup(
        self,
        reason: str = "manual",
        *,
        protect_paths: Optional[Iterable[Path]] = None,
    ) -> Optional[Path]:
        """Create an atomic, WAL-consistent SQLite ``.bak`` snapshot and prune older backups."""
        with self._lock:
            if self.is_memory:
                return None
            src_path = Path(self._db_path)
            if not src_path.exists():
                return None

            conn = self._require_conn()
            backup_dir = src_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)

            safe_reason = "".join(
                ch if (ch.isalnum() or ch in ("_", "-")) else "_"
                for ch in (reason or "manual")
            )[:32]
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            backup_path = backup_dir / f"{src_path.stem}_{stamp}_{safe_reason}.db.bak"
            tmp_path = backup_dir / f".{src_path.stem}_{stamp}_{uuid.uuid4().hex[:8]}.db.bak.tmp"

            dst_conn: Optional[sqlite3.Connection] = None
            try:
                dst_conn = sqlite3.connect(str(tmp_path))
                conn.backup(dst_conn)
                dst_conn.close()
                dst_conn = None
                tmp_path.replace(backup_path)
            except (sqlite3.Error, OSError) as exc:
                if dst_conn is not None:
                    try:
                        dst_conn.close()
                    except sqlite3.Error:
                        pass
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise DatabaseError(f"创建数据库备份失败：{exc}") from exc

            self._prune_backups(backup_dir, src_path.stem, protect_paths=protect_paths)
            return backup_path

    def _prune_backups(
        self,
        backup_dir: Path,
        db_stem: str,
        *,
        protect_paths: Optional[Iterable[Path]] = None,
    ) -> None:
        protected = {p.resolve() for p in (protect_paths or ())}
        backups = self._list_backups_in_dir(backup_dir, db_stem)
        for old_file in backups[self._backup_keep_count :]:
            if old_file.resolve() in protected:
                continue
            try:
                old_file.unlink()
            except OSError:
                pass

    def list_backups(self) -> List[Path]:
        """List existing ``.bak`` snapshots for the current database, newest first."""
        with self._lock:
            if self.is_memory:
                return []
            src_path = Path(self._db_path)
            backup_dir = src_path.parent / "backups"
            return self._list_backups_in_dir(backup_dir, src_path.stem)

    @staticmethod
    def _validate_backup_file(bak: Path) -> None:
        """Verify that ``bak`` is a readable, intact SQLite DB with compatible schema."""
        uri = f"{bak.resolve().as_uri()}?mode=ro"
        check_conn: Optional[sqlite3.Connection] = None
        try:
            check_conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            qc_rows = check_conn.execute("PRAGMA quick_check;").fetchall()
            if not qc_rows or any(str(r[0]).lower() != "ok" for r in qc_rows):
                detail = "; ".join(str(r[0]) for r in qc_rows) or "完整性校验未通过"
                raise DatabaseError(f"备份文件完整性校验失败：{detail}")

            tables = {
                str(r[0])
                for r in check_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            required_tables = {"records", "import_batches"}
            missing = required_tables - tables
            if missing:
                raise DatabaseError(
                    f"备份文件缺少必要数据表（{', '.join(sorted(missing))}），无法恢复。"
                )

            if "schema_meta" in tables:
                ver_row = check_conn.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                if ver_row is not None:
                    raw_ver = str(ver_row[0]).strip()
                    try:
                        bak_ver = int(raw_ver)
                    except ValueError as exc:
                        raise DatabaseError(
                            f"备份文件架构版本号异常（{raw_ver!r}），无法恢复。"
                        ) from exc
                    if bak_ver > int(SCHEMA_VERSION):
                        raise DatabaseError(
                            f"备份文件架构版本（v{bak_ver}）高于当前程序支持版本（v{SCHEMA_VERSION}），无法恢复。"
                        )
        except DatabaseError:
            raise
        except sqlite3.Error as exc:
            raise DatabaseError(f"备份文件无效或已损坏（{bak.name}）：{exc}") from exc
        finally:
            if check_conn is not None:
                try:
                    check_conn.close()
                except sqlite3.Error:
                    pass

    def restore_backup(self, backup_path: str | Path) -> Path:
        """Validate and atomically restore the current database from a ``.bak`` file."""
        with self._lock:
            if self.is_memory:
                raise DatabaseError("内存数据库不支持备份恢复。")
            bak = Path(backup_path).expanduser().resolve()
            if not bak.is_file():
                raise DatabaseError(f"备份文件不存在：{bak}")

            # 1. Validate backup integrity & schema BEFORE mutating live DB.
            self._validate_backup_file(bak)

            conn = self._require_conn()
            target = Path(self._db_path)

            # 2. Take a safety snapshot before restoring.
            pre_restore: Optional[Path] = None
            if target.exists():
                pre_restore = self.create_backup(reason="pre_restore", protect_paths=[bak])

            # 3. Restore via SQLite Online Backup API (WAL-safe, atomic page copy).
            bak_conn: Optional[sqlite3.Connection] = None
            try:
                uri = f"{bak.as_uri()}?mode=ro"
                bak_conn = sqlite3.connect(
                    uri,
                    uri=True,
                    timeout=_SQLITE_BUSY_TIMEOUT_MS / 1000.0,
                )
                bak_conn.backup(conn)
                bak_conn.close()
                bak_conn = None

                # Ensure indexes/pragmas are up to date and checkpoint WAL cleanly.
                conn.execute("PRAGMA foreign_keys = ON;")
                self._init_schema()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except (sqlite3.Error, DatabaseError) as exc:
                if bak_conn is not None:
                    try:
                        bak_conn.close()
                    except sqlite3.Error:
                        pass
                # Best-effort rollback to pre_restore snapshot so live DB stays intact.
                if pre_restore is not None and pre_restore.is_file():
                    rb_conn: Optional[sqlite3.Connection] = None
                    try:
                        rb_conn = sqlite3.connect(
                            f"{pre_restore.resolve().as_uri()}?mode=ro",
                            uri=True,
                            timeout=_SQLITE_BUSY_TIMEOUT_MS / 1000.0,
                        )
                        rb_conn.backup(conn)
                    except sqlite3.Error:
                        pass
                    finally:
                        if rb_conn is not None:
                            try:
                                rb_conn.close()
                            except sqlite3.Error:
                                pass
                if isinstance(exc, DatabaseError):
                    raise
                raise DatabaseError(f"恢复备份失败：{exc}") from exc
            return target

    @staticmethod
    def _remove_db_files(target: Path) -> None:
        for suffix in ("", "-wal", "-shm"):
            p = Path(f"{target}{suffix}")
            if p.exists():
                try:
                    p.unlink()
                except OSError as exc:
                    raise DatabaseError(
                        f"无法删除数据库文件「{p.name}」（可能正被其他进程占用）：{exc}"
                    ) from exc
