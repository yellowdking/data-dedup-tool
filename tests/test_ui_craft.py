"""Headless PySide6 tests for v2.0.0 Visual Craft & DuplicateTableModel subsystems.

Covers:
- win32_style: BGR COLORREF conversion, offscreen safety guard, mocked Win11 DWM attribute calls
- icons.py: pure-QPainter rendering of all 30+ icons @1x/@2x DPR + Light/Dark vector brand mark
- theme.py & AppProxyStyle: Option B palette tokens, clean QSS (no conflicting indicators),
  and offscreen QPainter verification of all custom primitives (checkbox, radio, chevrons, focus suppression)
- ModernItemDelegate & SlimScrollBar: pill badges, group dividers/headers, inline hover copy click,
  and logical-index drag-reorder safety
- DuplicateTableModel: flat/grouped mode toggle, persistent checkmarks across filter/sort/mode switch,
  sibling-preserving search, check_all_except_one, numeric sort & whole-group deletion warning
"""

from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyleOption,
    QStyleOptionHeader,
    QStyleOptionViewItem,
    QTableView,
    QWidget,
)

from src.core.models import DataRecord, DuplicateGroup
from src.ui.delegates import (
    ROLE_BADGE,
    ROLE_COPY_TEXT,
    ROLE_GROUP_DIVIDER,
    ROLE_GROUP_HEADER,
    ModernItemDelegate,
    SlimScrollBar,
    enhance_table_view,
    sync_widget_craft_theme,
)
from src.ui.icons import (
    ICON_NAMES,
    clear_icon_cache,
    get_brand_icon,
    get_brand_pixmap,
    get_icon,
    has_icon,
    render_icon_pixmap,
)
from src.ui.proxy_style import AppProxyStyle
from src.ui.table_models import DuplicateTableModel
from src.ui.theme import build_stylesheet, get_mono_font, get_palette, get_ui_font
from src.ui import win32_style


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(["pytest-ui-craft", "-platform", "offscreen"])
    assert isinstance(app, QApplication)
    return app


def _count_non_transparent_pixels(img: QImage) -> int:
    count = 0
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() > 0:
                count += 1
    return count


# ======================================================================
# 1. win32_style: BGR COLORREF & Offscreen Guard
# ======================================================================


def test_win32_hex_to_colorref_bgr_layout() -> None:
    # Win32 COLORREF is 0x00BBGGRR (BGR, not RGB!)
    assert win32_style.hex_to_colorref("#112233") == 0x00332211
    assert win32_style.hex_to_colorref("#FF0000") == 0x000000FF
    assert win32_style.hex_to_colorref("#00FF00") == 0x0000FF00
    assert win32_style.hex_to_colorref("#0000FF") == 0x00FF0000
    assert win32_style.hex_to_colorref("#abc") == 0x00CCBBAA
    assert win32_style.hex_to_colorref("0B0C0E") == 0x000E0C0B

    # Palette win32 tokens convert cleanly
    for theme in ("light", "dark"):
        p = get_palette(theme)
        assert isinstance(win32_style.hex_to_colorref(p["win32_caption"]), int)
        assert isinstance(win32_style.hex_to_colorref(p["win32_border"]), int)

    with pytest.raises(ValueError):
        win32_style.hex_to_colorref("#12345")
    with pytest.raises(ValueError):
        win32_style.hex_to_colorref("not-a-color")


