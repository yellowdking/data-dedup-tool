"""Virtualized Qt6 table model for DuplicatesView (平铺 & 分组折叠 modes).

Re-exported from ``src.ui.table_models`` for backward-friendly single-module import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QFont

from src.core.models import DataRecord, DuplicateGroup
from src.ui.table_header import ColumnSpec
from src.ui.widgets import format_file_size

try:
    from src.ui.delegates import (
        ROLE_BADGE,
        ROLE_COPY_TEXT,
        ROLE_GROUP_DIVIDER,
        ROLE_GROUP_HEADER,
    )
except ImportError:
    ROLE_BADGE = int(Qt.ItemDataRole.UserRole) + 40
    ROLE_COPY_TEXT = int(Qt.ItemDataRole.UserRole) + 41
    ROLE_GROUP_DIVIDER = int(Qt.ItemDataRole.UserRole) + 42
    ROLE_GROUP_HEADER = int(Qt.ItemDataRole.UserRole) + 43

ROLE_GROUP_COLLAPSED: int = int(Qt.ItemDataRole.UserRole) + 44

DUPLICATE_COLUMN_SPECS: list[ColumnSpec] = [
    # 7 zero-noise default visible columns
    ColumnSpec("check", "选", default_width=40, default_visible=True, min_width=36, sortable=False),
    ColumnSpec("code", "标准编码", default_width=148, default_visible=True, min_width=100, lock_visible=True),
    ColumnSpec("dup_count", "重复数", default_width=76, default_visible=True, min_width=60),
    ColumnSpec("file_name", "原始文件名", default_width=320, default_visible=True, min_width=140, stretch_weight=1.0),
    ColumnSpec("source", "来源", default_width=160, default_visible=True, min_width=90),
    ColumnSpec("file_size", "大小", default_width=96, default_visible=True, min_width=72),
    ColumnSpec("created_at", "入库时间", default_width=148, default_visible=True, min_width=110),
    # 7 hidden advanced columns (1 right-click away on table header)
    ColumnSpec("status", "状态", default_width=86, default_visible=False, min_width=68, category="advanced"),
    ColumnSpec("dup_type", "类型", default_width=94, default_visible=False, min_width=76, category="advanced"),
    ColumnSpec("tags", "版本标签", default_width=115, default_visible=False, min_width=72, category="advanced"),
    ColumnSpec("part", "分集", default_width=72, default_visible=False, min_width=56, category="advanced"),
    ColumnSpec("prefix", "前缀", default_width=76, default_visible=False, min_width=56, category="advanced"),
    ColumnSpec("rule", "匹配规则", default_width=130, default_visible=False, min_width=90, category="advanced"),
    ColumnSpec("record_id", "ID", default_width=68, default_visible=False, min_width=52, category="advanced"),
]


@dataclass(slots=True)
class _DupDisplayRow:
    """Single virtualized display row (record row or collapsible group header row)."""

    is_group: bool
    group: DuplicateGroup
    record: Optional[DataRecord]
    band: int
    is_group_start: bool = False


def build_duplicate_table_model_class(theme_mixin: type, mono_font_factory: Any) -> type:
    """Create ``DuplicateTableModel`` bound to shared ``_ThemedTableModelMixin``."""

    class DuplicateTableModel(theme_mixin, QAbstractTableModel):  # type: ignore[misc,valid-type]
        """Virtualized QAbstractTableModel for DuplicatesView supporting flat & grouped modes."""

        COLUMN_SPECS: list[ColumnSpec] = DUPLICATE_COLUMN_SPECS

        COL_CHECK = 0
        COL_CODE = 1
        COL_DUP_COUNT = 2
        COL_FILE_NAME = 3
        COL_SOURCE = 4
        COL_FILE_SIZE = 5
        COL_CREATED_AT = 6
        COL_STATUS = 7
        COL_DUP_TYPE = 8
        COL_TAGS = 9
        COL_PART = 10
        COL_PREFIX = 11
        COL_RULE = 12
        COL_RECORD_ID = 13

        checked_changed = Signal(int)

        def __init__(
            self,
            parent: Optional[Any] = None,
            theme: str = "light",
            view_mode: str = "flat",
        ) -> None:
            super().__init__(parent)
            self._init_theme_colors(theme)
            self._mono_font = mono_font_factory(10, bold=True)
            self._bold_font = QFont()
            self._bold_font.setBold(True)

            self._groups: list[DuplicateGroup] = []
            self._record_map: dict[int, DataRecord] = {}
            self._record_to_group: dict[int, DuplicateGroup] = {}
            self._group_haystacks: dict[str, str] = {}
            self._checked_ids: set[int] = set()
            self._collapsed_groups: set[str] = set()
            self._view_mode: str = "grouped" if view_mode == "grouped" else "flat"
            self._search_query: str = ""
            self._sort_col: int = -1
            self._sort_order: Qt.SortOrder = Qt.SortOrder.AscendingOrder
            self._rows: list[_DupDisplayRow] = []
            self._visible_group_count: int = 0

        # ------------------------------------------------------------------
        # Data population, view mode, search & collapse controls
        # ------------------------------------------------------------------

        def set_groups(
            self,
            groups: Sequence[DuplicateGroup],
            *,
            preserve_checks: bool = False,
        ) -> None:
            """Bind duplicate groups and rebuild display rows."""
            self._groups = list(groups)
            self._record_map.clear()
            self._record_to_group.clear()
            self._group_haystacks.clear()

            valid_codes: set[str] = set()
            for grp in self._groups:
                valid_codes.add(grp.canonical_code)
                tokens: list[str] = [grp.canonical_code or ""]
                for rec in grp.records:
                    if rec.id is not None:
                        self._record_map[rec.id] = rec
                        self._record_to_group[rec.id] = grp
                    tokens.extend(
                        (
                            rec.file_name or "",
                            rec.raw_text or "",
                            rec.source_name or "",
                            rec.source_type or "",
                            rec.prefix or "",
                            rec.part or "",
                            rec.matched_rule or "",
                            " ".join(rec.tags),
                        )
                    )
                self._group_haystacks[grp.canonical_code] = " ".join(tokens).lower()

            if preserve_checks:
                self._checked_ids &= set(self._record_map.keys())
            else:
                self._checked_ids.clear()

            self._collapsed_groups &= valid_codes
            self._rebuild_rows()
            self.checked_changed.emit(len(self._checked_ids))

        def view_mode(self) -> str:
            return self._view_mode

        def set_view_mode(self, mode: str) -> None:
            """Switch between 'flat' and 'grouped' modes while preserving checks/filter."""
            normalized = "grouped" if mode == "grouped" else "flat"
            if normalized == self._view_mode:
                return
            self._view_mode = normalized
            self._rebuild_rows()

        def search_query(self) -> str:
            return self._search_query

        def set_search_query(self, query: str) -> None:
            """Filter visible groups with sibling-preserving multi-term search."""
            cleaned = (query or "").strip().lower()
            if cleaned == self._search_query:
                return
            self._search_query = cleaned
            self._rebuild_rows()

        def is_group_collapsed(self, canonical_code: str) -> bool:
            return canonical_code in self._collapsed_groups

        def is_row_collapsed(self, row: int) -> bool:
            if 0 <= row < len(self._rows):
                item = self._rows[row]
                if item.is_group:
                    return item.group.canonical_code in self._collapsed_groups
            return False

        def toggle_group_collapsed(self, canonical_code: str) -> bool:
            if canonical_code in self._collapsed_groups:
                self._collapsed_groups.remove(canonical_code)
                collapsed = False
            else:
                self._collapsed_groups.add(canonical_code)
                collapsed = True
            if self._view_mode == "grouped":
                self._rebuild_rows()
            return collapsed

        def toggle_row_group_collapsed(self, row: int) -> bool:
            if 0 <= row < len(self._rows) and self._rows[row].is_group:
                return self.toggle_group_collapsed(self._rows[row].group.canonical_code)
            return False

        def expand_all_groups(self) -> None:
            if not self._collapsed_groups:
                return
            self._collapsed_groups.clear()
            if self._view_mode == "grouped":
                self._rebuild_rows()

        def collapse_all_groups(self) -> None:
            self._collapsed_groups = {g.canonical_code for g in self._groups}
            if self._view_mode == "grouped":
                self._rebuild_rows()

        # ------------------------------------------------------------------
        # Persistent checkmarks & cleanup helpers
        # ------------------------------------------------------------------

        def checked_ids(self) -> list[int]:
            return [rid for rid in self._record_map if rid in self._checked_ids]

        def checked_id_set(self) -> set[int]:
            return set(self._checked_ids)

        def checked_count(self) -> int:
            return len(self._checked_ids)

        def is_record_checked(self, record_id: int) -> bool:
            return record_id in self._checked_ids

        def _emit_check_refresh(self) -> None:
            if self._rows:
                self.dataChanged.emit(
                    self.index(0, 0),
                    self.index(len(self._rows) - 1, len(self.COLUMN_SPECS) - 1),
                    [
                        Qt.ItemDataRole.CheckStateRole,
                        Qt.ItemDataRole.DisplayRole,
                        Qt.ItemDataRole.BackgroundRole,
                        Qt.ItemDataRole.ForegroundRole,
                        ROLE_BADGE,
                    ],
                )
            self.checked_changed.emit(len(self._checked_ids))

        def set_checked_ids(self, record_ids: Iterable[int]) -> None:
            self._checked_ids = {int(rid) for rid in record_ids if int(rid) in self._record_map}
            self._emit_check_refresh()

        def clear_checks(self) -> None:
            if not self._checked_ids:
                return
            self._checked_ids.clear()
            self._emit_check_refresh()

        def set_record_checked(self, record_id: int, checked: bool) -> bool:
            if record_id not in self._record_map:
                return False
            if checked:
                self._checked_ids.add(record_id)
            else:
                self._checked_ids.discard(record_id)
            self._emit_check_refresh()
            return True

        def toggle_rows_checked(self, rows: Sequence[int]) -> int:
            """Toggle delete checkmarks for all selected record rows (Space shortcut)."""
            target_ids: list[int] = []
            seen: set[int] = set()
            for row in rows:
                if not (0 <= row < len(self._rows)):
                    continue
                item = self._rows[row]
                if not item.is_group and item.record is not None and item.record.id is not None:
                    rid = item.record.id
                    if rid not in seen:
                        seen.add(rid)
                        target_ids.append(rid)
            if not target_ids:
                return len(self._checked_ids)

            if any(rid not in self._checked_ids for rid in target_ids):
                self._checked_ids.update(target_ids)
            else:
                self._checked_ids.difference_update(target_ids)
            self._emit_check_refresh()
            return len(self._checked_ids)

        def check_all_except_one(self, record_id: int) -> int:
            """Keep ``record_id`` unchecked and check every sibling copy in its group."""
            grp = self._record_to_group.get(record_id)
            if grp is None:
                return 0
            self._checked_ids.discard(record_id)
            checked_siblings = 0
            for rec in grp.records:
                if rec.id is not None and rec.id != record_id:
                    self._checked_ids.add(rec.id)
                    checked_siblings += 1
            self._emit_check_refresh()
            return checked_siblings

        def fully_checked_groups(self) -> list[str]:
            """Return canonical codes where EVERY record in the group is checked."""
            fully_checked: list[str] = []
            for grp in self._groups:
                valid_ids = [r.id for r in grp.records if r.id is not None]
                if valid_ids and all(rid in self._checked_ids for rid in valid_ids):
                    fully_checked.append(grp.canonical_code)
            return fully_checked

        # ------------------------------------------------------------------
        # Row & metadata inspection
        # ------------------------------------------------------------------

        def groups(self) -> list[DuplicateGroup]:
            return list(self._groups)

        def total_group_count(self) -> int:
            return len(self._groups)

        def visible_group_count(self) -> int:
            return self._visible_group_count

        def total_record_count(self) -> int:
            return len(self._record_map)

        def is_group_row(self, row: int) -> bool:
            return 0 <= row < len(self._rows) and self._rows[row].is_group

        def group_at(self, row: int) -> Optional[DuplicateGroup]:
            return self._rows[row].group if 0 <= row < len(self._rows) else None

        def record_at(self, row: int) -> Optional[DataRecord]:
            return self._rows[row].record if 0 <= row < len(self._rows) else None

        def record_by_id(self, record_id: int) -> Optional[DataRecord]:
            return self._record_map.get(record_id)

        def visible_codes(self) -> list[str]:
            return list(
                dict.fromkeys(r.group.canonical_code for r in self._rows if r.group.canonical_code)
            )

        # ------------------------------------------------------------------
        # Internal row building & sorting
        # ------------------------------------------------------------------

        def _filtered_groups(self) -> list[DuplicateGroup]:
            if not self._search_query:
                return list(self._groups)
            terms = [t for t in self._search_query.split() if t]
            if not terms:
                return list(self._groups)
            return [
                grp
                for grp in self._groups
                if all(t in self._group_haystacks.get(grp.canonical_code, "") for t in terms)
            ]

        def _record_sort_key(self, pair: tuple[DuplicateGroup, DataRecord], col: int) -> Any:
            grp, rec = pair
            code_l = (rec.canonical_code or "").lower()
            rid = rec.id or 0
            keys: dict[int, Any] = {
                self.COL_CODE: (code_l, rid),
                self.COL_DUP_COUNT: (grp.count, code_l, rid),
                self.COL_FILE_NAME: ((rec.file_name or rec.raw_text or "").lower(), code_l, rid),
                self.COL_SOURCE: ((rec.source_name or rec.source_type or "").lower(), code_l, rid),
                self.COL_FILE_SIZE: (int(rec.file_size or 0), code_l, rid),
                self.COL_CREATED_AT: (rec.created_at or "", code_l, rid),
                self.COL_STATUS: (1 if rec.id in self._checked_ids else 0, code_l, rid),
                self.COL_DUP_TYPE: (1 if grp.is_possible_multipart else 0, code_l, rid),
                self.COL_TAGS: (rec.tag_score, rec.tags_str.lower(), rid),
                self.COL_PART: ((rec.part or "").lower(), code_l, rid),
                self.COL_PREFIX: ((rec.prefix or "").lower(), code_l, rid),
                self.COL_RULE: ((rec.matched_rule or "").lower(), code_l, rid),
                self.COL_RECORD_ID: (rid,),
            }
            return keys.get(col, (code_l, rid))

        def _group_sort_key(self, grp: DuplicateGroup, col: int) -> Any:
            code_l = (grp.canonical_code or "").lower()
            recs = grp.records
            if col == self.COL_DUP_COUNT:
                return (grp.count, code_l)
            if col == self.COL_FILE_NAME:
                return ((recs[0].file_name or recs[0].raw_text or "").lower() if recs else "", code_l)
            if col == self.COL_SOURCE:
                return ((recs[0].source_name or recs[0].source_type or "").lower() if recs else "", code_l)
            if col == self.COL_FILE_SIZE:
                return (sum(int(r.file_size or 0) for r in recs), code_l)
            if col == self.COL_CREATED_AT:
                return (min((r.created_at or "") for r in recs) if recs else "", code_l)
            if col == self.COL_STATUS:
                return (sum(1 for r in recs if r.id in self._checked_ids), code_l)
            if col == self.COL_DUP_TYPE:
                return (1 if grp.is_possible_multipart else 0, code_l)
            if col == self.COL_TAGS:
                return (max((r.tag_score for r in recs), default=0), code_l)
            if col == self.COL_PART:
                return (min(((r.part or "").lower() for r in recs), default=""), code_l)
            if col == self.COL_PREFIX:
                return ((recs[0].prefix or "").lower() if recs else "", code_l)
            if col == self.COL_RULE:
                return ((recs[0].matched_rule or "").lower() if recs else "", code_l)
            if col == self.COL_RECORD_ID:
                return (min((r.id or 0 for r in recs), default=0),)
            return (code_l,)

        def _rebuild_rows(self) -> None:
            groups = self._filtered_groups()
            self._visible_group_count = len(groups)
            reverse = self._sort_order == Qt.SortOrder.DescendingOrder
            new_rows: list[_DupDisplayRow] = []

            if self._view_mode == "grouped":
                if self._sort_col > 0:
                    groups.sort(
                        key=lambda g: self._group_sort_key(g, self._sort_col),
                        reverse=reverse,
                    )
                for g_idx, grp in enumerate(groups):
                    new_rows.append(
                        _DupDisplayRow(True, grp, None, g_idx, is_group_start=(g_idx > 0))
                    )
                    if grp.canonical_code not in self._collapsed_groups:
                        recs = [r for r in grp.records if r.id is not None]
                        if self._sort_col > 0:
                            recs.sort(
                                key=lambda r: self._record_sort_key((grp, r), self._sort_col),
                                reverse=reverse,
                            )
                        for rec in recs:
                            new_rows.append(_DupDisplayRow(False, grp, rec, g_idx, False))
            else:
                pairs = [
                    (grp, rec)
                    for grp in groups
                    for rec in grp.records
                    if rec.id is not None
                ]
                if self._sort_col > 0:
                    pairs.sort(
                        key=lambda pair: self._record_sort_key(pair, self._sort_col),
                        reverse=reverse,
                    )
                band = -1
                prev_code: Optional[str] = None
                for idx, (grp, rec) in enumerate(pairs):
                    is_new_group = grp.canonical_code != prev_code
                    if is_new_group:
                        band += 1
                        prev_code = grp.canonical_code
                    new_rows.append(
                        _DupDisplayRow(False, grp, rec, band, is_group_start=(is_new_group and idx > 0))
                    )

            self.beginResetModel()
            self._rows = new_rows
            self.endResetModel()

        # ------------------------------------------------------------------
        # QAbstractTableModel interface
        # ------------------------------------------------------------------

        def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
            return 0 if parent.isValid() else len(self._rows)

        def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
            return 0 if parent.isValid() else len(self.COLUMN_SPECS)

        def headerData(
            self,
            section: int,
            orientation: Qt.Orientation,
            role: int = Qt.ItemDataRole.DisplayRole,
        ) -> Any:
            if role == Qt.ItemDataRole.DisplayRole:
                if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.COLUMN_SPECS):
                    return self.COLUMN_SPECS[section].label
                if orientation == Qt.Orientation.Vertical:
                    return str(section + 1)
            return None

        def flags(self, index: QModelIndex) -> Qt.ItemFlag:
            base = super().flags(index)
            if not index.isValid() or not (0 <= index.row() < len(self._rows)):
                return base
            if not self._rows[index.row()].is_group and index.column() == self.COL_CHECK:
                return base | Qt.ItemFlag.ItemIsUserCheckable
            return base

        def setData(
            self,
            index: QModelIndex,
            value: Any,
            role: int = Qt.ItemDataRole.EditRole,
        ) -> bool:
            if (
                role == Qt.ItemDataRole.CheckStateRole
                and index.isValid()
                and index.column() == self.COL_CHECK
                and 0 <= index.row() < len(self._rows)
            ):
                row_obj = self._rows[index.row()]
                if row_obj.is_group or row_obj.record is None or row_obj.record.id is None:
                    return False
                checked = (
                    value == Qt.CheckState.Checked
                    or value == Qt.CheckState.Checked.value
                    or value is True
                )
                rid = row_obj.record.id
                if checked:
                    self._checked_ids.add(rid)
                else:
                    self._checked_ids.discard(rid)
                self.dataChanged.emit(
                    self.index(index.row(), 0),
                    self.index(index.row(), len(self.COLUMN_SPECS) - 1),
                    [
                        Qt.ItemDataRole.CheckStateRole,
                        Qt.ItemDataRole.DisplayRole,
                        Qt.ItemDataRole.BackgroundRole,
                        Qt.ItemDataRole.ForegroundRole,
                        ROLE_BADGE,
                    ],
                )
                if self._view_mode == "grouped":
                    for r_idx, item in enumerate(self._rows):
                        if item.is_group and item.group.canonical_code == row_obj.group.canonical_code:
                            self.dataChanged.emit(
                                self.index(r_idx, self.COL_FILE_NAME),
                                self.index(r_idx, self.COL_FILE_NAME),
                                [Qt.ItemDataRole.DisplayRole],
                            )
                            break
                self.checked_changed.emit(len(self._checked_ids))
                return True
            return False

        def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
            if not index.isValid() or not (0 <= index.row() < len(self._rows)):
                return None
            row_obj = self._rows[index.row()]
            col = index.column()
            grp = row_obj.group

            if role == ROLE_GROUP_HEADER:
                if row_obj.is_group:
                    return {"expanded": grp.canonical_code not in self._collapsed_groups}
                return False
            if role == ROLE_GROUP_COLLAPSED:
                return row_obj.is_group and (grp.canonical_code in self._collapsed_groups)
            if role == ROLE_GROUP_DIVIDER:
                return (not row_obj.is_group) and row_obj.is_group_start
            if role == ROLE_COPY_TEXT:
                return grp.canonical_code if col == self.COL_CODE and grp.canonical_code else None

            if row_obj.is_group:
                return self._group_row_data(grp, col, role)
            if row_obj.record is None:
                return None
            return self._record_row_data(row_obj, grp, row_obj.record, col, role)

        def _group_row_data(self, grp: DuplicateGroup, col: int, role: int) -> Any:
            if role == Qt.ItemDataRole.DisplayRole:
                if col == self.COL_CODE:
                    return grp.canonical_code
                if col == self.COL_DUP_COUNT:
                    return f"×{grp.count}"
                if col == self.COL_FILE_NAME:
                    checked_in_grp = sum(1 for r in grp.records if r.id in self._checked_ids)
                    base = f"共 {grp.count} 条副本"
                    return f"{base} · 已勾选 {checked_in_grp} 条" if checked_in_grp else base
                if col == self.COL_FILE_SIZE:
                    total_size = sum(int(r.file_size or 0) for r in grp.records)
                    return format_file_size(total_size) if total_size > 0 else "-"
                if col == self.COL_DUP_TYPE:
                    return "疑似分集" if grp.is_possible_multipart else "完全重复"
                return ""
            if role == ROLE_BADGE:
                if col == self.COL_DUP_COUNT:
                    return (f"×{grp.count}", "purple" if grp.is_possible_multipart else "warning")
                if col == self.COL_DUP_TYPE:
                    return ("疑似分集", "purple") if grp.is_possible_multipart else ("完全重复", "warning")
                return None
            if role == Qt.ItemDataRole.BackgroundRole:
                return self._bg_group_header
            if role == Qt.ItemDataRole.FontRole:
                return self._mono_font if col == self.COL_CODE else self._bold_font
            if role == Qt.ItemDataRole.ForegroundRole:
                if col == self.COL_CODE:
                    return self._color_purple if grp.is_possible_multipart else self._color_warning
                if col == self.COL_FILE_NAME:
                    return self._color_secondary
                return None
            if role == Qt.ItemDataRole.TextAlignmentRole:
                align = (
                    Qt.AlignmentFlag.AlignCenter
                    if col in (self.COL_CHECK, self.COL_DUP_COUNT, self.COL_DUP_TYPE, self.COL_FILE_SIZE)
                    else Qt.AlignmentFlag.AlignLeft
                )
                return int(align | Qt.AlignmentFlag.AlignVCenter)
            if role == Qt.ItemDataRole.ToolTipRole:
                kind = "疑似分集" if grp.is_possible_multipart else "完全重复"
                return f"标准编码: {grp.canonical_code}\n类型: {kind}\n共 {grp.count} 条记录（点击展开或折叠）"
            return None

        def _record_row_data(
            self,
            row_obj: _DupDisplayRow,
            grp: DuplicateGroup,
            rec: DataRecord,
            col: int,
            role: int,
        ) -> Any:
            is_checked = rec.id is not None and rec.id in self._checked_ids

            if role == Qt.ItemDataRole.CheckStateRole:
                if col == self.COL_CHECK:
                    return Qt.CheckState.Checked if is_checked else Qt.CheckState.Unchecked
                return None

            if role == Qt.ItemDataRole.DisplayRole:
                vals = (
                    "",
                    rec.canonical_code,
                    f"×{grp.count}",
                    rec.file_name or rec.raw_text,
                    rec.source_name or rec.source_type or "-",
                    format_file_size(rec.file_size),
                    rec.created_at[:19] if rec.created_at else "-",
                    "待删除" if is_checked else "保留",
                    "疑似分集" if grp.is_possible_multipart else "完全重复",
                    " / ".join(rec.tags) if rec.tags else "-",
                    rec.part or "-",
                    rec.prefix or "-",
                    rec.matched_rule or "-",
                    str(rec.id) if rec.id is not None else "-",
                )
                return vals[col] if 0 <= col < len(vals) else None

            if role == ROLE_BADGE:
                if col == self.COL_DUP_COUNT:
                    return (f"×{grp.count}", "purple" if grp.is_possible_multipart else "warning")
                if col == self.COL_STATUS:
                    return ("待删除", "danger") if is_checked else ("保留", "success")
                if col == self.COL_DUP_TYPE:
                    return ("疑似分集", "purple") if grp.is_possible_multipart else ("完全重复", "warning")
                return None

            if role == Qt.ItemDataRole.BackgroundRole:
                if is_checked:
                    return self._bg_checked
                return self._bg_group_band if row_obj.band % 2 == 1 else None

            if role == Qt.ItemDataRole.FontRole and col == self.COL_CODE:
                return self._mono_font

            if role == Qt.ItemDataRole.ForegroundRole:
                if col == self.COL_CODE:
                    if is_checked:
                        return self._color_danger
                    return self._color_purple if grp.is_possible_multipart else self._color_warning
                if col == self.COL_TAGS and rec.tags:
                    return self._color_accent
                if col == self.COL_PART and rec.part:
                    return self._color_purple
                if col == self.COL_CREATED_AT:
                    return self._color_muted
                return None

            if role == Qt.ItemDataRole.TextAlignmentRole:
                center_cols = (
                    self.COL_CHECK,
                    self.COL_DUP_COUNT,
                    self.COL_FILE_SIZE,
                    self.COL_STATUS,
                    self.COL_DUP_TYPE,
                    self.COL_PART,
                    self.COL_PREFIX,
                    self.COL_RECORD_ID,
                )
                align = Qt.AlignmentFlag.AlignCenter if col in center_cols else Qt.AlignmentFlag.AlignLeft
                return int(align | Qt.AlignmentFlag.AlignVCenter)

            if role == Qt.ItemDataRole.ToolTipRole:
                parts = [
                    f"标准编码: {rec.canonical_code} (共 {grp.count} 条)",
                    f"文件名: {rec.file_name or rec.raw_text}",
                    f"来源: {rec.source_name or rec.source_type or '-'}",
                    f"大小: {format_file_size(rec.file_size)}",
                ]
                if rec.full_path:
                    parts.append(f"完整路径: {rec.full_path}")
                if rec.tags:
                    parts.append(f"版本标签: {', '.join(rec.tags)}")
                if rec.part:
                    parts.append(f"分集: {rec.part}")
                if rec.created_at:
                    parts.append(f"入库时间: {rec.created_at[:19]}")
                return "\n".join(parts)

            return None

        def sort(
            self,
            column: int,
            order: Qt.SortOrder = Qt.SortOrder.AscendingOrder,
        ) -> None:
            if column < 0 or column == self.COL_CHECK:
                return
            self._sort_col = column
            self._sort_order = order
            self._rebuild_rows()

    DuplicateTableModel.__qualname__ = "DuplicateTableModel"
    return DuplicateTableModel
