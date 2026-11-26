"""SQLite WAL-mode repository with multi-library support for 数据查重程序.

Provides indexed storage for parsed catalog/code records, batch import history,
fast search/filtering/sorting/statistics, duplicate grouping, and rolling
``.bak`` backups prior to destructive operations.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .database_backup import (
    SCHEMA_VERSION,
    DatabaseBackupMixin,
    DatabaseError,
    _SCHEMA_DDL,
    _SQLITE_BUSY_TIMEOUT_MS,
)
from .models import DataRecord, DuplicateGroup, ImportBatch, LibraryStats

_SQLITE_PARAM_CHUNK = 800

_VALID_SORT_COLUMNS: Dict[str, str] = {
    "id": "r.id",
    "canonical_code": "r.canonical_code",
    "prefix": "r.prefix",
    "number": "r.number",
    "raw_code": "r.raw_code",
    "file_name": "r.file_name",
    "source_name": "r.source_name",
    "source_type": "r.source_type",
    "file_size": "r.file_size",
    "file_ext": "r.file_ext",
    "part": "r.part",
    "tags": "r.tags",
    "confidence": "r.confidence",
    "created_at": "r.created_at",
    "updated_at": "r.updated_at",
    "duplicate_count": "dup_cnt",
}


def _now_iso() -> str:
    """Return current local timestamp formatted as YYYY-MM-DD HH:MM:SS."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _encode_tags(tags: Sequence[str]) -> str:
    cleaned: List[str] = []
    seen = set()
    for tag in tags:
        t = str(tag).strip()
        if t and t not in seen:
            seen.add(t)
            cleaned.append(t)
    return ",".join(cleaned)


