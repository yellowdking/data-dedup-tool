"""High-performance virtualized Qt6 table models for 数据查重程序."""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor, QFont

from src.core.models import DataRecord, ParseResult
from src.ui.widgets import format_file_size


class RecordTableModel(QAbstractTableModel):
    """Virtualized table model backing View 1 (数据总库)."""

    COLUMNS = [
        ("标准编码", 125),
        ("前缀", 72),
        ("版本标签", 105),
        ("分集", 66),
        ("原始文件名", 230),
        ("来源", 125),
        ("大小", 82),
        ("匹配规则", 130),
        ("入库时间", 135),
    ]

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self._records: list[DataRecord] = []
        self._duplicate_codes: set[str] = set()
        self._mono_font = QFont("Consolas", 10)
        self._mono_font.setBold(True)

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
        if 0 <= row < len(self._records):
            return self._records[row]
        return None

    def all_records(self) -> list[DataRecord]:
        return list(self._records)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._records)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self.COLUMNS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole:
            if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.COLUMNS):
                return self.COLUMNS[section][0]
            if orientation == Qt.Orientation.Vertical:
                return str(section + 1)
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        if not (0 <= row < len(self._records)):
            return None
        rec = self._records[row]

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return rec.canonical_code
            if col == 1:
                return rec.prefix or "-"
            if col == 2:
                return " / ".join(rec.tags) if rec.tags else "-"
            if col == 3:
                return rec.part or "-"
            if col == 4:
                return rec.file_name or rec.raw_text
            if col == 5:
                return rec.source_name or rec.source_type
            if col == 6:
                return format_file_size(rec.file_size)
            if col == 7:
                return rec.matched_rule or "-"
            if col == 8:
                return rec.created_at[:19] if rec.created_at else "-"

        if role == Qt.ItemDataRole.FontRole and col == 0:
            return self._mono_font

        if role == Qt.ItemDataRole.ForegroundRole:
            if col == 0 and rec.canonical_code in self._duplicate_codes:
                return QColor("#D97706")
            if col == 2 and rec.tags:
                return QColor("#2563EB")
            if col == 3 and rec.part:
                return QColor("#7C3AED")

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (1, 3, 6):
                return int(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

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
        reverse = order == Qt.SortOrder.DescendingOrder
        key_funcs = {
            0: lambda r: (r.canonical_code or "").lower(),
            1: lambda r: (r.prefix or "").lower(),
            2: lambda r: (r.tag_score, r.tags_str),
            3: lambda r: (r.part or "").lower(),
            4: lambda r: (r.file_name or r.raw_text or "").lower(),
            5: lambda r: (r.source_name or "").lower(),
            6: lambda r: r.file_size,
            7: lambda r: (r.matched_rule or "").lower(),
            8: lambda r: r.created_at or "",
        }
        key_fn = key_funcs.get(column, key_funcs[0])
        self.layoutAboutToBeChanged.emit()
        self._records.sort(key=key_fn, reverse=reverse)
        self.layoutChanged.emit()


class PreviewTableModel(QAbstractTableModel):
    """Virtualized table model for Batch Import Preview tabs.

    Supports editable canonical code on the 'unrecognized' tab so users can
    manually fix obscure filenames directly inside the preview table.
    """

    code_manually_fixed = Signal(int, str)

    def __init__(self, mode: str = "new", parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self._mode = mode  # "new" | "db_dup" | "batch_dup" | "unrecognized"
        self._rows: list[Any] = []
        self._mono_font = QFont("Consolas", 10)
        self._mono_font.setBold(True)

        if mode == "db_dup":
            self._headers = ["标准编码", "本次文件", "库内已有记录", "标签", "分集", "来源"]
        elif mode == "unrecognized":
            self._headers = ["手动修正编码 (双击输入)", "原始文本 / 文件名", "上级目录", "来源", "说明"]
        else:
            self._headers = ["标准编码", "前缀", "标签", "分集", "原始文件名 / 文本", "匹配规则", "来源"]

    def set_rows(self, rows: list[Any]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rows(self) -> list[Any]:
        return self._rows

    def row_at(self, row: int) -> Optional[Any]:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

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
        ):
            row = index.row()
            if 0 <= row < len(self._rows):
                new_code = str(value or "").strip().upper()
                item: ParseResult = self._rows[row]
                item.canonical_code = new_code
                item.recognized = bool(new_code)
                if new_code:
                    item.matched_rule = "手动修正"
                    item.confidence = 1.0
                    parts = new_code.split("-", 1)
                    item.prefix = parts[0] if len(parts) == 2 else ""
                    item.number = parts[1] if len(parts) == 2 else new_code
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole])
                self.code_manually_fixed.emit(row, new_code)
                return True
        return False

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        if not (0 <= row < len(self._rows)):
            return None

        if self._mode == "db_dup":
            item, existing = self._rows[row]
            if role == Qt.ItemDataRole.DisplayRole:
                if col == 0:
                    return item.canonical_code
                if col == 1:
                    return item.file_name or item.raw_text
                if col == 2:
                    first = existing[0].file_name or existing[0].raw_text if existing else ""
                    if len(existing) > 1:
                        return f"共 {len(existing)} 条：{first} ..."
                    return first
                if col == 3:
                    return " / ".join(item.tags) if item.tags else "-"
                if col == 4:
                    return item.part or "-"
                if col == 5:
                    return item.source_name or item.source_type
            if role == Qt.ItemDataRole.FontRole and col == 0:
                return self._mono_font
            if role == Qt.ItemDataRole.ForegroundRole and col == 0:
                return QColor("#D97706")
            if role == Qt.ItemDataRole.ToolTipRole:
                existing_lines = "\n".join(
                    f"  - [{r.source_name}] {r.file_name or r.raw_text}" for r in existing[:10]
                )
                return f"新项: {item.file_name or item.raw_text}\n库内匹配:\n{existing_lines}"
            return None

        item: ParseResult = self._rows[row]

        if self._mode == "unrecognized":
            if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
                if col == 0:
                    return item.canonical_code if item.canonical_code else ("" if role == Qt.ItemDataRole.EditRole else "（双击填写编码）")
                if col == 1:
                    return item.file_name or item.raw_text
                if col == 2:
                    return item.parent_dir or "-"
                if col == 3:
                    return item.source_name or item.source_type
                if col == 4:
                    if item.canonical_code:
                        return "已手动修正，可随本批入库"
                    return item.note or "未匹配内置或自定义规则"
            if role == Qt.ItemDataRole.ForegroundRole and col == 0:
                return QColor("#059669") if item.canonical_code else QColor("#94A3B8")
            if role == Qt.ItemDataRole.FontRole and col == 0 and item.canonical_code:
                return self._mono_font
            if role == Qt.ItemDataRole.ToolTipRole:
                return f"原始文本: {item.raw_text}\n清洗后: {item.cleaned_text or '-'}\n路径: {item.full_path or '-'}"
            return None

        # mode == "new" or "batch_dup"
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return item.canonical_code
            if col == 1:
                return item.prefix or "-"
            if col == 2:
                return " / ".join(item.tags) if item.tags else "-"
            if col == 3:
                return item.part or "-"
            if col == 4:
                return item.file_name or item.raw_text
            if col == 5:
                suffix = " (目录回退)" if item.from_parent else ""
                return f"{item.matched_rule}{suffix}" if item.matched_rule else "-"
            if col == 6:
                return item.source_name or item.source_type
        if role == Qt.ItemDataRole.FontRole and col == 0:
            return self._mono_font
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == 0:
                return QColor("#059669") if self._mode == "new" else QColor("#7C3AED")
            if col == 2 and item.tags:
                return QColor("#2563EB")
        if role == Qt.ItemDataRole.ToolTipRole:
            return (
                f"编码: {item.canonical_code}\n"
                f"原始文本: {item.raw_text}\n"
                f"规则: {item.matched_rule} (置信度 {item.confidence:.2f})"
            )
        return None


