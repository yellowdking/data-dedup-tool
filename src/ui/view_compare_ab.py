"""Mode B (Dual-List Coverage & Missing-Item Comparison) mixin for CompareView.

All UI labels, placeholders, tooltips, and comments are strictly neutral.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.dedup import compare_two_sources
from src.core.exporter import (
    export_checklist_csv,
    export_codes_txt,
    export_compare_result_excel,
)
from src.core.models import ChecklistEntry, CompareResult
from src.ui.compare_ab_models import (
    _AB_MAX_DIRECT_PASTE_LINES,
    _AB_VISUAL_PREVIEW_LINES,
    _COMPARE_FILE_FILTER,
    ChecklistTableModel,
    CoverageBannerWidget,
    MatchedDetailTableModel,
    MissingOnlyTableModel,
    OnlyInATableModel,
    _DroppablePlainTextEdit,
    _SourceSideState,
    _build_files_preview_text,
    _repolish,
    _stop_worker_safely,
    build_ab_table_view,
    load_ab_side_items,
)
from src.ui.widgets import ScrollableDetailDialog, WorkerThread

__all__ = [
    "CompareABMixin",
    "_COMPARE_FILE_FILTER",
    "_DroppablePlainTextEdit",
    "_stop_worker_safely",
]


class CompareABMixin:
    """Mixin implementing Mode B (双列表覆盖率与缺漏对比) for CompareView."""

    def _init_ab_state(self, theme: str) -> None:
        self._ab_result: Optional[CompareResult] = None
        self._ab_worker: Optional[WorkerThread] = None
        self._ab_gen: int = 0
        self._ab_cancel_handled_gen: int = -1
        self._ab_sides: dict[str, _SourceSideState] = {
            "A": _SourceSideState(),
            "B": _SourceSideState(),
        }
        self._ab_editors: dict[str, _DroppablePlainTextEdit] = {}
        self._ab_badges: dict[str, QLabel] = {}
        self._ab_side_buttons: dict[str, list[QPushButton]] = {"A": [], "B": []}

        self._model_ab_checklist = ChecklistTableModel(parent=self, theme=theme)
        self._model_ab_missing = MissingOnlyTableModel(parent=self, theme=theme)
        self._model_ab_matched = MatchedDetailTableModel(parent=self, theme=theme)
        self._model_ab_only_a = OnlyInATableModel(parent=self, theme=theme)

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ab_compare_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)

        # Top: Dual-source cards + swap + control bar
        top_panel = QWidget()
        top_layout = QVBoxLayout(top_panel)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(8)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(8)
        cards_row.addWidget(
            self._build_ab_source_card(
                side="A",
                title="清单 A · 已有基准清单",
                placeholder=(
                    "点击「载入当前总库」一键载入全库，或粘贴 / 拖入基准清单文件：\n"
                    "ABC-001.mp4\nPRJ-0042_4K_CHS.mkv\n支持 TXT / Word / Excel / CSV 多文件拖入"
                ),
            ),
            1,
        )
        swap_col = QVBoxLayout()
        swap_col.addStretch()
        self._btn_swap_ab = QPushButton("⇄")
        self._btn_swap_ab.setObjectName("SwapABButton")
        self._btn_swap_ab.setToolTip("互换清单 A（基准清单）与清单 B（目标核对清单）")
        self._btn_swap_ab.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_swap_ab.clicked.connect(self.swap_ab_sides)
        swap_col.addWidget(self._btn_swap_ab, 0, Qt.AlignmentFlag.AlignCenter)
        swap_col.addStretch()
        cards_row.addLayout(swap_col)
        cards_row.addWidget(
            self._build_ab_source_card(
                side="B",
                title="清单 B · 目标核对清单",
                placeholder=(
                    "在此粘贴待核对的目标清单（每行一条），或直接拖入文档文件：\n"
                    "#1 ABC-001 示例条目一\nNo.2 PRJ-0042 示例条目二\n按原顺序逐条核对覆盖率与缺失项"
                ),
            ),
            1,
        )
        top_layout.addLayout(cards_row, 1)

        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(10)
        mode_lbl = QLabel("比对模式：")
        mode_lbl.setProperty("role", "subtitle")
        ctrl_row.addWidget(mode_lbl)

        self._cmb_ab_match_mode = QComboBox()
        self._cmb_ab_match_mode.addItem("智能编码比对（推荐）", "smart_code")
        self._cmb_ab_match_mode.addItem("精确文本行比对", "exact_line")
        self._cmb_ab_match_mode.setToolTip(
            "智能编码比对：自动提取标准编码，忽略命名格式、画质标签与排名序号差异；\n"
            "精确文本行比对：按原始文本行直接对比，适合通用办公名单/非编码清单"
        )
        self._cmb_ab_match_mode.setMinimumWidth(168)
        ctrl_row.addWidget(self._cmb_ab_match_mode)

        self._chk_ab_ignore_comp = QCheckBox("过滤伴随扩展名")
        self._chk_ab_ignore_comp.setChecked(self.ctx.config.ignore_companion_files)
        self._chk_ab_ignore_comp.setToolTip("自动过滤 .jpg / .nfo / .srt 等伴随扩展名条目")
        ctrl_row.addWidget(self._chk_ab_ignore_comp)
        ctrl_row.addStretch()

        self._ab_progress = QProgressBar()
        self._ab_progress.setFixedWidth(120)
        self._ab_progress.setVisible(False)
        ctrl_row.addWidget(self._ab_progress)

        self._btn_cancel_ab = QPushButton("取消")
        self._btn_cancel_ab.setVisible(False)
        self._btn_cancel_ab.clicked.connect(self._cancel_ab_worker)
        ctrl_row.addWidget(self._btn_cancel_ab)

        self._btn_run_ab = QPushButton("▶ 开始覆盖率对比")
        self._btn_run_ab.setProperty("variant", "primary")
        self._btn_run_ab.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_run_ab.clicked.connect(self.run_ab_compare)
        ctrl_row.addWidget(self._btn_run_ab)

        top_layout.addLayout(ctrl_row)
        splitter.addWidget(top_panel)

        # Bottom: Coverage Banner + Action Bar + 4 Sub-Tabs
        bottom_panel = QWidget()
        bottom_layout = QVBoxLayout(bottom_panel)
        bottom_layout.setContentsMargins(0, 4, 0, 0)
        bottom_layout.setSpacing(8)

        banner = CoverageBannerWidget()
        self._ab_coverage_banner = banner
        self._lbl_ab_verdict = banner.lbl_verdict
        self._lbl_ab_percent = banner.lbl_percent
        self._ab_coverage_bar = banner.coverage_bar
        self._stat_b_total = banner.stat_b_total
        self._stat_b_matched = banner.stat_b_matched
        self._stat_b_missing = banner.stat_b_missing
        self._stat_a_only = banner.stat_a_only
        bottom_layout.addWidget(banner)

        bottom_layout.addLayout(self._build_ab_action_bar())
        bottom_layout.addWidget(self._build_ab_result_tabs(), 1)
        splitter.addWidget(bottom_panel)
        splitter.setSizes([215, 435])

        layout.addWidget(splitter, 1)
        self._update_ab_run_button()
        return widget

    def _build_ab_source_card(self, *, side: str, title: str, placeholder: str) -> QFrame:
        card = QFrame()
        card.setProperty("sourceCard", True)
        card.setProperty("sourceRole", "base" if side == "A" else "target")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        lbl_title = QLabel(title)
        lbl_title.setProperty("role", "section")
        hdr.addWidget(lbl_title)
        hdr.addStretch()

        badge = QLabel("未载入")
        badge.setProperty("badge", "muted")
        self._ab_badges[side] = badge
        hdr.addWidget(badge)
        card_layout.addLayout(hdr)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        btn_db = QPushButton("载入当前总库")
        btn_db.setProperty("compact", True)
        if side == "A":
            btn_db.setProperty("variant", "outline")
        btn_db.setToolTip("一键载入当前激活数据库中的全部记录，无需手动导出")
        btn_db.clicked.connect(lambda _=False, s=side: self.load_db_to_ab_side(s))
        btn_row.addWidget(btn_db)

        btn_file = QPushButton("选择文件")
        btn_file.setProperty("compact", True)
        btn_file.setToolTip("支持多选 .txt / .docx / .xlsx / .xlsm / .csv / .tsv 文件")
        btn_file.clicked.connect(lambda _=False, s=side: self._pick_ab_files(s))
        btn_row.addWidget(btn_file)
        btn_row.addStretch()

        btn_clear = QPushButton("清空")
        btn_clear.setProperty("compact", True)
        btn_clear.setProperty("variant", "ghost")
        btn_clear.clicked.connect(lambda _=False, s=side: self.clear_ab_side(s))
        btn_row.addWidget(btn_clear)
        card_layout.addLayout(btn_row)

        self._ab_side_buttons[side] = [btn_db, btn_file, btn_clear]
        if side == "A":
            self._btn_ab_load_db_a, self._btn_ab_pick_a, self._btn_ab_clear_a = btn_db, btn_file, btn_clear
        else:
            self._btn_ab_load_db_b, self._btn_ab_pick_b, self._btn_ab_clear_b = btn_db, btn_file, btn_clear

        editor = _DroppablePlainTextEdit()
        editor.setPlaceholderText(placeholder)
        editor.setMinimumHeight(88)
        editor.files_dropped.connect(lambda paths, s=side: self.set_ab_files(s, paths))
        editor.textChanged.connect(lambda s=side: self._on_ab_editor_text_changed(s))
        card_layout.addWidget(editor, 1)
        self._ab_editors[side] = editor
        if side == "A":
            self._ab_input_a = editor
        else:
            self._ab_input_b = editor
        return card

    def _build_ab_action_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self._btn_copy_missing = QPushButton("📋 复制全部缺失编码")
        self._btn_copy_missing.setProperty("variant", "outline")
        self._btn_copy_missing.setEnabled(False)
        self._btn_copy_missing.setToolTip("按目标清单原顺序复制全部缺失编码到剪贴板")
        self._btn_copy_missing.clicked.connect(self.copy_ab_missing_codes)
        self._btn_copy_matched = QPushButton("📋 复制已包含编码")
        self._btn_copy_matched.setEnabled(False)
        self._btn_copy_matched.clicked.connect(self.copy_ab_matched_codes)
        self._btn_export_missing = QPushButton("📄 导出缺失清单 (TXT/CSV)")
        self._btn_export_missing.setEnabled(False)
        self._btn_export_missing.clicked.connect(lambda: self.export_ab_missing())
        self._btn_export_ab = QPushButton("📊 导出完整对比报告 (Excel)")
        self._btn_export_ab.setEnabled(False)
        self._btn_export_ab.clicked.connect(lambda: self.export_ab_full_report())
        row.addWidget(self._btn_copy_missing)
        row.addWidget(self._btn_copy_matched)
        row.addStretch()
        row.addWidget(self._btn_export_missing)
        row.addWidget(self._btn_export_ab)
        return row

    def _make_filter_pill(self, text: str, status: str, pill_id: int, tone: str = "") -> QPushButton:
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setProperty("filterPill", True)
        if tone:
            btn.setProperty("pillTone", tone)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda _=False, st=status: self.set_ab_checklist_filter(st))
        self._ab_filter_group.addButton(btn, pill_id)
        return btn

    def _build_ab_result_tabs(self) -> QTabWidget:
        self._ab_result_tabs = QTabWidget()
        self._ab_result_tabs.setProperty("subtabs", True)

        tab_checklist = QWidget()
        cl_layout = QVBoxLayout(tab_checklist)
        cl_layout.setContentsMargins(8, 8, 8, 8)
        cl_layout.setSpacing(8)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        self._ab_filter_group = QButtonGroup(self)
        self._ab_filter_group.setExclusive(True)

        self._btn_filter_all = self._make_filter_pill("全部 (0)", "all", 0)
        self._btn_filter_all.setChecked(True)
        self._btn_filter_missing = self._make_filter_pill("❌ 只看缺失 (0)", "missing", 1, "danger")
        self._btn_filter_matched = self._make_filter_pill("✅ 只看已包含 (0)", "matched", 2, "success")
        for btn in (self._btn_filter_all, self._btn_filter_missing, self._btn_filter_matched):
            filter_row.addWidget(btn)
        filter_row.addStretch()

        self._ab_checklist_search = QLineEdit()
        self._ab_checklist_search.setPlaceholderText("搜索编码 / 目标文本 / 基准A文件名...")
        self._ab_checklist_search.setClearButtonEnabled(True)
        self._ab_checklist_search.setMinimumWidth(220)
        self._ab_checklist_search.setMaximumWidth(320)
        self._ab_checklist_search.textChanged.connect(self._model_ab_checklist.set_search_query)
        filter_row.addWidget(self._ab_checklist_search)
        cl_layout.addLayout(filter_row)

        self._tbl_ab_checklist = build_ab_table_view(
            self._model_ab_checklist,
            ChecklistTableModel.DEFAULT_WIDTHS,
            alternating=False,
            menu_callback=lambda tv, pos: self._show_ab_table_menu(tv, self._model_ab_checklist, "checklist", pos),
        )
        cl_layout.addWidget(self._tbl_ab_checklist, 1)

        self._tbl_ab_missing = build_ab_table_view(
            self._model_ab_missing,
            MissingOnlyTableModel.DEFAULT_WIDTHS,
            alternating=True,
            menu_callback=lambda tv, pos: self._show_ab_table_menu(tv, self._model_ab_missing, "missing", pos),
        )
        self._tbl_ab_matched = build_ab_table_view(
            self._model_ab_matched,
            MatchedDetailTableModel.DEFAULT_WIDTHS,
            alternating=True,
            menu_callback=lambda tv, pos: self._show_ab_table_menu(tv, self._model_ab_matched, "matched", pos),
        )
        self._tbl_ab_only_a = build_ab_table_view(
            self._model_ab_only_a,
            OnlyInATableModel.DEFAULT_WIDTHS,
            alternating=True,
            menu_callback=lambda tv, pos: self._show_ab_table_menu(tv, self._model_ab_only_a, "only_a", pos),
        )

        self._ab_result_tabs.addTab(tab_checklist, "📋 逐条核对 (0)")
        self._ab_result_tabs.addTab(self._tbl_ab_missing, "❌ 缺失清单 (0)")
        self._ab_result_tabs.addTab(self._tbl_ab_matched, "✅ 已包含对照 (0)")
        self._ab_result_tabs.addTab(self._tbl_ab_only_a, "📦 仅基准A独有 (0)")
        self._ab_result_tabs.setCurrentIndex(0)
        return self._ab_result_tabs

    # ------------------------------------------------------------------
    # Source Input Actions (DB / Multi-file / Paste / Clear / Swap)
    # ------------------------------------------------------------------

    def _set_side_badge(self, side: str, text: str, tone: str, tooltip: str = "") -> None:
        badge = self._ab_badges[side]
        badge.setText(text)
        badge.setToolTip(tooltip or text)
        badge.setProperty("badge", tone)
        _repolish(badge)

    def _set_editor_text_silently(self, side: str, text: str) -> None:
        editor = self._ab_editors[side]
        editor.blockSignals(True)
        editor.setPlainText(text)
        editor.blockSignals(False)

    def load_db_to_ab_side(self, side: str) -> int:
        """Bind the active SQLite database to List A or List B with an instant preview."""
        if side not in ("A", "B"):
            return 0
        stats = self.ctx.db.get_stats()
        total = stats.total_records
        if total <= 0:
            self.ctx.notify("当前数据库为空，请先导入数据或改用文件/粘贴方式", "warning")
            return 0

        db_path = str(self.ctx.db.db_path)
        db_name = Path(db_path).name
        preview_records = self.ctx.db.query_records(limit=120, sort_by="id", sort_desc=False)
        preview_lines = [
            f"{rec.canonical_code}  |  {rec.file_name or rec.raw_text}" for rec in preview_records
        ]
        if total > len(preview_lines):
            preview_lines.append(
                f"... 当前总库共 {total:,} 条记录（仅预览前 {len(preview_lines)} 条，对比时自动载入全库数据）"
            )

        self._ab_sides[side] = _SourceSideState(
            kind="db", db_path=db_path, db_name=db_name, db_count=total, line_count=total
        )
        self._set_editor_text_silently(side, "\n".join(preview_lines))
        self._set_side_badge(
            side,
            f"当前总库 ({total:,} 条)",
            "success",
            tooltip=f"已绑定当前数据库：{db_path}（共 {total:,} 条记录，{stats.unique_codes:,} 个唯一编码）",
        )
        self._update_ab_run_button()
        self.ctx.notify(f"已载入当前总库至清单 {side}（{total:,} 条记录）", "success")
        return total

    def _pick_ab_files(self, side: str) -> None:
        start_dir = self.ctx.config.default_import_dir or str(Path.home())
        paths, _ = QFileDialog.getOpenFileNames(
            self, f"选择清单 {side} 文件（可多选）", start_dir, _COMPARE_FILE_FILTER
        )
        if not paths:
            return
        try:
            self.ctx.config.default_import_dir = str(Path(paths[0]).parent)
            self.ctx.config_manager.save()
        except OSError:
            pass
        self.set_ab_files(side, paths)

    def set_ab_files(self, side: str, paths: list[str]) -> None:
        """Bind one or more local files to List A or List B."""
        if side not in ("A", "B"):
            return
        valid_paths = [str(Path(p)) for p in paths if p and Path(p).is_file()]
        if not valid_paths:
            return
        self._ab_sides[side] = _SourceSideState(kind="files", file_paths=valid_paths)
        self._set_editor_text_silently(side, _build_files_preview_text(valid_paths))
        if len(valid_paths) == 1:
            self._set_side_badge(side, f"文件: {Path(valid_paths[0]).name}", "accent", tooltip=valid_paths[0])
        else:
            self._set_side_badge(
                side, f"已选 {len(valid_paths)} 个文件", "accent", tooltip="\n".join(valid_paths)
            )
        self._update_ab_run_button()

    def set_ab_text(self, side: str, text: str) -> None:
        """Set text content on List A or List B, capping huge previews gracefully."""
        if side not in ("A", "B"):
            return
        raw_lines = [ln for ln in text.splitlines() if ln.strip()]
        line_count = len(raw_lines)
        if line_count == 0:
            self.clear_ab_side(side)
            return
        if line_count > _AB_MAX_DIRECT_PASTE_LINES:
            preview = raw_lines[:_AB_VISUAL_PREVIEW_LINES]
            preview.append(
                f"... 共 {line_count:,} 行（为保证界面流畅，仅预览前 {len(preview)} 行，完整内容已缓存在内存中）"
            )
            self._ab_sides[side] = _SourceSideState(
                kind="text", full_text_override=text, line_count=line_count
            )
            self._set_editor_text_silently(side, "\n".join(preview))
        else:
            self._ab_sides[side] = _SourceSideState(kind="text", line_count=line_count)
            self._set_editor_text_silently(side, text)
        self._set_side_badge(side, f"手动粘贴 ({line_count:,} 行)", "accent")
        self._update_ab_run_button()

    def clear_ab_side(self, side: str) -> None:
        if side not in ("A", "B"):
            return
        self._ab_sides[side] = _SourceSideState()
        self._set_editor_text_silently(side, "")
        self._set_side_badge(side, "未载入", "muted")
        self._update_ab_run_button()

    def _on_ab_editor_text_changed(self, side: str) -> None:
        non_empty = sum(1 for ln in self._ab_editors[side].toPlainText().splitlines() if ln.strip())
        if non_empty == 0:
            self._ab_sides[side] = _SourceSideState()
            self._set_side_badge(side, "未载入", "muted")
        else:
            self._ab_sides[side] = _SourceSideState(kind="text", line_count=non_empty)
            self._set_side_badge(side, f"手动粘贴 ({non_empty:,} 行)", "accent")
        self._update_ab_run_button()

    def swap_ab_sides(self) -> None:
        """Swap all source bindings, preview texts, and badges between List A and List B."""
        if self._is_worker_running(self._ab_worker):
            self.ctx.notify("正在执行对比，请等待完成或先取消后再互换", "warning")
            return
        text_a, text_b = self._ab_editors["A"].toPlainText(), self._ab_editors["B"].toPlainText()
        ba = (self._ab_badges["A"].text(), str(self._ab_badges["A"].property("badge") or "muted"), self._ab_badges["A"].toolTip())
        bb = (self._ab_badges["B"].text(), str(self._ab_badges["B"].property("badge") or "muted"), self._ab_badges["B"].toolTip())
        self._ab_sides["A"], self._ab_sides["B"] = self._ab_sides["B"], self._ab_sides["A"]
        self._set_editor_text_silently("A", text_b)
        self._set_editor_text_silently("B", text_a)
        self._set_side_badge("A", *bb)
        self._set_side_badge("B", *ba)
        self._update_ab_run_button()
        self.ctx.notify("已互换清单 A 与清单 B", "info")

    def set_ab_match_mode(self, mode: str) -> None:
        idx = self._cmb_ab_match_mode.findData(mode)
        if idx >= 0:
            self._cmb_ab_match_mode.setCurrentIndex(idx)

    def _ab_side_ready(self, side: str) -> bool:
        return self._ab_sides[side].has_source(self._ab_editors[side].toPlainText())

    def _update_ab_run_button(self) -> None:
        running = self._is_worker_running(self._ab_worker)
        self._btn_run_ab.setEnabled((not running) and self._ab_side_ready("A") and self._ab_side_ready("B"))

    def _set_ab_busy(self, busy: bool) -> None:
        self._ab_progress.setVisible(busy)
        if busy:
            self._ab_progress.setRange(0, 0)
        self._btn_cancel_ab.setVisible(busy)
        self._btn_cancel_ab.setEnabled(busy)
        self._btn_swap_ab.setEnabled(not busy)
        self._cmb_ab_match_mode.setEnabled(not busy)
        self._chk_ab_ignore_comp.setEnabled(not busy)
        for side in ("A", "B"):
            for btn in self._ab_side_buttons[side]:
                btn.setEnabled(not busy)
            self._ab_editors[side].setReadOnly(busy)
        if busy:
            for btn in (
                self._btn_run_ab,
                self._btn_copy_missing,
                self._btn_copy_matched,
                self._btn_export_missing,
                self._btn_export_ab,
            ):
                btn.setEnabled(False)
        else:
            self._update_ab_run_button()

    # ------------------------------------------------------------------
    # DB Lifecycle & Theme Hooks
    # ------------------------------------------------------------------

    def _on_ab_db_switched(self, _db_path: str) -> None:
        bound_sides = [s for s in ("A", "B") if self._ab_sides[s].kind == "db"]
        if not bound_sides:
            return
        if self._is_worker_running(self._ab_worker):
            self._cancel_ab_worker()
        stats = self.ctx.db.get_stats()
        for side in bound_sides:
            if stats.total_records > 0:
                self.load_db_to_ab_side(side)
            else:
                self.clear_ab_side(side)

    def _on_ab_data_changed(self) -> None:
        stats = None
        for side in ("A", "B"):
            if self._ab_sides[side].kind == "db":
                if stats is None:
                    stats = self.ctx.db.get_stats()
                self._ab_sides[side].db_count = stats.total_records
                self._ab_sides[side].line_count = stats.total_records
                self._set_side_badge(
                    side,
                    f"当前总库 ({stats.total_records:,} 条)",
                    "success" if stats.total_records > 0 else "muted",
                )
        self._update_ab_run_button()

    def _apply_ab_theme(self, theme: str) -> None:
        for model in (
            self._model_ab_checklist,
            self._model_ab_missing,
            self._model_ab_matched,
            self._model_ab_only_a,
        ):
            model.set_theme(theme)
        self._ab_coverage_banner.repolish_all()
        _repolish(self._ab_badges["A"])
        _repolish(self._ab_badges["B"])

    # ------------------------------------------------------------------
    # Async Dual-List Coverage Comparison
    # ------------------------------------------------------------------

    def _snapshot_side_payload(self, side: str, default_name: str) -> dict[str, Any]:
        st = self._ab_sides[side]
        text_data = st.full_text_override if st.full_text_override is not None else self._ab_editors[side].toPlainText()
        return {
            "kind": st.kind,
            "file_paths": list(st.file_paths),
            "text": text_data,
            "source_name": st.source_label(default_name),
        }

    def run_ab_compare(self) -> None:
        if self._is_worker_running(self._ab_worker):
            self.ctx.notify("正在执行覆盖率对比，请稍候或先取消", "warning")
            return
        if not self._ab_side_ready("A") or not self._ab_side_ready("B"):
            self.ctx.notify("请先为清单 A 和清单 B 分别提供数据来源", "warning")
            return

        match_mode = str(self._cmb_ab_match_mode.currentData() or "smart_code")
        ignore_comp = self._chk_ab_ignore_comp.isChecked()
        comp_exts = self._get_companion_exts(ignore_comp)
        payload_a = self._snapshot_side_payload("A", "基准清单A")
        payload_b = self._snapshot_side_payload("B", "目标清单B")

        self._ab_gen += 1
        gen = self._ab_gen
        self._set_ab_busy(True)
        self._lbl_ab_verdict.setText("正在后台解析并计算覆盖率与缺失清单...")

        worker_box: list[Optional[WorkerThread]] = [None]

        def _is_cancelled() -> bool:
            w = worker_box[0]
            return bool(w is not None and w.is_cancelled)

        def _task() -> Optional[CompareResult]:
            items_a = load_ab_side_items(
                self.ctx, payload_a, match_mode=match_mode, ignore_comp=ignore_comp, comp_exts=comp_exts, cancel_check=_is_cancelled
            )
            if items_a is None or _is_cancelled():
                return None
            items_b = load_ab_side_items(
                self.ctx, payload_b, match_mode=match_mode, ignore_comp=ignore_comp, comp_exts=comp_exts, cancel_check=_is_cancelled
            )
            if items_b is None or _is_cancelled():
                return None
            return compare_two_sources(
                items_a,
                items_b,
                source_a_name=payload_a["source_name"],
                source_b_name=payload_b["source_name"],
                match_mode=match_mode,
            )

        worker = WorkerThread(_task, parent=self)
        worker_box[0] = worker
        self._ab_worker = worker
        worker.succeeded.connect(lambda res, g=gen: self._on_ab_compare_succeeded(g, res))
        worker.failed.connect(lambda detail, g=gen: self._on_ab_compare_failed(g, detail))
        if hasattr(worker, "cancelled"):
            worker.cancelled.connect(lambda g=gen: self._on_ab_compare_cancelled(g))
        worker.finished.connect(lambda w=worker, g=gen: self._on_ab_worker_finished(w, g))
        if not hasattr(worker, "safe_stop"):
            worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_ab_compare_succeeded(self, gen: int, result: Optional[CompareResult]) -> None:
        if gen != self._ab_gen:
            return
        self._set_ab_busy(False)
        if result is None:
            self._on_ab_compare_cancelled(gen)
            return

        self._ab_result = result
        checklist = list(result.target_checklist)
        missing_entries = [e for e in checklist if e.status == "missing"]
        matched_entries = [e for e in checklist if e.status == "matched"]

        self._ab_checklist_search.blockSignals(True)
        self._ab_checklist_search.clear()
        self._ab_checklist_search.blockSignals(False)
        self._btn_filter_all.setChecked(True)

        self._model_ab_checklist.set_entries(checklist, reset_filter=True)
        self._model_ab_missing.set_entries(missing_entries, source_b_name=result.source_b_name)
        self._model_ab_matched.set_entries(matched_entries)
        self._model_ab_only_a.set_rows(result.only_in_a)

        for tv in (self._tbl_ab_checklist, self._tbl_ab_missing, self._tbl_ab_matched, self._tbl_ab_only_a):
            tv.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)

        self._btn_filter_all.setText(f"全部 ({len(checklist):,})")
        self._btn_filter_missing.setText(f"❌ 只看缺失 ({len(missing_entries):,})")
        self._btn_filter_matched.setText(f"✅ 只看已包含 ({len(matched_entries):,})")

        self._ab_result_tabs.setTabText(0, f"📋 逐条核对 ({len(checklist):,})")
        self._ab_result_tabs.setTabText(1, f"❌ 缺失清单 ({len(missing_entries):,})")
        self._ab_result_tabs.setTabText(2, f"✅ 已包含对照 ({len(matched_entries):,})")
        self._ab_result_tabs.setTabText(3, f"📦 仅基准A独有 ({len(result.only_in_a):,})")
        self._ab_result_tabs.setCurrentIndex(0)

        self._ab_coverage_banner.update_from_result(result)
        self._restore_ab_action_buttons()
        self.ctx.notify(
            f"覆盖率对比完成：{result.coverage_rate:.1f}%（已包含 {result.hit_b_count:,} / {result.unique_b_count:,}，缺失 {result.missing_b_count:,} 项）",
            "success" if result.is_fully_covered else "info",
        )

    def _on_ab_compare_cancelled(self, gen: int) -> None:
        if gen != self._ab_gen or self._ab_cancel_handled_gen == gen:
            return
        self._ab_cancel_handled_gen = gen
        self._set_ab_busy(False)
        self._lbl_ab_verdict.setText("已取消覆盖率对比")
        self._restore_ab_action_buttons()
        self.ctx.notify("已取消双列表覆盖率对比", "info")

    def _on_ab_compare_failed(self, gen: int, detail: str) -> None:
        if gen != self._ab_gen:
            return
        self._set_ab_busy(False)
        self._lbl_ab_verdict.setText("覆盖率对比失败，请查看详情")
        self._restore_ab_action_buttons()
        ScrollableDetailDialog("读取或比对失败", "执行双列表覆盖率对比时发生错误：", detail, parent=self).exec()

    def _on_ab_worker_finished(self, worker: WorkerThread, gen: int) -> None:
        was_cancelled = False
        try:
            was_cancelled = worker.is_cancelled
        except RuntimeError:
            pass
        if self._ab_worker is worker:
            self._ab_worker = None
        if was_cancelled and gen == self._ab_gen and self._ab_cancel_handled_gen != gen:
            self._on_ab_compare_cancelled(gen)

    def _cancel_ab_worker(self) -> None:
        if self._is_worker_running(self._ab_worker):
            assert self._ab_worker is not None
            self._ab_worker.cancel()
            self._btn_cancel_ab.setEnabled(False)
            self._lbl_ab_verdict.setText("正在取消覆盖率对比...")

    def _restore_ab_action_buttons(self) -> None:
        res = self._ab_result
        has_missing = bool(res and res.missing_b_codes)
        self._btn_copy_missing.setEnabled(has_missing)
        self._btn_copy_matched.setEnabled(bool(res and res.matched_b_codes))
        self._btn_export_missing.setEnabled(has_missing)
        self._btn_export_ab.setEnabled(res is not None)

    # ------------------------------------------------------------------
    # Checklist Filtering, Clipboard Actions, Context Menu & Async Export
    # ------------------------------------------------------------------

    def set_ab_checklist_filter(self, status: str) -> None:
        norm = (status or "all").strip().lower()
        if norm == "missing":
            self._btn_filter_missing.setChecked(True)
        elif norm == "matched":
            self._btn_filter_matched.setChecked(True)
        else:
            norm = "all"
            self._btn_filter_all.setChecked(True)
        self._model_ab_checklist.set_status_filter(norm)

    def _copy_ab_codes(self, codes: list[str], label: str) -> int:
        if not codes:
            return 0
        QApplication.clipboard().setText("\n".join(codes))
        self.ctx.notify(f"已复制 {len(codes):,} 个{label}到剪贴板", "success")
        return len(codes)

    def copy_ab_missing_codes(self) -> int:
        return self._copy_ab_codes(list(self._ab_result.missing_b_codes), "缺失编码") if self._ab_result else 0

    def copy_ab_matched_codes(self) -> int:
        return self._copy_ab_codes(list(self._ab_result.matched_b_codes), "已包含编码") if self._ab_result else 0

    def _show_ab_table_menu(self, tv: QTableView, model: Any, menu_kind: str, pos: Any) -> None:
        sel = tv.selectionModel()
        if sel is None:
            return
        rows = sorted({idx.row() for idx in sel.selectedRows()})
        if not rows:
            return
        menu = QMenu(self)
        if menu_kind in ("checklist", "missing", "matched"):
            entries = [e for r in rows if (e := model.entry_at(r)) is not None]
            if not entries:
                return
            menu.addAction(f"复制标准编码 ({len(entries)} 条)").triggered.connect(
                lambda: self._copy_checklist_entries(entries, "code")
            )
            menu.addAction(f"复制整行 ({len(entries)} 条)").triggered.connect(
                lambda: self._copy_checklist_entries(entries, "row")
            )
            if self._ab_result and self._ab_result.missing_b_codes:
                menu.addSeparator()
                menu.addAction(
                    f"复制全部缺失编码 ({len(self._ab_result.missing_b_codes)} 条)"
                ).triggered.connect(self.copy_ab_missing_codes)
        else:
            items = [it for r in rows if (it := model.row_at(r)) is not None]
            if not items:
                return
            menu.addAction(f"复制标准编码 ({len(items)} 条)").triggered.connect(
                lambda: self._copy_selected_items(items, "code")
            )
            menu.addAction("复制文件名 / 原始文本").triggered.connect(
                lambda: self._copy_selected_items(items, "name")
            )
        menu.exec(tv.viewport().mapToGlobal(pos))

    def _copy_checklist_entries(self, entries: list[ChecklistEntry], field_mode: str) -> None:
        if field_mode == "code":
            lines = [e.canonical_code for e in entries if e.canonical_code]
        else:
            lines = [
                f"#{e.order_index}\t"
                f"{'已包含' if e.status == 'matched' else ('缺失' if e.status == 'missing' else '未识别')}\t"
                f"{e.canonical_code}\t{e.target_text}\t{e.matched_a_count}\t"
                f"{' | '.join(e.matched_a_names) if e.matched_a_names else '-'}"
                for e in entries
            ]
        QApplication.clipboard().setText("\n".join(lines))
        self.ctx.notify(f"已复制 {len(lines)} 条到剪贴板", "success")

    def _run_ab_export(
        self, title: str, default_name: str, file_filter: str, explicit_path: Optional[str], task_factory: Any
    ) -> None:
        if self._is_worker_running(self._export_worker):
            self.ctx.notify("正在导出文件，请稍候...", "warning")
            return
        path, sel_filter = self._pick_export_path(title, default_name, file_filter, explicit_path)
        if not path:
            return
        self._disable_export_buttons()
        self.ctx.notify(f"正在后台导出 {Path(path).name}...", "info")
        self._start_export_worker(lambda p=path, f=sel_filter: task_factory(p, f))

    def export_ab_missing(self, save_path: Optional[str] = None) -> None:
        result = self._ab_result
        if result is None or not result.missing_b_codes:
            return
        self._run_ab_export(
            "导出缺失清单",
            "缺失清单.txt",
            "缺失编码清单 (*.txt);;缺失明细表格 (*.csv)",
            save_path,
            lambda p, f: (
                export_checklist_csv(result.target_checklist, p, only_missing=True, source_b_name=result.source_b_name)
                if (p.lower().endswith(".csv") or "CSV" in f.upper())
                else export_codes_txt(
                    result.missing_b_codes, p, unique=True, sort_codes=False, uppercase=(result.match_mode == "smart_code")
                )
            )
            and p
            or p,
        )

    def export_ab_full_report(self, save_path: Optional[str] = None) -> None:
        result = self._ab_result
        if result is None:
            return
        self._run_ab_export(
            "导出完整覆盖率对比报告",
            "双列表覆盖率对比报告.xlsx",
            "Excel 完整报告 (5工作表) (*.xlsx);;逐条核对明细 (*.csv)",
            save_path,
            lambda p, f: (
                export_checklist_csv(result.target_checklist, p, only_missing=False, source_b_name=result.source_b_name)
                if (p.lower().endswith(".csv") or "CSV" in f.upper())
                else export_compare_result_excel(result, p)
            )
            and p
            or p,
        )
