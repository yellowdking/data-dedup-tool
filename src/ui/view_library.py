"""View 1: 数据总库 — High-performance searchable, filterable, editable, exportable table."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.core.database import DatabaseError
from src.core.exporter import (
    ExportError,
    export_codes_txt,
    export_records_csv,
    export_records_excel,
)
from src.core.models import DataRecord
from src.ui.app_context import AppContext
from src.ui.table_models import RecordTableModel
from src.ui.widgets import ScrollableDetailDialog, WorkerThread, format_file_size


def _stop_worker_safely(worker: Optional[WorkerThread], timeout_ms: int = 3000) -> None:
    """Cancel and wait for a WorkerThread safely without risking QThread destruction crashes."""
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


def _split_code_prefix_number(code: str) -> tuple[str, str]:
    """Derive `(prefix, number)` from a manually edited canonical code."""
    normalized = code.strip().upper()
    if not normalized:
        return "", ""
    if "-" in normalized:
        left, right = normalized.rsplit("-", 1)
        if left and right:
            return left, right
    m = re.match(r"^([A-Z]+)(\d+)$", normalized)
    if m:
        return m.group(1), m.group(2)
    return normalized, ""


def _parse_tags_input(raw: str) -> list[str]:
    """Parse user-entered comma/slash/whitespace separated tags into a deduplicated list."""
    tokens = re.split(r"[,，;/／|\s]+", raw.strip())
    result: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        t = tok.strip()
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


def open_local_folder_safely(full_path: str, ctx: AppContext) -> None:
    """Safely reveal a local file or directory, rejecting UNC/network paths and non-dir targets."""
    raw = (full_path or "").strip()
    if not raw:
        ctx.notify("该记录未关联本地路径", "warning")
        return

    # Security: reject UNC network paths (prevents SMB NTLMv2 leak) and URI schemes.
    if raw.startswith("\\\\") or raw.startswith("//") or "://" in raw:
        ctx.notify("出于安全考虑，不支持直接打开网络共享 (UNC) 或远程链接路径", "warning")
        return

    try:
        p = Path(raw)
        if str(p).startswith(("\\\\", "//")):
            ctx.notify("出于安全考虑，不支持直接打开网络共享 (UNC) 路径", "warning")
            return

        if p.is_file():
            if os.name == "nt":
                # Pass /select,<path> as a single argument so paths containing commas work reliably.
                subprocess.Popen(["explorer", f"/select,{os.path.normpath(str(p))}"])
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent)))
        elif p.is_dir():
            if os.name == "nt":
                os.startfile(os.path.normpath(str(p)))
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
        elif p.parent.is_dir():
            if os.name == "nt":
                os.startfile(os.path.normpath(str(p.parent)))
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent)))
        else:
            ctx.notify("未在本地磁盘找到该文件或目录（可能来源于文本清单导入）", "warning")
    except OSError as exc:
        ctx.notify(f"无法打开目录：{exc}", "error")


class RecordEditDialog(QDialog):
    """Modal dialog for inspecting full record details and editing mutable metadata."""

    def __init__(self, record: DataRecord, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._orig = record
        self.setWindowTitle(f"记录详情与编辑  #{record.id or '-'}")
        self.resize(580, 480)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        # Read-only metadata summary card
        meta_card = QFrame()
        meta_card.setProperty("card", True)
        meta_form = QFormLayout(meta_card)
        meta_form.setContentsMargins(14, 12, 14, 12)
        meta_form.setSpacing(6)

        def _readonly_lbl(text: str) -> QLabel:
            lbl = QLabel(text or "-")
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lbl.setProperty("role", "subtitle")
            return lbl

        meta_form.addRow("原始文件名：", _readonly_lbl(record.file_name or record.raw_text))
        if record.raw_text and record.raw_text != record.file_name:
            meta_form.addRow("原始文本：", _readonly_lbl(record.raw_text))
        if record.full_path:
            meta_form.addRow("完整路径：", _readonly_lbl(record.full_path))
        source_info = f"{record.source_name or '-'}  ({record.source_type or 'txt'})"
        size_info = format_file_size(record.file_size)
        meta_form.addRow("来源 / 大小：", _readonly_lbl(f"{source_info}  ·  {size_info}"))
        meta_form.addRow(
            "匹配规则：",
            _readonly_lbl(f"{record.matched_rule or '-'}  (置信度 {record.confidence:.2f})"),
        )
        time_info = f"入库 {record.created_at[:19] if record.created_at else '-'}"
        if record.updated_at and record.updated_at != record.created_at:
            time_info += f"  ·  更新 {record.updated_at[:19]}"
        meta_form.addRow("时间信息：", _readonly_lbl(time_info))
        root.addWidget(meta_card)

        # Editable fields form
        edit_form = QFormLayout()
        edit_form.setSpacing(10)

        self._code_edit = QLineEdit(record.canonical_code)
        self._code_edit.setPlaceholderText("必填，例如：ABC-123 或 PRJ-0042")
        edit_form.addRow("标准编码*：", self._code_edit)

        self._tags_edit = QLineEdit(", ".join(record.tags))
        self._tags_edit.setPlaceholderText("可选，用逗号分隔，例如：4K, 字幕, 高码率")
        edit_form.addRow("版本标签：", self._tags_edit)

        self._part_edit = QLineEdit(record.part)
        self._part_edit.setPlaceholderText("可选，例如：CD1 / CD2 / Part1")
        edit_form.addRow("分集标记：", self._part_edit)

        self._note_edit = QPlainTextEdit(record.note or "")
        self._note_edit.setPlaceholderText("可选备注信息...")
        self._note_edit.setMaximumHeight(84)
        edit_form.addRow("备注：", self._note_edit)

        root.addLayout(edit_form)

        self._err_label = QLabel("")
        self._err_label.setProperty("role", "subtitle")
        self._err_label.setVisible(False)
        root.addWidget(self._err_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_save = QPushButton("保存修改")
        btn_save.setProperty("variant", "primary")
        btn_save.clicked.connect(self._on_accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_save)
        root.addLayout(btn_row)

    def _on_accept(self) -> None:
        code = self._code_edit.text().strip().upper()
        if not code:
            self._err_label.setText("❌ 标准编码不能为空")
            self._err_label.setVisible(True)
            self._code_edit.setFocus()
            return
        self.accept()

    def build_updated_record(self) -> DataRecord:
        code = self._code_edit.text().strip().upper()
        tags = _parse_tags_input(self._tags_edit.text())
        part = self._part_edit.text().strip().upper()
        note = self._note_edit.toPlainText().strip()

        if code != self._orig.canonical_code.strip().upper():
            prefix, number = _split_code_prefix_number(code)
            matched_rule = "手动编辑"
        else:
            prefix = self._orig.prefix
            number = self._orig.number
            matched_rule = self._orig.matched_rule

        return replace(
            self._orig,
            canonical_code=code,
            prefix=prefix,
            number=number,
            tags=tags,
            part=part,
            matched_rule=matched_rule,
            note=note,
        )


class LibraryView(QWidget):
    """Main database browsing view with instant search, filters, inline edit, shortcuts & export."""

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        try:
            self._model = RecordTableModel(self, theme=self.ctx.config.theme)
        except TypeError:
            self._model = RecordTableModel(self)

        self._dirty: bool = False
        self._sort_col: int = -1
        self._sort_order: Qt.SortOrder = Qt.SortOrder.AscendingOrder
        self._export_worker: Optional[WorkerThread] = None
        self._delete_worker: Optional[WorkerThread] = None

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self.refresh_table)

        self._setup_ui()
        self.ctx.data_changed.connect(self._on_data_changed)
        self.ctx.db_switched.connect(self._on_db_switched)
        self.ctx.theme_changed.connect(self.apply_theme)
        self.refresh_all()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # Toolbar card
        toolbar_card = QFrame()
        toolbar_card.setProperty("card", True)
        tb_layout = QHBoxLayout(toolbar_card)
        tb_layout.setContentsMargins(14, 10, 14, 10)
        tb_layout.setSpacing(10)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("🔍 搜索标准编码、文件名、路径、来源或标签（Ctrl+F 聚焦）...")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.textChanged.connect(lambda _: self._search_timer.start())
        tb_layout.addWidget(self._search_input, 3)

        self._filter_combo = QComboBox()
        self._filter_combo.addItem("全部记录", "all")
        self._filter_combo.addItem("仅重复项", "duplicates")
        self._filter_combo.addItem("疑似分集", "multipart")
        self._filter_combo.addItem("仅唯一项", "unique")
        self._filter_combo.setMinimumWidth(115)
        self._filter_combo.currentIndexChanged.connect(lambda _: self.refresh_table())
        tb_layout.addWidget(self._filter_combo)

        self._prefix_combo = QComboBox()
        self._prefix_combo.setMinimumWidth(125)
        self._prefix_combo.addItem("全部前缀", "")
        self._prefix_combo.currentIndexChanged.connect(lambda _: self.refresh_table())
        tb_layout.addWidget(self._prefix_combo)

        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.clicked.connect(self.refresh_all)
        tb_layout.addWidget(self._refresh_btn)

        self._export_btn = QPushButton("导出数据 ▾")
        self._export_btn.setProperty("variant", "primary")
        export_menu = QMenu(self)
        export_menu.addAction("导出为 Excel (.xlsx)", lambda: self._export("xlsx"))
        export_menu.addAction("导出为 CSV (.csv)", lambda: self._export("csv"))
        export_menu.addAction("导出编码清单 (.txt)", lambda: self._export("txt"))
        self._export_btn.setMenu(export_menu)
        tb_layout.addWidget(self._export_btn)

        root.addWidget(toolbar_card)

        # Virtualized TableView
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(False)
        self._table.setSortingEnabled(True)
        header = self._table.horizontalHeader()
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        header.sortIndicatorChanged.connect(self._on_sort_indicator_changed)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)
        self._table.doubleClicked.connect(self._on_row_double_clicked)
        self._table.verticalHeader().setDefaultSectionSize(32)
        self._table.verticalHeader().setVisible(False)
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        for col_idx, (_, width) in enumerate(RecordTableModel.COLUMNS):
            self._table.setColumnWidth(col_idx, width)

        self._table.selectionModel().selectionChanged.connect(self._update_footer)

        # Keyboard shortcuts scoped to table
        self._sc_delete = QShortcut(QKeySequence(Qt.Key.Key_Delete), self._table)
        self._sc_delete.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._sc_delete.activated.connect(self._delete_current_selection)

        self._sc_copy = QShortcut(QKeySequence.StandardKey.Copy, self._table)
        self._sc_copy.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._sc_copy.activated.connect(self._copy_current_selection)

        root.addWidget(self._table, 1)

        # Footer status row
        footer = QHBoxLayout()
        self._footer_label = QLabel("当前显示 0 条记录")
        self._footer_label.setProperty("role", "subtitle")
        footer.addWidget(self._footer_label)
        footer.addStretch()

        hint_label = QLabel("提示：双击记录可查看/编辑详情 · Ctrl+C 复制编码 · Delete 删除 · 右键更多操作")
        hint_label.setProperty("role", "muted")
        footer.addWidget(hint_label)
        root.addLayout(footer)

    # ------------------------------------------------------------------
    # Lifecycle, lazy refresh & MainWindow hooks
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self._search_timer.stop()
        _stop_worker_safely(self._export_worker)
        self._export_worker = None
        _stop_worker_safely(self._delete_worker)
        self._delete_worker = None

    def focus_search(self) -> None:
        self._search_input.setFocus()
        self._search_input.selectAll()

    def mark_dirty(self) -> None:
        self._dirty = True
        if self.isVisible():
            self.refresh_all()

    def on_activated(self) -> None:
        if self._dirty:
            self.refresh_all()

    def refresh_if_dirty(self) -> None:
        self.on_activated()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dirty:
            self.refresh_all()

    def apply_theme(self, theme: str) -> None:
        if hasattr(self._model, "set_theme"):
            self._model.set_theme(theme)

    def _on_data_changed(self) -> None:
        if not self.isVisible():
            self._dirty = True
            return
        self.refresh_all()

    def _on_db_switched(self, _db_path: str) -> None:
        self._search_timer.stop()
        self._search_input.blockSignals(True)
        self._search_input.clear()
        self._search_input.blockSignals(False)

        self._filter_combo.blockSignals(True)
        self._filter_combo.setCurrentIndex(0)
        self._filter_combo.blockSignals(False)

        self._prefix_combo.blockSignals(True)
        self._prefix_combo.setCurrentIndex(0)
        self._prefix_combo.blockSignals(False)

        if not self.isVisible():
            self._dirty = True
            return
        self.refresh_all()

    def _on_sort_indicator_changed(self, section: int, order: Qt.SortOrder) -> None:
        self._sort_col = section
        self._sort_order = order

    # ------------------------------------------------------------------
    # Data loading & table refresh
    # ------------------------------------------------------------------

    def refresh_all(self) -> None:
        self._dirty = False
        self._reload_prefixes()
        self.refresh_table()

    def _reload_prefixes(self) -> None:
        current_prefix = self._prefix_combo.currentData() or ""
        try:
            prefixes = self.ctx.db.get_distinct_prefixes()
        except (DatabaseError, OSError, Exception) as exc:
            self.ctx.notify(f"加载前缀列表失败：{exc}", "error")
            return
        self._prefix_combo.blockSignals(True)
        self._prefix_combo.clear()
        self._prefix_combo.addItem("全部前缀", "")
        for p in prefixes:
            self._prefix_combo.addItem(p, p)
        idx = self._prefix_combo.findData(current_prefix)
        self._prefix_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._prefix_combo.blockSignals(False)

    def refresh_table(self) -> None:
        search = self._search_input.text().strip()
        filter_mode = self._filter_combo.currentData() or "all"
        prefix = self._prefix_combo.currentData() or ""

        try:
            records = self.ctx.db.query_records(
                search=search,
                filter_mode=filter_mode,
                prefix=prefix,
                sort_by="id",
                sort_desc=True,
            )
            code_counts = self.ctx.db.get_code_counts()
        except (DatabaseError, OSError, Exception) as exc:
            ScrollableDetailDialog("读取数据总库失败", "查询记录时发生错误：", str(exc), parent=self).exec()
            return

        dup_codes = {k for k, v in code_counts.items() if v > 1}
        self._model.set_records(records, duplicate_codes=dup_codes)

        # Preserve active user sort column/order across refreshes
        if 0 <= self._sort_col < self._model.columnCount():
            self._model.sort(self._sort_col, self._sort_order)
            header = self._table.horizontalHeader()
            header.blockSignals(True)
            header.setSortIndicator(self._sort_col, self._sort_order)
            header.blockSignals(False)

        self._update_footer()

    def _update_footer(self) -> None:
        total = self._model.rowCount()
        sel_model = self._table.selectionModel()
        selected = len(sel_model.selectedRows()) if sel_model is not None else 0
        if selected > 0:
            self._footer_label.setText(f"当前显示 {total:,} 条记录 · 已选中 {selected} 条")
        else:
            self._footer_label.setText(f"当前显示 {total:,} 条记录")

    def _selected_records(self) -> list[DataRecord]:
        sel_model = self._table.selectionModel()
        if sel_model is None:
            return []
        records: list[DataRecord] = []
        for idx in sel_model.selectedRows():
            rec = self._model.record_at(idx.row())
            if rec is not None:
                records.append(rec)
        return records

    # ------------------------------------------------------------------
    # Record inspection / editing & context menu / shortcuts
    # ------------------------------------------------------------------

    def _on_row_double_clicked(self, index) -> None:
        if not index.isValid():
            return
        rec = self._model.record_at(index.row())
        if rec is not None:
            self._open_edit_dialog(rec)

    def _open_edit_dialog(self, record: DataRecord) -> None:
        dlg = RecordEditDialog(record, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dlg.build_updated_record()
        try:
            ok = self.ctx.db.update_record(updated)
            if ok:
                self.ctx.notify(f"已更新记录 #{updated.id}（{updated.canonical_code}）", "success")
                self.ctx.data_changed.emit()
            else:
                self.ctx.notify("未找到该记录，可能已被其他操作删除", "warning")
        except (DatabaseError, ValueError, OSError, Exception) as exc:
            ScrollableDetailDialog("更新记录失败", "保存记录修改时发生错误：", str(exc), parent=self).exec()

    def _copy_current_selection(self) -> None:
        records = self._selected_records()
        if records:
            self._copy_field(records, "code")

    def _delete_current_selection(self) -> None:
        records = self._selected_records()
        if records:
            self._delete_selected(records)

    def _show_context_menu(self, pos) -> None:
        records = self._selected_records()
        if not records:
            return
        menu = QMenu(self)

        if len(records) == 1:
            act_edit = QAction("查看 / 编辑记录详情", self)
            act_edit.triggered.connect(lambda: self._open_edit_dialog(records[0]))
            menu.addAction(act_edit)
            menu.addSeparator()

        act_copy_code = QAction(f"复制标准编码 ({len(records)} 条)", self)
        act_copy_code.triggered.connect(lambda: self._copy_field(records, "code"))
        menu.addAction(act_copy_code)

        act_copy_name = QAction("复制原始文件名", self)
        act_copy_name.triggered.connect(lambda: self._copy_field(records, "name"))
        menu.addAction(act_copy_name)

        if len(records) == 1 and records[0].full_path:
            act_open_dir = QAction("打开所在目录", self)
            act_open_dir.triggered.connect(lambda: self._open_containing_folder(records[0].full_path))
            menu.addAction(act_open_dir)

        menu.addSeparator()

        act_delete = QAction(f"删除选中记录 ({len(records)} 条)", self)
        act_delete.triggered.connect(lambda: self._delete_selected(records))
        menu.addAction(act_delete)

        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _copy_field(self, records: list[DataRecord], field_type: str) -> None:
        if field_type == "code":
            lines = [r.canonical_code for r in records if r.canonical_code]
        else:
            lines = [r.file_name or r.raw_text for r in records]
        QApplication.clipboard().setText("\n".join(lines))
        self.ctx.notify(f"已复制 {len(lines)} 条到剪贴板", "success")

    def _open_containing_folder(self, full_path: str) -> None:
        open_local_folder_safely(full_path, self.ctx)

    # ------------------------------------------------------------------
    # Threaded delete & export
    # ------------------------------------------------------------------

    def _is_worker_running(self, worker: Optional[WorkerThread]) -> bool:
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            return False

    def _delete_selected(self, records: list[DataRecord]) -> None:
        if self._is_worker_running(self._delete_worker):
            self.ctx.notify("正在执行删除任务，请稍候...", "warning")
            return
        ids = [r.id for r in records if r.id is not None]
        if not ids:
            return
        preview_lines = [
            f"[{r.canonical_code}] {r.file_name or r.raw_text}" for r in records[:100]
        ]
        if len(records) > 100:
            preview_lines.append(f"... 共 {len(records)} 条记录")
        dlg = ScrollableDetailDialog(
            title="确认删除记录",
            summary=f"即将从当前数据库永久删除 {len(ids)} 条记录（删除前将自动创建数据库备份）：",
            detail_text="\n".join(preview_lines),
            confirm_mode=True,
            confirm_label="确认删除",
            danger=True,
            parent=self,
        )
        if dlg.exec() != ScrollableDetailDialog.DialogCode.Accepted:
            return

        self._table.setEnabled(False)
        self._footer_label.setText(f"正在后台安全删除 {len(ids)} 条记录（含自动备份）...")

        def _task() -> int:
            return self.ctx.db.delete_records(ids)

        worker = WorkerThread(_task, parent=self)
        self._delete_worker = worker
        worker.succeeded.connect(self._on_delete_succeeded)
        worker.failed.connect(self._on_delete_failed)
        worker.finished.connect(lambda w=worker: self._on_delete_finished(w))
        if not hasattr(worker, "safe_stop"):
            worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_delete_succeeded(self, deleted: int) -> None:
        self._table.setEnabled(True)
        self.ctx.notify(f"已删除 {deleted} 条记录（已自动备份）", "success")
        self.ctx.data_changed.emit()

    def _on_delete_failed(self, detail: str) -> None:
        self._table.setEnabled(True)
        self._update_footer()
        ScrollableDetailDialog("删除记录失败", "删除过程中发生数据库错误：", detail, parent=self).exec()

    def _on_delete_finished(self, worker: WorkerThread) -> None:
        if self._delete_worker is worker:
            self._delete_worker = None
        self._table.setEnabled(True)

    def _export(self, fmt: str) -> None:
        if self._is_worker_running(self._export_worker):
            self.ctx.notify("正在导出文件，请稍候...", "warning")
            return
        records = self._model.all_records()
        if not records:
            self.ctx.notify("当前列表没有可导出的记录", "warning")
            return

        default_dir = self.ctx.config.default_export_dir or str(Path.home() / "Desktop")
        ext_map = {
            "xlsx": ("Excel 工作簿 (*.xlsx)", "数据总库导出.xlsx"),
            "csv": ("CSV 表格 (*.csv)", "数据总库导出.csv"),
            "txt": ("编码清单 (*.txt)", "标准编码清单.txt"),
        }
        file_filter, default_name = ext_map[fmt]
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "选择导出保存位置",
            str(Path(default_dir) / default_name),
            file_filter,
        )
        if not save_path:
            return

        try:
            self.ctx.config.default_export_dir = str(Path(save_path).parent)
            self.ctx.config_manager.save()
        except OSError:
            pass

        self._export_btn.setEnabled(False)
        self._footer_label.setText(f"正在后台导出 {len(records):,} 条记录至 {Path(save_path).name}...")

        def _task() -> str:
            if fmt == "xlsx":
                code_counts = self.ctx.db.get_code_counts()
                stats = self.ctx.db.get_stats()
                export_records_excel(records, save_path, code_counts=code_counts, stats=stats)
            elif fmt == "csv":
                code_counts = self.ctx.db.get_code_counts()
                export_records_csv(records, save_path, code_counts=code_counts)
            else:
                export_codes_txt(records, save_path, unique=True, sort_codes=True)
            return save_path

        worker = WorkerThread(_task, parent=self)
        self._export_worker = worker
        worker.succeeded.connect(self._on_export_succeeded)
        worker.failed.connect(self._on_export_failed)
        worker.finished.connect(lambda w=worker: self._on_export_finished(w))
        if not hasattr(worker, "safe_stop"):
            worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_export_succeeded(self, save_path: str) -> None:
        self._export_btn.setEnabled(True)
        self._update_footer()
        self.ctx.notify(f"导出成功：{Path(save_path).name}", "success")

    def _on_export_failed(self, detail: str) -> None:
        self._export_btn.setEnabled(True)
        self._update_footer()
        ScrollableDetailDialog("导出失败", "导出过程中发生错误：", detail, parent=self).exec()

    def _on_export_finished(self, worker: WorkerThread) -> None:
        if self._export_worker is worker:
            self._export_worker = None
        self._export_btn.setEnabled(True)
