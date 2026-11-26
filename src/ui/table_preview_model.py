"""High-performance virtualized table model for dynamic-column DataTable views.

Supports arbitrary headers/rows, instant substring search filtering,
numeric-aware column sorting, and semantic row tinting adapted for both
Light and Dark themes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor, QFont

if TYPE_CHECKING:
    from src.core.table_data import DataTable


# Subtle semantic row background & key-column foreground colors per theme
_LIGHT_ROW_TINTS: dict[str, str] = {
    "duplicate": "#FEF3C7",
    "cross_dup": "#FEF3C7",
    "intra_dup": "#F3E8FF",
    "intersection": "#DCFCE7",
    "dropped": "#FEE2E2",
    "info": "#DBEAFE",
    "group_alt": "#F8FAFC",
}

_DARK_ROW_TINTS: dict[str, str] = {
    "duplicate": "#3B2809",
    "cross_dup": "#3B2809",
    "intra_dup": "#2D1B4E",
    "intersection": "#0B3B2C",
    "dropped": "#3F1219",
    "info": "#172E54",
    "group_alt": "#1A2538",
}

_LIGHT_KEY_FG: dict[str, str] = {
    "duplicate": "#B45309",
    "cross_dup": "#B45309",
    "intra_dup": "#6D28D9",
    "intersection": "#047857",
    "dropped": "#B91C1C",
    "info": "#1D4ED8",
    "default": "#2563EB",
}

_DARK_KEY_FG: dict[str, str] = {
    "duplicate": "#FBBF24",
    "cross_dup": "#FBBF24",
    "intra_dup": "#C4B5FD",
    "intersection": "#34D399",
    "dropped": "#F87171",
    "info": "#60A5FA",
    "default": "#60A5FA",
}


def _try_float(text: str) -> Optional[float]:
    """Attempt fast numeric parse for natural column sorting."""
    s = text.strip()
    if not s:
        return None
    # Reject strings with leading zeros like "00123" so IDs sort lexicographically
    if len(s) > 1 and s[0] == "0" and s[1].isdigit():
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _sort_key_for_cell(val: str) -> tuple[int, Any]:
    """Produce a comparable tuple sorting numbers before text and blanks last."""
    s = val.strip()
    if not s or s == "-":
        return (2, "")
    num = _try_float(s)
    if num is not None:
        return (0, num)
    return (1, s.lower())


class DynamicTableModel(QAbstractTableModel):
    """Virtualized Qt6 table model for dynamic-column DataTable & report tables."""

    def __init__(
        self,
        theme: str = "light",
        parent: Optional[Any] = None,
    ) -> None:
        super().__init__(parent)
        self._theme: str = "dark" if theme == "dark" else "light"
        self._headers: list[str] = []
        self._rows: list[list[str]] = []
        self._search_cache: list[str] = []
        self._visible_indices: list[int] = []
        self._filter_query: str = ""
        self._row_tints: dict[int, str] = {}
        self._highlight_columns: set[int] = set()
        self._mono_columns: set[int] = set()
        self._center_columns: set[int] = set()
        self._sort_col: int = -1
        self._sort_order: Qt.SortOrder = Qt.SortOrder.AscendingOrder

        self._mono_font = QFont("Consolas", 10)
        self._mono_font.setBold(True)

    # ------------------------------------------------------------------
    # Data population & configuration
    # ------------------------------------------------------------------

    def set_theme(self, theme: str) -> None:
        """Update Light/Dark palette and repaint tinted rows immediately."""
        normalized = "dark" if theme == "dark" else "light"
        if normalized == self._theme:
            return
        self._theme = normalized
        if self._visible_indices and self._headers:
            top_left = self.index(0, 0)
            bottom_right = self.index(len(self._visible_indices) - 1, len(self._headers) - 1)
            self.dataChanged.emit(
                top_left,
                bottom_right,
                [Qt.ItemDataRole.BackgroundRole, Qt.ItemDataRole.ForegroundRole],
            )

    def set_table(
        self,
        table: Optional["DataTable"],
        row_tints: Optional[dict[int, str]] = None,
        highlight_columns: Optional[set[int]] = None,
        mono_columns: Optional[set[int]] = None,
        center_columns: Optional[set[int]] = None,
    ) -> None:
        """Bind a DataTable object (or None to clear)."""
        if table is None:
            self.clear()
            return
        self.set_data(
            headers=list(table.headers),
            rows=table.rows,
            row_tints=row_tints,
            highlight_columns=highlight_columns,
            mono_columns=mono_columns,
            center_columns=center_columns,
        )

    def set_data(
        self,
        headers: list[str],
        rows: list[list[Any]],
        row_tints: Optional[dict[int, str]] = None,
        highlight_columns: Optional[set[int]] = None,
        mono_columns: Optional[set[int]] = None,
        center_columns: Optional[set[int]] = None,
    ) -> None:
        """Populate model with arbitrary headers and rows."""
        self.beginResetModel()
        col_count = len(headers)
        self._headers = [str(h) for h in headers]
        normalized_rows: list[list[str]] = []
        search_cache: list[str] = []

        for raw_row in rows:
            cells = ["" if v is None else str(v) for v in raw_row]
            if len(cells) < col_count:
                cells.extend([""] * (col_count - len(cells)))
            elif len(cells) > col_count and col_count > 0:
                cells = cells[:col_count]
            normalized_rows.append(cells)
            search_cache.append("\x1f".join(cells).lower())

        self._rows = normalized_rows
        self._search_cache = search_cache
        self._row_tints = dict(row_tints) if row_tints else {}
        self._highlight_columns = set(highlight_columns) if highlight_columns else set()
        self._mono_columns = set(mono_columns) if mono_columns else set()
        self._center_columns = set(center_columns) if center_columns else set()
        self._sort_col = -1
        self._rebuild_visible_indices()
        self.endResetModel()

    def clear(self) -> None:
        """Reset model to empty state."""
        self.beginResetModel()
        self._headers = []
        self._rows = []
        self._search_cache = []
        self._visible_indices = []
        self._row_tints = {}
        self._highlight_columns = set()
        self._mono_columns = set()
        self._center_columns = set()
        self._sort_col = -1
        self.endResetModel()

    def set_highlighted_rows(self, row_tints: dict[int, str]) -> None:
        """Update semantic row tint mapping {original_row_idx: tone}."""
        self._row_tints = dict(row_tints)
        if self._visible_indices and self._headers:
            top_left = self.index(0, 0)
            bottom_right = self.index(len(self._visible_indices) - 1, len(self._headers) - 1)
            self.dataChanged.emit(
                top_left,
                bottom_right,
                [Qt.ItemDataRole.BackgroundRole, Qt.ItemDataRole.ForegroundRole],
            )

    def set_filter(self, query: str) -> None:
        """Filter visible rows by space-separated case-insensitive keywords."""
        cleaned = (query or "").strip().lower()
        if cleaned == self._filter_query:
            return
        self._filter_query = cleaned
        self.beginResetModel()
        self._rebuild_visible_indices()
        if self._sort_col >= 0 and self._sort_col < len(self._headers):
            self._apply_sort_in_place(self._sort_col, self._sort_order)
        self.endResetModel()

    def _rebuild_visible_indices(self) -> None:
        if not self._filter_query:
            self._visible_indices = list(range(len(self._rows)))
            return
        terms = [t for t in self._filter_query.split() if t]
        if not terms:
            self._visible_indices = list(range(len(self._rows)))
            return
        if len(terms) == 1:
            needle = terms[0]
            self._visible_indices = [
                idx for idx, blob in enumerate(self._search_cache) if needle in blob
            ]
        else:
            self._visible_indices = [
                idx
                for idx, blob in enumerate(self._search_cache)
                if all(t in blob for t in terms)
            ]

    # ------------------------------------------------------------------
    # Inspection & export helpers
    # ------------------------------------------------------------------

    def headers(self) -> list[str]:
        return list(self._headers)

    def total_row_count(self) -> int:
        return len(self._rows)

    def filtered_row_count(self) -> int:
        return len(self._visible_indices)

    def original_row_index(self, view_row: int) -> int:
        if 0 <= view_row < len(self._visible_indices):
            return self._visible_indices[view_row]
        return -1

    def row_values(self, view_row: int) -> list[str]:
        orig = self.original_row_index(view_row)
        if orig >= 0:
            return list(self._rows[orig])
        return []

    def visible_rows(self) -> list[list[str]]:
        return [list(self._rows[i]) for i in self._visible_indices]

    def all_rows(self) -> list[list[str]]:
        return [list(r) for r in self._rows]

    # ------------------------------------------------------------------
    # QAbstractTableModel interface
    # ------------------------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._visible_indices)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._headers)

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
                if 0 <= section < len(self._visible_indices):
                    return str(self._visible_indices[section] + 1)
                return str(section + 1)
        if role == Qt.ItemDataRole.ToolTipRole and orientation == Qt.Orientation.Horizontal:
            if 0 <= section < len(self._headers):
                return f"列 {section + 1}: {self._headers[section]}"
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        view_row = index.row()
        col = index.column()
        if not (0 <= view_row < len(self._visible_indices)) or not (0 <= col < len(self._headers)):
            return None

        orig_row = self._visible_indices[view_row]
        row_cells = self._rows[orig_row]
        value = row_cells[col] if col < len(row_cells) else ""

        if role == Qt.ItemDataRole.DisplayRole:
            if "\n" in value:
                return value.replace("\r\n", " ").replace("\n", " ↵ ")
            return value

        if role == Qt.ItemDataRole.FontRole:
            if col in self._mono_columns and value and value != "-":
                return self._mono_font
            return None

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in self._center_columns:
                return int(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.BackgroundRole:
            tone = self._row_tints.get(orig_row)
            if tone:
                palette = _DARK_ROW_TINTS if self._theme == "dark" else _LIGHT_ROW_TINTS
                hex_color = palette.get(tone)
                if hex_color:
                    return QBrush(QColor(hex_color))
            return None

        if role == Qt.ItemDataRole.ForegroundRole:
            if col in self._highlight_columns and value and value != "-":
                tone = self._row_tints.get(orig_row, "default")
                fg_palette = _DARK_KEY_FG if self._theme == "dark" else _LIGHT_KEY_FG
                hex_fg = fg_palette.get(tone, fg_palette["default"])
                return QBrush(QColor(hex_fg))
            return None

        if role == Qt.ItemDataRole.ToolTipRole:
            if len(value) > 28 or "\n" in value:
                return f"{self._headers[col]}:\n{value}"
            return None

        return None

    def sort(
        self,
        column: int,
        order: Qt.SortOrder = Qt.SortOrder.AscendingOrder,
    ) -> None:
        if not (0 <= column < len(self._headers)) or not self._visible_indices:
            return
        self._sort_col = column
        self._sort_order = order
        self.layoutAboutToBeChanged.emit()
        self._apply_sort_in_place(column, order)
        self.layoutChanged.emit()

    def _apply_sort_in_place(self, column: int, order: Qt.SortOrder) -> None:
        reverse = order == Qt.SortOrder.DescendingOrder
        rows = self._rows
        self._visible_indices.sort(
            key=lambda orig_idx: _sort_key_for_cell(
                rows[orig_idx][column] if column < len(rows[orig_idx]) else ""
            ),
            reverse=reverse,
        )