def _decode_tags(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def normalize_part_key(part: str) -> str:
    """Normalize a part/episode marker for comparison (e.g. 'cd1' -> 'CD1')."""
    return str(part or "").strip().upper()


def _escape_like(value: str) -> str:
    """Escape SQL LIKE wildcard characters (``\\``, ``%``, ``_``) for ``ESCAPE '\\'``."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def is_group_possible_multipart(records: Sequence[DataRecord]) -> bool:
    """Return True if a group of records sharing a code looks like multi-part files.

    A group is considered possible multi-part when at least one record has a
    non-empty ``part`` marker and the records do not all share the exact same
    part marker (for example ``CD1`` + ``CD2``, or ``""`` + ``CD2``).
    """
    if len(records) < 2:
        return False
    part_keys = {normalize_part_key(r.part) for r in records}
    non_empty_parts = {p for p in part_keys if p}
    if not non_empty_parts:
        return False
    return len(part_keys) >= 2


class DatabaseRepository(DatabaseBackupMixin):
    """Thread-safe SQLite WAL repository supporting multiple ``.db`` libraries."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        auto_backup: bool = True,
        backup_keep_count: int = 5,
    ) -> None:
        self._lock = threading.RLock()
        self._auto_backup = bool(auto_backup)
        self._backup_keep_count = max(1, int(backup_keep_count))
        self._db_path: str = ""
        self._conn: Optional[sqlite3.Connection] = None
        self.open(db_path)

    @property
    def db_path(self) -> str:
        """Return the active database file path (or ``':memory:'``)."""
        return self._db_path

    @property
    def auto_backup(self) -> bool:
        return self._auto_backup

    @auto_backup.setter
    def auto_backup(self, enabled: bool) -> None:
        self._auto_backup = bool(enabled)

    @property
    def backup_keep_count(self) -> int:
        return self._backup_keep_count

    @backup_keep_count.setter
    def backup_keep_count(self, count: int) -> None:
        self._backup_keep_count = max(1, int(count))

    def __enter__(self) -> "DatabaseRepository":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def open(self, db_path: str | Path) -> Path | str:
        """Open (or create) a SQLite database file and ensure schema/indexes exist."""
        with self._lock:
            self.close()
            raw_str = str(db_path).strip()
            if raw_str == ":memory:":
                self._db_path = ":memory:"
            else:
                target = Path(raw_str).expanduser().resolve()
                target.parent.mkdir(parents=True, exist_ok=True)
                self._db_path = str(target)

            try:
                conn = sqlite3.connect(
                    self._db_path,
                    timeout=_SQLITE_BUSY_TIMEOUT_MS / 1000.0,
                    check_same_thread=False,
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON;")
                conn.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS};")
                if not self.is_memory:
                    conn.execute("PRAGMA journal_mode = WAL;")
                    conn.execute("PRAGMA synchronous = NORMAL;")
                self._conn = conn
                self._init_schema()
            except DatabaseError:
                self.close()
                raise
            except sqlite3.Error as exc:
                self.close()
                raise DatabaseError(f"无法打开数据库文件「{self._db_path}」：{exc}") from exc

            return self._db_path if self.is_memory else Path(self._db_path)

    def switch_database(self, new_db_path: str | Path) -> Path | str:
        """Switch to another SQLite database library cleanly."""
        return self.open(new_db_path)

    def create_database(self, new_db_path: str | Path, *, overwrite: bool = False) -> Path:
        """Create a new database file and switch to it."""
        target = Path(new_db_path).expanduser().resolve()
        if target.exists() and not overwrite:
            raise DatabaseError(f"数据库文件已存在：{target}")
        with self._lock:
            if self._db_path and not self.is_memory and Path(self._db_path) == target:
                self.close()
            if target.exists() and overwrite:
                self._remove_db_files(target)
            result = self.open(target)
            if not isinstance(result, Path):
                raise DatabaseError(f"创建数据库失败：{target}")
            return result

    def close(self) -> None:
        """Close the active SQLite connection cleanly."""
        with self._lock:
            if self._conn is not None:
                try:
                    if not self.is_memory:
                        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                except sqlite3.Error:
                    pass
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise DatabaseError("数据库连接已关闭，请重新打开数据库。")
        return self._conn

    def _init_schema(self) -> None:
        conn = self._require_conn()
        # Guard against opening a database created by a newer schema version.
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
        ).fetchone()
        if has_meta is not None:
            ver_row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if ver_row is not None:
                raw_ver = str(ver_row[0]).strip()
                try:
                    db_ver = int(raw_ver)
                except ValueError as exc:
                    raise DatabaseError(
                        f"数据库架构版本号异常（{raw_ver!r}），拒绝打开以防数据损坏。"
                    ) from exc
                if db_ver > int(SCHEMA_VERSION):
                    raise DatabaseError(
                        f"数据库架构版本（v{db_ver}）高于当前程序支持版本（v{SCHEMA_VERSION}），"
                        "请升级程序后再打开该数据库。"
                    )

        conn.executescript(_SCHEMA_DDL)
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                ("schema_version", SCHEMA_VERSION),
            )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DataRecord:
        return DataRecord(
            id=int(row["id"]),
            canonical_code=str(row["canonical_code"] or ""),
            prefix=str(row["prefix"] or ""),
            number=str(row["number"] or ""),
            raw_code=str(row["raw_code"] or ""),
            raw_text=str(row["raw_text"] or ""),
            file_name=str(row["file_name"] or ""),
            full_path=str(row["full_path"] or ""),
            source_name=str(row["source_name"] or ""),
            source_type=str(row["source_type"] or "txt"),
            file_size=int(row["file_size"] or 0),
            file_ext=str(row["file_ext"] or ""),
            tags=_decode_tags(row["tags"]),
            part=str(row["part"] or ""),
            matched_rule=str(row["matched_rule"] or ""),
            confidence=float(row["confidence"] if row["confidence"] is not None else 1.0),
            batch_id=int(row["batch_id"]) if row["batch_id"] is not None else None,
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
            note=str(row["note"] or ""),
        )

    @staticmethod
    def _row_to_batch(row: sqlite3.Row) -> ImportBatch:
        return ImportBatch(
            id=int(row["id"]),
            source_name=str(row["source_name"] or ""),
            source_type=str(row["source_type"] or "txt"),
            total_count=int(row["total_count"] or 0),
            added_count=int(row["added_count"] or 0),
            duplicate_count=int(row["duplicate_count"] or 0),
            unrecognized_count=int(row["unrecognized_count"] or 0),
            created_at=str(row["created_at"] or ""),
        )

    # ------------------------------------------------------------------
    # Import batches
    # ------------------------------------------------------------------

    def create_batch(self, batch: ImportBatch) -> ImportBatch:
        """Insert an import batch history entry and return it with ``id``."""
        with self._lock:
            conn = self._require_conn()
            created_at = batch.created_at or _now_iso()
            with conn:
                cur = conn.execute(
                    """
                    INSERT INTO import_batches (
                        source_name, source_type, total_count,
                        added_count, duplicate_count, unrecognized_count, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch.source_name,
                        batch.source_type,
                        int(batch.total_count),
                        int(batch.added_count),
                        int(batch.duplicate_count),
                        int(batch.unrecognized_count),
                        created_at,
                    ),
                )
            batch.id = int(cur.lastrowid)
            batch.created_at = created_at
            return batch

    def get_batch(self, batch_id: int) -> Optional[ImportBatch]:
        with self._lock:
            conn = self._require_conn()
            row = conn.execute(
                "SELECT * FROM import_batches WHERE id = ?",
                (int(batch_id),),
            ).fetchone()
            return self._row_to_batch(row) if row else None

    def get_batches(self, limit: int = 100) -> List[ImportBatch]:
        """Return recent import batches, newest first."""
        with self._lock:
            conn = self._require_conn()
            rows = conn.execute(
                "SELECT * FROM import_batches ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            return [self._row_to_batch(r) for r in rows]

    # ------------------------------------------------------------------
    # Record CRUD & batch insert
    # ------------------------------------------------------------------

    def add_record(self, record: DataRecord) -> DataRecord:
        """Insert a single record and populate its ``id`` and timestamps."""
        inserted, _ = self.add_records([record])
        return inserted[0]

    def add_records(
        self,
        records: Sequence[DataRecord],
        batch: Optional[ImportBatch] = None,
    ) -> Tuple[List[DataRecord], Optional[ImportBatch]]:
        """Insert multiple records in a single SQLite transaction.

        If ``batch`` is provided, inserts the batch metadata inside the same
        transaction and links every inserted record to ``batch.id``.
        """
        with self._lock:
            conn = self._require_conn()
            now = _now_iso()
            saved_records: List[DataRecord] = []

            with conn:
                batch_id: Optional[int] = None
                if batch is not None:
                    batch_created = batch.created_at or now
                    cur_b = conn.execute(
                        """
                        INSERT INTO import_batches (
                            source_name, source_type, total_count,
                            added_count, duplicate_count, unrecognized_count, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            batch.source_name,
                            batch.source_type,
                            int(batch.total_count),
                            int(batch.added_count or len(records)),
                            int(batch.duplicate_count),
                            int(batch.unrecognized_count),
                            batch_created,
                        ),
                    )
                    batch_id = int(cur_b.lastrowid)
                    batch.id = batch_id
                    batch.created_at = batch_created

                insert_sql = """
                    INSERT INTO records (
                        canonical_code, prefix, number, raw_code, raw_text,
                        file_name, full_path, source_name, source_type,
                        file_size, file_ext, tags, part, matched_rule,
                        confidence, batch_id, created_at, updated_at, note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                cur = conn.cursor()
                for rec in records:
                    created_at = rec.created_at or now
                    updated_at = rec.updated_at or created_at
                    rec_batch_id = batch_id if batch_id is not None else rec.batch_id
                    code = rec.canonical_code.strip().upper()
                    prefix = rec.prefix.strip().upper()
                    cur.execute(
                        insert_sql,
                        (
                            code,
                            prefix,
                            rec.number.strip(),
                            rec.raw_code.strip(),
                            rec.raw_text,
                            rec.file_name or rec.raw_text,
                            rec.full_path,
                            rec.source_name,
                            rec.source_type or "txt",
                            int(rec.file_size or 0),
                            rec.file_ext.strip().lower(),
                            _encode_tags(rec.tags),
                            rec.part.strip().upper(),
                            rec.matched_rule,
                            float(rec.confidence),
                            rec_batch_id,
                            created_at,
                            updated_at,
                            rec.note,
                        ),
                    )
                    rec.id = int(cur.lastrowid)
                    rec.canonical_code = code
                    rec.prefix = prefix
                    rec.part = rec.part.strip().upper()
                    rec.batch_id = rec_batch_id
                    rec.created_at = created_at
                    rec.updated_at = updated_at
                    saved_records.append(rec)

            return saved_records, batch

    def get_record(self, record_id: int) -> Optional[DataRecord]:
        with self._lock:
            conn = self._require_conn()
            row = conn.execute(
                "SELECT * FROM records WHERE id = ?",
                (int(record_id),),
            ).fetchone()
            return self._row_to_record(row) if row else None

    def get_records_by_ids(self, record_ids: Sequence[int]) -> List[DataRecord]:
        unique_ids = [int(rid) for rid in dict.fromkeys(record_ids)]
        if not unique_ids:
            return []
        with self._lock:
            conn = self._require_conn()
            found: Dict[int, DataRecord] = {}
            for i in range(0, len(unique_ids), _SQLITE_PARAM_CHUNK):
                chunk = unique_ids[i : i + _SQLITE_PARAM_CHUNK]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT * FROM records WHERE id IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    rec = self._row_to_record(row)
                    if rec.id is not None:
                        found[rec.id] = rec
            return [found[rid] for rid in unique_ids if rid in found]

    def find_by_code(self, canonical_code: str) -> List[DataRecord]:
        code = canonical_code.strip().upper()
        if not code:
            return []
        with self._lock:
            conn = self._require_conn()
            rows = conn.execute(
                "SELECT * FROM records WHERE canonical_code = ? ORDER BY id ASC",
                (code,),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]

    def find_by_codes(self, codes: Iterable[str]) -> Dict[str, List[DataRecord]]:
        """Batch lookup existing records for a collection of canonical codes."""
        unique_codes = list(
            dict.fromkeys(c.strip().upper() for c in codes if c and c.strip())
        )
        if not unique_codes:
            return {}
        with self._lock:
            conn = self._require_conn()
            mapping: Dict[str, List[DataRecord]] = {}
            for i in range(0, len(unique_codes), _SQLITE_PARAM_CHUNK):
                chunk = unique_codes[i : i + _SQLITE_PARAM_CHUNK]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT * FROM records
                    WHERE canonical_code IN ({placeholders})
                    ORDER BY id ASC
                    """,
                    chunk,
                ).fetchall()
                for row in rows:
                    rec = self._row_to_record(row)
                    mapping.setdefault(rec.canonical_code, []).append(rec)
            return mapping

    def get_all_codes(self) -> set[str]:
        with self._lock:
            conn = self._require_conn()
            rows = conn.execute(
                "SELECT DISTINCT canonical_code FROM records WHERE canonical_code != ''"
            ).fetchall()
            return {str(r[0]) for r in rows}

    def update_record(self, record: DataRecord) -> bool:
        """Update mutable fields of an existing record by ``record.id``."""
        if record.id is None:
            raise ValueError("更新记录需要有效的 record.id")
        with self._lock:
            conn = self._require_conn()
            now = _now_iso()
            code = record.canonical_code.strip().upper()
            prefix = record.prefix.strip().upper()
            part = record.part.strip().upper()
            with conn:
                cur = conn.execute(
                    """
                    UPDATE records
                    SET canonical_code = ?,
                        prefix = ?,
                        number = ?,
                        raw_code = ?,
                        raw_text = ?,
                        file_name = ?,
                        full_path = ?,
                        source_name = ?,
                        source_type = ?,
                        file_size = ?,
                        file_ext = ?,
                        tags = ?,
                        part = ?,
                        matched_rule = ?,
                        confidence = ?,
                        updated_at = ?,
                        note = ?
                    WHERE id = ?
                    """,
                    (
                        code,
                        prefix,
                        record.number.strip(),
                        record.raw_code.strip(),
                        record.raw_text,
                        record.file_name,
                        record.full_path,
                        record.source_name,
                        record.source_type,
                        int(record.file_size or 0),
                        record.file_ext.strip().lower(),
                        _encode_tags(record.tags),
                        part,
                        record.matched_rule,
                        float(record.confidence),
                        now,
                        record.note,
                        int(record.id),
                    ),
                )
            if cur.rowcount > 0:
                record.canonical_code = code
                record.prefix = prefix
                record.part = part
                record.updated_at = now
                return True
            return False

    def delete_record(
        self,
        record_id: int,
        *,
        create_backup: Optional[bool] = None,
    ) -> bool:
        return self.delete_records([record_id], create_backup=create_backup) > 0

    def delete_records(
        self,
        record_ids: Sequence[int],
        *,
        create_backup: Optional[bool] = None,
    ) -> int:
        """Delete records by ID in a transaction, backing up first if configured."""
        unique_ids = [int(rid) for rid in dict.fromkeys(record_ids)]
        if not unique_ids:
            return 0
        with self._lock:
            conn = self._require_conn()
            has_match = False
            for i in range(0, len(unique_ids), _SQLITE_PARAM_CHUNK):
                chunk = unique_ids[i : i + _SQLITE_PARAM_CHUNK]
                placeholders = ",".join("?" for _ in chunk)
                if conn.execute(
                    f"SELECT 1 FROM records WHERE id IN ({placeholders}) LIMIT 1",
                    chunk,
                ).fetchone():
                    has_match = True
                    break
            if not has_match:
                return 0

            should_backup = self._auto_backup if create_backup is None else bool(create_backup)
            if should_backup and not self.is_memory:
                self.create_backup(reason="pre_delete")

            deleted = 0
            with conn:
                for i in range(0, len(unique_ids), _SQLITE_PARAM_CHUNK):
                    chunk = unique_ids[i : i + _SQLITE_PARAM_CHUNK]
                    placeholders = ",".join("?" for _ in chunk)
                    cur = conn.execute(
                        f"DELETE FROM records WHERE id IN ({placeholders})",
                        chunk,
                    )
                    deleted += int(cur.rowcount)
            return deleted

    def delete_by_batch(
        self,
        batch_id: int,
        *,
        create_backup: Optional[bool] = None,
    ) -> int:
        """Delete all records belonging to a specific import batch."""
        with self._lock:
            conn = self._require_conn()
            bid = int(batch_id)
            has_rows = (
                conn.execute(
                    "SELECT 1 FROM records WHERE batch_id = ? LIMIT 1",
                    (bid,),
                ).fetchone()
                is not None
                or conn.execute(
                    "SELECT 1 FROM import_batches WHERE id = ? LIMIT 1",
                    (bid,),
                ).fetchone()
                is not None
            )
            if not has_rows:
                return 0

            should_backup = self._auto_backup if create_backup is None else bool(create_backup)
            if should_backup and not self.is_memory:
                self.create_backup(reason="pre_delete_batch")

            with conn:
                cur = conn.execute(
                    "DELETE FROM records WHERE batch_id = ?",
                    (bid,),
                )
                conn.execute(
                    "DELETE FROM import_batches WHERE id = ?",
                    (bid,),
                )
            return int(cur.rowcount)

    def clear_all_records(self, *, create_backup: Optional[bool] = None) -> int:
        """Remove all records and import batches from the active database."""
        with self._lock:
            conn = self._require_conn()
            has_any = (
                conn.execute("SELECT 1 FROM records LIMIT 1").fetchone() is not None
                or conn.execute("SELECT 1 FROM import_batches LIMIT 1").fetchone() is not None
            )
            if not has_any:
                return 0

            should_backup = self._auto_backup if create_backup is None else bool(create_backup)
            if should_backup and not self.is_memory:
                self.create_backup(reason="pre_clear")

            with conn:
                cur = conn.execute("DELETE FROM records")
                conn.execute("DELETE FROM import_batches")
            return int(cur.rowcount)

    # ------------------------------------------------------------------
    # Search, filtering, sorting, duplicates & statistics
    # ------------------------------------------------------------------

    def _build_filter_clause(
        self,
        *,
        search: str = "",
        filter_mode: str = "all",
        prefix: str = "",
        source_name: str = "",
        tag: str = "",
        batch_id: Optional[int] = None,
    ) -> Tuple[str, List[object]]:
        clauses: List[str] = []
        params: List[object] = []

        if search and search.strip():
            like_val = f"%{_escape_like(search.strip())}%"
            clauses.append(
                "("
                "r.canonical_code LIKE ? ESCAPE '\\' OR "
                "r.raw_code LIKE ? ESCAPE '\\' OR "
                "r.file_name LIKE ? ESCAPE '\\' OR "
                "r.raw_text LIKE ? ESCAPE '\\' OR "
                "r.full_path LIKE ? ESCAPE '\\' OR "
                "r.source_name LIKE ? ESCAPE '\\' OR "
                "r.tags LIKE ? ESCAPE '\\' OR "
                "r.note LIKE ? ESCAPE '\\'"
                ")"
            )
            params.extend([like_val] * 8)

        if prefix and prefix.strip():
            clauses.append("r.prefix = ?")
            params.append(prefix.strip().upper())

        if source_name and source_name.strip():
            clauses.append("r.source_name = ?")
            params.append(source_name.strip())

        if tag and tag.strip():
            clauses.append("(',' || r.tags || ',') LIKE ? ESCAPE '\\'")
            params.append(f"%,{_escape_like(tag.strip())},%")

        if batch_id is not None:
            clauses.append("r.batch_id = ?")
            params.append(int(batch_id))

        mode = (filter_mode or "all").strip().lower()
        if mode == "duplicates":
            clauses.append(
                "r.canonical_code IN ("
                "SELECT canonical_code FROM records "
                "WHERE canonical_code != '' "
                "GROUP BY canonical_code HAVING COUNT(*) > 1"
                ")"
            )
        elif mode == "unique":
            clauses.append(
                "r.canonical_code IN ("
                "SELECT canonical_code FROM records "
                "WHERE canonical_code != '' "
                "GROUP BY canonical_code HAVING COUNT(*) = 1"
                ")"
            )
        elif mode == "multipart":
            clauses.append(
                "r.canonical_code IN ("
                "SELECT canonical_code FROM records "
                "WHERE canonical_code != '' "
                "GROUP BY canonical_code "
                "HAVING COUNT(*) > 1 "
                "AND MAX(UPPER(TRIM(part))) != '' "
                "AND MIN(UPPER(TRIM(part))) != MAX(UPPER(TRIM(part)))"
                ")"
            )

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where_sql, params

    def query_records(
        self,
        *,
        search: str = "",
        filter_mode: str = "all",
        prefix: str = "",
        source_name: str = "",
        tag: str = "",
        batch_id: Optional[int] = None,
        sort_by: str = "id",
        sort_desc: bool = True,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[DataRecord]:
        """Search, filter, sort, and paginate records cleanly."""
        with self._lock:
            conn = self._require_conn()
            where_sql, params = self._build_filter_clause(
                search=search,
                filter_mode=filter_mode,
                prefix=prefix,
                source_name=source_name,
                tag=tag,
                batch_id=batch_id,
            )

            sort_col = _VALID_SORT_COLUMNS.get(sort_by, "r.id")
            direction = "DESC" if sort_desc else "ASC"

            if sort_col == "dup_cnt":
                sql = f"""
                    SELECT r.*,
                           CASE WHEN r.canonical_code = '' THEN 0
                                ELSE COALESCE(c.cnt, 1)
                           END AS dup_cnt
                    FROM records r
                    LEFT JOIN (
                        SELECT canonical_code, COUNT(*) AS cnt
                        FROM records
                        WHERE canonical_code != ''
                        GROUP BY canonical_code
                    ) c ON c.canonical_code = r.canonical_code
                    {where_sql}
                    ORDER BY dup_cnt {direction}, r.canonical_code ASC, r.id ASC
                """
            else:
                sql = f"""
                    SELECT r.*
                    FROM records r
                    {where_sql}
                    ORDER BY {sort_col} {direction}, r.id {direction}
                """

            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                params.extend([max(0, int(limit)), max(0, int(offset))])

            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_record(row) for row in rows]

    def count_records(
        self,
        *,
        search: str = "",
        filter_mode: str = "all",
        prefix: str = "",
        source_name: str = "",
        tag: str = "",
        batch_id: Optional[int] = None,
    ) -> int:
        with self._lock:
            conn = self._require_conn()
            where_sql, params = self._build_filter_clause(
                search=search,
                filter_mode=filter_mode,
                prefix=prefix,
                source_name=source_name,
                tag=tag,
                batch_id=batch_id,
            )
            row = conn.execute(
                f"SELECT COUNT(*) FROM records r {where_sql}",
                params,
            ).fetchone()
            return int(row[0]) if row else 0

    def get_code_counts(self) -> Dict[str, int]:
        """Return mapping of ``canonical_code -> occurrence count``."""
        with self._lock:
            conn = self._require_conn()
            rows = conn.execute(
                """
                SELECT canonical_code, COUNT(*) AS cnt
                FROM records
                WHERE canonical_code != ''
                GROUP BY canonical_code
                """
            ).fetchall()
            return {str(r["canonical_code"]): int(r["cnt"]) for r in rows}

    def get_distinct_prefixes(self) -> List[str]:
        with self._lock:
            conn = self._require_conn()
            rows = conn.execute(
                """
                SELECT prefix, COUNT(*) AS cnt
                FROM records
                WHERE prefix != ''
                GROUP BY prefix
                ORDER BY cnt DESC, prefix ASC
                """
            ).fetchall()
            return [str(r["prefix"]) for r in rows]

    def get_distinct_sources(self) -> List[str]:
        with self._lock:
            conn = self._require_conn()
            rows = conn.execute(
                """
                SELECT DISTINCT source_name
                FROM records
                WHERE source_name != ''
                ORDER BY source_name ASC
                """
            ).fetchall()
            return [str(r["source_name"]) for r in rows]

    def get_duplicate_groups(
        self,
        *,
        include_multipart: bool = True,
    ) -> List[DuplicateGroup]:
        """Return all groups of records sharing the same canonical code (count > 1)."""
        with self._lock:
            conn = self._require_conn()
            rows = conn.execute(
                """
                SELECT r.*
                FROM records r
                INNER JOIN (
                    SELECT canonical_code, COUNT(*) AS cnt
                    FROM records
                    WHERE canonical_code != ''
                    GROUP BY canonical_code
                    HAVING cnt > 1
                ) dup ON dup.canonical_code = r.canonical_code
                ORDER BY dup.cnt DESC, r.canonical_code ASC, r.id ASC
                """
            ).fetchall()

            grouped: Dict[str, List[DataRecord]] = {}
            for row in rows:
                rec = self._row_to_record(row)
                grouped.setdefault(rec.canonical_code, []).append(rec)

            groups: List[DuplicateGroup] = []
            for code, recs in grouped.items():
                is_mp = is_group_possible_multipart(recs)
                if not include_multipart and is_mp:
                    continue
                groups.append(
                    DuplicateGroup(
                        canonical_code=code,
                        records=recs,
                        is_possible_multipart=is_mp,
                        count=len(recs),
                    )
                )
            return groups

    def get_stats(self) -> LibraryStats:
        """Compute summary statistics for the active library using SQL aggregates."""
        with self._lock:
            conn = self._require_conn()
            summary = conn.execute(
                """
                SELECT
                    COALESCE(SUM(cnt), 0) AS total_records,
                    COALESCE(SUM(CASE WHEN canonical_code != '' THEN 1 ELSE 0 END), 0) AS unique_codes,
                    COALESCE(SUM(CASE WHEN canonical_code != '' AND cnt > 1 THEN 1 ELSE 0 END), 0) AS dup_groups,
                    COALESCE(SUM(CASE WHEN canonical_code != '' AND cnt > 1 THEN cnt - 1 ELSE 0 END), 0) AS dup_records,
                    COALESCE(SUM(
                        CASE
                            WHEN canonical_code != '' AND cnt > 1
                                 AND max_part != '' AND min_part != max_part
                            THEN 1 ELSE 0
                        END
                    ), 0) AS mp_groups
                FROM (
                    SELECT
                        canonical_code,
                        COUNT(*) AS cnt,
                        MIN(UPPER(TRIM(part))) AS min_part,
                        MAX(UPPER(TRIM(part))) AS max_part
                    FROM records
                    GROUP BY canonical_code
                )
                """
            ).fetchone()

            total_records = int(summary["total_records"]) if summary else 0
            unique_codes = int(summary["unique_codes"]) if summary else 0
            duplicate_groups = int(summary["dup_groups"]) if summary else 0
            duplicate_records = int(summary["dup_records"]) if summary else 0
            possible_multipart_groups = int(summary["mp_groups"]) if summary else 0

            prefix_rows = conn.execute(
                """
                SELECT prefix, COUNT(*) AS cnt
                FROM records
                WHERE prefix != ''
                GROUP BY prefix
                ORDER BY cnt DESC, prefix ASC
                """
            ).fetchall()
            prefix_counts = {str(r["prefix"]): int(r["cnt"]) for r in prefix_rows}

            return LibraryStats(
                total_records=total_records,
                unique_codes=unique_codes,
                duplicate_groups=duplicate_groups,
                duplicate_records=duplicate_records,
                possible_multipart_groups=possible_multipart_groups,
                prefix_counts=prefix_counts,
            )
