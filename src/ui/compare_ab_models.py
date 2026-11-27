"""Virtualized Qt6 table models & helpers for Mode B (Dual-List Coverage Comparison).

Provides ``ChecklistTableModel``, ``MissingOnlyTableModel``,
``MatchedDetailTableModel``, ``OnlyInATableModel``, and ``CoverageBannerWidget``
with Option B Light/Dark theming, zero-emoji status badges, inline hover-copy
roles, and default ``COLUMN_SPECS``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.core.dedup import database_records_to_parse_results
from src.core.models import ChecklistEntry, CompareResult, ParseResult
from src.core.scanner import scan_document_file
from src.ui.table_header import ColumnSpec
from src.ui.table_models import (
    ROLE_BADGE,
    ROLE_COPY_TEXT,
    _ThemedTableModelMixin,
    _make_mono_font,
    _sort_with_persistent_indexes,
)
from src.ui.theme import get_palette
from src.ui.widgets import WorkerThread

_COMPARE_FILE_FILTER: str = (
    "支持的文档与清单 (*.txt *.docx *.xlsx *.xlsm *.csv *.tsv *.efu *.log);;"
    "Word 文档 (*.docx);;Excel 表格 (*.xlsx *.xlsm);;"
    "文本与 CSV (*.txt *.csv *.tsv *.efu *.log);;所有文件 (*.*)"
)
_AB_TEXT_FILE_EXTS: frozenset[str] = frozenset({".txt", ".csv", ".tsv", ".efu", ".log"})
_AB_MAX_DIRECT_PASTE_LINES: int = 5_000
_AB_VISUAL_PREVIEW_LINES: int = 250
_AB_VISUAL_PREVIEW_CHARS: int = 80_000


def _repolish(widget: QWidget) -> None:
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)


def _stop_worker_safely(worker: Optional[WorkerThread], timeout_ms: int = 3000) -> None:
    if worker is None:
        return
    try:
        if hasattr(worker, "safe_stop"):
            worker.safe_stop(timeout_ms)
            return
        worker.cancel()
        for sig_name in ("progress", "succeeded", "failed", "cancelled"):
            sig = getattr(worker, sig_name, None)
            if sig is not None:
                try:
                    sig.disconnect()
                except (RuntimeError, TypeError):
                    pass
        if worker.isRunning():
            worker.wait(timeout_ms)
    except RuntimeError:
        pass


class _DroppablePlainTextEdit(QPlainTextEdit):
    """QPlainTextEdit accepting pasted multi-line text and dropped local files."""

    file_dropped = Signal(str)
    files_dropped = Signal(list)

    def _set_drag_active(self, active: bool) -> None:
        self.setProperty("dragActive", active)
        _repolish(self)

    @staticmethod
    def _extract_local_files(event: Any) -> list[str]:
        if not event.mimeData().hasUrls():
            return []
        return [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile() and Path(url.toLocalFile()).is_file()
        ]

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._extract_local_files(event):
            self._set_drag_active(True)
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragLeaveEvent(self, event: QDragEnterEvent) -> None:
        self._set_drag_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_drag_active(False)
        paths = self._extract_local_files(event)
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)
            self.file_dropped.emit(paths[0])
            return
        super().dropEvent(event)


@dataclass
class _SourceSideState:
    """Tracks the bound data source for List A (Base) or List B (Target)."""

    kind: str = "empty"  # "empty" | "db" | "files" | "text"
    db_path: str = ""
    db_name: str = ""
    db_count: int = 0
    file_paths: list[str] = field(default_factory=list)
    full_text_override: Optional[str] = None
    line_count: int = 0

    def has_source(self, editor_text: str) -> bool:
        if self.kind == "db":
            return self.db_count > 0
        if self.kind == "files":
            return bool(self.file_paths)
        if self.full_text_override is not None:
            return bool(self.full_text_override.strip())
        return bool(editor_text.strip())

    def source_label(self, default_name: str) -> str:
        if self.kind == "db":
            return f"当前总库 ({self.db_name})" if self.db_name else "当前总库"
        if self.kind == "files" and self.file_paths:
            if len(self.file_paths) == 1:
                return Path(self.file_paths[0]).name
            return f"{Path(self.file_paths[0]).name} 等 {len(self.file_paths)} 个文件"
        return default_name


def _build_files_preview_text(paths: list[str]) -> str:
    """Build a fast, non-blocking visual preview for selected files."""
    if len(paths) == 1:
        p = Path(paths[0])
        if p.suffix.lower() in _AB_TEXT_FILE_EXTS:
            lines: list[str] = []
            total_chars = 0
            truncated = False
            try:
                with p.open("r", encoding="utf-8-sig", errors="replace") as fh:
                    for idx, raw in enumerate(fh):
                        if idx >= _AB_VISUAL_PREVIEW_LINES:
                            truncated = True
                            break
                        line = raw.rstrip("\r\n")
                        if total_chars + len(line) > _AB_VISUAL_PREVIEW_CHARS:
                            truncated = True
                            break
                        lines.append(line)
                        total_chars += len(line) + 1
            except OSError:
                return f"[文件] {p.name}"
            if truncated:
                lines.append(f"... 仅预览前 {len(lines)} 行（对比时将在后台解析完整文件：{p.name}）")
            return "\n".join(lines) if lines else f"[空文件] {p.name}"
        return f"[文档文件] {p.name}\n路径：{p}\n（点击「开始覆盖率对比」后将在后台解析全部内容）"

    lines = [f"已选择 {len(paths)} 个文件（对比时将在后台依次解析全部内容）："]
    for idx, item in enumerate(paths[:50], start=1):
        lines.append(f"  {idx}. {Path(item).name}")
    if len(paths) > 50:
        lines.append(f"  ... 另有 {len(paths) - 50} 个文件")
    return "\n".join(lines)


def _format_matched_names(names: Sequence[str], max_show: int = 2) -> str:
    if not names:
        return "-"
    shown = list(names[:max_show])
    extra = len(names) - len(shown)
    joined = " | ".join(shown)
    return f"{joined} (+{extra})" if extra > 0 else joined


def _status_display(status: str) -> str:
    return "已包含" if status == "matched" else ("缺失" if status == "missing" else "未识别")


def _status_badge(status: str) -> tuple[str, str]:
    if status == "matched":
        return ("已包含", "success")
    if status == "missing":
        return ("缺失", "danger")
    return ("未识别", "warning")


def _status_sort_rank(status: str) -> int:
    return 0 if status == "missing" else (1 if status == "matched" else 2)


def _build_checklist_tooltip(entry: ChecklistEntry) -> str:
    lines = [
        f"序号: #{entry.order_index}",
        f"状态: {_status_display(entry.status)}",
        f"标准编码: {entry.canonical_code or '（未识别）'}",
        f"目标原始文本: {entry.target_text or '-'}",
    ]
    if entry.status == "matched":
        lines.append(f"基准A命中数: {entry.matched_a_count}")
        if entry.matched_a_tags:
            lines.append(f"基准A标签: {entry.matched_a_tags}")
        if entry.matched_a_names:
            lines.append("基准A匹配文件:")
            for name in entry.matched_a_names[:10]:
                lines.append(f"  · {name}")
            if len(entry.matched_a_names) > 10:
                lines.append(f"  ... 共 {len(entry.matched_a_names)} 个文件")
    return "\n".join(lines)


class _CoverageThemedModel(_ThemedTableModelMixin, QAbstractTableModel):
    """Base themed model with Option B Light/Dark colors and row/header boilerplate."""

    COLUMN_SPECS: list[ColumnSpec] = []
    HEADERS: list[str] = []
    DEFAULT_WIDTHS: list[int] = []

    def __init__(self, parent: Optional[Any] = None, theme: str = "light") -> None:
        super().__init__(parent)
        self._init_theme_colors(theme)
        self._mono_font = _make_mono_font(10, bold=True)
        self._bold_font = QFont()
        self._bold_font.setBold(True)

    def _row_store(self) -> list[Any]:
        raise NotImplementedError

    def _refresh_theme_colors(self) -> None:
        super()._refresh_theme_colors()
        p = get_palette(self._theme)
        is_dark = self._theme == "dark"
        self._color_status_matched = QColor(p.get("success", "#4ADE80" if is_dark else "#15803D"))
        self._color_status_missing = QColor(p.get("danger", "#FB7185" if is_dark else "#B91C1C"))
        self._color_status_unrec = QColor(p.get("warning", "#FBBF24" if is_dark else "#B45309"))
        self._bg_matched = QBrush(
            QColor(p.get("row_tint_matched", "#111E17" if is_dark else "#F6FBF7"))
        )
        self._bg_missing = QBrush(
            QColor(p.get("row_tint_missing", "#221417" if is_dark else "#FEF7F7"))
        )
        self._bg_unrec = QBrush(
            QColor(p.get("warning_soft", "#261E0F" if is_dark else "#FFFBEB"))
        )

    def set_theme(self, theme: str) -> None:
        normalized = "dark" if theme == "dark" else "light"
        if normalized == self._theme:
            return
        self._theme = normalized
        self._refresh_theme_colors()
        rows, cols = self.rowCount(), self.columnCount()
        if rows > 0 and cols > 0:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(rows - 1, cols - 1),
                [Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.BackgroundRole, ROLE_BADGE],
            )

    def column_specs(self) -> list[ColumnSpec]:
        return list(self.COLUMN_SPECS)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._row_store())

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


class ChecklistTableModel(_CoverageThemedModel):
    """Virtualized Target List B checklist with instant status filter, live search, and tinting."""

    # Cols 0..5 exact for test_ui_smoke.py; col 6 exposes 基准A标签 hidden by default.
    COLUMN_SPECS: list[ColumnSpec] = [
        ColumnSpec("order", "序号", 62, True, 48),
        ColumnSpec("status", "状态", 94, True, 76),
        ColumnSpec("code", "标准编码", 142, True, 100, lock_visible=True),
        ColumnSpec("target_text", "目标原始文本", 250, True, 130, stretch_weight=0.5),
        ColumnSpec("hit_count", "基准A命中数", 96, True, 76),
        ColumnSpec("matched_names", "基准A匹配文件名", 260, True, 130, stretch_weight=0.5),
        ColumnSpec("matched_tags", "基准A标签", 120, False, 76, category="advanced"),
    ]
    HEADERS = [s.label for s in COLUMN_SPECS]
    DEFAULT_WIDTHS = [s.default_width for s in COLUMN_SPECS]

    def __init__(self, parent: Optional[Any] = None, theme: str = "light") -> None:
        super().__init__(parent=parent, theme=theme)
        self._all_entries: list[ChecklistEntry] = []
        self._visible_entries: list[ChecklistEntry] = []
        self._status_filter: str = "all"
        self._search_query: str = ""
        self._sort_column: int = -1
        self._sort_order: Qt.SortOrder = Qt.SortOrder.AscendingOrder

    def _row_store(self) -> list[ChecklistEntry]:
        return self._visible_entries

    def set_entries(self, entries: Sequence[ChecklistEntry], *, reset_filter: bool = False) -> None:
        self._all_entries = list(entries)
        if reset_filter:
            self._status_filter = "all"
            self._search_query = ""
        self._sort_column = -1
        self._rebuild_visible()

    def set_status_filter(self, status_filter: str) -> None:
        norm = (status_filter or "all").strip().lower()
        if norm not in {"all", "missing", "matched"}:
            norm = "all"
        if norm != self._status_filter:
            self._status_filter = norm
            self._rebuild_visible()

    def status_filter(self) -> str:
        return self._status_filter

    def set_search_query(self, query: str) -> None:
        norm = (query or "").strip()
        if norm != self._search_query:
            self._search_query = norm
            self._rebuild_visible()

    def search_query(self) -> str:
        return self._search_query

    def counts(self) -> tuple[int, int, int]:
        total = len(self._all_entries)
        matched = sum(1 for e in self._all_entries if e.status == "matched")
        missing = sum(1 for e in self._all_entries if e.status == "missing")
        return total, matched, missing

    def entries(self) -> list[ChecklistEntry]:
        return list(self._visible_entries)

    def all_entries(self) -> list[ChecklistEntry]:
        return list(self._all_entries)

    def entry_at(self, row: int) -> Optional[ChecklistEntry]:
        return self._visible_entries[row] if 0 <= row < len(self._visible_entries) else None

    def _matches_filter(self, entry: ChecklistEntry, q: str) -> bool:
        if self._status_filter != "all" and entry.status != self._status_filter:
            return False
        if not q:
            return True
        if q in (entry.canonical_code or "").lower() or q in (entry.target_text or "").lower():
            return True
        if entry.matched_a_tags and q in entry.matched_a_tags.lower():
            return True
        return any(q in name.lower() for name in entry.matched_a_names)

    def _rebuild_visible(self) -> None:
        q = self._search_query.lower()
        filtered = [e for e in self._all_entries if self._matches_filter(e, q)]
        if self._sort_column >= 0:
            filtered.sort(
                key=self._sort_key_for_column(self._sort_column),
                reverse=self._sort_order == Qt.SortOrder.DescendingOrder,
            )
        self.beginResetModel()
        self._visible_entries = filtered
        self.endResetModel()

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._visible_entries)):
            return None
        entry = self._visible_entries[index.row()]
        col = index.column()

        if role == ROLE_COPY_TEXT:
            return entry.canonical_code if col == 2 and entry.canonical_code else None
        if role == ROLE_BADGE:
            return _status_badge(entry.status) if col == 1 else None
        if role == Qt.ItemDataRole.DisplayRole:
            vals = (
                f"#{entry.order_index}",
                _status_display(entry.status),
                entry.canonical_code or "（未识别）",
                entry.target_text,
                str(entry.matched_a_count) if entry.matched_a_count > 0 else "0",
                _format_matched_names(entry.matched_a_names, max_show=2)
                if entry.status == "matched"
                else "-",
                entry.matched_a_tags or "-",
            )
            return vals[col] if 0 <= col < len(vals) else None
        if role == Qt.ItemDataRole.BackgroundRole:
            if entry.status == "matched":
                return self._bg_matched
            return self._bg_missing if entry.status == "missing" else self._bg_unrec
        if role == Qt.ItemDataRole.ForegroundRole:
            if col in (1, 2):
                if entry.status == "matched":
                    return self._color_status_matched
                if entry.status == "missing":
                    return self._color_status_missing
                return self._color_status_unrec if col == 1 else self._color_muted
            if (col == 4 and entry.matched_a_count == 0) or (col in (5, 6) and entry.status != "matched"):
                return self._color_muted
            if col == 6 and entry.matched_a_tags:
                return self._color_accent
        if role == Qt.ItemDataRole.FontRole:
            if col == 1:
                return self._bold_font
            if col == 2 and entry.canonical_code:
                return self._mono_font
        if role == Qt.ItemDataRole.TextAlignmentRole:
            align = Qt.AlignmentFlag.AlignCenter if col in (0, 1, 4) else Qt.AlignmentFlag.AlignLeft
            return int(align | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            return _build_checklist_tooltip(entry)
        return None

    @staticmethod
    def _sort_key_for_column(column: int):
        keys = {
            0: lambda e: e.order_index,
            1: lambda e: (_status_sort_rank(e.status), e.order_index),
            2: lambda e: ((e.canonical_code or "").lower(), e.order_index),
            3: lambda e: ((e.target_text or "").lower(), e.order_index),
            4: lambda e: (e.matched_a_count, e.order_index),
            5: lambda e: (_format_matched_names(e.matched_a_names, max_show=2).lower(), e.order_index),
            6: lambda e: ((e.matched_a_tags or "").lower(), e.order_index),
        }
        return keys.get(column, keys[0])

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        if column < 0:
            return
        self._sort_column = column
        self._sort_order = order
        _sort_with_persistent_indexes(
            self,
            self._visible_entries,
            self._sort_key_for_column(column),
            order == Qt.SortOrder.DescendingOrder,
        )


class MissingOnlyTableModel(_CoverageThemedModel):
    """Virtualized table model displaying pure missing items (Target B missing from Base A)."""

    COLUMN_SPECS: list[ColumnSpec] = [
        ColumnSpec("order", "序号", 64, True, 48),
        ColumnSpec("code", "标准编码", 148, True, 100, lock_visible=True),
        ColumnSpec("target_text", "目标原始文本", 360, True, 140, stretch_weight=1.0),
        ColumnSpec("source", "来源", 165, True, 90),
    ]
    HEADERS = [s.label for s in COLUMN_SPECS]
    DEFAULT_WIDTHS = [s.default_width for s in COLUMN_SPECS]

    def __init__(self, parent: Optional[Any] = None, theme: str = "light") -> None:
        super().__init__(parent=parent, theme=theme)
        self._entries: list[ChecklistEntry] = []
        self._source_b_name: str = ""

    def _row_store(self) -> list[ChecklistEntry]:
        return self._entries

    def set_entries(self, entries: Sequence[ChecklistEntry], *, source_b_name: str = "") -> None:
        self.beginResetModel()
        self._entries = list(entries)
        self._source_b_name = source_b_name
        self.endResetModel()

    def entries(self) -> list[ChecklistEntry]:
        return list(self._entries)

    def entry_at(self, row: int) -> Optional[ChecklistEntry]:
        return self._entries[row] if 0 <= row < len(self._entries) else None

    def _source_label(self, entry: ChecklistEntry) -> str:
        if entry.target_item and entry.target_item.source_name:
            return entry.target_item.source_name
        return self._source_b_name or "目标清单B"

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._entries)):
            return None
        entry = self._entries[index.row()]
        col = index.column()

        if role == ROLE_COPY_TEXT:
            return entry.canonical_code if col == 1 and entry.canonical_code else None
        if role == Qt.ItemDataRole.DisplayRole:
            vals = (f"#{entry.order_index}", entry.canonical_code, entry.target_text, self._source_label(entry))
            return vals[col] if 0 <= col < len(vals) else None
        if role == Qt.ItemDataRole.FontRole and col == 1:
            return self._mono_font
        if role == Qt.ItemDataRole.ForegroundRole and col == 1:
            return self._color_danger
        if role == Qt.ItemDataRole.TextAlignmentRole:
            align = Qt.AlignmentFlag.AlignCenter if col == 0 else Qt.AlignmentFlag.AlignLeft
            return int(align | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            return _build_checklist_tooltip(entry)
        return None

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        if column < 0:
            return
        keys = {
            0: lambda e: e.order_index,
            1: lambda e: ((e.canonical_code or "").lower(), e.order_index),
            2: lambda e: ((e.target_text or "").lower(), e.order_index),
            3: lambda e: (self._source_label(e).lower(), e.order_index),
        }
        _sort_with_persistent_indexes(
            self, self._entries, keys.get(column, keys[0]), order == Qt.SortOrder.DescendingOrder
        )


class MatchedDetailTableModel(_CoverageThemedModel):
    """Virtualized table model showing matched items (A ∩ B) with Base A filenames & optional tags."""

    COLUMN_SPECS: list[ColumnSpec] = [
        ColumnSpec("order", "序号", 62, True, 48),
        ColumnSpec("code", "标准编码", 142, True, 100, lock_visible=True),
        ColumnSpec("target_text", "目标原始文本", 240, True, 130, stretch_weight=0.45),
        ColumnSpec("hit_count", "基准A命中数", 94, True, 76),
        ColumnSpec("matched_names", "基准A匹配文件名", 270, True, 140, stretch_weight=0.55),
        ColumnSpec("matched_tags", "基准A标签", 120, False, 76, category="advanced"),
    ]
    HEADERS = [s.label for s in COLUMN_SPECS]
    DEFAULT_WIDTHS = [s.default_width for s in COLUMN_SPECS]

    def __init__(self, parent: Optional[Any] = None, theme: str = "light") -> None:
        super().__init__(parent=parent, theme=theme)
        self._entries: list[ChecklistEntry] = []

    def _row_store(self) -> list[ChecklistEntry]:
        return self._entries

    def set_entries(self, entries: Sequence[ChecklistEntry]) -> None:
        self.beginResetModel()
        self._entries = list(entries)
        self.endResetModel()

    def entries(self) -> list[ChecklistEntry]:
        return list(self._entries)

    def entry_at(self, row: int) -> Optional[ChecklistEntry]:
        return self._entries[row] if 0 <= row < len(self._entries) else None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._entries)):
            return None
        entry = self._entries[index.row()]
        col = index.column()

        if role == ROLE_COPY_TEXT:
            return entry.canonical_code if col == 1 and entry.canonical_code else None
        if role == Qt.ItemDataRole.DisplayRole:
            vals = (
                f"#{entry.order_index}",
                entry.canonical_code,
                entry.target_text,
                str(entry.matched_a_count),
                _format_matched_names(entry.matched_a_names, max_show=3),
                entry.matched_a_tags or "-",
            )
            return vals[col] if 0 <= col < len(vals) else None
        if role == Qt.ItemDataRole.FontRole and col == 1:
            return self._mono_font
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == 1:
                return self._color_status_matched
            if col == 5 and entry.matched_a_tags:
                return self._color_accent
        if role == Qt.ItemDataRole.TextAlignmentRole:
            align = Qt.AlignmentFlag.AlignCenter if col in (0, 3) else Qt.AlignmentFlag.AlignLeft
            return int(align | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            return _build_checklist_tooltip(entry)
        return None

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        if column < 0:
            return
        keys = {
            0: lambda e: e.order_index,
            1: lambda e: ((e.canonical_code or "").lower(), e.order_index),
            2: lambda e: ((e.target_text or "").lower(), e.order_index),
            3: lambda e: (e.matched_a_count, e.order_index),
            4: lambda e: (" | ".join(e.matched_a_names).lower(), e.order_index),
            5: lambda e: ((e.matched_a_tags or "").lower(), e.order_index),
        }
        _sort_with_persistent_indexes(
            self, self._entries, keys.get(column, keys[0]), order == Qt.SortOrder.DescendingOrder
        )


class OnlyInATableModel(_CoverageThemedModel):
    """Virtualized table model for items present only in Base List A."""

    COLUMN_SPECS: list[ColumnSpec] = [
        ColumnSpec("code", "标准编码", 142, True, 100, lock_visible=True),
        ColumnSpec("prefix", "前缀", 76, False, 56, category="advanced"),
        ColumnSpec("tags", "版本标签", 110, False, 72, category="advanced"),
        ColumnSpec("part", "分集", 68, False, 54, category="advanced"),
        ColumnSpec("file_name", "文件名 / 原始文本", 320, True, 140, stretch_weight=1.0),
        ColumnSpec("source", "来源", 165, True, 90),
    ]
    HEADERS = [s.label for s in COLUMN_SPECS]
    DEFAULT_WIDTHS = [s.default_width for s in COLUMN_SPECS]

    def __init__(self, parent: Optional[Any] = None, theme: str = "light") -> None:
        super().__init__(parent=parent, theme=theme)
        self._rows: list[ParseResult] = []

    def _row_store(self) -> list[ParseResult]:
        return self._rows

    def set_rows(self, rows: Sequence[ParseResult]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rows(self) -> list[ParseResult]:
        return list(self._rows)

    def row_at(self, row: int) -> Optional[ParseResult]:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        item = self._rows[index.row()]
        col = index.column()

        if role == ROLE_COPY_TEXT:
            return item.canonical_code if col == 0 and item.canonical_code else None
        if role == Qt.ItemDataRole.DisplayRole:
            vals = (
                item.canonical_code or "（未识别）",
                item.prefix or "-",
                " / ".join(item.tags) if item.tags else "-",
                item.part or "-",
                item.file_name or item.raw_text,
                item.source_name or item.source_type or "-",
            )
            return vals[col] if 0 <= col < len(vals) else None
        if role == Qt.ItemDataRole.FontRole and col == 0 and item.canonical_code:
            return self._mono_font
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == 0:
                return self._color_accent if item.canonical_code else self._color_muted
            if col == 2 and item.tags:
                return self._color_accent
            if col == 3 and item.part:
                return self._color_purple
        if role == Qt.ItemDataRole.TextAlignmentRole:
            align = Qt.AlignmentFlag.AlignCenter if col in (1, 3) else Qt.AlignmentFlag.AlignLeft
            return int(align | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            parts = [
                f"标准编码: {item.canonical_code or '（未识别）'}",
                f"文件名 / 原始文本: {item.file_name or item.raw_text}",
            ]
            if item.tags:
                parts.append(f"版本标签: {', '.join(item.tags)}")
            if item.source_name:
                parts.append(f"来源: {item.source_name}")
            return "\n".join(parts)
        return None

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        if column < 0:
            return
        keys = {
            0: lambda it: (it.canonical_code or "").lower(),
            1: lambda it: (it.prefix or "").lower(),
            2: lambda it: " / ".join(it.tags).lower(),
            3: lambda it: (it.part or "").lower(),
            4: lambda it: (it.file_name or it.raw_text or "").lower(),
            5: lambda it: (it.source_name or it.source_type or "").lower(),
        }
        _sort_with_persistent_indexes(
            self, self._rows, keys.get(column, keys[0]), order == Qt.SortOrder.DescendingOrder
        )


def build_ab_table_view(
    model: Any,
    widths: list[int],
    *,
    alternating: bool,
    menu_callback: Any,
) -> QTableView:
    """Create a virtualized QTableView configured for Mode B results."""
    tv = QTableView()
    tv.setModel(model)
    tv.setAlternatingRowColors(alternating)
    tv.setShowGrid(False)
    tv.setWordWrap(False)
    tv.setSortingEnabled(False)
    tv.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
    tv.setSortingEnabled(True)
    tv.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    tv.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    tv.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    tv.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    tv.customContextMenuRequested.connect(lambda pos, t=tv: menu_callback(t, pos))
    tv.verticalHeader().setDefaultSectionSize(34)
    tv.verticalHeader().setVisible(False)
    tv.horizontalHeader().setStretchLastSection(False)
    tv.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    for idx, width in enumerate(widths):
        if idx < model.columnCount():
            tv.setColumnWidth(idx, width)
    return tv


def load_ab_side_items(
    ctx: Any,
    payload: dict[str, Any],
    *,
    match_mode: str,
    ignore_comp: bool,
    comp_exts: set[str],
    cancel_check: Any,
) -> Optional[list[ParseResult]]:
    """Load and parse one side (DB / multi-file / pasted text) off the UI thread."""
    kind, source_name = payload["kind"], payload["source_name"]
    if kind == "db":
        return None if cancel_check() else database_records_to_parse_results(ctx.db)

    if kind == "files":
        all_items: list[ParseResult] = []
        for fpath in payload["file_paths"]:
            if cancel_check():
                return None
            p = Path(fpath)
            scanned = scan_document_file(p, companion_extensions=comp_exts)
            if cancel_check():
                return None
            if match_mode == "exact_line":
                all_items.extend(
                    ParseResult(
                        raw_text=sc.raw_text,
                        file_name=sc.file_name or sc.raw_text,
                        source_name=sc.source_name or p.name,
                        source_type=sc.source_type,
                        line_number=sc.line_number,
                    )
                    for sc in scanned
                    if not (ignore_comp and sc.is_companion)
                )
            else:
                parsed = ctx.parser.parse_items(
                    scanned,
                    ignore_companions=ignore_comp,
                    companion_extensions=comp_exts,
                    cancel_check=cancel_check,
                )
                if cancel_check():
                    return None
                all_items.extend(parsed)
        return all_items

    text = payload["text"]
    if match_mode == "exact_line":
        return [
            ParseResult(
                raw_text=ln, file_name=ln, source_name=source_name, source_type="paste", line_number=idx
            )
            for idx, ln in enumerate(text.splitlines(), start=1)
            if ln.strip()
        ]
    return ctx.parser.parse_text(
        text, source_name=source_name, ignore_companions=ignore_comp, companion_extensions=comp_exts
    )


class CoverageBannerWidget(QFrame):
    """Compact zero-emoji Linear/Obsidian Pro coverage strip with inline stats & progress bar."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("CoverageBanner")
        self.setProperty("coverageState", "idle")

        b_layout = QVBoxLayout(self)
        b_layout.setContentsMargins(12, 8, 12, 8)
        b_layout.setSpacing(6)

        top_row = QHBoxLayout()
        top_row.setSpacing(10)

        self.lbl_percent = QLabel("--")
        self.lbl_percent.setObjectName("CoveragePercent")
        self.lbl_percent.setProperty("coverageLevel", "idle")
        top_row.addWidget(self.lbl_percent, 0, Qt.AlignmentFlag.AlignVCenter)

        self.lbl_verdict = QLabel("等待对比 — 载入已有基准清单 A 与目标核对清单 B 后开始核验")
        self.lbl_verdict.setObjectName("CoverageVerdict")
        self.lbl_verdict.setWordWrap(False)
        top_row.addWidget(self.lbl_verdict, 1, Qt.AlignmentFlag.AlignVCenter)

        self.stat_b_total = self._make_pill("目标", "0", tone="true")
        self.stat_b_matched = self._make_pill("已包含", "0", tone="success")
        self.stat_b_missing = self._make_pill("缺失", "0", tone="danger")
        self.stat_a_only = self._make_pill("仅A", "0", tone="accent")
        for frame, _ in (
            self.stat_b_total,
            self.stat_b_matched,
            self.stat_b_missing,
            self.stat_a_only,
        ):
            top_row.addWidget(frame, 0, Qt.AlignmentFlag.AlignVCenter)
        b_layout.addLayout(top_row)

        self.coverage_bar = QProgressBar()
        self.coverage_bar.setObjectName("CoverageBar")
        self.coverage_bar.setProperty("coverageLevel", "idle")
        self.coverage_bar.setRange(0, 1000)
        self.coverage_bar.setValue(0)
        self.coverage_bar.setFixedHeight(6)
        self.coverage_bar.setTextVisible(False)
        b_layout.addWidget(self.coverage_bar)

    @staticmethod
    def _make_pill(title: str, initial_val: str, *, tone: str = "true") -> tuple[QFrame, QLabel]:
        frame = QFrame()
        frame.setProperty("coverageStat", tone)
        fl = QHBoxLayout(frame)
        fl.setContentsMargins(8, 2, 8, 2)
        fl.setSpacing(5)
        lbl_t = QLabel(title)
        lbl_t.setProperty("coverageStatTitle", True)
        fl.addWidget(lbl_t)
        lbl_v = QLabel(initial_val)
        lbl_v.setProperty("coverageStatValue", tone)
        fl.addWidget(lbl_v)
        return frame, lbl_v

    def update_from_result(self, result: CompareResult) -> None:
        rate, total_b, hit_b, missing_b = (
            result.coverage_rate,
            result.unique_b_count,
            result.hit_b_count,
            result.missing_b_count,
        )
        if total_b <= 0:
            level, state, pct_text, bar_val = "idle", "idle", "0.0%", 0
            verdict = "目标清单 B 未识别到有效比对项（可切换为精确文本行比对）"
        elif result.is_fully_covered:
            level, state, pct_text, bar_val = "full", "full", "100.0%", 1000
            verdict = f"目标清单已全部包含 · 覆盖率 100.0%（{hit_b:,} / {total_b:,}，无缺失）"
        else:
            state = "partial"
            level = "high" if rate >= 70.0 else ("mid" if rate >= 30.0 else "low")
            verdict = (
                f"覆盖率 {rate:.1f}% · 已包含 {hit_b:,} / {total_b:,} · "
                f"缺失 {missing_b:,} 项（基准 A {result.unique_a_count:,} 项）"
            )
            if result.unrecognized_b:
                verdict += f" · 未识别 {len(result.unrecognized_b):,}"
            pct_text, bar_val = f"{rate:.1f}%", max(0, min(1000, int(round(rate * 10))))

        self.lbl_verdict.setText(verdict)
        self.lbl_verdict.setToolTip(verdict)
        self.lbl_verdict.setProperty("coverageState", state)
        self.setProperty("coverageState", state)
        self.lbl_percent.setText(pct_text)
        self.lbl_percent.setProperty("coverageLevel", level)
        self.coverage_bar.setValue(bar_val)
        self.coverage_bar.setProperty("coverageLevel", level)
        self.repolish_all()

        self.stat_b_total[1].setText(f"{total_b:,}")
        self.stat_b_matched[1].setText(f"{hit_b:,}")
        self.stat_b_missing[1].setText(f"{missing_b:,}")
        self.stat_a_only[1].setText(f"{len(result.only_in_a):,}")

    def repolish_all(self) -> None:
        for w in (self, self.lbl_verdict, self.lbl_percent, self.coverage_bar):
            _repolish(w)
        for frame, lbl_v in (
            self.stat_b_total,
            self.stat_b_matched,
            self.stat_b_missing,
            self.stat_a_only,
        ):
            _repolish(frame)
            _repolish(lbl_v)