class CompareTableModel(QAbstractTableModel):
    """Virtualized table model for View 3 (快速查重 — DB Compare & A-vs-B)."""

    def __init__(self, with_matches: bool = False, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self._with_matches = with_matches
        self._rows: list[Any] = []
        self._mono_font = QFont("Consolas", 10)
        self._mono_font.setBold(True)
        if with_matches:
            self._headers = ["标准编码", "输入项", "库内已有记录", "标签", "分集"]
        else:
            self._headers = ["标准编码", "前缀", "标签", "分集", "文件名 / 原始文本", "来源"]

    def set_rows(self, rows: list[Any]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rows(self) -> list[Any]:
        return list(self._rows)

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
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        if not (0 <= row < len(self._rows)):
            return None

        if self._with_matches:
            item, matches = self._rows[row]
            if role == Qt.ItemDataRole.DisplayRole:
                if col == 0:
                    return item.canonical_code
                if col == 1:
                    return item.file_name or item.raw_text
                if col == 2:
                    if not matches:
                        return "-"
                    first = matches[0].file_name or matches[0].raw_text
                    return f"共 {len(matches)} 条：{first}" if len(matches) > 1 else first
                if col == 3:
                    return " / ".join(item.tags) if item.tags else "-"
                if col == 4:
                    return item.part or "-"
            if role == Qt.ItemDataRole.FontRole and col == 0:
                return self._mono_font
            if role == Qt.ItemDataRole.ForegroundRole and col == 0:
                return QColor("#D97706")
            return None

        item: ParseResult = self._rows[row]
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return item.canonical_code or "（未识别）"
            if col == 1:
                return item.prefix or "-"
            if col == 2:
                return " / ".join(item.tags) if item.tags else "-"
            if col == 3:
                return item.part or "-"
            if col == 4:
                return item.file_name or item.raw_text
            if col == 5:
                return item.source_name or item.source_type
        if role == Qt.ItemDataRole.FontRole and col == 0 and item.canonical_code:
            return self._mono_font
        if role == Qt.ItemDataRole.ForegroundRole and col == 0:
            return QColor("#059669") if item.canonical_code else QColor("#94A3B8")
        return None
