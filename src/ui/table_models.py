"""High-performance virtualized Qt6 table models for 数据查重程序.

Exports:
- ``DuplicateTableModel`` & ``DUPLICATE_COLUMN_SPECS`` (implemented in
  ``table_duplicate_model.py`` and re-exported here)
- ``RecordTableModel`` (View 1: 数据总库)
- ``PreviewTableModel`` (View 2: 批量导入)
- ``CompareTableModel`` (View 3 Mode A: 查当前总库)
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont

from src.core.models import DataRecord, ParseResult
from src.ui.table_duplicate_model import (
    DUPLICATE_COLUMN_SPECS,
    ROLE_BADGE,
    ROLE_COPY_TEXT,
    ROLE_GROUP_COLLAPSED,
    ROLE_GROUP_DIVIDER,
    ROLE_GROUP_HEADER,
    build_duplicate_table_model_class,
)
from src.ui.table_header import ColumnSpec
from src.ui.theme import get_palette
from src.ui.widgets import format_file_size

__all__ = [
    "DUPLICATE_COLUMN_SPECS",
    "ROLE_BADGE",
    "ROLE_COPY_TEXT",
    "ROLE_GROUP_COLLAPSED",
    "ROLE_GROUP_DIVIDER",
    "ROLE_GROUP_HEADER",
    "DuplicateTableModel",
    "RecordTableModel",
    "PreviewTableModel",
    "CompareTableModel",
    "_ThemedTableModelMixin",
    "_make_mono_font",
    "_sort_with_persistent_indexes",
    "_split_manual_code",
]


def _make_mono_font(point_size: float = 9.5, *, bold: bool = True) -> QFont:
    """Create a crisp tabular monospace font synced with theme.get_mono_font."""
    try:
        from src.ui.theme import get_mono_font

        weight = QFont.Weight.DemiBold if bold else QFont.Weight.Normal
        return get_mono_font(point_size=point_size, weight=weight)
    except Exception:
        font = QFont("Cascadia Mono", int(round(point_size)))
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFamilies(["Cascadia Mono", "JetBrains Mono", "Consolas", "monospace"])
        font.setBold(bold)
        return font


def _split_manual_code(code: str) -> tuple[str, str]:
    """Split a manually entered canonical code into (prefix, number).

    Uses ``rsplit('-', 1)`` so multi-hyphen codes such as ``M2-SUB-1234567``
    preserve ``prefix='M2-SUB'`` and ``number='1234567'``.
    """
    if "-" in code:
        left, right = code.rsplit("-", 1)
        left, right = left.strip(), right.strip()
        if left and right:
            return left, right
    return "", code


def _sort_with_persistent_indexes(
    model: QAbstractTableModel,
    items: list[Any],
    key_fn: Callable[[Any], Any],
    reverse: bool,
) -> None:
    """Sort ``items`` in place while remapping Qt persistent indexes cleanly."""
    model.layoutAboutToBeChanged.emit()
    old_persistent = model.persistentIndexList()
    old_ids = [id(item) for item in items]
    items.sort(key=key_fn, reverse=reverse)
    if old_persistent:
        new_pos = {id(item): idx for idx, item in enumerate(items)}
        new_persistent: list[QModelIndex] = []
        for idx in old_persistent:
            if idx.isValid() and 0 <= idx.row() < len(old_ids):
                target_row = new_pos.get(old_ids[idx.row()], -1)
                new_persistent.append(
                    model.index(target_row, idx.column()) if target_row >= 0 else QModelIndex()
                )
            else:
                new_persistent.append(QModelIndex())
        model.changePersistentIndexList(old_persistent, new_persistent)
    model.layoutChanged.emit()


class _ThemedTableModelMixin:
    """Shared Light/Dark Option B palette colors for QAbstractTableModel subclasses."""

    _theme: str
    _color_accent: QColor
    _color_success: QColor
    _color_warning: QColor
    _color_danger: QColor
    _color_purple: QColor
    _color_muted: QColor
    _color_primary: QColor
    _color_secondary: QColor
    _bg_group_band: QBrush
    _bg_checked: QBrush
    _bg_group_header: QBrush

    def _init_theme_colors(self, theme: str = "light") -> None:
        self._theme = "dark" if theme == "dark" else "light"
        self._refresh_theme_colors()

    def _refresh_theme_colors(self) -> None:
        p = get_palette(self._theme)
        is_dark = self._theme == "dark"
        self._color_accent = QColor(p.get("accent", "#7C86E8" if is_dark else "#5E6AD2"))
        self._color_success = QColor(p.get("success", "#4ADE80" if is_dark else "#15803D"))
        self._color_warning = QColor(p.get("warning", "#FBBF24" if is_dark else "#B45309"))
        self._color_danger = QColor(p.get("danger", "#FB7185" if is_dark else "#B91C1C"))
        self._color_purple = QColor(p.get("purple", "#A78BFA" if is_dark else "#6D28D9"))
        self._color_muted = QColor(p.get("text_muted", "#63636E" if is_dark else "#A1A1AA"))
        self._color_primary = QColor(p.get("text_primary", "#F4F4F5" if is_dark else "#09090B"))
        self._color_secondary = QColor(
            p.get("text_secondary", "#A1A1AA" if is_dark else "#52525B")
        )
        self._bg_group_band = QBrush(
            QColor(p.get("bg_group_band", "#13151A" if is_dark else "#FAFAFB"))
        )
        self._bg_checked = QBrush(
            QColor(p.get("row_tint_checked", "#261418" if is_dark else "#FEF2F2"))
        )
        self._bg_group_header = QBrush(
            QColor(p.get("bg_surface_alt", "#1C1F26" if is_dark else "#F4F4F5"))
        )

    def set_theme(self, theme: str) -> None:
        """Update model foreground/background colors for Light or Dark mode."""
        normalized = "dark" if theme == "dark" else "light"
        if normalized == self._theme:
            return
        self._theme = normalized
        self._refresh_theme_colors()
        rows = self.rowCount()  # type: ignore[attr-defined]
        cols = self.columnCount()  # type: ignore[attr-defined]
        if rows > 0 and cols > 0:
            self.dataChanged.emit(  # type: ignore[attr-defined]
                self.index(0, 0),  # type: ignore[attr-defined]
                self.index(rows - 1, cols - 1),  # type: ignore[attr-defined]
                [
                    Qt.ItemDataRole.ForegroundRole,
                    Qt.ItemDataRole.BackgroundRole,
                    ROLE_BADGE,
                ],
            )


DuplicateTableModel = build_duplicate_table_model_class(_ThemedTableModelMixin, _make_mono_font)


class RecordTableModel(_ThemedTableModelMixin, QAbstractTableModel):
    """Virtualized table model backing View 1 (数据总库)."""

    # Logical indices 0..8 preserved strictly for backward compatibility with existing tests.
    COLUMN_SPECS: list[ColumnSpec] = [
        ColumnSpec("code", "标准编码", default_width=142, default_visible=True, min_width=100, lock_visible=True),
        ColumnSpec("prefix", "前缀", default_width=76, default_visible=False, min_width=56, category="advanced"),
        ColumnSpec("tags", "版本标签", default_width=110, default_visible=False, min_width=72, category="advanced"),
        ColumnSpec("part", "分集", default_width=68, default_visible=False, min_width=54, category="advanced"),
        ColumnSpec("file_name", "原始文件名", default_width=320, default_visible=True, min_width=140, stretch_weight=1.0),
        ColumnSpec("source", "来源", default_width=160, default_visible=True, min_width=90),
        ColumnSpec("file_size", "大小", default_width=96, default_visible=True, min_width=72),
        ColumnSpec("rule", "匹配规则", default_width=130, default_visible=False, min_width=90, category="advanced"),
        ColumnSpec("created_at", "入库时间", default_width=148, default_visible=True, min_width=110),
        ColumnSpec("record_id", "ID", default_width=68, default_visible=False, min_width=52, category="advanced"),
        ColumnSpec("note", "备注", default_width=180, default_visible=False, min_width=90, category="advanced"),
    ]

    COLUMNS: list[tuple[str, int]] = [(s.label, s.default_width) for s in COLUMN_SPECS]

    def __init__(self, parent: Optional[Any] = None, theme: str = "light") -> None:
        super().__init__(parent)
        self._init_theme_colors(theme)
        self._records: list[DataRecord] = []
        self._duplicate_codes: set[str] = set()
        self._mono_font = _make_mono_font(10, bold=True)

    def set_records(
        self,
        records: list[DataRecord],
        duplicate_codes: Optional[set[str]] = None,
    ) -> None:
        self.beginResetModel()
        self._records = list(records)
        if duplicate_codes is not None:
            self._duplicate_codes = set(duplicate_codes)
        else:
            counts: dict[str, int] = {}
            for rec in self._records:
                counts[rec.canonical_code] = counts.get(rec.canonical_code, 0) + 1
            self._duplicate_codes = {k for k, v in counts.items() if v > 1}
        self.endResetModel()

    def record_at(self, row: int) -> Optional[DataRecord]:
        return self._records[row] if 0 <= row < len(self._records) else None

    def all_records(self) -> list[DataRecord]:
        return list(self._records)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._records)

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

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._records)):
            return None
        rec = self._records[index.row()]
        col = index.column()

        if role == ROLE_COPY_TEXT:
            return rec.canonical_code if col == 0 and rec.canonical_code else None

        if role == Qt.ItemDataRole.DisplayRole:
            vals = (
                rec.canonical_code,
                rec.prefix or "-",
                " / ".join(rec.tags) if rec.tags else "-",
                rec.part or "-",
                rec.file_name or rec.raw_text,
                rec.source_name or rec.source_type,
                format_file_size(rec.file_size),
                rec.matched_rule or "-",
                rec.created_at[:19] if rec.created_at else "-",
                str(rec.id) if rec.id is not None else "-",
                rec.note or "-",
            )
            return vals[col] if 0 <= col < len(vals) else None

        if role == Qt.ItemDataRole.FontRole and col == 0:
            return self._mono_font

        if role == Qt.ItemDataRole.ForegroundRole:
            if col == 0 and rec.canonical_code in self._duplicate_codes:
                return self._color_warning
            if col == 2 and rec.tags:
                return self._color_accent
            if col == 3 and rec.part:
                return self._color_purple
            if col in (8, 9):
                return self._color_muted

        if role == Qt.ItemDataRole.TextAlignmentRole:
            align = Qt.AlignmentFlag.AlignCenter if col in (1, 3, 6, 9) else Qt.AlignmentFlag.AlignLeft
            return int(align | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.ToolTipRole:
            parts = [
                f"标准编码: {rec.canonical_code}",
                f"原始文本: {rec.raw_text or rec.file_name}",
            ]
            if rec.full_path:
                parts.append(f"完整路径: {rec.full_path}")
            if rec.tags:
                parts.append(f"版本标签: {', '.join(rec.tags)}")
            if rec.part:
                parts.append(f"分集标记: {rec.part}")
            if rec.note:
                parts.append(f"备注: {rec.note}")
            return "\n".join(parts)

        return None

    def sort(
        self,
        column: int,
        order: Qt.SortOrder = Qt.SortOrder.AscendingOrder,
    ) -> None:
        if column < 0:
            return
        key_funcs = {
            0: lambda r: (r.canonical_code or "").lower(),
            1: lambda r: (r.prefix or "").lower(),
            2: lambda r: (r.tag_score, r.tags_str),
            3: lambda r: (r.part or "").lower(),
            4: lambda r: (r.file_name or r.raw_text or "").lower(),
            5: lambda r: (r.source_name or "").lower(),
            6: lambda r: int(r.file_size or 0),
            7: lambda r: (r.matched_rule or "").lower(),
            8: lambda r: r.created_at or "",
            9: lambda r: int(r.id or 0),
            10: lambda r: (r.note or "").lower(),
        }
        _sort_with_persistent_indexes(
            self, self._records, key_funcs.get(column, key_funcs[0]), order == Qt.SortOrder.DescendingOrder
        )


class PreviewTableModel(_ThemedTableModelMixin, QAbstractTableModel):
    """Virtualized table model for Batch Import Preview tabs with inline code fix."""

    code_manually_fixed = Signal(int, str)

    @classmethod
    def specs_for_mode(cls, mode: str) -> list[ColumnSpec]:
        if mode == "db_dup":
            return [
                ColumnSpec("code", "标准编码", default_width=142, default_visible=True, min_width=100, lock_visible=True),
                ColumnSpec("file_name", "本次文件", default_width=240, default_visible=True, min_width=120, stretch_weight=0.5),
                ColumnSpec("existing", "库内已有记录", default_width=260, default_visible=True, min_width=130, stretch_weight=0.5),
                ColumnSpec("tags", "版本标签", default_width=110, default_visible=False, min_width=72, category="advanced"),
                ColumnSpec("part", "分集", default_width=68, default_visible=False, min_width=54, category="advanced"),
                ColumnSpec("source", "来源", default_width=155, default_visible=True, min_width=90),
            ]
        if mode == "unrecognized":
            return [
                ColumnSpec("code", "手动修正编码 (双击输入)", default_width=175, default_visible=True, min_width=120, lock_visible=True),
                ColumnSpec("file_name", "原始文本 / 文件名", default_width=290, default_visible=True, min_width=140, stretch_weight=1.0),
                ColumnSpec("parent_dir", "上级目录", default_width=160, default_visible=False, min_width=90, category="advanced"),
                ColumnSpec("source", "来源", default_width=150, default_visible=True, min_width=90),
                ColumnSpec("note", "说明", default_width=185, default_visible=True, min_width=110),
            ]
        return [
            ColumnSpec("code", "标准编码", default_width=142, default_visible=True, min_width=100, lock_visible=True),
            ColumnSpec("prefix", "前缀", default_width=76, default_visible=False, min_width=56, category="advanced"),
            ColumnSpec("tags", "版本标签", default_width=110, default_visible=False, min_width=72, category="advanced"),
            ColumnSpec("part", "分集", default_width=68, default_visible=False, min_width=54, category="advanced"),
            ColumnSpec("file_name", "原始文件名 / 文本", default_width=320, default_visible=True, min_width=140, stretch_weight=1.0),
            ColumnSpec("rule", "匹配规则", default_width=130, default_visible=False, min_width=90, category="advanced"),
            ColumnSpec("source", "来源", default_width=160, default_visible=True, min_width=90),
        ]

    def __init__(
        self,
        mode: str = "new",
        parent: Optional[Any] = None,
        theme: str = "light",
    ) -> None:
        super().__init__(parent)
        self._init_theme_colors(theme)
        self._mode = mode  # "new" | "db_dup" | "batch_dup" | "unrecognized"
        self._rows: list[Any] = []
        self._mono_font = _make_mono_font(10, bold=True)
        self.COLUMN_SPECS: list[ColumnSpec] = self.specs_for_mode(mode)
        self._headers: list[str] = [s.label for s in self.COLUMN_SPECS]

    def column_specs(self) -> list[ColumnSpec]:
        return list(self.COLUMN_SPECS)

    def set_rows(self, rows: list[Any]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rows(self) -> list[Any]:
        return self._rows

    def row_at(self, row: int) -> Optional[Any]:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._headers)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole:
            if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self._headers):
                return self._headers[section]
            if orientation == Qt.Orientation.Vertical:
                return str(section + 1)
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        base = super().flags(index)
        if self._mode == "unrecognized" and index.column() == 0:
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def setData(
        self,
        index: QModelIndex,
        value: Any,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if (
            self._mode == "unrecognized"
            and role == Qt.ItemDataRole.EditRole
            and index.isValid()
            and index.column() == 0
            and 0 <= index.row() < len(self._rows)
        ):
            row = index.row()
            new_code = str(value or "").strip().upper()
            item: ParseResult = self._rows[row]
            item.canonical_code = new_code
            item.recognized = bool(new_code)
            if new_code:
                item.matched_rule = "手动修正"
                item.confidence = 1.0
                item.prefix, item.number = _split_manual_code(new_code)
            else:
                item.matched_rule = ""
                item.confidence = 0.0
                item.prefix = ""
                item.number = ""

            self.dataChanged.emit(
                self.index(row, 0),
                self.index(row, self.columnCount() - 1),
                [
                    Qt.ItemDataRole.DisplayRole,
                    Qt.ItemDataRole.EditRole,
                    Qt.ItemDataRole.ForegroundRole,
                    Qt.ItemDataRole.FontRole,
                    ROLE_COPY_TEXT,
                ],
            )
            self.code_manually_fixed.emit(row, new_code)
            return True
        return False

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row, col = index.row(), index.column()

        if self._mode == "db_dup":
            item, existing = self._rows[row]
            if role == ROLE_COPY_TEXT:
                return item.canonical_code if col == 0 and item.canonical_code else None
            if role == Qt.ItemDataRole.DisplayRole:
                first = existing[0].file_name or existing[0].raw_text if existing else ""
                existing_txt = f"共 {len(existing)} 条：{first} ..." if len(existing) > 1 else first
                vals = (
                    item.canonical_code,
                    item.file_name or item.raw_text,
                    existing_txt,
                    " / ".join(item.tags) if item.tags else "-",
                    item.part or "-",
                    item.source_name or item.source_type,
                )
                return vals[col] if 0 <= col < len(vals) else None
            if role == Qt.ItemDataRole.FontRole and col == 0:
                return self._mono_font
            if role == Qt.ItemDataRole.ForegroundRole and col == 0:
                return self._color_warning
            if role == Qt.ItemDataRole.ToolTipRole:
                existing_lines = "\n".join(
                    f"  - [{r.source_name}] {r.file_name or r.raw_text}" for r in existing[:10]
                )
                return f"新项: {item.file_name or item.raw_text}\n库内匹配:\n{existing_lines}"
            return None

        item: ParseResult = self._rows[row]

        if self._mode == "unrecognized":
            if role == ROLE_COPY_TEXT:
                return item.canonical_code if col == 0 and item.canonical_code else None
            if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
                code_txt = (
                    item.canonical_code
                    if item.canonical_code
                    else ("" if role == Qt.ItemDataRole.EditRole else "（双击填写编码）")
                )
                note_txt = (
                    "已手动修正，可随本批入库"
                    if item.canonical_code
                    else (item.note or "未匹配内置或自定义规则")
                )
                vals = (code_txt, item.file_name or item.raw_text, item.parent_dir or "-", item.source_name or item.source_type, note_txt)
                return vals[col] if 0 <= col < len(vals) else None
            if role == Qt.ItemDataRole.ForegroundRole and col == 0:
                return self._color_success if item.canonical_code else self._color_muted
            if role == Qt.ItemDataRole.FontRole and col == 0 and item.canonical_code:
                return self._mono_font
            if role == Qt.ItemDataRole.ToolTipRole:
                return f"原始文本: {item.raw_text}\n清洗后: {item.cleaned_text or '-'}\n路径: {item.full_path or '-'}"
            return None

        if role == ROLE_COPY_TEXT:
            return item.canonical_code if col == 0 and item.canonical_code else None
        if role == Qt.ItemDataRole.DisplayRole:
            suffix = " (目录回退)" if item.from_parent else ""
            rule_txt = f"{item.matched_rule}{suffix}" if item.matched_rule else "-"
            vals = (
                item.canonical_code,
                item.prefix or "-",
                " / ".join(item.tags) if item.tags else "-",
                item.part or "-",
                item.file_name or item.raw_text,
                rule_txt,
                item.source_name or item.source_type,
            )
            return vals[col] if 0 <= col < len(vals) else None
        if role == Qt.ItemDataRole.FontRole and col == 0:
            return self._mono_font
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == 0:
                return self._color_success if self._mode == "new" else self._color_purple
            if col == 2 and item.tags:
                return self._color_accent
        if role == Qt.ItemDataRole.ToolTipRole:
            return (
                f"编码: {item.canonical_code}\n"
                f"原始文本: {item.raw_text}\n"
                f"规则: {item.matched_rule} (置信度 {item.confidence:.2f})"
            )
        return None

    def sort(
        self,
        column: int,
        order: Qt.SortOrder = Qt.SortOrder.AscendingOrder,
    ) -> None:
        if column < 0:
            return
        if self._mode == "db_dup":
            key_funcs = {
                0: lambda pair: (pair[0].canonical_code or "").lower(),
                1: lambda pair: (pair[0].file_name or pair[0].raw_text or "").lower(),
                2: lambda pair: (
                    len(pair[1]),
                    (pair[1][0].file_name or pair[1][0].raw_text or "").lower() if pair[1] else "",
                ),
                3: lambda pair: " / ".join(pair[0].tags).lower(),
                4: lambda pair: (pair[0].part or "").lower(),
                5: lambda pair: (pair[0].source_name or pair[0].source_type or "").lower(),
            }
        elif self._mode == "unrecognized":
            key_funcs = {
                0: lambda it: (it.canonical_code or "").lower(),
                1: lambda it: (it.file_name or it.raw_text or "").lower(),
                2: lambda it: (it.parent_dir or "").lower(),
                3: lambda it: (it.source_name or it.source_type or "").lower(),
                4: lambda it: ("已手动修正" if it.canonical_code else (it.note or "")).lower(),
            }
        else:
            key_funcs = {
                0: lambda it: (it.canonical_code or "").lower(),
                1: lambda it: (it.prefix or "").lower(),
                2: lambda it: " / ".join(it.tags).lower(),
                3: lambda it: (it.part or "").lower(),
                4: lambda it: (it.file_name or it.raw_text or "").lower(),
                5: lambda it: (it.matched_rule or "").lower(),
                6: lambda it: (it.source_name or it.source_type or "").lower(),
            }
        _sort_with_persistent_indexes(
            self, self._rows, key_funcs.get(column, key_funcs[0]), order == Qt.SortOrder.DescendingOrder
        )


class CompareTableModel(_ThemedTableModelMixin, QAbstractTableModel):
    """Virtualized table model for View 3 (快速查重 — DB Compare)."""

    @classmethod
    def specs_for_mode(cls, with_matches: bool) -> list[ColumnSpec]:
        if with_matches:
            return [
                ColumnSpec("code", "标准编码", default_width=142, default_visible=True, min_width=100, lock_visible=True),
                ColumnSpec("input_item", "输入项", default_width=240, default_visible=True, min_width=120, stretch_weight=0.5),
                ColumnSpec("existing", "库内已有记录", default_width=260, default_visible=True, min_width=130, stretch_weight=0.5),
                ColumnSpec("tags", "版本标签", default_width=110, default_visible=False, min_width=72, category="advanced"),
                ColumnSpec("part", "分集", default_width=68, default_visible=False, min_width=54, category="advanced"),
            ]
        return [
            ColumnSpec("code", "标准编码", default_width=142, default_visible=True, min_width=100, lock_visible=True),
            ColumnSpec("prefix", "前缀", default_width=76, default_visible=False, min_width=56, category="advanced"),
            ColumnSpec("tags", "版本标签", default_width=110, default_visible=False, min_width=72, category="advanced"),
            ColumnSpec("part", "分集", default_width=68, default_visible=False, min_width=54, category="advanced"),
            ColumnSpec("file_name", "文件名 / 原始文本", default_width=320, default_visible=True, min_width=140, stretch_weight=1.0),
            ColumnSpec("source", "来源", default_width=160, default_visible=True, min_width=90),
        ]

    def __init__(
        self,
        with_matches: bool = False,
        parent: Optional[Any] = None,
        theme: str = "light",
    ) -> None:
        super().__init__(parent)
        self._init_theme_colors(theme)
        self._with_matches = with_matches
        self._rows: list[Any] = []
        self._mono_font = _make_mono_font(10, bold=True)
        self.COLUMN_SPECS: list[ColumnSpec] = self.specs_for_mode(with_matches)
        self._headers: list[str] = [s.label for s in self.COLUMN_SPECS]

    def column_specs(self) -> list[ColumnSpec]:
        return list(self.COLUMN_SPECS)

    def set_rows(self, rows: list[Any]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rows(self) -> list[Any]:
        return list(self._rows)

    def row_at(self, row: int) -> Optional[Any]:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._headers)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole:
            if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self._headers):
                return self._headers[section]
            if orientation == Qt.Orientation.Vertical:
                return str(section + 1)
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row, col = index.row(), index.column()

        if self._with_matches:
            item, matches = self._rows[row]
            if role == ROLE_COPY_TEXT:
                return item.canonical_code if col == 0 and item.canonical_code else None
            if role == Qt.ItemDataRole.DisplayRole:
                if not matches:
                    m_txt = "-"
                else:
                    first = matches[0].file_name or matches[0].raw_text
                    m_txt = f"共 {len(matches)} 条：{first}" if len(matches) > 1 else first
                vals = (
                    item.canonical_code,
                    item.file_name or item.raw_text,
                    m_txt,
                    " / ".join(item.tags) if item.tags else "-",
                    item.part or "-",
                )
                return vals[col] if 0 <= col < len(vals) else None
            if role == Qt.ItemDataRole.FontRole and col == 0:
                return self._mono_font
            if role == Qt.ItemDataRole.ForegroundRole and col == 0:
                return self._color_warning
            return None

        item: ParseResult = self._rows[row]
        if role == ROLE_COPY_TEXT:
            return item.canonical_code if col == 0 and item.canonical_code else None
        if role == Qt.ItemDataRole.DisplayRole:
            vals = (
                item.canonical_code or "（未识别）",
                item.prefix or "-",
                " / ".join(item.tags) if item.tags else "-",
                item.part or "-",
                item.file_name or item.raw_text,
                item.source_name or item.source_type,
            )
            return vals[col] if 0 <= col < len(vals) else None
        if role == Qt.ItemDataRole.FontRole and col == 0 and item.canonical_code:
            return self._mono_font
        if role == Qt.ItemDataRole.ForegroundRole and col == 0:
            return self._color_success if item.canonical_code else self._color_muted
        return None

    def sort(
        self,
        column: int,
        order: Qt.SortOrder = Qt.SortOrder.AscendingOrder,
    ) -> None:
        if column < 0:
            return
        if self._with_matches:
            key_funcs = {
                0: lambda pair: (pair[0].canonical_code or "").lower(),
                1: lambda pair: (pair[0].file_name or pair[0].raw_text or "").lower(),
                2: lambda pair: (
                    len(pair[1]),
                    (pair[1][0].file_name or pair[1][0].raw_text or "").lower() if pair[1] else "",
                ),
                3: lambda pair: " / ".join(pair[0].tags).lower(),
                4: lambda pair: (pair[0].part or "").lower(),
            }
        else:
            key_funcs = {
                0: lambda it: (it.canonical_code or "").lower(),
                1: lambda it: (it.prefix or "").lower(),
                2: lambda it: " / ".join(it.tags).lower(),
                3: lambda it: (it.part or "").lower(),
                4: lambda it: (it.file_name or it.raw_text or "").lower(),
                5: lambda it: (it.source_name or it.source_type or "").lower(),
            }
        _sort_with_persistent_indexes(
            self, self._rows, key_funcs.get(column, key_funcs[0]), order == Qt.SortOrder.DescendingOrder
        )
