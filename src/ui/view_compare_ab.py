"""Mode B (Dual-List Coverage & Missing-Item Comparison) mixin for CompareView.

Strictly neutral UI, zero emojis, zero broken Unicode arrows, and collapsible
dual-source inputs keeping 10-14 result rows visible at 1200x760.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QProgressBar, QPushButton, QStackedWidget, QTableView, QTabWidget, QToolButton,
    QVBoxLayout, QWidget,
)

from src.core.dedup import compare_two_sources
from src.core.exporter import export_checklist_csv, export_codes_txt, export_compare_result_excel
from src.core.models import CompareResult
from src.ui.compare_ab_models import (
    _AB_MAX_DIRECT_PASTE_LINES, _AB_VISUAL_PREVIEW_LINES, _COMPARE_FILE_FILTER,
    ChecklistTableModel, CoverageBannerWidget, MatchedDetailTableModel, MissingOnlyTableModel,
    OnlyInATableModel, _DroppablePlainTextEdit, _SourceSideState, _build_files_preview_text,
    _repolish, _stop_worker_safely, build_ab_table_view, load_ab_side_items,
)
from src.ui.delegates import apply_slim_scrollbars, enhance_table_view
from src.ui.icons import get_icon
from src.ui.table_copy import TableCopyHelper, copy_text_to_clipboard, format_codes_for_clipboard
from src.ui.table_header import TableHeaderManager
from src.ui.theme import get_palette
from src.ui.widgets import EmptyStateWidget, ScrollableDetailDialog, WorkerThread

__all__ = ["CompareABMixin", "_COMPARE_FILE_FILTER", "_DroppablePlainTextEdit", "_stop_worker_safely"]


def _ab_btn(
    text: str = "", *, variant: str = "", compact: bool = False,
    tooltip: str = "", slot: Optional[Callable[..., Any]] = None,
) -> QPushButton:
    btn = QPushButton(text)
    if variant:
        btn.setProperty("variant", variant)
    if compact:
        btn.setProperty("compact", True)
    if tooltip:
        btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if slot is not None:
        btn.clicked.connect(slot)
    return btn


class CompareABMixin:
    """Mixin implementing Mode B (双列表覆盖率与缺漏对比) for CompareView."""

    def _init_ab_state(self, theme: str) -> None:
        self._ab_result: Optional[CompareResult] = None
        self._ab_worker: Optional[WorkerThread] = None
        self._ab_gen: int = 0
        self._ab_cancel_handled_gen: int = -1
        self._ab_input_collapsed: bool = False
        self._ab_sides: dict[str, _SourceSideState] = {"A": _SourceSideState(), "B": _SourceSideState()}
        self._ab_editors: dict[str, _DroppablePlainTextEdit] = {}
        self._ab_badges: dict[str, QLabel] = {}
        self._ab_side_buttons: dict[str, list[QPushButton]] = {"A": [], "B": []}

        self._model_ab_checklist = ChecklistTableModel(parent=self, theme=theme)
        self._model_ab_missing = MissingOnlyTableModel(parent=self, theme=theme)
        self._model_ab_matched = MatchedDetailTableModel(parent=self, theme=theme)
        self._model_ab_only_a = OnlyInATableModel(parent=self, theme=theme)

    # --- UI Construction (collapsible inputs + 56px slim banner + consolidated toolbar) ---

    def _build_ab_compare_tab(self) -> QWidget:
        theme = self.ctx.config.theme
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(18, 10, 18, 10)
        layout.setSpacing(8)

        self._ab_input_expanded = QWidget()
        top_layout = QVBoxLayout(self._ab_input_expanded)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(6)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(8)
        cards_row.addWidget(
            self._build_ab_source_card(
                side="A", title="清单 A · 基准",
                placeholder="点击「总库」一键载入全库，或粘贴 / 拖入基准清单文件：\nABC-001.mp4\nPRJ-0042_4K.mkv",
            ), 1,
        )
        swap_col = QVBoxLayout()
        swap_col.addStretch()
        self._btn_swap_ab = _ab_btn(tooltip="互换清单 A（基准）与清单 B（目标）", slot=self.swap_ab_sides)
        self._btn_swap_ab.setObjectName("SwapABButton")
        swap_col.addWidget(self._btn_swap_ab, 0, Qt.AlignmentFlag.AlignCenter)
        swap_col.addStretch()
        cards_row.addLayout(swap_col)
        cards_row.addWidget(
            self._build_ab_source_card(
                side="B", title="清单 B · 目标",
                placeholder="粘贴待核对的目标清单（每行一条），或拖入文档文件：\n#1 ABC-001 示例条目一\nNo.2 PRJ-0042 示例条目二",
            ), 1,
        )
        top_layout.addLayout(cards_row)

        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(8)
        mode_lbl = QLabel("比对模式")
        mode_lbl.setProperty("role", "subtitle")
        ctrl_row.addWidget(mode_lbl)

        self._cmb_ab_match_mode = QComboBox()
        self._cmb_ab_match_mode.addItem("智能编码比对（推荐）", "smart_code")
        self._cmb_ab_match_mode.addItem("精确文本行比对", "exact_line")
        self._cmb_ab_match_mode.setToolTip("智能编码比对：提取标准编码忽略命名差异；精确文本行比对：按原始文本行对比")
        self._cmb_ab_match_mode.setMinimumWidth(164)
        ctrl_row.addWidget(self._cmb_ab_match_mode)

        self._chk_ab_ignore_comp = QCheckBox("过滤伴随扩展名")
        self._chk_ab_ignore_comp.setChecked(self.ctx.config.ignore_companion_files)
        self._chk_ab_ignore_comp.setToolTip("自动过滤 .jpg / .nfo / .srt 等伴随扩展名条目")
        ctrl_row.addWidget(self._chk_ab_ignore_comp)
        ctrl_row.addStretch()

        self._ab_progress = QProgressBar()
        self._ab_progress.setFixedSize(110, 10)
        self._ab_progress.setVisible(False)
        ctrl_row.addWidget(self._ab_progress)

        self._btn_cancel_ab = _ab_btn("取消", variant="ghost", compact=True, slot=self._cancel_ab_worker)
        self._btn_cancel_ab.setVisible(False)
        self._btn_toggle_ab_input = _ab_btn("收起输入区", variant="ghost", compact=True, slot=lambda: self.set_ab_input_collapsed(True))
        self._btn_run_ab = _ab_btn("开始覆盖率对比", variant="primary", slot=self.run_ab_compare)
        for b in (self._btn_cancel_ab, self._btn_toggle_ab_input, self._btn_run_ab):
            ctrl_row.addWidget(b)
        top_layout.addLayout(ctrl_row)
        layout.addWidget(self._ab_input_expanded)

        # Collapsed 34px single-row summary strip
        self._ab_input_collapsed_bar = QFrame()
        self._ab_input_collapsed_bar.setProperty("summaryStrip", True)
        self._ab_input_collapsed_bar.setFixedHeight(34)
        self._ab_input_collapsed_bar.setVisible(False)
        col_layout = QHBoxLayout(self._ab_input_collapsed_bar)
        col_layout.setContentsMargins(12, 0, 8, 0)
        col_layout.setSpacing(8)
        self._lbl_ab_collapsed_summary = QLabel("基准 A：未载入  vs  目标 B：未载入")
        self._lbl_ab_collapsed_summary.setProperty("role", "subtitle")
        col_layout.addWidget(self._lbl_ab_collapsed_summary, 1)
        self._ab_collapsed_progress = QProgressBar()
        self._ab_collapsed_progress.setFixedSize(100, 8)
        self._ab_collapsed_progress.setVisible(False)
        col_layout.addWidget(self._ab_collapsed_progress)
        self._btn_expand_ab_input = _ab_btn("展开修改", variant="ghost", compact=True, slot=lambda: self.set_ab_input_collapsed(False))
        self._btn_rerun_ab = _ab_btn("重新对比", variant="outline", compact=True, slot=self.run_ab_compare)
        col_layout.addWidget(self._btn_expand_ab_input)
        col_layout.addWidget(self._btn_rerun_ab)
        layout.addWidget(self._ab_input_collapsed_bar)

        banner = CoverageBannerWidget()
        self._ab_coverage_banner = banner
        self._lbl_ab_verdict, self._lbl_ab_percent, self._ab_coverage_bar = banner.lbl_verdict, banner.lbl_percent, banner.coverage_bar
        self._stat_b_total, self._stat_b_matched, self._stat_b_missing, self._stat_a_only = (
            banner.stat_b_total, banner.stat_b_matched, banner.stat_b_missing, banner.stat_a_only
        )
        layout.addWidget(banner)
        layout.addLayout(self._build_ab_action_bar())

        self._ab_result_stack = QStackedWidget()
        self._ab_empty_state = EmptyStateWidget(
            icon_name="layers", title="双列表覆盖率与缺失核验",
            subtitle="左侧载入基准清单 A（可一键载入当前总库），右侧提供目标核对清单 B，点击「开始覆盖率对比」快速定位缺失项",
            theme=theme,
        )
        self._ab_result_stack.addWidget(self._ab_empty_state)
        self._ab_result_stack.addWidget(self._build_ab_result_tabs())
        self._ab_result_stack.setCurrentIndex(0)
        layout.addWidget(self._ab_result_stack, 1)

        self._update_ab_run_button()
        return widget

    def _build_ab_source_card(self, *, side: str, title: str, placeholder: str) -> QFrame:
        card = QFrame()
        card.setProperty("sourceCard", True)
        card.setProperty("sourceRole", "base" if side == "A" else "target")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 8, 10, 8)
        card_layout.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        lbl_title = QLabel(title)
        lbl_title.setProperty("role", "section")
        hdr.addWidget(lbl_title)
        badge = QLabel("未载入")
        badge.setProperty("badge", "muted")
        self._ab_badges[side] = badge
        hdr.addWidget(badge)
        hdr.addStretch()

        btn_db = _ab_btn("总库", variant="outline" if side == "A" else "", compact=True, tooltip="一键载入当前激活数据库", slot=lambda _=False, s=side: self.load_db_to_ab_side(s))
        btn_file = _ab_btn("文件", compact=True, tooltip="多选 .txt / .docx / .xlsx / .csv 文件", slot=lambda _=False, s=side: self._pick_ab_files(s))
        btn_clear = _ab_btn("清空", variant="ghost", compact=True, slot=lambda _=False, s=side: self.clear_ab_side(s))
        for b in (btn_db, btn_file, btn_clear):
            hdr.addWidget(b)
        card_layout.addLayout(hdr)

        self._ab_side_buttons[side] = [btn_db, btn_file, btn_clear]
        if side == "A":
            self._btn_ab_load_db_a, self._btn_ab_pick_a, self._btn_ab_clear_a = btn_db, btn_file, btn_clear
        else:
            self._btn_ab_load_db_b, self._btn_ab_pick_b, self._btn_ab_clear_b = btn_db, btn_file, btn_clear

        editor = _DroppablePlainTextEdit()
        editor.setPlaceholderText(placeholder)
        editor.setMinimumHeight(76)
        editor.setMaximumHeight(92)
        apply_slim_scrollbars(editor, self.ctx.config.theme)
        editor.files_dropped.connect(lambda paths, s=side: self.set_ab_files(s, paths))
        editor.textChanged.connect(lambda s=side: self._on_ab_editor_text_changed(s))
        card_layout.addWidget(editor)
        self._ab_editors[side] = editor
        if side == "A":
            self._ab_input_a = editor
        else:
            self._ab_input_b = editor
        return card

    def _build_ab_action_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self._btn_copy_missing = QToolButton()
        self._btn_copy_missing.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._btn_copy_missing.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._btn_copy_missing.setProperty("variant", "outline")
        self._btn_copy_missing.setText("复制缺失编码")
        self._btn_copy_missing.setToolTip("按目标清单原顺序复制全部缺失编码（下拉可选空格/逗号/正则格式）")
        self._btn_copy_missing.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_copy_missing.setEnabled(False)
        self._btn_copy_missing.clicked.connect(lambda _=False: self.copy_ab_missing_codes())

        copy_menu = QMenu(self._btn_copy_missing)
        for lbl, fmt in (
            ("复制缺失编码（每行一个）", "newline"), ("复制为多词检索（空格分隔）", "space"),
            ("复制为逗号分隔", "comma"), ("复制为正则 OR 检索式", "regex"),
        ):
            copy_menu.addAction(lbl).triggered.connect(lambda _=False, f=fmt: self.copy_ab_missing_codes(f))
        self._btn_copy_missing.setMenu(copy_menu)
        row.addWidget(self._btn_copy_missing)

        self._btn_copy_matched = _ab_btn("复制已包含编码", variant="ghost", compact=True, slot=self.copy_ab_matched_codes)
        self._btn_copy_matched.setEnabled(False)
        row.addWidget(self._btn_copy_matched)
        row.addStretch()

        self._btn_export_ab = _ab_btn("导出报告")
        self._btn_export_ab.setEnabled(False)
        export_menu = QMenu(self._btn_export_ab)
        export_menu.addAction("完整对比报告 Excel (5 Sheet)").triggered.connect(lambda: self.export_ab_full_report())
        export_menu.addSeparator()
        act_txt = QAction("缺失清单 TXT", export_menu)
        act_txt.setEnabled(False)
        act_txt.triggered.connect(lambda: self.export_ab_missing(default_ext="txt"))
        export_menu.addAction(act_txt)
        act_csv = QAction("缺失清单 CSV", export_menu)
        act_csv.setEnabled(False)
        act_csv.triggered.connect(lambda: self.export_ab_missing(default_ext="csv"))
        export_menu.addAction(act_csv)
        self._btn_export_missing, self._act_export_missing_csv = act_txt, act_csv
        self._btn_export_ab.setMenu(export_menu)
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

    def _make_ab_table(self, model: Any, table_id: str, *, code_col: int, menu_kind: str) -> QTableView:
        tv = build_ab_table_view(
            model, model.DEFAULT_WIDTHS, alternating=False,
            menu_callback=lambda t, pos, mk=menu_kind: self._show_ab_table_menu(t, mk, pos),
        )
        enhance_table_view(tv, theme=self.ctx.config.theme, on_code_copied=self._on_inline_code_copied, row_height=34, header_height=34)
        mgr = TableHeaderManager.attach(tv, table_id, model.COLUMN_SPECS, self.ctx)
        self._header_managers.append(mgr)
        name_fn = (
            (lambda r, m=model: (e.target_text if (e := m.entry_at(r)) is not None else ""))
            if menu_kind in ("checklist", "missing", "matched")
            else (lambda r, m=model: ((it.file_name or it.raw_text) if (it := m.row_at(r)) is not None else ""))
        )
        self._copy_helpers[tv] = TableCopyHelper(
            tv, self.ctx, header_manager=mgr, code_column=code_col, name_extractor=name_fn, install_shortcuts=True
        )
        return tv

    def _build_ab_result_tabs(self) -> QTabWidget:
        self._ab_result_tabs = QTabWidget()
        self._ab_result_tabs.setProperty("subtabs", True)

        tab_checklist = QWidget()
        cl_layout = QVBoxLayout(tab_checklist)
        cl_layout.setContentsMargins(8, 6, 8, 8)
        cl_layout.setSpacing(6)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        self._ab_filter_group = QButtonGroup(self)
        self._ab_filter_group.setExclusive(True)
        self._btn_filter_all = self._make_filter_pill("全部 (0)", "all", 0)
        self._btn_filter_all.setChecked(True)
        self._btn_filter_missing = self._make_filter_pill("只看缺失 (0)", "missing", 1, "danger")
        self._btn_filter_matched = self._make_filter_pill("只看已包含 (0)", "matched", 2, "success")
        for b in (self._btn_filter_all, self._btn_filter_missing, self._btn_filter_matched):
            filter_row.addWidget(b)
        filter_row.addStretch()

        self._ab_checklist_search = QLineEdit()
        self._ab_checklist_search.setPlaceholderText("搜索编码 / 目标文本 / 基准A文件名...")
        self._ab_checklist_search.setClearButtonEnabled(True)
        self._ab_checklist_search.setMinimumWidth(220)
        self._ab_checklist_search.setMaximumWidth(320)
        self._ab_search_icon_action = self._ab_checklist_search.addAction(
            get_icon("search", 14, theme=self.ctx.config.theme), QLineEdit.ActionPosition.LeadingPosition
        )
        self._ab_checklist_search.textChanged.connect(self._model_ab_checklist.set_search_query)
        filter_row.addWidget(self._ab_checklist_search)
        cl_layout.addLayout(filter_row)

        self._tbl_ab_checklist = self._make_ab_table(self._model_ab_checklist, "compare_ab_checklist", code_col=2, menu_kind="checklist")
        self._tbl_ab_missing = self._make_ab_table(self._model_ab_missing, "compare_ab_missing", code_col=1, menu_kind="missing")
        self._tbl_ab_matched = self._make_ab_table(self._model_ab_matched, "compare_ab_matched", code_col=1, menu_kind="matched")
        self._tbl_ab_only_a = self._make_ab_table(self._model_ab_only_a, "compare_ab_only_a", code_col=0, menu_kind="only_a")
        cl_layout.addWidget(self._tbl_ab_checklist, 1)

        self._ab_result_tabs.addTab(tab_checklist, "逐条核对 (0)")
        self._ab_result_tabs.addTab(self._tbl_ab_missing, "缺失清单 (0)")
        self._ab_result_tabs.addTab(self._tbl_ab_matched, "已包含对照 (0)")
        self._ab_result_tabs.addTab(self._tbl_ab_only_a, "仅基准A独有 (0)")
        self._ab_result_tabs.setCurrentIndex(0)
        return self._ab_result_tabs

    # --- Collapsible Input Drawer ---

    def set_ab_input_collapsed(self, collapsed: bool) -> None:
        self._ab_input_collapsed = bool(collapsed)
        self._ab_input_expanded.setVisible(not self._ab_input_collapsed)
        self._ab_input_collapsed_bar.setVisible(self._ab_input_collapsed)
        self._update_ab_collapsed_summary()

    def _update_ab_collapsed_summary(self) -> None:
        self._lbl_ab_collapsed_summary.setText(
            f"基准 A：{self._ab_badges['A'].text()}   vs   目标 B：{self._ab_badges['B'].text()}"
            f"   ·   {self._cmb_ab_match_mode.currentText()}"
        )

    # --- Source Input Actions (DB / Multi-file / Paste / Clear / Swap) ---

    def _set_side_badge(self, side: str, text: str, tone: str, tooltip: str = "") -> None:
        badge = self._ab_badges[side]
        badge.setText(text)
        badge.setToolTip(tooltip or text)
        badge.setProperty("badge", tone)
        _repolish(badge)
        self._update_ab_collapsed_summary()

    def _set_editor_text_silently(self, side: str, text: str) -> None:
        editor = self._ab_editors[side]
        editor.blockSignals(True)
        editor.setPlainText(text)
        editor.blockSignals(False)

    def load_db_to_ab_side(self, side: str) -> int:
        if side not in ("A", "B"):
            return 0
        stats = self.ctx.db.get_stats()
        total = stats.total_records
        if total <= 0:
            self.ctx.notify("当前数据库为空，请先导入数据或改用文件/粘贴方式", "warning")
            return 0
        db_path = str(self.ctx.db.db_path)
        lines = [f"{r.canonical_code}  |  {r.file_name or r.raw_text}" for r in self.ctx.db.query_records(limit=120, sort_by="id", sort_desc=False)]
        if total > len(lines):
            lines.append(f"... 当前总库共 {total:,} 条记录（仅预览前 {len(lines)} 条，对比时自动载入全库）")
        self._ab_sides[side] = _SourceSideState(kind="db", db_path=db_path, db_name=Path(db_path).name, db_count=total, line_count=total)
        self._set_editor_text_silently(side, "\n".join(lines))
        self._set_side_badge(side, f"当前总库 ({total:,} 条)", "success", tooltip=f"当前数据库：{db_path}（{total:,} 条，{stats.unique_codes:,} 编码）")
        self._update_ab_run_button()
        self.ctx.notify(f"已载入当前总库至清单 {side}（{total:,} 条记录）", "success")
        return total

    def _pick_ab_files(self, side: str) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, f"选择清单 {side} 文件（可多选）", self.ctx.config.default_import_dir or str(Path.home()), _COMPARE_FILE_FILTER)
        if not paths:
            return
        try:
            self.ctx.config.default_import_dir = str(Path(paths[0]).parent)
            self.ctx.config_manager.save()
        except OSError:
            pass
        self.set_ab_files(side, paths)

    def set_ab_files(self, side: str, paths: list[str]) -> None:
        if side not in ("A", "B") or not (valid := [str(Path(p)) for p in paths if p and Path(p).is_file()]):
            return
        self._ab_sides[side] = _SourceSideState(kind="files", file_paths=valid)
        self._set_editor_text_silently(side, _build_files_preview_text(valid))
        self._set_side_badge(side, f"文件: {Path(valid[0]).name}" if len(valid) == 1 else f"已选 {len(valid)} 个文件", "accent", tooltip="\n".join(valid))
        self._update_ab_run_button()

    def set_ab_text(self, side: str, text: str) -> None:
        if side not in ("A", "B"):
            return
        raw_lines = [ln for ln in text.splitlines() if ln.strip()]
        if not (cnt := len(raw_lines)):
            self.clear_ab_side(side)
            return
        if cnt > _AB_MAX_DIRECT_PASTE_LINES:
            prev = raw_lines[:_AB_VISUAL_PREVIEW_LINES] + [f"... 共 {cnt:,} 行（仅预览前 {_AB_VISUAL_PREVIEW_LINES} 行，完整内容已缓存）"]
            self._ab_sides[side] = _SourceSideState(kind="text", full_text_override=text, line_count=cnt)
            self._set_editor_text_silently(side, "\n".join(prev))
        else:
            self._ab_sides[side] = _SourceSideState(kind="text", line_count=cnt)
            self._set_editor_text_silently(side, text)
        self._set_side_badge(side, f"手动粘贴 ({cnt:,} 行)", "accent")
        self._update_ab_run_button()

    def clear_ab_side(self, side: str) -> None:
        if side not in ("A", "B"):
            return
        self._ab_sides[side] = _SourceSideState()
        self._set_editor_text_silently(side, "")
        self._set_side_badge(side, "未载入", "muted")
        if self._ab_input_collapsed:
            self.set_ab_input_collapsed(False)
        self._update_ab_run_button()

    def _on_ab_editor_text_changed(self, side: str) -> None:
        n = sum(1 for ln in self._ab_editors[side].toPlainText().splitlines() if ln.strip())
        self._ab_sides[side] = _SourceSideState(kind="text", line_count=n) if n else _SourceSideState()
        self._set_side_badge(side, f"手动粘贴 ({n:,} 行)" if n else "未载入", "accent" if n else "muted")
        self._update_ab_run_button()

    def swap_ab_sides(self) -> None:
        if self._is_worker_running(self._ab_worker):
            self.ctx.notify("正在执行对比，请等待完成或先取消后再互换", "warning")
            return
        ta, tb = self._ab_editors["A"].toPlainText(), self._ab_editors["B"].toPlainText()
        ba = (self._ab_badges["A"].text(), str(self._ab_badges["A"].property("badge") or "muted"), self._ab_badges["A"].toolTip())
        bb = (self._ab_badges["B"].text(), str(self._ab_badges["B"].property("badge") or "muted"), self._ab_badges["B"].toolTip())
        self._ab_sides["A"], self._ab_sides["B"] = self._ab_sides["B"], self._ab_sides["A"]
        self._set_editor_text_silently("A", tb)
        self._set_editor_text_silently("B", ta)
        self._set_side_badge("A", *bb)
        self._set_side_badge("B", *ba)
        self._update_ab_run_button()
        self.ctx.notify("已互换清单 A 与清单 B", "info")

    def set_ab_match_mode(self, mode: str) -> None:
        idx = self._cmb_ab_match_mode.findData(mode)
        if idx >= 0:
            self._cmb_ab_match_mode.setCurrentIndex(idx)
            self._update_ab_collapsed_summary()

    def _ab_side_ready(self, side: str) -> bool:
        return self._ab_sides[side].has_source(self._ab_editors[side].toPlainText())

    def _update_ab_run_button(self) -> None:
        ready = (not self._is_worker_running(self._ab_worker)) and self._ab_side_ready("A") and self._ab_side_ready("B")
        self._btn_run_ab.setEnabled(ready)
        self._btn_rerun_ab.setEnabled(ready)

    def _set_ab_busy(self, busy: bool) -> None:
        for bar in (self._ab_progress, self._ab_collapsed_progress):
            bar.setVisible(busy)
            if busy:
                bar.setRange(0, 0)
        self._btn_cancel_ab.setVisible(busy)
        self._btn_cancel_ab.setEnabled(busy)
        for ctrl in (self._btn_swap_ab, self._cmb_ab_match_mode, self._chk_ab_ignore_comp, self._btn_expand_ab_input):
            ctrl.setEnabled(not busy)
        for side in ("A", "B"):
            for btn in self._ab_side_buttons[side]:
                btn.setEnabled(not busy)
            self._ab_editors[side].setReadOnly(busy)
        if busy:
            for ctrl in (
                self._btn_run_ab, self._btn_rerun_ab, self._btn_copy_missing, self._btn_copy_matched,
                self._btn_export_ab, self._btn_export_missing, self._act_export_missing_csv,
            ):
                ctrl.setEnabled(False)
        else:
            self._update_ab_run_button()

    def _on_ab_db_switched(self, _db_path: str) -> None:
        bound = [s for s in ("A", "B") if self._ab_sides[s].kind == "db"]
        if not bound:
            return
        if self._is_worker_running(self._ab_worker):
            self._cancel_ab_worker()
        total = self.ctx.db.get_stats().total_records
        for side in bound:
            self.load_db_to_ab_side(side) if total > 0 else self.clear_ab_side(side)

    def _on_ab_data_changed(self) -> None:
        stats = None
        for side in ("A", "B"):
            if self._ab_sides[side].kind == "db":
                stats = stats or self.ctx.db.get_stats()
                self._ab_sides[side].db_count = self._ab_sides[side].line_count = stats.total_records
                self._set_side_badge(
                    side, f"当前总库 ({stats.total_records:,} 条)", "success" if stats.total_records > 0 else "muted"
                )
        self._update_ab_run_button()

    def _refresh_ab_icons(self, theme: str) -> None:
        p = get_palette(theme)
        for btn, name, sz, color in (
            (self._btn_swap_ab, "arrow-left-right", 14, p["icon_default"]),
            (self._btn_run_ab, "play", 14, p["cta_text"]),
            (self._btn_toggle_ab_input, "chevron-up", 14, p["icon_default"]),
            (self._btn_expand_ab_input, "chevron-down", 14, p["icon_default"]),
            (self._btn_rerun_ab, "refresh-cw", 13, p["icon_default"]),
            (self._btn_copy_missing, "copy", 14, p["icon_default"]),
            (self._btn_export_ab, "download", 14, p["icon_default"]),
        ):
            btn.setIcon(get_icon(name, sz, color, theme=theme))
            btn.setIconSize(QSize(sz, sz))
        self._ab_search_icon_action.setIcon(get_icon("search", 14, p["icon_muted"], theme=theme))

    def _apply_ab_theme(self, theme: str) -> None:
        for m in (self._model_ab_checklist, self._model_ab_missing, self._model_ab_matched, self._model_ab_only_a):
            m.set_theme(theme)
        self._ab_empty_state.apply_theme(theme)
        self._ab_coverage_banner.repolish_all()
        _repolish(self._ab_badges["A"])
        _repolish(self._ab_badges["B"])

    def _snapshot_side_payload(self, side: str, default_name: str) -> dict[str, Any]:
        st = self._ab_sides[side]
        return {
            "kind": st.kind, "file_paths": list(st.file_paths),
            "text": st.full_text_override if st.full_text_override is not None else self._ab_editors[side].toPlainText(),
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
        pa, pb = self._snapshot_side_payload("A", "基准清单A"), self._snapshot_side_payload("B", "目标清单B")

        self._ab_gen += 1
        gen = self._ab_gen
        self._set_ab_busy(True)
        self._lbl_ab_verdict.setText("正在后台解析并计算覆盖率与缺失清单...")

        wbox: list[Optional[WorkerThread]] = [None]
        _cancelled = lambda: bool(wbox[0] is not None and wbox[0].is_cancelled)

        def _task() -> Optional[CompareResult]:
            a = load_ab_side_items(self.ctx, pa, match_mode=match_mode, ignore_comp=ignore_comp, comp_exts=comp_exts, cancel_check=_cancelled)
            if a is None or _cancelled():
                return None
            b = load_ab_side_items(self.ctx, pb, match_mode=match_mode, ignore_comp=ignore_comp, comp_exts=comp_exts, cancel_check=_cancelled)
            if b is None or _cancelled():
                return None
            return compare_two_sources(a, b, source_a_name=pa["source_name"], source_b_name=pb["source_name"], match_mode=match_mode)

        worker = WorkerThread(_task, parent=self)
        wbox[0] = self._ab_worker = worker
        worker.succeeded.connect(lambda res, g=gen: self._on_ab_compare_succeeded(g, res))
        worker.failed.connect(lambda detail, g=gen: self._on_ab_compare_failed(g, detail))
        worker.cancelled.connect(lambda g=gen: self._on_ab_compare_cancelled(g))
        worker.finished.connect(lambda w=worker, g=gen: self._on_ab_worker_finished(w, g))
        worker.start()

    def _on_ab_compare_succeeded(self, gen: int, result: Optional[CompareResult]) -> None:
        if gen != self._ab_gen:
            return
        self._set_ab_busy(False)
        if result is None:
            self._on_ab_compare_cancelled(gen)
            return

        self._ab_result = result
        cl = list(result.target_checklist)
        missing, matched = [e for e in cl if e.status == "missing"], [e for e in cl if e.status == "matched"]

        self._ab_checklist_search.blockSignals(True)
        self._ab_checklist_search.clear()
        self._ab_checklist_search.blockSignals(False)
        self._btn_filter_all.setChecked(True)

        self._model_ab_checklist.set_entries(cl, reset_filter=True)
        self._model_ab_missing.set_entries(missing, source_b_name=result.source_b_name)
        self._model_ab_matched.set_entries(matched)
        self._model_ab_only_a.set_rows(result.only_in_a)
        for tv in (self._tbl_ab_checklist, self._tbl_ab_missing, self._tbl_ab_matched, self._tbl_ab_only_a):
            tv.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)

        self._btn_filter_all.setText(f"全部 ({len(cl):,})")
        self._btn_filter_missing.setText(f"只看缺失 ({len(missing):,})")
        self._btn_filter_matched.setText(f"只看已包含 ({len(matched):,})")
        for idx, txt in enumerate((f"逐条核对 ({len(cl):,})", f"缺失清单 ({len(missing):,})", f"已包含对照 ({len(matched):,})", f"仅基准A独有 ({len(result.only_in_a):,})")):
            self._ab_result_tabs.setTabText(idx, txt)
        self._ab_result_tabs.setCurrentIndex(0)
        self._ab_result_stack.setCurrentIndex(1)

        self._ab_coverage_banner.update_from_result(result)
        self._restore_ab_action_buttons()
        self.set_ab_input_collapsed(True)
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
        try:
            was_cancelled = worker.is_cancelled
        except RuntimeError:
            was_cancelled = False
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
        missing_cnt = len(res.missing_b_codes) if res else 0
        has_missing = missing_cnt > 0
        self._btn_copy_missing.setText(f"复制缺失编码 ({missing_cnt:,})" if missing_cnt else "复制缺失编码")
        self._btn_copy_missing.setEnabled(has_missing)
        self._btn_copy_matched.setEnabled(bool(res and res.matched_b_codes))
        self._btn_export_ab.setEnabled(res is not None)
        self._btn_export_missing.setEnabled(has_missing)
        self._act_export_missing_csv.setEnabled(has_missing)

    def set_ab_checklist_filter(self, status: str) -> None:
        norm = (status or "all").strip().lower()
        target = {"missing": self._btn_filter_missing, "matched": self._btn_filter_matched}.get(norm, self._btn_filter_all)
        if target is self._btn_filter_all:
            norm = "all"
        target.setChecked(True)
        self._model_ab_checklist.set_status_filter(norm)

    def _copy_ab_codes(self, codes: list[str], label: str, fmt: str = "newline") -> int:
        payload = format_codes_for_clipboard(codes, fmt=fmt) if codes else ""
        if not payload or not copy_text_to_clipboard(payload):
            return 0
        self.ctx.notify(f"已复制 {len(codes):,} 个{label}到剪贴板", "success")
        return len(codes)

    def copy_ab_missing_codes(self, fmt: str = "newline") -> int:
        return self._copy_ab_codes(list(self._ab_result.missing_b_codes), "缺失编码", fmt=fmt) if self._ab_result else 0

    def copy_ab_matched_codes(self, fmt: str = "newline") -> int:
        return self._copy_ab_codes(list(self._ab_result.matched_b_codes), "已包含编码", fmt=fmt) if self._ab_result else 0

    def _show_ab_table_menu(self, tv: QTableView, menu_kind: str, pos: Any) -> None:
        helper = self._copy_helpers.get(tv)
        if helper is None or not helper.selected_rows():
            return
        menu = QMenu(self)
        helper.populate_context_menu(menu)
        if menu_kind in ("checklist", "missing", "matched") and self._ab_result and self._ab_result.missing_b_codes:
            menu.addSeparator()
            menu.addAction(f"复制全部缺失编码 ({len(self._ab_result.missing_b_codes)} 条)").triggered.connect(lambda: self.copy_ab_missing_codes("newline"))
            menu.addAction("复制全部缺失编码（多词空格分隔）").triggered.connect(lambda: self.copy_ab_missing_codes("space"))
        if not menu.isEmpty():
            menu.exec(tv.viewport().mapToGlobal(pos))

    def _run_ab_export(self, title: str, default_name: str, file_filter: str, explicit_path: Optional[str], task_factory: Any) -> None:
        if self._is_worker_running(self._export_worker):
            self.ctx.notify("正在导出文件，请稍候...", "warning")
            return
        path, sel_filter = self._pick_export_path(title, default_name, file_filter, explicit_path)
        if not path:
            return
        self._disable_export_buttons()
        self.ctx.notify(f"正在后台导出 {Path(path).name}...", "info")
        self._start_export_worker(lambda p=path, f=sel_filter: task_factory(p, f))

    def export_ab_missing(self, save_path: Optional[str] = None, *, default_ext: str = "txt") -> None:
        res = self._ab_result
        if res is None or not res.missing_b_codes:
            return
        name, flt = ("缺失清单.csv", "缺失明细表格 (*.csv);;缺失编码清单 (*.txt)") if default_ext == "csv" else ("缺失清单.txt", "缺失编码清单 (*.txt);;缺失明细表格 (*.csv)")

        def _do(p: str, f: str) -> str:
            if p.lower().endswith(".csv") or (default_ext == "csv" and not p.lower().endswith(".txt")) or "CSV" in f.upper():
                export_checklist_csv(res.target_checklist, p, only_missing=True, source_b_name=res.source_b_name)
            else:
                export_codes_txt(res.missing_b_codes, p, unique=True, sort_codes=False, uppercase=(res.match_mode == "smart_code"))
            return p

        self._run_ab_export("导出缺失清单", name, flt, save_path, _do)

    def export_ab_full_report(self, save_path: Optional[str] = None) -> None:
        res = self._ab_result
        if res is None:
            return

        def _do(p: str, f: str) -> str:
            if p.lower().endswith(".csv") or "CSV" in f.upper():
                export_checklist_csv(res.target_checklist, p, only_missing=False, source_b_name=res.source_b_name)
            else:
                export_compare_result_excel(res, p)
            return p

        self._run_ab_export("导出完整覆盖率对比报告", "双列表覆盖率对比报告.xlsx", "Excel 完整报告 (5工作表) (*.xlsx);;逐条核对明细 (*.csv)", save_path, _do)
