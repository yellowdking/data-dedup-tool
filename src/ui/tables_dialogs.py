"""Modal dialogs and file-inspection helpers for Multi-Table Studio.

Houses ordered composite-key configuration, multi-sheet / Word-table / header-row
import picking, column profiling & renaming, and full-row inspection dialogs.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core import doc_reader
from src.core.doc_reader import list_excel_sheets, load_tables_from_file
from src.core.table_data import DataTable


def _escape_mnemonic(text: str) -> str:
    """Escape '&' so Qt does not turn header/sheet names into mnemonic accelerators."""
    return str(text or "").replace("&", "&&")


def _inspect_file_for_import(path_str: str) -> dict[str, Any]:
    """Quickly inspect an Excel/Word/CSV file to decide whether Import Picker is needed."""
    file_path = Path(path_str)
    ext = file_path.suffix.lower()
    if ext in {".xlsx", ".xlsm"}:
        sheets = list_excel_sheets(file_path)
        return {"path": path_str, "kind": "excel", "sheets": sheets}

    if ext == ".docx":
        tables_info: list[dict[str, Any]] = []
        para_count = 0
        list_word_fn = getattr(doc_reader, "list_word_tables_info", None)
        if callable(list_word_fn):
            tables_info = list(list_word_fn(file_path))
        try:
            import docx

            doc = docx.Document(str(file_path))
            para_count = sum(1 for p in doc.paragraphs if str(p.text or "").strip())
            if not callable(list_word_fn):
                for idx, tbl in enumerate(doc.tables, start=1):
                    r_n = len(tbl.rows)
                    c_n = len(tbl.columns) if r_n else 0
                    prev_cells: list[str] = []
                    if r_n:
                        for cell in tbl.rows[0].cells[:4]:
                            txt = " ".join(str(cell.text or "").split())
                            if txt and txt not in prev_cells:
                                prev_cells.append(txt[:16])
                    tables_info.append(
                        {
                            "index": idx,
                            "label": f"表格{idx}",
                            "rows": r_n,
                            "cols": c_n,
                            "preview": " / ".join(prev_cells),
                        }
                    )
        except Exception:
            if not tables_info:
                raise
        return {
            "path": path_str,
            "kind": "word",
            "tables": tables_info,
            "paragraph_count": para_count,
        }

    return {"path": path_str, "kind": "csv" if ext in {".csv", ".tsv", ".efu"} else "other"}


def _call_load_tables_from_file(
    path_str: str,
    opts: dict[str, Any],
    parser_engine: Any,
) -> list[DataTable]:
    """Invoke doc_reader.load_tables_from_file forwarding only supported keyword arguments."""
    sig_params = inspect.signature(load_tables_from_file).parameters
    kwargs: dict[str, Any] = {"parser_engine": parser_engine}
    for key in (
        "sheet_names",
        "has_header",
        "header_row",
        "include_paragraphs_as_table",
        "include_nested_tables",
        "table_indices",
    ):
        if key in opts and key in sig_params:
            kwargs[key] = opts[key]
    return load_tables_from_file(path_str, **kwargs)


class _PerTableKeyDialog(QDialog):
    """Ordered composite-key alignment dialog supporting Key 1, Key 2, ... across tables."""

    def __init__(
        self,
        tables: list[DataTable],
        current_mapping: dict[str, list[str]],
        parent: Optional[QWidget] = None,
        *,
        default_column: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("配置各表关键列（支持有序多列组合键）")
        self.resize(680, 440)
        self._tables = list(tables)
        self._default_column = default_column
        self.was_cleared: bool = False

        max_cols = max((len(t.headers) for t in self._tables), default=1)
        self._max_slots = max(1, min(max_cols, 6))

        existing_max = max(
            (len([c for c in current_mapping.get(t.table_id, []) if c in t.headers]) for t in self._tables),
            default=1,
        )
        self._slot_count = max(1, min(existing_max, self._max_slots))

        # Working state: table_id -> list of selected column names per slot ("" means unused)
        self._slot_values: dict[str, list[str]] = {}
        for tbl in self._tables:
            existing = [c for c in current_mapping.get(tbl.table_id, []) if c in tbl.headers]
            if not existing and tbl.headers:
                if default_column and default_column in tbl.headers:
                    existing = [default_column]
                else:
                    existing = [tbl.headers[0]]
            padded = list(existing[: self._slot_count])
            while len(padded) < self._slot_count:
                padded.append("")
            self._slot_values[tbl.table_id] = padded

        self._combos_by_table: dict[str, list[QComboBox]] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        hint = QLabel(
            "按槽位顺序（关键列 1、关键列 2 …）对齐各表的比对列，避免不同表列顺序不一致导致组合键错位。"
        )
        hint.setWordWrap(True)
        hint.setProperty("role", "subtitle")
        root.addWidget(hint)

        # Slot toolbar
        slot_bar = QHBoxLayout()
        slot_bar.setSpacing(8)

        self._btn_add_slot = QPushButton("＋ 添加组合键列")
        self._btn_add_slot.setProperty("compact", True)
        self._btn_add_slot.clicked.connect(self._add_slot)
        slot_bar.addWidget(self._btn_add_slot)

        self._btn_remove_slot = QPushButton("－ 移除末列")
        self._btn_remove_slot.setProperty("compact", True)
        self._btn_remove_slot.clicked.connect(self._remove_slot)
        slot_bar.addWidget(self._btn_remove_slot)

        slot_bar.addStretch()

        btn_clear = QPushButton("恢复默认 / 清除按表配置")
        btn_clear.setProperty("compact", True)
        btn_clear.setToolTip("清除按表自定义配置，重新跟随主界面的统一关键列下拉框")
        btn_clear.clicked.connect(self._on_clear_and_accept)
        slot_bar.addWidget(btn_clear)

        root.addLayout(slot_bar)

        self._warn_lbl = QLabel("")
        self._warn_lbl.setWordWrap(True)
        self._warn_lbl.setProperty("badge", "warning")
        self._warn_lbl.setVisible(False)
        root.addWidget(self._warn_lbl)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(self._scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("应用关键列配置")
        btn_ok.setProperty("variant", "primary")
        btn_ok.clicked.connect(self.accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_ok)
        root.addLayout(btn_row)

        self._rebuild_grid()

    def _snapshot_current_combos(self) -> None:
        for tbl in self._tables:
            combos = self._combos_by_table.get(tbl.table_id)
            if not combos:
                continue
            self._slot_values[tbl.table_id] = [str(cb.currentData() or "") for cb in combos]

    def _rebuild_grid(self) -> None:
        self._snapshot_current_combos()
        container = QWidget()
        grid = QGridLayout(container)
        grid.setContentsMargins(4, 4, 8, 4)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        hdr_tbl = QLabel("参与表格")
        hdr_tbl.setProperty("role", "section")
        grid.addWidget(hdr_tbl, 0, 0)

        for s_idx in range(self._slot_count):
            lbl = QLabel(f"关键列 {s_idx + 1}" + ("（必选）" if s_idx == 0 else "（可选）"))
            lbl.setProperty("role", "section")
            grid.addWidget(lbl, 0, s_idx + 1)

        self._combos_by_table.clear()
        for row_idx, tbl in enumerate(self._tables, start=1):
            name_lbl = QLabel(f"{tbl.name}\n({tbl.row_count:,} 行 × {tbl.col_count} 列)")
            name_lbl.setToolTip(tbl.name)
            name_lbl.setMinimumWidth(150)
            name_lbl.setMaximumWidth(210)
            grid.addWidget(name_lbl, row_idx, 0)

            vals = self._slot_values.get(tbl.table_id, [])
            combos: list[QComboBox] = []
            for s_idx in range(self._slot_count):
                cb = QComboBox()
                cb.setMinimumWidth(120)
                if s_idx > 0 or not tbl.headers:
                    cb.addItem("（不启用）", "")
                for col_name in tbl.headers:
                    cb.addItem(col_name, col_name)

                target = vals[s_idx] if s_idx < len(vals) else ""
                found = cb.findData(target) if target else -1
                if found >= 0:
                    cb.setCurrentIndex(found)
                elif s_idx == 0 and tbl.headers:
                    cb.setCurrentIndex(0)
                else:
                    cb.setCurrentIndex(0)

                cb.currentIndexChanged.connect(self._update_warnings)
                grid.addWidget(cb, row_idx, s_idx + 1)
                combos.append(cb)
            self._combos_by_table[tbl.table_id] = combos

        grid.setRowStretch(len(self._tables) + 1, 1)
        for s_idx in range(self._slot_count):
            grid.setColumnStretch(s_idx + 1, 1)

        self._scroll.setWidget(container)
        self._btn_add_slot.setEnabled(self._slot_count < self._max_slots)
        self._btn_remove_slot.setEnabled(self._slot_count > 1)
        self._update_warnings()

    def _add_slot(self) -> None:
        if self._slot_count >= self._max_slots:
            return
        self._snapshot_current_combos()
        self._slot_count += 1
        for tbl in self._tables:
            vals = self._slot_values.setdefault(tbl.table_id, [])
            while len(vals) < self._slot_count:
                vals.append("")
        self._rebuild_grid()

    def _remove_slot(self) -> None:
        if self._slot_count <= 1:
            return
        self._snapshot_current_combos()
        self._slot_count -= 1
        for tbl in self._tables:
            vals = self._slot_values.get(tbl.table_id, [])
            self._slot_values[tbl.table_id] = vals[: self._slot_count]
        self._rebuild_grid()

    def _update_warnings(self) -> None:
        mapping = self._collect_mapping()
        lengths = {len(v) for v in mapping.values() if v}
        dup_tables: list[str] = []
        for tbl in self._tables:
            raw = [
                str(cb.currentData() or "")
                for cb in self._combos_by_table.get(tbl.table_id, [])
                if cb.currentData()
            ]
            if len(raw) != len(set(raw)):
                dup_tables.append(tbl.name)

        msgs: list[str] = []
        if len(lengths) > 1:
            msgs.append("提示：各表启用的组合键列数不一致，可能导致跨表比对无法命中。")
        if dup_tables:
            msgs.append(f"提示：{'、'.join(dup_tables[:3])} 中存在重复选择的列（应用时将自动去重）。")
        self._warn_lbl.setText(" ".join(msgs))
        self._warn_lbl.setVisible(bool(msgs))

    def _on_clear_and_accept(self) -> None:
        self.was_cleared = True
        self.accept()

    def _collect_mapping(self) -> dict[str, list[str]]:
        mapping: dict[str, list[str]] = {}
        for tbl in self._tables:
            chosen: list[str] = []
            for cb in self._combos_by_table.get(tbl.table_id, []):
                col_name = str(cb.currentData() or "").strip()
                if col_name and col_name in tbl.headers and col_name not in chosen:
                    chosen.append(col_name)
            if chosen:
                mapping[tbl.table_id] = chosen
            elif tbl.headers:
                mapping[tbl.table_id] = [tbl.headers[0]]
        return mapping

    def get_key_mapping(self) -> dict[str, list[str]]:
        if self.was_cleared:
            return {}
        return self._collect_mapping()


class _ImportOptionsDialog(QDialog):
    """Interactive import picker for multi-sheet Excel workbooks, Word tables/paragraphs, and header row."""

    def __init__(
        self,
        file_specs: list[dict[str, Any]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择导入的工作表、表格与表头行")
        self.resize(660, 480)
        self._file_specs = list(file_specs)
        self._controls_by_path: dict[str, dict[str, Any]] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        hint = QLabel("勾选需要导入的 Excel 工作表或 Word 表格，并可指定表头所在行（支持跳过标题行或无表头）。")
        hint.setWordWrap(True)
        hint.setProperty("role", "subtitle")
        root.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        cont_layout = QVBoxLayout(container)
        cont_layout.setContentsMargins(2, 2, 6, 2)
        cont_layout.setSpacing(10)

        for spec in self._file_specs:
            path_str = str(spec["path"])
            kind = str(spec.get("kind", "excel"))
            file_name = Path(path_str).name

            card = QFrame()
            card.setProperty("card", True)
            c_layout = QVBoxLayout(card)
            c_layout.setContentsMargins(12, 10, 12, 10)
            c_layout.setSpacing(8)

            top = QHBoxLayout()
            badge_lbl = QLabel("XLSX" if kind == "excel" else ("DOCX" if kind == "word" else "CSV"))
            badge_lbl.setProperty("badge", "success" if kind == "excel" else "info")
            top.addWidget(badge_lbl)

            name_lbl = QLabel(file_name)
            name_lbl.setProperty("role", "section")
            name_lbl.setToolTip(path_str)
            top.addWidget(name_lbl, 1)

            ctrl: dict[str, Any] = {"kind": kind}

            if kind == "excel":
                sheets: list[str] = list(spec.get("sheets") or [])
                chk_list: list[QCheckBox] = []

                btn_all = QPushButton("全选")
                btn_all.setProperty("compact", True)
                btn_none = QPushButton("全不选")
                btn_none.setProperty("compact", True)
                top.addWidget(btn_all)
                top.addWidget(btn_none)
                c_layout.addLayout(top)

                row_lay: Optional[QHBoxLayout] = None
                for idx, sheet_name in enumerate(sheets):
                    if idx % 3 == 0:
                        row_lay = QHBoxLayout()
                        row_lay.setSpacing(12)
                        c_layout.addLayout(row_lay)
                    chk = QCheckBox(_escape_mnemonic(sheet_name))
                    chk.setProperty("raw_name", sheet_name)
                    chk.setChecked(True)
                    chk_list.append(chk)
                    assert row_lay is not None
                    row_lay.addWidget(chk)
                if row_lay is not None:
                    row_lay.addStretch()

                btn_all.clicked.connect(lambda _=False, items=chk_list: [c.setChecked(True) for c in items])
                btn_none.clicked.connect(lambda _=False, items=chk_list: [c.setChecked(False) for c in items])
                ctrl["sheet_checks"] = chk_list

            elif kind == "word":
                tables_info: list[dict[str, Any]] = list(spec.get("tables") or [])
                para_count = int(spec.get("paragraph_count", 0))
                chk_list = []

                if tables_info:
                    btn_all = QPushButton("全选")
                    btn_all.setProperty("compact", True)
                    btn_none = QPushButton("全不选")
                    btn_none.setProperty("compact", True)
                    top.addWidget(btn_all)
                    top.addWidget(btn_none)
                c_layout.addLayout(top)

                if tables_info:
                    for t_info in tables_info:
                        t_idx = int(t_info.get("index", 1))
                        t_label = str(t_info.get("label", f"表格{t_idx}"))
                        r_n = int(t_info.get("rows", 0))
                        c_n = int(t_info.get("cols", 0))
                        prev = str(t_info.get("preview", "")).strip()
                        text = f"{t_label} ({r_n} 行 × {c_n} 列)" + (f" — {prev}" if prev else "")
                        chk = QCheckBox(_escape_mnemonic(text))
                        chk.setProperty("table_index", t_idx)
                        chk.setChecked(True)
                        chk_list.append(chk)
                        c_layout.addWidget(chk)
                    btn_all.clicked.connect(lambda _=False, items=chk_list: [c.setChecked(True) for c in items])
                    btn_none.clicked.connect(lambda _=False, items=chk_list: [c.setChecked(False) for c in items])
                else:
                    empty_lbl = QLabel("未检测到嵌入式表格，可导入正文段落。")
                    empty_lbl.setProperty("role", "muted")
                    c_layout.addWidget(empty_lbl)

                opt_row = QHBoxLayout()
                opt_row.setSpacing(14)
                chk_para = QCheckBox(
                    "同时导入正文段落为独立表" + (f"（约 {para_count} 段）" if para_count else "")
                )
                chk_para.setChecked(not bool(tables_info))
                opt_row.addWidget(chk_para)

                chk_nested = QCheckBox("包含单元格嵌套表格")
                chk_nested.setChecked(False)
                opt_row.addWidget(chk_nested)
                opt_row.addStretch()
                c_layout.addLayout(opt_row)

                ctrl["table_checks"] = chk_list
                ctrl["chk_paragraphs"] = chk_para
                ctrl["chk_nested"] = chk_nested
            else:
                c_layout.addLayout(top)

            # Header row selector
            hdr_row = QHBoxLayout()
            hdr_row.setSpacing(8)
            hdr_lbl = QLabel("表头设置：")
            hdr_lbl.setProperty("role", "subtitle")
            hdr_row.addWidget(hdr_lbl)

            hdr_combo = QComboBox()
            hdr_combo.addItem("第 1 行作为表头（默认）", (True, 0))
            hdr_combo.addItem("第 2 行作为表头（跳过第 1 行标题）", (True, 1))
            hdr_combo.addItem("第 3 行作为表头（跳过前 2 行）", (True, 2))
            hdr_combo.addItem("无表头（自动生成 列1、列2…）", (False, 0))
            hdr_row.addWidget(hdr_combo)
            hdr_row.addStretch()
            c_layout.addLayout(hdr_row)

            ctrl["header_combo"] = hdr_combo
            self._controls_by_path[path_str] = ctrl
            cont_layout.addWidget(card)

        cont_layout.addStretch()
        scroll.setWidget(container)
        root.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("取消导入")
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("确认导入选中内容")
        btn_ok.setProperty("variant", "primary")
        btn_ok.clicked.connect(self.accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_ok)
        root.addLayout(btn_row)

    def get_options(self) -> dict[str, dict[str, Any]]:
        """Return per-file import options keyed by absolute file path."""
        result: dict[str, dict[str, Any]] = {}
        for path_str, ctrl in self._controls_by_path.items():
            has_header, header_row = ctrl["header_combo"].currentData()
            opts: dict[str, Any] = {
                "has_header": bool(has_header),
                "header_row": int(header_row),
            }
            kind = ctrl["kind"]
            if kind == "excel":
                opts["sheet_names"] = [
                    str(c.property("raw_name"))
                    for c in ctrl.get("sheet_checks", [])
                    if c.isChecked()
                ]
            elif kind == "word":
                table_checks: list[QCheckBox] = ctrl.get("table_checks", [])
                if table_checks:
                    opts["table_indices"] = [
                        int(c.property("table_index")) for c in table_checks if c.isChecked()
                    ]
                else:
                    opts["table_indices"] = []
                opts["include_paragraphs_as_table"] = bool(ctrl["chk_paragraphs"].isChecked())
                opts["include_nested_tables"] = bool(ctrl["chk_nested"].isChecked())
            result[path_str] = opts
        return result


class _ColumnManagerDialog(QDialog):
    """Column profiling, renaming, and pruning dialog backed by DataTable.get_column_info()."""

    def __init__(self, table: DataTable, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"列管理 — {table.name}")
        self.resize(780, 480)
        self._table = table
        self._col_infos = table.get_column_info(max_samples=4)
        self._keep_checks: list[QCheckBox] = []
        self._name_edits: list[QLineEdit] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        hint = QLabel(
            f"当前表「{table.name}」共 {table.row_count:,} 行 × {table.col_count} 列。"
            "可重命名列名以对齐多表合并字段（如「联系电话」→「手机号」），或取消勾选不需要的列。"
        )
        hint.setWordWrap(True)
        hint.setProperty("role", "subtitle")
        root.addWidget(hint)

        tool_row = QHBoxLayout()
        tool_row.setSpacing(8)
        btn_all = QPushButton("全部保留")
        btn_all.setProperty("compact", True)
        btn_all.clicked.connect(lambda: [c.setChecked(True) for c in self._keep_checks])
        btn_inv = QPushButton("反选")
        btn_inv.setProperty("compact", True)
        btn_inv.clicked.connect(lambda: [c.setChecked(not c.isChecked()) for c in self._keep_checks])
        btn_reset = QPushButton("恢复原列名")
        btn_reset.setProperty("compact", True)
        btn_reset.clicked.connect(self._reset_names)
        tool_row.addWidget(btn_all)
        tool_row.addWidget(btn_inv)
        tool_row.addWidget(btn_reset)
        tool_row.addStretch()
        root.addLayout(tool_row)

        self._err_lbl = QLabel("")
        self._err_lbl.setProperty("badge", "danger")
        self._err_lbl.setVisible(False)
        root.addWidget(self._err_lbl)

        self._grid_tbl = QTableWidget(len(self._col_infos), 6)
        self._grid_tbl.setHorizontalHeaderLabels(
            ["保留", "原列名", "目标列名（可编辑）", "非空 / 填充率", "唯一值", "样本预览"]
        )
        self._grid_tbl.verticalHeader().setVisible(False)
        self._grid_tbl.verticalHeader().setDefaultSectionSize(34)
        self._grid_tbl.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._grid_tbl.setAlternatingRowColors(True)

        total_rows = max(1, table.row_count)
        for idx, info in enumerate(self._col_infos):
            chk_wrap = QWidget()
            chk_lay = QHBoxLayout(chk_wrap)
            chk_lay.setContentsMargins(0, 0, 0, 0)
            chk_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chk = QCheckBox()
            chk.setChecked(True)
            chk_lay.addWidget(chk)
            self._keep_checks.append(chk)
            self._grid_tbl.setCellWidget(idx, 0, chk_wrap)

            orig_item = QTableWidgetItem(info.name)
            orig_item.setFlags(orig_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._grid_tbl.setItem(idx, 1, orig_item)

            edit = QLineEdit(info.name)
            edit.setPlaceholderText("输入新列名...")
            self._name_edits.append(edit)
            self._grid_tbl.setCellWidget(idx, 2, edit)

            pct = (info.non_empty_count / total_rows * 100.0) if table.row_count else 0.0
            fill_item = QTableWidgetItem(f"{info.non_empty_count:,} / {table.row_count:,} ({pct:.0f}%)")
            fill_item.setFlags(fill_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            fill_item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
            self._grid_tbl.setItem(idx, 3, fill_item)

            uniq_item = QTableWidgetItem(f"{info.unique_count:,}")
            uniq_item.setFlags(uniq_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            uniq_item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
            self._grid_tbl.setItem(idx, 4, uniq_item)

            sample_txt = " | ".join(s.replace("\n", " ")[:24] for s in info.sample_values) or "-"
            samp_item = QTableWidgetItem(sample_txt)
            samp_item.setFlags(samp_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            samp_item.setToolTip("\n".join(info.sample_values))
            self._grid_tbl.setItem(idx, 5, samp_item)

        hdr = self._grid_tbl.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self._grid_tbl.setColumnWidth(0, 48)
        self._grid_tbl.setColumnWidth(1, 120)
        self._grid_tbl.setColumnWidth(2, 160)
        self._grid_tbl.setColumnWidth(3, 130)
        self._grid_tbl.setColumnWidth(4, 70)
        hdr.setStretchLastSection(True)
        root.addWidget(self._grid_tbl, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("应用列变更")
        btn_ok.setProperty("variant", "primary")
        btn_ok.clicked.connect(self._validate_and_accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_ok)
        root.addLayout(btn_row)

    def _reset_names(self) -> None:
        for info, edit in zip(self._col_infos, self._name_edits):
            edit.setText(info.name)

    def _validate_and_accept(self) -> None:
        kept = [idx for idx, chk in enumerate(self._keep_checks) if chk.isChecked()]
        if not kept:
            self._err_lbl.setText("至少需要保留 1 列数据。")
            self._err_lbl.setVisible(True)
            return
        self._err_lbl.setVisible(False)
        self.accept()

    def get_column_plan(self) -> list[tuple[int, str]]:
        """Return ordered `(original_col_idx, unique_new_name)` pairs for kept columns."""
        plan: list[tuple[int, str]] = []
        used: set[str] = set()
        for idx, (info, chk, edit) in enumerate(zip(self._col_infos, self._keep_checks, self._name_edits)):
            if not chk.isChecked():
                continue
            base = edit.text().strip() or info.name or f"列{idx + 1}"
            candidate = base
            suffix = 2
            while candidate in used:
                candidate = f"{base}_{suffix}"
                suffix += 1
            used.add(candidate)
            plan.append((info.index, candidate))
        return plan


class _RowDetailDialog(QDialog):
    """Full-row inspector modal displaying every field without truncation."""

    def __init__(
        self,
        title: str,
        headers: list[str],
        values: list[str],
        *,
        subtitle: str = "",
        can_jump: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 440)
        self.jump_requested: bool = False
        self._headers = list(headers)
        self._values = list(values)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        if subtitle:
            sub_lbl = QLabel(subtitle)
            sub_lbl.setWordWrap(True)
            sub_lbl.setProperty("role", "subtitle")
            root.addWidget(sub_lbl)

        row_n = max(len(self._headers), len(self._values))
        tbl = QTableWidget(row_n, 2)
        tbl.setHorizontalHeaderLabels(["字段名", "完整内容"])
        tbl.verticalHeader().setVisible(False)
        tbl.setWordWrap(True)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tbl.setAlternatingRowColors(True)

        for i in range(row_n):
            h = self._headers[i] if i < len(self._headers) else f"列{i + 1}"
            v = self._values[i] if i < len(self._values) else ""
            tbl.setItem(i, 0, QTableWidgetItem(str(h)))
            tbl.setItem(i, 1, QTableWidgetItem(str(v)))

        tbl.setColumnWidth(0, 160)
        tbl.horizontalHeader().setStretchLastSection(True)
        tbl.resizeRowsToContents()
        root.addWidget(tbl, 1)

        btn_row = QHBoxLayout()
        btn_copy = QPushButton("复制全部字段")
        btn_copy.clicked.connect(self._copy_all)
        btn_row.addWidget(btn_copy)

        if can_jump:
            btn_jump = QPushButton("定位到原表此行")
            btn_jump.setProperty("variant", "outline")
            btn_jump.clicked.connect(self._on_jump)
            btn_row.addWidget(btn_jump)

        btn_row.addStretch()
        btn_close = QPushButton("关闭")
        btn_close.setProperty("variant", "primary")
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

    def _copy_all(self) -> None:
        lines = [
            f"{self._headers[i] if i < len(self._headers) else f'列{i + 1}'}: "
            f"{self._values[i] if i < len(self._values) else ''}"
            for i in range(max(len(self._headers), len(self._values)))
        ]
        QApplication.clipboard().setText("\n".join(lines))

    def _on_jump(self) -> None:
        self.jump_requested = True
        self.accept()
