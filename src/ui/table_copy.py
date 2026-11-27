"""Rapid clipboard copy engine for QTableView workflows in 数据查重程序.

Provides order-preserving deduplicated code formatters (newline, space-separated
multi-keyword search, comma, regex OR), visual-order TSV row export, unified
Ctrl+C / Ctrl+Shift+C shortcuts, context menu builders, and a dynamic split
QToolButton (zero literal unicode arrows or emojis).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Optional, Sequence

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QMenu, QTableView, QToolButton, QWidget

from src.ui.table_header import TableHeaderManager
from src.ui.theme import get_palette

try:
    from src.ui.delegates import ROLE_COPY_TEXT
except ImportError:
    ROLE_COPY_TEXT = int(Qt.ItemDataRole.UserRole) + 41

# Backward-friendly alias referenced in design notes
CODE_COPY_ROLE: int = ROLE_COPY_TEXT

_PLACEHOLDER_CODES = frozenset({"", "-", "（未识别）", "（双击填写编码）"})
_REGEX_META_CHARS = frozenset(r".^$*+?{}[]\|()")

_FORMAT_LABELS: dict[str, str] = {
    "newline": "每行一个",
    "space": "空格分隔检索",
    "comma": "逗号分隔",
    "regex": "正则 OR 检索式",
}


def deduplicate_preserve_order(codes: Iterable[str]) -> list[str]:
    """Strip, drop blank/placeholder entries, and deduplicate while preserving order."""
    cleaned = [
        text
        for raw in codes
        if raw is not None and (text := str(raw).strip()) not in _PLACEHOLDER_CODES
    ]
    return list(dict.fromkeys(cleaned))


def format_codes_for_clipboard(
    codes: Sequence[str],
    fmt: str = "newline",
) -> str:
    """Format canonical codes with order-preserving deduplication for rapid search.

    Supported formats:
    - ``"newline"``: one code per line (default)
    - ``"space"``: space-separated multi-keyword search (``ABC-001 DEF-002``)
    - ``"comma"``: comma-space separated (``ABC-001, DEF-002``)
    - ``"regex"``: regex OR expression (``ABC-001|DEF-002``)
    """
    unique = deduplicate_preserve_order(codes)
    if not unique:
        return ""
    norm_fmt = (fmt or "newline").strip().lower()
    if norm_fmt == "space":
        return " ".join(unique)
    if norm_fmt == "comma":
        return ", ".join(unique)
    if norm_fmt == "regex":
        parts = [
            re.escape(code) if any(ch in _REGEX_META_CHARS for ch in code) else code
            for code in unique
        ]
        return "|".join(parts)
    return "\n".join(unique)


def copy_text_to_clipboard(text: str) -> bool:
    """Write non-empty text to the system clipboard safely."""
    if not text:
        return False
    app = QApplication.instance()
    if app is None:
        return False
    clipboard = QApplication.clipboard()
    if clipboard is None:
        return False
    clipboard.setText(text)
    return True


def _sanitize_tsv_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\t", " ").replace("\r\n", " ").replace("\n", " ").strip()


def format_rows_as_tsv(
    table_view: QTableView,
    rows: Sequence[int],
    header_manager: Optional[TableHeaderManager] = None,
    *,
    include_headers: bool = False,
) -> str:
    """Format specified rows as TSV following the table's current visible visual order."""
    model = table_view.model()
    if model is None or not rows:
        return ""

    if header_manager is not None:
        vis_cols = header_manager.visible_logical_columns_in_visual_order()
    else:
        header = table_view.horizontalHeader()
        vis_cols = [
            logical
            for visual in range(header.count())
            if (logical := header.logicalIndex(visual)) >= 0
            and not header.isSectionHidden(logical)
        ]

    # Exclude checkbox-only indicator columns with empty display text
    export_cols: list[int] = []
    for col in vis_cols:
        if header_manager is not None:
            spec = header_manager.spec_for_logical(col)
            if spec is not None and spec.key == "check":
                continue
        export_cols.append(col)

    if not export_cols:
        return ""

    lines: list[str] = []
    if include_headers:
        hdr_cells = [
            _sanitize_tsv_cell(
                model.headerData(col, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
            )
            for col in export_cols
        ]
        lines.append("\t".join(hdr_cells))

    row_count = model.rowCount()
    for row in rows:
        if not (0 <= row < row_count):
            continue
        cells = [
            _sanitize_tsv_cell(model.data(model.index(row, col), Qt.ItemDataRole.DisplayRole))
            for col in export_cols
        ]
        lines.append("\t".join(cells))

    return "\n".join(lines)


def _load_copy_icon(theme: str, *, primary: bool = False) -> Any:
    """Load vector 'copy' QIcon dynamically from src.ui.icons when available."""
    try:
        from src.ui.icons import get_icon

        p = get_palette(theme)
        color = p.get("cta_text", "#FFFFFF") if primary else p.get("text_secondary", "#52525B")
        return get_icon("copy", size=15, color=color, theme=theme)
    except Exception:
        return None


class TableCopyHelper(QObject):
    """Unified clipboard copy engine, shortcuts, context menu & split button for a QTableView."""

    copied = Signal(str, int)  # (summary_message, item_count)

    def __init__(
        self,
        table_view: QTableView,
        ctx: Optional[Any] = None,
        *,
        header_manager: Optional[TableHeaderManager] = None,
        code_column: Optional[int] = None,
        name_column: Optional[int] = None,
        code_extractor: Optional[Callable[[int], str]] = None,
        name_extractor: Optional[Callable[[int], str]] = None,
        path_extractor: Optional[Callable[[int], str]] = None,
        default_ctrl_c_mode: str = "code",  # "code" | "tsv"
        install_shortcuts: bool = True,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent if parent is not None else table_view)
        self._table: QTableView = table_view
        self._ctx: Optional[Any] = ctx
        self._header_manager: Optional[TableHeaderManager] = header_manager or getattr(
            table_view, "_table_header_manager", None
        )
        self._code_column: Optional[int] = code_column
        self._name_column: Optional[int] = name_column
        self._code_extractor = code_extractor
        self._name_extractor = name_extractor
        self._path_extractor = path_extractor
        self._default_ctrl_c_mode: str = default_ctrl_c_mode

        self._sc_copy: Optional[QShortcut] = None
        self._sc_copy_names: Optional[QShortcut] = None
        self._split_buttons: list[tuple[QToolButton, str, str, bool]] = []
        self._model_connected = False
        self._sel_connected = False

        if install_shortcuts:
            self.install_shortcuts()

        if self._ctx is not None and hasattr(self._ctx, "theme_changed"):
            self._ctx.theme_changed.connect(self._refresh_button_icons)

    def set_header_manager(self, manager: Optional[TableHeaderManager]) -> None:
        self._header_manager = manager

    # ------------------------------------------------------------------
    # Row & field extraction
    # ------------------------------------------------------------------

    def selected_rows(self) -> list[int]:
        """Return sorted unique selected row indices."""
        sel_model = self._table.selectionModel()
        if sel_model is None:
            return []
        rows = {idx.row() for idx in sel_model.selectedRows() if idx.isValid()}
        if not rows:
            rows = {idx.row() for idx in sel_model.selectedIndexes() if idx.isValid()}
        return sorted(rows)

    def all_rows(self) -> list[int]:
        model = self._table.model()
        if model is None:
            return []
        return list(range(model.rowCount()))

    def extract_code(self, row: int) -> str:
        """Extract canonical code string for one row."""
        if self._code_extractor is not None:
            raw = self._code_extractor(row)
            text = str(raw).strip() if raw is not None else ""
            return "" if text in _PLACEHOLDER_CODES else text

        model = self._table.model()
        if model is None or not (0 <= row < model.rowCount()):
            return ""

        col_count = model.columnCount()
        if self._code_column is not None and 0 <= self._code_column < col_count:
            idx = model.index(row, self._code_column)
            val = model.data(idx, ROLE_COPY_TEXT)
            if not val:
                val = model.data(idx, Qt.ItemDataRole.DisplayRole)
            text = str(val).strip() if val is not None else ""
            return "" if text in _PLACEHOLDER_CODES else text

        # Auto-detect column exposing ROLE_COPY_TEXT
        for col in range(min(col_count, 5)):
            val = model.data(model.index(row, col), ROLE_COPY_TEXT)
            if val:
                text = str(val).strip()
                if text not in _PLACEHOLDER_CODES:
                    return text
        return ""

    def extract_codes(
        self,
        rows: Optional[Sequence[int]] = None,
        *,
        deduplicate: bool = True,
    ) -> list[str]:
        """Extract codes from ``rows`` (or selected/all rows when omitted)."""
        target_rows = list(rows) if rows is not None else self.selected_rows()
        if rows is None and not target_rows:
            target_rows = self.all_rows()
        raw_codes = [c for r in target_rows if (c := self.extract_code(r))]
        return deduplicate_preserve_order(raw_codes) if deduplicate else raw_codes

    def extract_names(self, rows: Sequence[int]) -> list[str]:
        """Extract original filenames for ``rows`` (preserves all selected entries)."""
        model = self._table.model()
        names: list[str] = []
        for row in rows:
            val: Any = ""
            if self._name_extractor is not None:
                val = self._name_extractor(row)
            elif (
                model is not None
                and self._name_column is not None
                and 0 <= row < model.rowCount()
                and 0 <= self._name_column < model.columnCount()
            ):
                val = model.data(model.index(row, self._name_column), Qt.ItemDataRole.DisplayRole)
            text = str(val).strip() if val is not None else ""
            if text and text != "-":
                names.append(text)
        return names

    def extract_paths(self, rows: Sequence[int]) -> list[str]:
        """Extract non-empty full file paths for ``rows``."""
        if self._path_extractor is None:
            return []
        paths: list[str] = []
        for row in rows:
            raw = self._path_extractor(row)
            text = str(raw).strip() if raw is not None else ""
            if text:
                paths.append(text)
        return paths

    # ------------------------------------------------------------------
    # Clipboard copy actions
    # ------------------------------------------------------------------

    def _notify(self, message: str, count: int, level: str = "success") -> None:
        self.copied.emit(message, count)
        if self._ctx is not None and hasattr(self._ctx, "notify"):
            self._ctx.notify(message, level)

    def copy_codes(
        self,
        fmt: str = "newline",
        *,
        rows: Optional[Sequence[int]] = None,
        all_rows: bool = False,
    ) -> int:
        """Copy deduplicated codes in the chosen format; returns unique code count."""
        if all_rows:
            target_rows = self.all_rows()
        elif rows is not None:
            target_rows = list(rows)
        else:
            target_rows = self.selected_rows()

        codes = self.extract_codes(target_rows, deduplicate=True)
        payload = format_codes_for_clipboard(codes, fmt=fmt)
        if not payload or not copy_text_to_clipboard(payload):
            if self._ctx is not None and hasattr(self._ctx, "notify"):
                self._ctx.notify("当前没有可复制的编码", "info")
            return 0

        if len(codes) == 1:
            msg = f"已复制编码：{codes[0]}"
        else:
            fmt_label = _FORMAT_LABELS.get(fmt, fmt)
            msg = f"已复制 {len(codes)} 个编码（已去重 · {fmt_label}）"
        self._notify(msg, len(codes))
        return len(codes)

    def copy_selected_or_all_codes(self, fmt: str = "newline") -> int:
        """Copy selected deduplicated codes, or all visible deduplicated codes if none selected."""
        sel = self.selected_rows()
        if sel and self.extract_codes(sel, deduplicate=True):
            return self.copy_codes(fmt=fmt, rows=sel)
        return self.copy_codes(fmt=fmt, all_rows=True)

    def copy_filenames(self, rows: Optional[Sequence[int]] = None) -> int:
        """Copy original filenames of selected rows (preserves duplicates)."""
        target_rows = list(rows) if rows is not None else self.selected_rows()
        if not target_rows:
            return 0
        names = self.extract_names(target_rows)
        if not names or not copy_text_to_clipboard("\n".join(names)):
            return 0
        self._notify(f"已复制 {len(names)} 条原始文件名", len(names))
        return len(names)

    def copy_paths(self, rows: Optional[Sequence[int]] = None) -> int:
        """Copy full file paths of selected rows."""
        target_rows = list(rows) if rows is not None else self.selected_rows()
        if not target_rows:
            return 0
        paths = self.extract_paths(target_rows)
        if not paths or not copy_text_to_clipboard("\n".join(paths)):
            return 0
        self._notify(f"已复制 {len(paths)} 条完整路径", len(paths))
        return len(paths)

    def copy_rows_tsv(
        self,
        rows: Optional[Sequence[int]] = None,
        *,
        include_headers: bool = False,
    ) -> int:
        """Copy rows as TSV matching current visible columns in visual dragged order."""
        target_rows = list(rows) if rows is not None else self.selected_rows()
        if not target_rows:
            target_rows = self.all_rows()
        if not target_rows:
            return 0
        tsv = format_rows_as_tsv(
            self._table,
            target_rows,
            header_manager=self._header_manager,
            include_headers=include_headers,
        )
        if not tsv or not copy_text_to_clipboard(tsv):
            return 0
        self._notify(f"已复制 {len(target_rows)} 行数据（可见列 TSV）", len(target_rows))
        return len(target_rows)

    # ------------------------------------------------------------------
    # Unified shortcuts (Ctrl+C / Ctrl+Shift+C)
    # ------------------------------------------------------------------

    def install_shortcuts(self) -> None:
        """Bind Ctrl+C and Ctrl+Shift+C scoped to the attached QTableView."""
        if self._sc_copy is None:
            self._sc_copy = QShortcut(QKeySequence.StandardKey.Copy, self._table)
            self._sc_copy.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self._sc_copy.activated.connect(self._on_ctrl_c)

        if self._sc_copy_names is None:
            self._sc_copy_names = QShortcut(QKeySequence("Ctrl+Shift+C"), self._table)
            self._sc_copy_names.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self._sc_copy_names.activated.connect(self._on_ctrl_shift_c)

    def _on_ctrl_c(self) -> None:
        sel = self.selected_rows()
        if not sel:
            return
        if self._default_ctrl_c_mode == "tsv":
            self.copy_rows_tsv(rows=sel)
        else:
            if self.extract_codes(sel, deduplicate=True):
                self.copy_codes("newline", rows=sel)
            else:
                self.copy_rows_tsv(rows=sel)

    def _on_ctrl_shift_c(self) -> None:
        sel = self.selected_rows()
        if sel:
            self.copy_filenames(rows=sel)

    # ------------------------------------------------------------------
    # Split QToolButton (MenuButtonPopup) & Context Menu helpers
    # ------------------------------------------------------------------

    def create_copy_split_button(
        self,
        *,
        parent: Optional[QWidget] = None,
        primary_variant: bool = False,
        all_label: str = "复制全部编码",
        selected_label: str = "复制选中编码",
    ) -> QToolButton:
        """Create a dynamic split QToolButton for one-click & multi-format code copy."""
        btn = QToolButton(parent)
        btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setProperty("copySplit", True)
        if primary_variant:
            btn.setProperty("variant", "primary")

        theme = self._ctx.config.theme if self._ctx is not None else "light"
        icon = _load_copy_icon(theme, primary=primary_variant)
        if icon is not None:
            btn.setIcon(icon)

        menu = QMenu(btn)
        menu.aboutToShow.connect(lambda m=menu: self._rebuild_split_menu(m))
        btn.setMenu(menu)
        btn.clicked.connect(lambda _=False: self.copy_selected_or_all_codes("newline"))

        self._split_buttons.append((btn, selected_label, all_label, primary_variant))
        self._ensure_model_signals()
        self.update_split_buttons()
        return btn

    def _ensure_model_signals(self) -> None:
        model = self._table.model()
        if model is not None and not self._model_connected:
            self._model_connected = True
            for sig in (
                model.modelReset,
                model.rowsInserted,
                model.rowsRemoved,
                model.layoutChanged,
            ):
                sig.connect(self.update_split_buttons)

        sel_model = self._table.selectionModel()
        if sel_model is not None and not self._sel_connected:
            self._sel_connected = True
            sel_model.selectionChanged.connect(self.update_split_buttons)

    def _refresh_button_icons(self, theme: str) -> None:
        alive: list[tuple[QToolButton, str, str, bool]] = []
        for entry in self._split_buttons:
            btn, _sel_lbl, _all_lbl, is_primary = entry
            try:
                icon = _load_copy_icon(theme, primary=is_primary)
                if icon is not None:
                    btn.setIcon(icon)
                alive.append(entry)
            except RuntimeError:
                pass
        self._split_buttons = alive

    def update_split_buttons(self, *_args: Any) -> None:
        """Sync split button labels and enabled state with current selection & visible codes."""
        if not self._split_buttons:
            return
        self._ensure_model_signals()
        sel_rows = self.selected_rows()
        sel_codes = self.extract_codes(sel_rows, deduplicate=True) if sel_rows else []
        all_codes = self.extract_codes(self.all_rows(), deduplicate=True)

        alive: list[tuple[QToolButton, str, str, bool]] = []
        for entry in self._split_buttons:
            btn, sel_label, all_label, _primary = entry
            try:
                if sel_codes:
                    btn.setText(f"{sel_label} ({len(sel_codes)})")
                    btn.setEnabled(True)
                    btn.setToolTip(f"点击复制已选中的 {len(sel_codes)} 个去重编码（每行一个）")
                elif all_codes:
                    btn.setText(f"{all_label} ({len(all_codes)})")
                    btn.setEnabled(True)
                    btn.setToolTip(f"点击复制当前列表全部 {len(all_codes)} 个去重编码（每行一个）")
                else:
                    btn.setText(all_label)
                    btn.setEnabled(False)
                    btn.setToolTip("当前没有可复制的编码")
                alive.append(entry)
            except RuntimeError:
                pass
        self._split_buttons = alive

    def _rebuild_split_menu(self, menu: QMenu) -> None:
        menu.clear()
        sel_rows = self.selected_rows()
        sel_codes = self.extract_codes(sel_rows, deduplicate=True) if sel_rows else []
        all_codes = self.extract_codes(self.all_rows(), deduplicate=True)
        has_any_codes = bool(sel_codes or all_codes)

        act_sel = QAction(
            f"复制选中编码（每行一个，去重 {len(sel_codes)} 个）"
            if sel_codes
            else "复制选中编码（每行一个，自动去重）",
            menu,
        )
        act_sel.setEnabled(bool(sel_codes))
        act_sel.triggered.connect(lambda _=False: self.copy_codes("newline", rows=sel_rows))
        menu.addAction(act_sel)

        act_all = QAction(
            f"复制全部当前编码（去重 {len(all_codes)} 个）"
            if all_codes
            else "复制全部当前编码（自动去重）",
            menu,
        )
        act_all.setEnabled(bool(all_codes))
        act_all.triggered.connect(lambda _=False: self.copy_codes("newline", all_rows=True))
        menu.addAction(act_all)

        act_space = QAction("复制为多词检索（空格分隔）", menu)
        act_space.setEnabled(has_any_codes)
        act_space.triggered.connect(lambda _=False: self.copy_selected_or_all_codes("space"))
        menu.addAction(act_space)

        act_comma = QAction("复制为逗号分隔", menu)
        act_comma.setEnabled(has_any_codes)
        act_comma.triggered.connect(lambda _=False: self.copy_selected_or_all_codes("comma"))
        menu.addAction(act_comma)

        act_regex = QAction("复制为正则 OR 检索式", menu)
        act_regex.setEnabled(has_any_codes)
        act_regex.triggered.connect(lambda _=False: self.copy_selected_or_all_codes("regex"))
        menu.addAction(act_regex)

        menu.addSeparator()

        sel_names = self.extract_names(sel_rows) if sel_rows else []
        act_names = QAction("复制选中原始文件名", menu)
        act_names.setEnabled(bool(sel_names))
        act_names.triggered.connect(lambda _=False: self.copy_filenames(rows=sel_rows))
        menu.addAction(act_names)

        sel_paths = self.extract_paths(sel_rows) if sel_rows else []
        act_paths = QAction("复制选中完整路径", menu)
        act_paths.setEnabled(bool(sel_paths))
        act_paths.triggered.connect(lambda _=False: self.copy_paths(rows=sel_rows))
        menu.addAction(act_paths)

        act_tsv = QAction("复制选中行（TSV，按当前可见列顺序）", menu)
        act_tsv.setEnabled(bool(sel_rows or self.all_rows()))
        act_tsv.triggered.connect(lambda _=False: self.copy_rows_tsv(rows=sel_rows or None))
        menu.addAction(act_tsv)

    def populate_context_menu(
        self,
        menu: QMenu,
        rows: Optional[Sequence[int]] = None,
    ) -> None:
        """Append standardized copy actions to an existing table right-click QMenu."""
        target_rows = list(rows) if rows is not None else self.selected_rows()
        if not target_rows:
            return
        codes = self.extract_codes(target_rows, deduplicate=True)

        if len(codes) == 1:
            act_code = QAction(f"复制编码 ({codes[0]})", menu)
            act_code.triggered.connect(lambda _=False: self.copy_codes("newline", rows=target_rows))
            menu.addAction(act_code)
        elif len(codes) > 1:
            act_codes = QAction(f"复制选中编码 ({len(codes)} 个，已去重)", menu)
            act_codes.triggered.connect(
                lambda _=False: self.copy_codes("newline", rows=target_rows)
            )
            menu.addAction(act_codes)

            act_space = QAction("复制为多词检索（空格分隔）", menu)
            act_space.triggered.connect(lambda _=False: self.copy_codes("space", rows=target_rows))
            menu.addAction(act_space)

        names = self.extract_names(target_rows)
        if names:
            act_name = QAction("复制原始文件名", menu)
            act_name.triggered.connect(lambda _=False: self.copy_filenames(rows=target_rows))
            menu.addAction(act_name)

        paths = self.extract_paths(target_rows)
        if paths:
            act_path = QAction("复制完整路径", menu)
            act_path.triggered.connect(lambda _=False: self.copy_paths(rows=target_rows))
            menu.addAction(act_path)

        act_tsv = QAction("复制选中行（可见列 TSV）", menu)
        act_tsv.triggered.connect(lambda _=False: self.copy_rows_tsv(rows=target_rows))
        menu.addAction(act_tsv)
