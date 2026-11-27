"""Reusable qBittorrent-style customizable table header engine for 数据查重程序.

Provides drag-to-reorder columns, interactive width resizing, right-click column
visibility menus (separating basic and advanced fields with >=1 visible guard),
fast O(60-row) sampled auto-fit, forward-compatible ``known_cols`` schema
evolution, safe Qt6 sort restoration without spurious column-0 sorts, and
400ms debounced atomic persistence to ``config.json``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence
from weakref import WeakSet

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QFontMetrics
from PySide6.QtWidgets import QHeaderView, QMenu, QTableView, QWidget

from src.core.config import TableLayoutState

try:
    from src.ui.delegates import ROLE_BADGE, ROLE_COPY_TEXT
except ImportError:
    ROLE_BADGE = int(Qt.ItemDataRole.UserRole) + 40
    ROLE_COPY_TEXT = int(Qt.ItemDataRole.UserRole) + 41

_MIN_COL_WIDTH: int = 36
_MAX_COL_WIDTH: int = 1600
_MAX_AUTOFIT_WIDTH: int = 460
_DEBOUNCE_SAVE_MS: int = 400
_SAMPLE_SLICE: int = 20


@dataclass(frozen=True)
class ColumnSpec:
    """Declarative specification for one logical column in a managed QTableView."""

    key: str
    label: str
    default_width: int = 120
    default_visible: bool = True
    min_width: int = 44
    sortable: bool = True
    lock_visible: bool = False
    stretch_weight: float = 0.0
    category: str = "basic"  # "basic" | "advanced"


def _clamp_width(width: int, min_width: int = _MIN_COL_WIDTH) -> int:
    floor = max(_MIN_COL_WIDTH, int(min_width))
    return max(floor, min(_MAX_COL_WIDTH, int(width)))


def _sampled_row_indices(row_count: int) -> list[int]:
    """Return at most 60 representative row indices (top 20 + mid 20 + bottom 20)."""
    if row_count <= 0:
        return []
    if row_count <= _SAMPLE_SLICE * 3:
        return list(range(row_count))
    mid_start = max(0, (row_count // 2) - (_SAMPLE_SLICE // 2))
    indices: set[int] = set(range(_SAMPLE_SLICE))
    indices.update(range(mid_start, min(row_count, mid_start + _SAMPLE_SLICE)))
    indices.update(range(max(0, row_count - _SAMPLE_SLICE), row_count))
    return sorted(indices)


class TableHeaderManager(QObject):
    """Manages qBittorrent-style column customization and persistence for a QTableView."""

    layout_changed = Signal()
    sort_changed = Signal(int, Qt.SortOrder)

    _instances: "WeakSet[TableHeaderManager]" = WeakSet()

    def __init__(
        self,
        table_view: QTableView,
        table_id: str,
        columns: Sequence[ColumnSpec],
        ctx: Optional[Any] = None,
        *,
        persist: bool = True,
        dynamic: bool = False,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent if parent is not None else table_view)
        self._table: QTableView = table_view
        self._table_id: str = (table_id or "").strip()
        self._columns: list[ColumnSpec] = list(columns)
        self._key_to_logical: dict[str, int] = {
            spec.key: idx for idx, spec in enumerate(self._columns)
        }
        self._ctx: Optional[Any] = ctx
        self._persist: bool = bool(persist and ctx is not None and self._table_id and not dynamic)
        self._dynamic: bool = bool(dynamic)

        self._restoring: bool = False
        self._dirty_save: bool = False
        self._destroyed: bool = False
        self._sort_col: int = -1
        self._sort_order: Qt.SortOrder = Qt.SortOrder.AscendingOrder

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(_DEBOUNCE_SAVE_MS)
        self._save_timer.timeout.connect(self.flush)

        # Detach any previous manager attached to the same QTableView cleanly.
        prev = getattr(table_view, "_table_header_manager", None)
        if isinstance(prev, TableHeaderManager) and prev is not self:
            prev.detach()
        setattr(table_view, "_table_header_manager", self)

        self._configure_header()
        self._restore_or_apply_defaults()

        if self._ctx is not None and hasattr(self._ctx, "table_layouts_reset"):
            self._ctx.table_layouts_reset.connect(self._on_global_layouts_reset)

        self._table.destroyed.connect(self._on_table_destroyed)
        TableHeaderManager._instances.add(self)

    # ------------------------------------------------------------------
    # Public factory methods
    # ------------------------------------------------------------------

    @classmethod
    def attach(
        cls,
        table_view: QTableView,
        table_id: str,
        columns: Sequence[ColumnSpec],
        ctx: Optional[Any],
        *,
        persist: bool = True,
    ) -> "TableHeaderManager":
        """Attach a persistent qBittorrent-style header manager to a fixed-schema table."""
        return cls(
            table_view=table_view,
            table_id=table_id,
            columns=columns,
            ctx=ctx,
            persist=persist,
            dynamic=False,
        )

    @classmethod
    def attach_dynamic(
        cls,
        table_view: QTableView,
        ctx: Optional[Any] = None,
        *,
        table_id: str = "",
    ) -> "TableHeaderManager":
        """Attach a non-persistent manager for dynamic-column tables (Excel/CSV/Merge)."""
        specs = cls._build_dynamic_specs(table_view)
        return cls(
            table_view=table_view,
            table_id=table_id,
            columns=specs,
            ctx=ctx,
            persist=False,
            dynamic=True,
        )

    @classmethod
    def flush_all(cls) -> None:
        """Immediately flush all pending debounced layout saves across active tables."""
        for mgr in list(cls._instances):
            try:
                mgr.flush()
            except RuntimeError:
                pass

    # ------------------------------------------------------------------
    # Properties & inspection
    # ------------------------------------------------------------------

    @property
    def table_view(self) -> QTableView:
        return self._table

    @property
    def table_id(self) -> str:
        return self._table_id

    @property
    def columns(self) -> list[ColumnSpec]:
        return list(self._columns)

    @property
    def is_dynamic(self) -> bool:
        return self._dynamic

    @property
    def persist(self) -> bool:
        return self._persist

    def spec_for_logical(self, logical_idx: int) -> Optional[ColumnSpec]:
        if 0 <= logical_idx < len(self._columns):
            return self._columns[logical_idx]
        return None

    def logical_index_for_key(self, key: str) -> int:
        return self._key_to_logical.get(key, -1)

    def current_sort(self) -> tuple[int, Qt.SortOrder]:
        return self._sort_col, self._sort_order

    def visible_logical_columns_in_visual_order(self) -> list[int]:
        """Return logical column indices currently visible, ordered left-to-right visually."""
        if self._destroyed:
            return []
        header = self._table.horizontalHeader()
        count = header.count()
        visible: list[int] = []
        for visual_idx in range(count):
            logical_idx = header.logicalIndex(visual_idx)
            if logical_idx >= 0 and not header.isSectionHidden(logical_idx):
                visible.append(logical_idx)
        return visible

    def visible_column_keys(self) -> list[str]:
        """Return keys of currently visible columns in visual left-to-right order."""
        keys: list[str] = []
        for logical_idx in self.visible_logical_columns_in_visual_order():
            if 0 <= logical_idx < len(self._columns):
                keys.append(self._columns[logical_idx].key)
        return keys

    def column_order_keys(self) -> list[str]:
        """Return keys of all columns (visible and hidden) in current visual order."""
        if self._destroyed:
            return [c.key for c in self._columns]
        header = self._table.horizontalHeader()
        count = min(header.count(), len(self._columns))
        order: list[str] = []
        seen: set[int] = set()
        for visual_idx in range(count):
            logical_idx = header.logicalIndex(visual_idx)
            if 0 <= logical_idx < len(self._columns) and logical_idx not in seen:
                seen.add(logical_idx)
                order.append(self._columns[logical_idx].key)
        for logical_idx, spec in enumerate(self._columns):
            if logical_idx not in seen:
                order.append(spec.key)
        return order

    # ------------------------------------------------------------------
    # Header setup & lifecycle
    # ------------------------------------------------------------------

    def _configure_header(self) -> None:
        header = self._table.horizontalHeader()
        header.setSectionsMovable(True)
        header.setFirstSectionMovable(True)
        header.setSectionsClickable(True)
        header.setHighlightSections(False)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(_MIN_COL_WIDTH)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        header.customContextMenuRequested.connect(self._on_header_context_menu)
        header.sectionMoved.connect(self._on_section_moved)
        header.sectionResized.connect(self._on_section_resized)
        header.sortIndicatorChanged.connect(self._on_sort_indicator_changed)

    def detach(self) -> None:
        """Disconnect header signals and stop timers before replacement."""
        self.flush()
        self._save_timer.stop()
        if not self._destroyed:
            try:
                header = self._table.horizontalHeader()
                for sig, slot in (
                    (header.customContextMenuRequested, self._on_header_context_menu),
                    (header.sectionMoved, self._on_section_moved),
                    (header.sectionResized, self._on_section_resized),
                    (header.sortIndicatorChanged, self._on_sort_indicator_changed),
                ):
                    try:
                        sig.disconnect(slot)
                    except (RuntimeError, TypeError):
                        pass
            except RuntimeError:
                pass
        if self._ctx is not None and hasattr(self._ctx, "table_layouts_reset"):
            try:
                self._ctx.table_layouts_reset.disconnect(self._on_global_layouts_reset)
            except (RuntimeError, TypeError):
                pass
        TableHeaderManager._instances.discard(self)

    def _on_table_destroyed(self, _obj: Any = None) -> None:
        self._destroyed = True
        self._save_timer.stop()
        TableHeaderManager._instances.discard(self)

    # ------------------------------------------------------------------
    # Dynamic table synchronization
    # ------------------------------------------------------------------

    @staticmethod
    def _build_dynamic_specs(table_view: QTableView) -> list[ColumnSpec]:
        model = table_view.model()
        if model is None:
            return []
        col_count = model.columnCount()
        specs: list[ColumnSpec] = []
        used_keys: set[str] = set()
        for idx in range(col_count):
            raw_label = model.headerData(
                idx, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole
            )
            label = str(raw_label).strip() if raw_label is not None else f"列 {idx + 1}"
            if not label:
                label = f"列 {idx + 1}"
            base_key = f"col_{idx}_{label}"
            key = base_key
            suffix = 2
            while key in used_keys:
                key = f"{base_key}_{suffix}"
                suffix += 1
            used_keys.add(key)
            specs.append(
                ColumnSpec(
                    key=key,
                    label=label,
                    default_width=140,
                    default_visible=True,
                    min_width=52,
                    sortable=True,
                    lock_visible=False,
                    stretch_weight=1.0 if idx == col_count - 1 else 0.0,
                    category="basic",
                )
            )
        return specs

    def sync_dynamic_columns(self, *, auto_fit: bool = True) -> None:
        """Rebuild dynamic column specs after a dynamic model loads new headers."""
        if self._destroyed:
            return
        new_specs = self._build_dynamic_specs(self._table)
        old_labels = [s.label for s in self._columns]
        new_labels = [s.label for s in new_specs]
        same_schema = bool(new_specs) and old_labels == new_labels

        self._columns = new_specs
        self._key_to_logical = {spec.key: idx for idx, spec in enumerate(self._columns)}

        if not self._columns:
            self._sort_col = -1
            return

        if same_schema:
            return

        self._apply_default_layout()
        if auto_fit:
            self.auto_fit_all()

    # ------------------------------------------------------------------
    # Restore, defaults & schema evolution
    # ------------------------------------------------------------------

    def _restore_or_apply_defaults(self) -> None:
        if self._destroyed or not self._columns:
            return
        saved: Optional[TableLayoutState] = None
        if self._persist and self._ctx is not None:
            saved = self._ctx.config.table_layouts.get(self._table_id)
        if saved is None:
            self._apply_default_layout()
        else:
            self._apply_saved_layout(saved)

    def _apply_default_layout(self) -> None:
        if self._destroyed:
            return
        header = self._table.horizontalHeader()
        col_count = min(header.count(), len(self._columns))

        self._restoring = True
        try:
            header.blockSignals(True)
            self._table.setSortingEnabled(False)
            header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)

            # Reset visual order back to logical 0..col_count-1
            for target_visual in range(col_count):
                current_visual = header.visualIndex(target_visual)
                if current_visual >= 0 and current_visual != target_visual:
                    header.moveSection(current_visual, target_visual)

            # Apply default visibility & widths
            visible_count = 0
            for logical_idx in range(col_count):
                spec = self._columns[logical_idx]
                visible = True if spec.lock_visible else bool(spec.default_visible)
                header.setSectionHidden(logical_idx, not visible)
                if visible:
                    visible_count += 1
                self._table.setColumnWidth(
                    logical_idx, _clamp_width(spec.default_width, spec.min_width)
                )

            if col_count > 0 and visible_count == 0:
                header.setSectionHidden(0, False)

            self._distribute_spare_width(col_count)

            self._sort_col = -1
            self._sort_order = Qt.SortOrder.AscendingOrder
            header.blockSignals(False)
            self._table.setSortingEnabled(True)
        finally:
            header.blockSignals(False)
            self._restoring = False

    def _apply_saved_layout(self, saved: TableLayoutState) -> None:
        if self._destroyed:
            return
        header = self._table.horizontalHeader()
        col_count = min(header.count(), len(self._columns))
        if col_count <= 0:
            return

        # Reconcile known_cols for forward-compatible schema evolution:
        # Unknown columns added in newer versions adopt spec.default_visible.
        known_set: set[str] = set(saved.known_cols)
        if not known_set:
            known_set.update(saved.column_order)
            known_set.update(saved.visible_cols)
            known_set.update(saved.column_widths.keys())

        saved_visible = set(saved.visible_cols)

        # Build complete permutation of logical indices in desired visual order.
        ordered_logical: list[int] = []
        seen_logical: set[int] = set()
        for key in saved.column_order:
            logical_idx = self._key_to_logical.get(key, -1)
            if 0 <= logical_idx < col_count and logical_idx not in seen_logical:
                seen_logical.add(logical_idx)
                ordered_logical.append(logical_idx)
        for logical_idx in range(col_count):
            if logical_idx not in seen_logical:
                seen_logical.add(logical_idx)
                ordered_logical.append(logical_idx)

        self._restoring = True
        sort_logical = -1
        sort_order = Qt.SortOrder.AscendingOrder
        try:
            header.blockSignals(True)
            self._table.setSortingEnabled(False)
            header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)

            # 1. Visibility
            visible_indices: list[int] = []
            for logical_idx in range(col_count):
                spec = self._columns[logical_idx]
                if spec.lock_visible:
                    visible = True
                elif spec.key in known_set:
                    visible = spec.key in saved_visible
                else:
                    visible = bool(spec.default_visible)
                header.setSectionHidden(logical_idx, not visible)
                if visible:
                    visible_indices.append(logical_idx)

            if not visible_indices:
                fallback = next(
                    (i for i in range(col_count) if self._columns[i].lock_visible),
                    0,
                )
                header.setSectionHidden(fallback, False)

            # 2. Widths
            for logical_idx in range(col_count):
                spec = self._columns[logical_idx]
                raw_w = saved.column_widths.get(spec.key, spec.default_width)
                self._table.setColumnWidth(logical_idx, _clamp_width(raw_w, spec.min_width))

            # 3. Visual order via moveSection
            for target_visual, logical_idx in enumerate(ordered_logical):
                current_visual = header.visualIndex(logical_idx)
                if current_visual >= 0 and current_visual != target_visual:
                    header.moveSection(current_visual, target_visual)

            # 4. Sort state (resolved before enabling sorting to prevent spurious col-0 sort)
            if saved.sort_col:
                cand = self._key_to_logical.get(saved.sort_col, -1)
                if 0 <= cand < col_count and self._columns[cand].sortable:
                    sort_logical = cand
                    sort_order = (
                        Qt.SortOrder.DescendingOrder
                        if saved.sort_desc
                        else Qt.SortOrder.AscendingOrder
                    )

            self._sort_col = sort_logical
            self._sort_order = sort_order
            header.blockSignals(False)
            self._table.setSortingEnabled(True)

            if sort_logical >= 0:
                self._table.sortByColumn(sort_logical, sort_order)
        finally:
            header.blockSignals(False)
            self._restoring = False

    def _distribute_spare_width(self, col_count: int) -> None:
        """Distribute leftover positive viewport width across visible stretch_weight columns."""
        vp_w = self._table.viewport().width()
        if vp_w <= 200:
            return
        header = self._table.horizontalHeader()
        total_w = 0
        stretch_cols: list[tuple[int, float]] = []
        for logical_idx in range(col_count):
            if header.isSectionHidden(logical_idx):
                continue
            total_w += header.sectionSize(logical_idx)
            weight = self._columns[logical_idx].stretch_weight
            if weight > 0.0:
                stretch_cols.append((logical_idx, weight))

        spare = vp_w - total_w - 4
        if spare <= 8 or not stretch_cols:
            return
        total_weight = sum(w for _, w in stretch_cols)
        if total_weight <= 0.0:
            return
        for logical_idx, weight in stretch_cols:
            extra = int(spare * (weight / total_weight))
            if extra > 0:
                spec = self._columns[logical_idx]
                cur = header.sectionSize(logical_idx)
                self._table.setColumnWidth(logical_idx, _clamp_width(cur + extra, spec.min_width))

    def reapply_sort(self) -> None:
        """Re-apply active user sort order after a model reload without spurious signals."""
        if self._destroyed or self._sort_col < 0:
            return
        model = self._table.model()
        if model is None or self._sort_col >= model.columnCount():
            return
        header = self._table.horizontalHeader()
        self._restoring = True
        try:
            model.sort(self._sort_col, self._sort_order)
            header.blockSignals(True)
            header.setSortIndicator(self._sort_col, self._sort_order)
        finally:
            header.blockSignals(False)
            self._restoring = False

    def reset_to_defaults(self, *, save_config: bool = True) -> None:
        """Restore default visibility, order, widths, clear sort, and clear persisted state."""
        if self._destroyed:
            return
        self._save_timer.stop()
        self._dirty_save = False
        self._apply_default_layout()

        if self._persist and self._ctx is not None:
            self._ctx.config.table_layouts.pop(self._table_id, None)
            if save_config:
                try:
                    self._ctx.config_manager.save()
                except OSError:
                    pass

        self.layout_changed.emit()

    def _on_global_layouts_reset(self) -> None:
        """Respond to AppContext.table_layouts_reset without redundant disk writes."""
        self.reset_to_defaults(save_config=False)

    # ------------------------------------------------------------------
    # Visibility & Auto-Fit operations
    # ------------------------------------------------------------------

    def set_column_visible(self, logical_idx: int, visible: bool) -> bool:
        """Show or hide a logical column, enforcing lock_visible and >=1 visible guard."""
        if self._destroyed or not (0 <= logical_idx < len(self._columns)):
            return False
        header = self._table.horizontalHeader()
        spec = self._columns[logical_idx]

        if not visible:
            if spec.lock_visible:
                return False
            visible_cols = self.visible_logical_columns_in_visual_order()
            if len(visible_cols) <= 1 and logical_idx in visible_cols:
                return False

        self._restoring = True
        try:
            header.setSectionHidden(logical_idx, not visible)
            if visible:
                cur_w = header.sectionSize(logical_idx)
                min_w = max(_MIN_COL_WIDTH, spec.min_width)
                if cur_w < min_w:
                    self._table.setColumnWidth(
                        logical_idx, _clamp_width(spec.default_width, spec.min_width)
                    )
        finally:
            self._restoring = False

        self._schedule_save()
        self.layout_changed.emit()
        return True

    def _compute_sampled_width(self, logical_idx: int) -> int:
        spec = self._columns[logical_idx]
        header = self._table.horizontalHeader()
        model = self._table.model()

        header_fm = QFontMetrics(header.font())
        hdr_text = spec.label
        if model is not None:
            raw_hdr = model.headerData(
                logical_idx, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole
            )
            if raw_hdr:
                hdr_text = str(raw_hdr)
        best_w = header_fm.horizontalAdvance(hdr_text) + 34

        if model is not None:
            base_font = self._table.font()
            base_fm = QFontMetrics(base_font)
            for row in _sampled_row_indices(model.rowCount()):
                idx = model.index(row, logical_idx)
                if not idx.isValid():
                    continue
                val = model.data(idx, Qt.ItemDataRole.DisplayRole)
                text = str(val) if val is not None else ""
                cell_font = model.data(idx, Qt.ItemDataRole.FontRole)
                fm = QFontMetrics(cell_font) if isinstance(cell_font, QFont) else base_fm
                w = fm.horizontalAdvance(text) + 22

                if model.data(idx, ROLE_COPY_TEXT):
                    w += 28
                if model.data(idx, ROLE_BADGE):
                    w += 24
                if model.data(idx, Qt.ItemDataRole.CheckStateRole) is not None:
                    w += 24
                if w > best_w:
                    best_w = w
                    if best_w >= _MAX_AUTOFIT_WIDTH:
                        best_w = _MAX_AUTOFIT_WIDTH
                        break

        floor = max(_MIN_COL_WIDTH, spec.min_width)
        return max(floor, min(_MAX_AUTOFIT_WIDTH, best_w))

    def auto_fit_column(self, logical_idx: int) -> int:
        """Resize one visible column using fast O(60-row) content sampling."""
        if self._destroyed or not (0 <= logical_idx < len(self._columns)):
            return 0
        header = self._table.horizontalHeader()
        if header.isSectionHidden(logical_idx):
            return 0
        width = self._compute_sampled_width(logical_idx)
        self._restoring = True
        try:
            self._table.setColumnWidth(logical_idx, width)
        finally:
            self._restoring = False
        self._schedule_save()
        self.layout_changed.emit()
        return width

    def auto_fit_all(self) -> None:
        """Resize all visible columns using O(60-row) sampling + spare viewport distribution."""
        if self._destroyed or not self._columns:
            return
        header = self._table.horizontalHeader()
        col_count = min(header.count(), len(self._columns))
        self._restoring = True
        try:
            for logical_idx in range(col_count):
                if header.isSectionHidden(logical_idx):
                    continue
                width = self._compute_sampled_width(logical_idx)
                self._table.setColumnWidth(logical_idx, width)
            self._distribute_spare_width(col_count)
        finally:
            self._restoring = False
        self._schedule_save()
        self.layout_changed.emit()

    # ------------------------------------------------------------------
    # Right-click column customization menu (qBittorrent UX)
    # ------------------------------------------------------------------

    def build_column_menu(
        self,
        logical_section: int = -1,
        parent: Optional[QWidget] = None,
    ) -> QMenu:
        """Construct the column visibility & sizing context menu without executing it."""
        menu = QMenu(parent if parent is not None else self._table)
        menu.setToolTipsVisible(True)
        header = self._table.horizontalHeader()
        visible_cols = self.visible_logical_columns_in_visual_order()
        visible_count = len(visible_cols)

        basic_items: list[tuple[int, ColumnSpec]] = []
        advanced_items: list[tuple[int, ColumnSpec]] = []
        for idx, spec in enumerate(self._columns):
            if idx >= header.count():
                break
            if spec.category == "advanced":
                advanced_items.append((idx, spec))
            else:
                basic_items.append((idx, spec))

        def _add_toggle_action(idx: int, spec: ColumnSpec) -> None:
            is_visible = not header.isSectionHidden(idx)
            act = QAction(spec.label, menu)
            act.setCheckable(True)
            act.setChecked(is_visible)
            act.setData(spec.key)
            if is_visible and (spec.lock_visible or visible_count <= 1):
                act.setEnabled(False)
                act.setToolTip(
                    "核心列不可隐藏" if spec.lock_visible else "至少保留 1 个显示列"
                )
            act.toggled.connect(
                lambda checked, col=idx: self.set_column_visible(col, checked)
            )
            menu.addAction(act)

        for idx, spec in basic_items:
            _add_toggle_action(idx, spec)

        if advanced_items:
            menu.addSection("进阶字段")
            for idx, spec in advanced_items:
                _add_toggle_action(idx, spec)

        menu.addSeparator()

        if (
            0 <= logical_section < len(self._columns)
            and logical_section < header.count()
            and not header.isSectionHidden(logical_section)
        ):
            act_fit_cur = QAction("自动调整当前列宽", menu)
            act_fit_cur.triggered.connect(
                lambda _=False, col=logical_section: self.auto_fit_column(col)
            )
            menu.addAction(act_fit_cur)

        act_fit_all = QAction("自动调整全部列宽", menu)
        act_fit_all.triggered.connect(lambda _=False: self.auto_fit_all())
        menu.addAction(act_fit_all)

        act_reset = QAction("恢复默认列与顺序", menu)
        act_reset.triggered.connect(lambda _=False: self.reset_to_defaults())
        menu.addAction(act_reset)

        return menu

    def open_column_menu(
        self,
        global_pos: QPoint,
        logical_section: int = -1,
    ) -> None:
        """Display the qBittorrent-style column menu at ``global_pos``."""
        if self._destroyed or not self._columns:
            return
        menu = self.build_column_menu(logical_section=logical_section)
        menu.exec(global_pos)

    def _on_header_context_menu(self, pos: QPoint) -> None:
        if self._destroyed or not self._columns:
            return
        header = self._table.horizontalHeader()
        logical_section = header.logicalIndexAt(pos)
        self.open_column_menu(header.mapToGlobal(pos), logical_section=logical_section)

    # ------------------------------------------------------------------
    # Header mutation hooks & debounced persistence
    # ------------------------------------------------------------------

    def _on_section_moved(self, _logical: int, _old_visual: int, _new_visual: int) -> None:
        if self._restoring or self._destroyed:
            return
        self._schedule_save()
        self.layout_changed.emit()

    def _on_section_resized(self, logical: int, _old_size: int, new_size: int) -> None:
        if self._restoring or self._destroyed:
            return
        if new_size <= 0:
            return
        if 0 <= logical < len(self._columns):
            spec = self._columns[logical]
            min_w = max(_MIN_COL_WIDTH, spec.min_width)
            if new_size < min_w:
                self._restoring = True
                try:
                    self._table.setColumnWidth(logical, min_w)
                finally:
                    self._restoring = False
        self._schedule_save()

    def _on_sort_indicator_changed(self, logical_idx: int, order: Qt.SortOrder) -> None:
        if self._restoring or self._destroyed:
            return
        header = self._table.horizontalHeader()
        if logical_idx < 0:
            self._sort_col = -1
            self._sort_order = Qt.SortOrder.AscendingOrder
            self._schedule_save()
            self.sort_changed.emit(-1, Qt.SortOrder.AscendingOrder)
            return

        if 0 <= logical_idx < len(self._columns) and not self._columns[logical_idx].sortable:
            # Ignore clicks on non-sortable columns (e.g. checkbox column) and restore indicator.
            self._restoring = True
            try:
                header.blockSignals(True)
                header.setSortIndicator(self._sort_col, self._sort_order)
            finally:
                header.blockSignals(False)
                self._restoring = False
            return

        self._sort_col = logical_idx
        self._sort_order = order
        self._schedule_save()
        self.sort_changed.emit(logical_idx, order)

    def capture_state(self) -> TableLayoutState:
        """Capture the current header visibility, order, widths, and sort state."""
        header = self._table.horizontalHeader()
        col_count = min(header.count(), len(self._columns))

        widths: dict[str, int] = {}
        for idx in range(col_count):
            spec = self._columns[idx]
            size = header.sectionSize(idx)
            if size > 0:
                widths[spec.key] = _clamp_width(size, spec.min_width)
            else:
                widths[spec.key] = _clamp_width(spec.default_width, spec.min_width)

        sort_key = ""
        if 0 <= self._sort_col < len(self._columns) and self._columns[self._sort_col].sortable:
            sort_key = self._columns[self._sort_col].key

        return TableLayoutState(
            visible_cols=self.visible_column_keys(),
            column_order=self.column_order_keys(),
            column_widths=widths,
            sort_col=sort_key,
            sort_desc=self._sort_order == Qt.SortOrder.DescendingOrder,
            known_cols=[spec.key for spec in self._columns],
        )

    def _schedule_save(self) -> None:
        if not self._persist or self._ctx is None or self._destroyed:
            return
        self._ctx.config.table_layouts[self._table_id] = self.capture_state()
        self._dirty_save = True
        self._save_timer.start()

    def flush(self) -> None:
        """Flush any pending debounced layout change to disk immediately."""
        self._save_timer.stop()
        if not self._dirty_save or not self._persist or self._ctx is None:
            return
        if not self._destroyed:
            self._ctx.config.table_layouts[self._table_id] = self.capture_state()
        self._dirty_save = False
        try:
            self._ctx.config_manager.save()
        except OSError:
            pass