def test_win32_offscreen_guard_and_mocked_win11_dwm(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Under QT_QPA_PLATFORM=offscreen, Win32 DWM must never fire
    assert win32_style.is_windows_native_platform() is False

    w = QWidget()
    try:
        w.show()
        QApplication.processEvents()
        assert win32_style.apply_win32_window_theme(w, "dark") is False
        styler = win32_style.install_win32_window_styler(qapp, "light")
        assert styler.theme == "light"
        assert win32_style.sync_win32_theme("dark") == 0
        assert styler.theme == "dark"

        # Simulate native Win11 build 22631 with mock dwmapi/user32 to verify DWM attributes
        dwm_calls: list[tuple[int, int, int]] = []

        class _MockDwmapi:
            def DwmSetWindowAttribute(self, hwnd, attr, ptr, size):
                val = int(ptr._obj.value)
                dwm_calls.append((int(hwnd), int(attr), val))
                return 0

        class _MockUser32:
            def IsWindow(self, hwnd):
                return True

            def SetWindowPos(self, *args):
                return True

        monkeypatch.setattr(win32_style, "is_windows_native_platform", lambda: True)
        monkeypatch.setattr(win32_style, "_load_win32_dlls", lambda: (_MockDwmapi(), _MockUser32()))
        monkeypatch.setattr(win32_style, "_get_windows_build", lambda: 22631)

        ok = win32_style.apply_win32_window_theme(w, "dark")
        assert ok is True
        attrs = {attr: val for _hwnd, attr, val in dwm_calls}
        dark_p = get_palette("dark")
        assert attrs[win32_style.DWMWA_USE_IMMERSIVE_DARK_MODE] == 1
        assert attrs[win32_style.DWMWA_WINDOW_CORNER_PREFERENCE] == win32_style.DWMWCP_ROUND
        assert attrs[win32_style.DWMWA_CAPTION_COLOR] == win32_style.hex_to_colorref(dark_p["win32_caption"])
        assert attrs[win32_style.DWMWA_TEXT_COLOR] == win32_style.hex_to_colorref(dark_p["text_primary"])
        assert attrs[win32_style.DWMWA_BORDER_COLOR] == win32_style.hex_to_colorref(dark_p["win32_border"])
    finally:
        w.close()
        w.deleteLater()
        QApplication.processEvents()


# ======================================================================
# 2. Vector Icon Engine (icons.py)
# ======================================================================


def test_vector_icons_all_render_1x_2x_and_brand_mark(qapp: QApplication) -> None:
    clear_icon_cache()
    assert len(ICON_NAMES) >= 30

    required_icons = (
        "database", "upload", "table", "git-compare", "layers", "settings",
        "sun", "moon", "copy", "check", "search", "plus", "folder-open",
        "file-text", "file-spreadsheet", "trash", "refresh-cw", "arrow-left-right",
        "play", "more-horizontal", "chevron-down", "chevron-up", "chevron-right",
        "chevrons-up-down", "check-circle", "x-circle", "alert-triangle", "info",
        "sparkles", "filter", "columns", "package", "x", "edit",
    )
    for name in required_icons:
        assert has_icon(name), f"Missing required vector icon: {name}"

    # Render every single icon in the catalog at @1x and @2x DPR
    for name in ICON_NAMES:
        for dpr in (1.0, 2.0):
            pm = render_icon_pixmap(name, size=16, color="#18181B", dpr=dpr)
            assert not pm.isNull(), f"Null pixmap for {name} @{dpr}x"
            assert pm.width() == int(16 * dpr)
            assert pm.height() == int(16 * dpr)
            assert pm.devicePixelRatio() == pytest.approx(dpr)
            assert _count_non_transparent_pixels(pm.toImage()) > 0, f"Blank icon: {name}"

        ico_light = get_icon(name, size=16, theme="light", dpr=2.0)
        ico_dark = get_icon(name, size=16, theme="dark", dpr=2.0)
        assert not ico_light.isNull()
        assert not ico_dark.isNull()

    # Unknown icon falls back safely without crashing
    fallback_pm = render_icon_pixmap("nonexistent_icon_xyz", size=16, dpr=2.0)
    assert not fallback_pm.isNull()
    assert _count_non_transparent_pixels(fallback_pm.toImage()) > 0

    # Programmatic brand mark @1x and @2x across Light & Dark themes
    pm_light = get_brand_pixmap(size=28, theme="light", dpr=2.0)
    pm_dark = get_brand_pixmap(size=28, theme="dark", dpr=2.0)
    pm_1x = get_brand_pixmap(size=24, theme="light", dpr=1.0)
    assert not pm_light.isNull() and not pm_dark.isNull() and not pm_1x.isNull()
    assert pm_light.width() == 56 and pm_light.devicePixelRatio() == pytest.approx(2.0)
    assert pm_1x.width() == 24 and pm_1x.devicePixelRatio() == pytest.approx(1.0)
    assert pm_light.toImage() != pm_dark.toImage()
    assert not get_brand_icon(size=28, theme="dark").isNull()


# ======================================================================
# 3. Option B Theme & AppProxyStyle
# ======================================================================


def test_option_b_theme_and_app_proxy_style_primitives(qapp: QApplication) -> None:
    light = get_palette("light")
    dark = get_palette("dark")
    # Monochrome Ink (Light) vs Ivory (Dark) primary CTAs
    assert light["cta_bg"] == "#18181B" and light["cta_text"] == "#FFFFFF"
    assert dark["cta_bg"] == "#F4F4F5" and dark["cta_text"] == "#09090B"
    assert dark["bg_sidebar"] == "#0E0F12"
    assert get_ui_font().family() != ""
    assert get_mono_font().family() != ""

    # Stylesheet must NOT re-introduce broken QSS indicator rules
    for theme in ("light", "dark"):
        qss = build_stylesheet(theme)
        assert "QCheckBox::indicator" not in qss
        assert "QRadioButton::indicator" not in qss
        assert "QComboBox::down-arrow" not in qss
        assert "QTableView::item:hover" not in qss

    style = AppProxyStyle(theme="light", base_style="Fusion")
    assert style.theme() == "light"
    assert style.pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth) == 16
    assert style.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin) == 0
    assert style.styleHint(QStyle.StyleHint.SH_EtchDisabledText) == 0

    def _render_primitive(
        pe: QStyle.PrimitiveElement,
        state: QStyle.StateFlag,
        *,
        header_sort: QStyleOptionHeader.SortIndicator | None = None,
    ) -> int:
        img = QImage(32, 32, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        opt = QStyleOptionHeader() if header_sort is not None else QStyleOption()
        opt.rect = QRect(4, 4, 24, 24)
        opt.state = state
        if header_sort is not None:
            opt.sortIndicator = header_sort
        painter = QPainter(img)
        try:
            style.drawPrimitive(pe, opt, painter, None)
        finally:
            painter.end()
        return _count_non_transparent_pixels(img)

    pe = QStyle.PrimitiveElement
    sf = QStyle.StateFlag

    for theme in ("light", "dark"):
        style.set_theme(theme)
        assert style.theme() == theme
        # Focus rect is intentionally suppressed -> 0 pixels
        assert _render_primitive(pe.PE_FrameFocusRect, sf.State_Enabled) == 0
        # Checkboxes, item checks & radios render crisp pixels in all states
        assert _render_primitive(pe.PE_IndicatorCheckBox, sf.State_Enabled | sf.State_Off) > 0
        assert _render_primitive(pe.PE_IndicatorCheckBox, sf.State_Enabled | sf.State_On) > 0
        assert _render_primitive(pe.PE_IndicatorCheckBox, sf.State_Enabled | sf.State_NoChange) > 0
        assert _render_primitive(pe.PE_IndicatorItemViewItemCheck, sf.State_Enabled | sf.State_On) > 0
        assert _render_primitive(pe.PE_IndicatorRadioButton, sf.State_Enabled | sf.State_Off) > 0
        assert _render_primitive(pe.PE_IndicatorRadioButton, sf.State_Enabled | sf.State_On) > 0
        # Vector arrows, spin indicators, header sort & branch chevrons
        for arrow in (
            pe.PE_IndicatorArrowDown,
            pe.PE_IndicatorArrowUp,
            pe.PE_IndicatorArrowLeft,
            pe.PE_IndicatorArrowRight,
            pe.PE_IndicatorSpinUp,
            pe.PE_IndicatorSpinDown,
            pe.PE_IndicatorMenuCheckMark,
        ):
            assert _render_primitive(arrow, sf.State_Enabled) > 0
        assert (
            _render_primitive(
                pe.PE_IndicatorHeaderArrow,
                sf.State_Enabled,
                header_sort=QStyleOptionHeader.SortIndicator.SortUp,
            )
            > 0
        )
        assert _render_primitive(pe.PE_IndicatorBranch, sf.State_Enabled | sf.State_Children | sf.State_Open) > 0


# ======================================================================
# 4. ModernItemDelegate: Pills, Inline Hover Copy & Drag-Reorder Safety
# ======================================================================


class _CraftTestModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._rows = [
            ("ABC-001", "已包含", "sample_alpha.mp4"),
            ("DEF-002", "缺失", "sample_beta.mkv"),
        ]

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else 3

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return ("标准编码", "状态", "文件名")[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        code, status, fname = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return (code, status, fname)[col]
        if role == ROLE_COPY_TEXT and col == 0:
            return code
        if role == ROLE_BADGE and col == 1:
            return (status, "success", "check") if status == "已包含" else (status, "danger")
        if role == ROLE_GROUP_DIVIDER and index.row() == 1:
            return True
        if role == ROLE_GROUP_HEADER:
            return False
        return None


def test_modern_delegate_badges_inline_hover_copy_and_drag_reorder_safety(
    qapp: QApplication,
) -> None:
    model = _CraftTestModel()
    tv = QTableView()
    tv.resize(620, 220)
    tv.setModel(model)
    copied_events: list[str] = []
    try:
        delegate = enhance_table_view(tv, theme="light", on_code_copied=copied_events.append)
        assert isinstance(delegate, ModernItemDelegate)
        assert tv.showGrid() is False
        assert isinstance(tv.verticalScrollBar(), SlimScrollBar)

        hdr = tv.horizontalHeader()
        hdr.setSectionsMovable(True)
        hdr.setFirstSectionMovable(True)
        tv.setColumnWidth(0, 180)
        tv.setColumnWidth(1, 120)
        tv.setColumnWidth(2, 220)

        # Drag logical col 0 ("标准编码") to visual position 2!
        hdr.moveSection(0, 2)
        assert hdr.visualIndex(0) == 2
        assert hdr.visualIndex(1) == 0

        tv.show()
        QApplication.processEvents()

        # Paint badge cell (logical col 1) & hovered copy cell (logical col 0 at visual pos 2)
        idx_code = model.index(0, 0)
        idx_badge = model.index(0, 1)
        idx_div = model.index(1, 0)

        delegate.set_hovered_index(idx_code)
        for theme in ("light", "dark"):
            sync_widget_craft_theme(tv, theme)
            assert delegate.theme() == theme
            pm = QPixmap(220, 40)
            pm.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pm)
            try:
                opt = QStyleOptionViewItem()
                opt.rect = QRect(0, 0, 200, 36)
                opt.widget = tv
                delegate.paint(painter, opt, idx_code)
                delegate.paint(painter, opt, idx_badge)
                delegate.paint(painter, opt, idx_div)
            finally:
                painter.end()
            assert _count_non_transparent_pixels(pm.toImage()) > 0

        # Simulate actual mouse hover + click on the right-aligned inline copy icon
        # inside logical col 0 AFTER it has been dragged to visual pos 2!
        cell_rect = tv.visualRect(idx_code)
        assert cell_rect.isValid()
        btn_center = QPointF(delegate.copy_button_rect(cell_rect).center())
        vp = tv.viewport()

        def _send_mouse(ev_type: QEvent.Type, pt: QPointF, btn: Qt.MouseButton) -> None:
            ev = QMouseEvent(
                ev_type,
                pt,
                vp.mapToGlobal(pt),
                btn,
                btn if ev_type == QEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            QApplication.sendEvent(vp, ev)

        _send_mouse(QEvent.Type.MouseMove, btn_center, Qt.MouseButton.NoButton)
        _send_mouse(QEvent.Type.MouseButtonPress, btn_center, Qt.MouseButton.LeftButton)
        _send_mouse(QEvent.Type.MouseButtonRelease, btn_center, Qt.MouseButton.LeftButton)
        QApplication.processEvents()

        assert copied_events == ["ABC-001"]
        assert QApplication.clipboard().text() == "ABC-001"
        # Clicking copy button must NOT hijack table selection
        assert tv.selectionModel().selectedIndexes() == []

        # Clicking the same right-edge offset on visual col 0 (logical col 1 = badge, no copy text)
        # must NOT trigger copy!
        badge_rect = tv.visualRect(idx_badge)
        badge_pt = QPointF(delegate.copy_button_rect(badge_rect).center())
        _send_mouse(QEvent.Type.MouseButtonPress, badge_pt, Qt.MouseButton.LeftButton)
        _send_mouse(QEvent.Type.MouseButtonRelease, badge_pt, Qt.MouseButton.LeftButton)
        QApplication.processEvents()
        assert copied_events == ["ABC-001"]
    finally:
        tv.close()
        tv.deleteLater()
        QApplication.processEvents()


# ======================================================================
# 5. DuplicateTableModel: Flat/Grouped, Persistent Checks & Guards
# ======================================================================


def _build_sample_dup_groups() -> list[DuplicateGroup]:
    g1 = DuplicateGroup(
        canonical_code="AAA-001",
        records=[
            DataRecord(
                id=101,
                canonical_code="AAA-001",
                prefix="AAA",
                number="001",
                file_name="aaa_small.mp4",
                file_size=200,
                source_name="disk_a.txt",
                created_at="2026-01-01 10:00:00",
            ),
            DataRecord(
                id=102,
                canonical_code="AAA-001",
                prefix="AAA",
                number="001",
                file_name="aaa_unique_needle.mp4",
                file_size=5_000_000,
                source_name="disk_b.txt",
                created_at="2026-01-02 10:00:00",
            ),
            DataRecord(
                id=103,
                canonical_code="AAA-001",
                prefix="AAA",
                number="001",
                file_name="aaa_medium.mp4",
                file_size=50_000,
                source_name="disk_c.txt",
                created_at="2026-01-03 10:00:00",
            ),
        ],
    )
    g2 = DuplicateGroup(
        canonical_code="BBB-002",
        records=[
            DataRecord(
                id=201,
                canonical_code="BBB-002",
                prefix="BBB",
                number="002",
                file_name="bbb_part1.mkv",
                part="CD1",
                file_size=10_000,
                source_name="archive.xlsx",
                created_at="2026-01-04 10:00:00",
            ),
            DataRecord(
                id=202,
                canonical_code="BBB-002",
                prefix="BBB",
                number="002",
                file_name="bbb_part2.mkv",
                part="CD2",
                file_size=20_000,
                source_name="archive.xlsx",
                created_at="2026-01-05 10:00:00",
            ),
        ],
        is_possible_multipart=True,
    )
    return [g1, g2]


def test_duplicate_table_model_flat_grouped_persistent_checks_and_guards(
    qapp: QApplication,
) -> None:
    model = DuplicateTableModel(theme="light", view_mode="flat")
    checked_counts: list[int] = []
    model.checked_changed.connect(checked_counts.append)
    model.set_groups(_build_sample_dup_groups())

    # Default flat mode: 5 concrete record rows, zero parent header rows
    assert model.view_mode() == "flat"
    assert model.rowCount() == 5
    assert model.visible_codes() == ["AAA-001", "BBB-002"]
    assert model.data(model.index(0, DuplicateTableModel.COL_CODE), ROLE_COPY_TEXT) == "AAA-001"
    assert model.data(model.index(0, DuplicateTableModel.COL_DUP_COUNT), ROLE_BADGE) == ("×3", "warning")
    assert model.data(model.index(3, DuplicateTableModel.COL_DUP_COUNT), ROLE_BADGE) == ("×2", "purple")
    # Divider between group 1 (rows 0..2) and group 2 (row 3)
    assert model.data(model.index(0, 0), ROLE_GROUP_DIVIDER) is False
    assert model.data(model.index(3, 0), ROLE_GROUP_DIVIDER) is True

    # Check via CheckStateRole and Space-style toggle_rows_checked
    assert model.setData(
        model.index(1, DuplicateTableModel.COL_CHECK),
        Qt.CheckState.Checked,
        Qt.ItemDataRole.CheckStateRole,
    ) is True
    assert model.checked_id_set() == {102}
    model.toggle_rows_checked([3])
    assert model.checked_id_set() == {102, 201}

    # Numeric file_size sort (200 < 10_000 < 20_000 < 50_000 < 5_000_000) preserves checkmarks!
    model.sort(DuplicateTableModel.COL_FILE_SIZE, Qt.SortOrder.AscendingOrder)
    sizes = [model.record_at(r).file_size for r in range(model.rowCount())]
    assert sizes == [200, 10_000, 20_000, 50_000, 5_000_000]
    assert model.checked_id_set() == {102, 201}

    # Sibling-preserving search: matching only id=102's filename keeps ALL 3 copies of AAA-001!
    model.set_search_query("unique_needle")
    assert model.rowCount() == 3
    assert model.visible_codes() == ["AAA-001"]
    assert model.checked_id_set() == {102, 201}  # hidden group check persists!

    model.set_search_query("")
    assert model.rowCount() == 5

    # Switch to grouped mode: 2 group headers + 5 record rows = 7 rows; checks still intact!
    model.set_view_mode("grouped")
    assert model.rowCount() == 7
    assert model.is_group_row(0) is True
    assert model.data(model.index(0, 0), ROLE_GROUP_HEADER) == {"expanded": True}
    assert model.checked_id_set() == {102, 201}

    # Collapse first group row -> hides its 3 child rows -> 4 rows total
    first_code = model.group_at(0).canonical_code
    assert model.toggle_row_group_collapsed(0) is True
    assert model.is_group_collapsed(first_code) is True
    assert model.data(model.index(0, 0), ROLE_GROUP_HEADER) == {"expanded": False}
    assert model.rowCount() < 7

    model.collapse_all_groups()
    assert model.rowCount() == 2
    model.expand_all_groups()
    assert model.rowCount() == 7

    # Switch back to flat mode -> test check_all_except_one ("仅保留此条，勾选本组其余副本")
    model.set_view_mode("flat")
    model.clear_checks()
    siblings_checked = model.check_all_except_one(101)
    assert siblings_checked == 2
    assert model.checked_id_set() == {102, 103}
    assert model.fully_checked_groups() == []

    # Whole-group deletion warning triggers when EVERY copy of a group is checked
    model.set_record_checked(101, True)
    assert model.fully_checked_groups() == ["AAA-001"]
    model.set_record_checked(103, False)
    assert model.fully_checked_groups() == []


def test_win32_win10_setwindowpos_nudge_and_popup_filter(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PySide6.QtWidgets import QDialog, QMenu

    nudge_calls: list[tuple[int, int]] = []

    class _MockDwmapiWin10:
        def DwmSetWindowAttribute(self, hwnd, attr, ptr, size):
            # Force fallback from attr 20 to attr 19 on older Win10
            return -1 if attr == win32_style.DWMWA_USE_IMMERSIVE_DARK_MODE else 0

    class _MockUser32Win10:
        def IsWindow(self, hwnd):
            return True

        def SetWindowPos(self, hwnd, _after, _x, _y, _cx, _cy, flags):
            nudge_calls.append((int(hwnd), int(flags)))
            return True

    monkeypatch.setattr(win32_style, "is_windows_native_platform", lambda: True)
    monkeypatch.setattr(win32_style, "_load_win32_dlls", lambda: (_MockDwmapiWin10(), _MockUser32Win10()))
    monkeypatch.setattr(win32_style, "_get_windows_build", lambda: 18363)

    dlg = QDialog()
    menu = QMenu()
    try:
        dlg.show()
        QApplication.processEvents()
        # Installed Win32WindowStyler automatically styled QDialog on Show!
        assert len(nudge_calls) >= 1
        before = len(nudge_calls)
        assert win32_style._is_styleable_window(dlg) is True
        assert win32_style._is_styleable_window(menu) is False
        assert win32_style.apply_win32_window_theme(dlg, "dark") is True
        assert len(nudge_calls) == before + 1
        assert bool(nudge_calls[-1][1] & win32_style.SWP_FRAMECHANGED)
    finally:
        dlg.close()
        dlg.deleteLater()
        menu.deleteLater()
        QApplication.processEvents()


def test_slim_scrollbar_paint_hover_and_theme_sync(qapp: QApplication) -> None:
    for orient in (Qt.Orientation.Vertical, Qt.Orientation.Horizontal):
        sb = SlimScrollBar(orient, theme="light")
        sb.resize(10, 140) if orient == Qt.Orientation.Vertical else sb.resize(140, 10)
        sb.setRange(0, 100)
        sb.setPageStep(20)
        sb.setValue(40)
        try:
            for theme in ("light", "dark"):
                sync_widget_craft_theme(sb, theme)
                pm = QPixmap(sb.size())
                pm.fill(Qt.GlobalColor.transparent)
                sb.render(pm)
                assert _count_non_transparent_pixels(pm.toImage()) > 0
        finally:
            sb.deleteLater()
            QApplication.processEvents()


def test_duplicate_model_grouped_sort_dark_theme_and_preserve_checks(
    qapp: QApplication,
) -> None:
    groups = _build_sample_dup_groups()
    model = DuplicateTableModel(theme="light", view_mode="grouped")
    model.set_groups(groups)
    model.set_checked_ids([102, 201])

    # Sort descending by dup_count puts AAA-001 (3 copies) before BBB-002 (2 copies)
    model.sort(DuplicateTableModel.COL_DUP_COUNT, Qt.SortOrder.DescendingOrder)
    assert model.is_group_row(0) is True
    assert model.group_at(0).canonical_code == "AAA-001"

    # Sort ascending puts BBB-002 (2 copies) first
    model.sort(DuplicateTableModel.COL_DUP_COUNT, Qt.SortOrder.AscendingOrder)
    assert model.group_at(0).canonical_code == "BBB-002"

    # preserve_checks=True keeps valid checked IDs while pruning removed records
    model.set_groups([groups[0]], preserve_checks=True)
    assert model.checked_id_set() == {102}

    # Dark theme refresh updates brushes cleanly without error
    model.set_theme("dark")
    assert model.data(model.index(0, 0), Qt.ItemDataRole.BackgroundRole) is not None


def test_modern_delegate_group_header_and_all_badge_tones_paint(qapp: QApplication) -> None:
    from src.ui.delegates import _resolve_badge_colors

    for theme in ("light", "dark"):
        p = get_palette(theme)
        for tone in ("success", "warning", "danger", "accent", "purple", "neutral"):
            fg, bg, border = _resolve_badge_colors(tone, p)
            assert fg.isValid() and bg.isValid() and border.isValid()

    model = DuplicateTableModel(theme="dark", view_mode="grouped")
    model.set_groups(_build_sample_dup_groups())
    tv = QTableView()
    tv.resize(700, 260)
    tv.setModel(model)
    try:
        delegate = enhance_table_view(tv, theme="dark")
        pm = QPixmap(220, 40)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        try:
            opt = QStyleOptionViewItem()
            opt.rect = QRect(0, 0, 200, 36)
            opt.widget = tv
            # Paint group header row (col 0 chevron + col 2 badge) and record checkbox row
            delegate.paint(painter, opt, model.index(0, 0))
            delegate.paint(painter, opt, model.index(0, DuplicateTableModel.COL_DUP_COUNT))
            delegate.paint(painter, opt, model.index(1, DuplicateTableModel.COL_CHECK))
        finally:
            painter.end()
        assert _count_non_transparent_pixels(pm.toImage()) > 0
    finally:
        tv.deleteLater()
        QApplication.processEvents()


def test_icons_cache_hit_and_custom_color_variants(qapp: QApplication) -> None:
    clear_icon_cache()
    ico1 = get_icon("copy", size=16, color="#18181B", active_color="#5E6AD2", dpr=2.0)
    ico2 = get_icon("copy", size=16, color="#18181B", active_color="#5E6AD2", dpr=2.0)
    assert ico1 is ico2
    ico_disabled = get_icon("copy", size=16, color="#18181B", disabled_color="#A1A1AA", dpr=2.0)
    assert not ico_disabled.isNull()
    assert ico_disabled is not ico1
