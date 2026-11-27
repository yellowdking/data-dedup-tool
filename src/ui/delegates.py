"""Modern table/tree item delegate, full-row hover controller, and slim scrollbars.

Standardized custom item-model roles (shared across all models & views):
- ``ROLE_BADGE = Qt.ItemDataRole.UserRole + 40``
  Tuple ``(label, tone)`` or ``(label, tone, icon_name)`` rendered as a 22px pill.
- ``ROLE_COPY_TEXT = Qt.ItemDataRole.UserRole + 41``
  Non-empty string triggers a right-aligned 1-click hover copy micro-button
  (logical-column safe under qBittorrent header drag-reordering!).
- ``ROLE_GROUP_DIVIDER = Qt.ItemDataRole.UserRole + 42``
  Truthy bool paints a crisp 1px top divider separating adjacent duplicate groups.
- ``ROLE_GROUP_HEADER = Qt.ItemDataRole.UserRole + 43``
  Truthy bool or dict (e.g. ``{"expanded": True}``) paints a styled group header
  row with expand/collapse vector chevron.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QApplication,
    QHeaderView,
    QScrollBar,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionButton,
    QStyleOptionViewItem,
    QTableView,
    QTreeView,
    QWidget,
)

from src.ui.icons import paint_icon
from src.ui.theme import get_palette

# Standardized item-model custom data roles
ROLE_BADGE: int = int(Qt.ItemDataRole.UserRole) + 40
ROLE_COPY_TEXT: int = int(Qt.ItemDataRole.UserRole) + 41
ROLE_GROUP_DIVIDER: int = int(Qt.ItemDataRole.UserRole) + 42
ROLE_GROUP_HEADER: int = int(Qt.ItemDataRole.UserRole) + 43

_COPY_BTN_SIZE: int = 22
_COPY_BTN_MARGIN_RIGHT: int = 6
_COPY_CONFIRM_MS: int = 1200


def _is_offscreen_platform() -> bool:
    try:
        return QGuiApplication.platformName().lower() == "offscreen"
    except Exception:
        return False


def _resolve_badge_colors(tone: str, p: dict[str, str]) -> tuple[QColor, QColor, QColor]:
    """Return ``(fg, bg, border)`` colors for a badge pill tone."""
    t = (tone or "neutral").lower()
    if t == "success":
        return QColor(p["success"]), QColor(p["success_soft"]), QColor(p["success_border"])
    if t == "warning":
        return QColor(p["warning"]), QColor(p["warning_soft"]), QColor(p["warning_border"])
    if t in ("danger", "error"):
        return QColor(p["danger"]), QColor(p["danger_soft"]), QColor(p["danger_border"])
    if t in ("accent", "primary", "info"):
        return QColor(p["accent"]), QColor(p["accent_soft"]), QColor(p["accent_border"])
    if t == "purple":
        return QColor(p["purple"]), QColor(p["purple_soft"]), QColor(p["purple_border"])
    return QColor(p["text_secondary"]), QColor(p["bg_surface_alt"]), QColor(p["border_default"])


class SlimScrollBar(QScrollBar):
    """Overlay-feel thin scrollbar (10px gutter, 3.5px resting -> 7px hover pill)."""

    _GUTTER: int = 10
    _THICK_REST: float = 3.5
    _THICK_HOVER: float = 7.0

    def __init__(
        self,
        orientation: Qt.Orientation,
        parent: Optional[QWidget] = None,
        *,
        theme: str = "light",
    ) -> None:
        super().__init__(orientation, parent)
        self._theme = "dark" if theme == "dark" else "light"
        self._hovered = False
        self._dragging = False
        self._thickness: float = self._THICK_REST

        if orientation == Qt.Orientation.Vertical:
            self.setFixedWidth(self._GUTTER)
        else:
            self.setFixedHeight(self._GUTTER)

        self.setMouseTracking(True)
        # Keep QSS from painting default chrome over our custom pill
        self.setStyleSheet("QScrollBar { background: transparent; border: none; margin: 0px; }")

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(120)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim_value)

    def set_theme(self, theme: str) -> None:
        self._theme = "dark" if theme == "dark" else "light"
        self.update()

    def _on_anim_value(self, value: Any) -> None:
        try:
            self._thickness = float(value)
        except (TypeError, ValueError):
            return
        self.update()

    def _animate_to(self, target: float) -> None:
        if _is_offscreen_platform():
            self._anim.stop()
            self._thickness = target
            self.update()
            return
        if abs(self._thickness - target) < 0.05:
            return
        self._anim.stop()
        self._anim.setStartValue(self._thickness)
        self._anim.setEndValue(target)
        self._anim.start()

    def enterEvent(self, event: QEvent) -> None:
        self._hovered = True
        self._animate_to(self._THICK_HOVER)
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hovered = False
        if not self._dragging:
            self._animate_to(self._THICK_REST)
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._animate_to(self._THICK_HOVER)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._dragging = False
        if not self._hovered:
            self._animate_to(self._THICK_REST)
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event: Any) -> None:
        span = self.maximum() - self.minimum()
        if span <= 0:
            return

        p = get_palette(self._theme)
        active = self._hovered or self._dragging
        color = QColor(p["scrollbar_hover"] if active else p["scrollbar_rest"])
        if not active:
            color.setAlphaF(0.78)

        page = max(1, self.pageStep())
        total = float(span + page)
        ratio_len = min(1.0, page / total)
        ratio_pos = (self.value() - self.minimum()) / float(span)

        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(color))

            margin_ends = 3.0
            edge_inset = 1.5
            thick = self._thickness
            radius = thick * 0.5

            if self.orientation() == Qt.Orientation.Vertical:
                track = max(1.0, self.height() - margin_ends * 2.0)
                handle_len = max(24.0, track * ratio_len)
                handle_len = min(track, handle_len)
                travel = max(0.0, track - handle_len)
                y = margin_ends + travel * ratio_pos
                x = self.width() - edge_inset - thick
                painter.drawRoundedRect(QRectF(x, y, thick, handle_len), radius, radius)
            else:
                track = max(1.0, self.width() - margin_ends * 2.0)
                handle_len = max(24.0, track * ratio_len)
                handle_len = min(track, handle_len)
                travel = max(0.0, track - handle_len)
                x = margin_ends + travel * ratio_pos
                y = self.height() - edge_inset - thick
                painter.drawRoundedRect(QRectF(x, y, handle_len, thick), radius, radius)
        finally:
            painter.end()


def apply_slim_scrollbars(
    widget: QAbstractScrollArea,
    theme: str = "light",
) -> tuple[SlimScrollBar, SlimScrollBar]:
    """Attach slim animated vertical & horizontal scrollbars to ``widget``."""
    vbar = SlimScrollBar(Qt.Orientation.Vertical, widget, theme=theme)
    hbar = SlimScrollBar(Qt.Orientation.Horizontal, widget, theme=theme)
    widget.setVerticalScrollBar(vbar)
    widget.setHorizontalScrollBar(hbar)
    setattr(widget, "_slim_scrollbars", (vbar, hbar))
    return vbar, hbar


class ModernItemDelegate(QStyledItemDelegate):
    """Commercial-grade table/tree item delegate with pills & inline hover copy."""

    code_copied = Signal(str)

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        theme: str = "light",
        row_height: int = 36,
        show_left_accent: bool = True,
        copy_columns: Optional[set[int]] = None,
    ) -> None:
        super().__init__(parent)
        self._theme = "dark" if theme == "dark" else "light"
        self._row_height = max(24, int(row_height))
        self._show_left_accent = bool(show_left_accent)
        self._copy_columns: set[int] = set(copy_columns) if copy_columns else set()
        self._hovered_row: int = -1
        self._hovered_parent: QPersistentModelIndex = QPersistentModelIndex()
        self._hovered_copy_index: QPersistentModelIndex = QPersistentModelIndex()
        self._copied_tokens: dict[tuple[int, int, int], int] = {}
        self._copy_gen: int = 0
        self._view: Optional[QAbstractItemView] = None

    def bind_view(self, view: QAbstractItemView) -> None:
        self._view = view

    def set_theme(self, theme: str) -> None:
        self._theme = "dark" if theme == "dark" else "light"
        if self._view is not None:
            try:
                vp = self._view.viewport()
                if vp is not None:
                    vp.update()
            except RuntimeError:
                self._view = None

    def theme(self) -> str:
        return self._theme

    def _palette(self) -> dict[str, str]:
        return get_palette(self._theme)

    def set_copy_columns(self, columns: set[int]) -> None:
        self._copy_columns = set(columns)

    def set_hovered_index(self, index: QModelIndex) -> None:
        if index.isValid():
            self._hovered_row = index.row()
            self._hovered_parent = QPersistentModelIndex(index.parent())
        else:
            self._hovered_row = -1
            self._hovered_parent = QPersistentModelIndex()

    def set_hovered_copy_index(self, index: QModelIndex) -> None:
        self._hovered_copy_index = (
            QPersistentModelIndex(index) if index.isValid() else QPersistentModelIndex()
        )

    def _is_row_hovered(self, index: QModelIndex) -> bool:
        if self._hovered_row < 0 or index.row() != self._hovered_row:
            return False
        parent = index.parent()
        if not parent.isValid() and not self._hovered_parent.isValid():
            return True
        return parent == QModelIndex(self._hovered_parent)

    def _is_copy_btn_hovered(self, index: QModelIndex) -> bool:
        return self._hovered_copy_index.isValid() and QModelIndex(self._hovered_copy_index) == index

    def _cell_key(self, index: QModelIndex) -> tuple[int, int, int]:
        return (index.row(), index.column(), int(index.internalId()))

    def _is_recently_copied(self, index: QModelIndex) -> bool:
        return self._cell_key(index) in self._copied_tokens

    def get_copy_text(self, index: QModelIndex) -> str:
        """Return copyable text for ``index`` using logical column / ``ROLE_COPY_TEXT``."""
        if not index.isValid():
            return ""
        if bool(index.data(ROLE_GROUP_HEADER)):
            return ""
        explicit = index.data(ROLE_COPY_TEXT)
        if isinstance(explicit, str):
            text = explicit.strip()
            return text if text and text != "-" else ""
        if index.column() in self._copy_columns:
            disp = index.data(Qt.ItemDataRole.DisplayRole)
            text = str(disp).strip() if disp is not None else ""
            return text if text and text != "-" else ""
        return ""

    def copy_button_rect(self, cell_rect: QRect) -> QRect:
        """Compute right-aligned 22x22 inline copy button rectangle inside ``cell_rect``."""
        size = min(_COPY_BTN_SIZE, max(16, cell_rect.height() - 6))
        x = cell_rect.right() - size - _COPY_BTN_MARGIN_RIGHT
        y = cell_rect.top() + (cell_rect.height() - size) // 2
        return QRect(x, y, size, size)

    def trigger_copy(self, index: QModelIndex) -> bool:
        """Copy cell text to clipboard, start 1.2s check feedback, and emit ``code_copied``."""
        text = self.get_copy_text(index)
        if not text:
            return False
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)

        self._copy_gen += 1
        token = self._copy_gen
        key = self._cell_key(index)
        self._copied_tokens[key] = token
        p_idx = QPersistentModelIndex(index)
        self._repaint_index(index)

        def _clear_feedback() -> None:
            if self._copied_tokens.get(key) == token:
                self._copied_tokens.pop(key, None)
                try:
                    if p_idx.isValid():
                        self._repaint_index(QModelIndex(p_idx))
                    elif self._view is not None:
                        vp = self._view.viewport()
                        if vp is not None:
                            vp.update()
                except RuntimeError:
                    self._view = None

        QTimer.singleShot(_COPY_CONFIRM_MS, _clear_feedback)
        self.code_copied.emit(text)
        return True

    def _repaint_index(self, index: QModelIndex) -> None:
        if self._view is None:
            return
        try:
            vp = self._view.viewport()
            if vp is None:
                return
            if index.isValid():
                rect = self._view.visualRect(index)
                if rect.isValid():
                    vp.update(rect)
                    return
            vp.update()
        except RuntimeError:
            self._view = None

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        base = super().sizeHint(option, index)
        is_group = bool(index.data(ROLE_GROUP_HEADER))
        target_h = max(32, self._row_height - 2) if is_group else self._row_height
        w = base.width()
        badge = index.data(ROLE_BADGE)
        if isinstance(badge, (tuple, list)) and len(badge) >= 2:
            label = str(badge[0])
            fm = QFontMetrics(option.font)
            w = max(w, fm.horizontalAdvance(label) + 42)
        return QSize(w, max(base.height(), target_h))

    def _visual_column(self, option: QStyleOptionViewItem, index: QModelIndex) -> int:
        widget = option.widget or self._view
        if isinstance(widget, QTableView):
            hdr = widget.horizontalHeader()
            if hdr is not None:
                return hdr.visualIndex(index.column())
        return index.column()

    def _paint_badge(
        self,
        painter: QPainter,
        rect: QRect,
        badge: tuple[Any, ...],
        option: QStyleOptionViewItem,
        p: dict[str, str],
    ) -> None:
        label = str(badge[0])
        tone = str(badge[1]) if len(badge) >= 2 else "neutral"
        icon_name = str(badge[2]) if len(badge) >= 3 and badge[2] else ""
        fg, bg, border = _resolve_badge_colors(tone, p)

        font = QFont(option.font)
        font.setPointSizeF(max(8.5, font.pointSizeF() - 1.0))
        font.setWeight(QFont.Weight.DemiBold)
        fm = QFontMetrics(font)

        pill_h = min(22, max(18, rect.height() - 10))
        pad_x = 8
        lead_w = 14 if icon_name else 10  # 11px icon+gap or 5px dot+gap
        avail_w = max(24, rect.width() - 12)
        max_text_w = max(12, avail_w - pad_x * 2 - lead_w)
        elided = fm.elidedText(label, Qt.TextElideMode.ElideRight, max_text_w)
        text_w = fm.horizontalAdvance(elided)
        pill_w = min(avail_w, pad_x * 2 + lead_w + text_w)

        align_data = option.displayAlignment
        if align_data & Qt.AlignmentFlag.AlignHCenter:
            px = rect.x() + (rect.width() - pill_w) // 2
        elif align_data & Qt.AlignmentFlag.AlignRight:
            px = rect.right() - pill_w - 8
        else:
            px = rect.x() + 8
        py = rect.y() + (rect.height() - pill_h) // 2
        pill_rect = QRectF(float(px) + 0.5, float(py) + 0.5, float(pill_w) - 1.0, float(pill_h) - 1.0)
        radius = pill_rect.height() * 0.5

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(QBrush(bg))
        painter.drawRoundedRect(pill_rect, radius, radius)

        content_x = pill_rect.left() + pad_x
        cy = pill_rect.center().y()
        if icon_name:
            icon_rect = QRectF(content_x, cy - 5.5, 11.0, 11.0)
            paint_icon(painter, icon_name, icon_rect, fg, stroke_width=1.6)
            text_x = content_x + 14.0
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(fg))
            painter.drawEllipse(QRectF(content_x, cy - 2.5, 5.0, 5.0))
            text_x = content_x + 9.5

        painter.setFont(font)
        painter.setPen(fg)
        text_rect = QRectF(
            text_x,
            pill_rect.top(),
            max(8.0, pill_rect.right() - text_x - pad_x + 2.0),
            pill_rect.height(),
        )
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            elided,
        )
        painter.restore()

    def _paint_copy_button(
        self,
        painter: QPainter,
        btn_rect: QRect,
        *,
        is_copied: bool,
        is_btn_hovered: bool,
        p: dict[str, str],
    ) -> None:
        rf = QRectF(btn_rect).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if is_copied:
            bg = QColor(p["success_soft"])
            border = QColor(p["success_border"])
            fg = QColor(p["success"])
            icon_name = "check"
        elif is_btn_hovered:
            bg = QColor(p["bg_surface"])
            border = QColor(p["border_strong"])
            fg = QColor(p["text_primary"])
            icon_name = "copy"
        else:
            bg = QColor(p["bg_surface_alt"])
            border = QColor(p["border_default"])
            fg = QColor(p["text_secondary"])
            icon_name = "copy"

        painter.setPen(QPen(border, 1.0))
        painter.setBrush(QBrush(bg))
        painter.drawRoundedRect(rf, 5.0, 5.0)

        icon_side = 12.5
        icon_rect = QRectF(
            rf.center().x() - icon_side * 0.5,
            rf.center().y() - icon_side * 0.5,
            icon_side,
            icon_side,
        )
        paint_icon(painter, icon_name, icon_rect, fg, stroke_width=1.6)
        painter.restore()

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.state &= ~QStyle.StateFlag.State_HasFocus

        p = self._palette()
        rect = opt.rect
        is_selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        is_hovered = self._is_row_hovered(index)
        group_hdr = index.data(ROLE_GROUP_HEADER)
        is_group_hdr = bool(group_hdr)
        is_group_div = bool(index.data(ROLE_GROUP_DIVIDER))
        visual_col = self._visual_column(opt, index)

        painter.save()

        # 1. Background layer (preserving semantic BackgroundRole row tints!)
        bg_data = index.data(Qt.ItemDataRole.BackgroundRole)
        custom_brush: Optional[QBrush] = None
        if isinstance(bg_data, QBrush) and bg_data.style() != Qt.BrushStyle.NoBrush:
            custom_brush = bg_data
        elif isinstance(bg_data, QColor) and bg_data.isValid() and bg_data.alpha() > 0:
            custom_brush = QBrush(bg_data)

        if is_group_hdr:
            painter.fillRect(rect, QColor(p["bg_hover"] if is_hovered else p["bg_surface_alt"]))
        else:
            painter.fillRect(rect, QColor(p["bg_surface"]))
            if custom_brush is not None:
                painter.fillRect(rect, custom_brush)
            if is_selected:
                sel_col = QColor(p["bg_selected"])
                if custom_brush is not None:
                    sel_col.setAlphaF(0.84)
                painter.fillRect(rect, sel_col)
            elif is_hovered:
                veil = (
                    QColor(255, 255, 255, 11)
                    if self._theme == "dark"
                    else QColor(9, 9, 11, 11)
                )
                painter.fillRect(rect, veil)

        # 2. Horizontal-only hairline & group separator
        painter.setPen(QPen(QColor(p["border_subtle"]), 1.0))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
        if is_group_div and index.row() > 0:
            painter.setPen(QPen(QColor(p["border_strong"]), 1.0))
            painter.drawLine(rect.left(), rect.top(), rect.right(), rect.top())

        # 3. 2px left surgical accent bar on visual column 0 when selected
        if self._show_left_accent and is_selected and not is_group_hdr and visual_col == 0:
            painter.fillRect(
                QRect(rect.left(), rect.top(), 2, rect.height()),
                QColor(p["accent"]),
            )

        # 4. Badge pill rendering (takes precedence over raw text)
        badge = index.data(ROLE_BADGE)
        if isinstance(badge, (tuple, list)) and len(badge) >= 2 and str(badge[0]):
            self._paint_badge(painter, rect, tuple(badge), opt, p)
            painter.restore()
            return

        # 5. Inline hover quick-copy geometry
        copy_text = self.get_copy_text(index)
        is_copied = self._is_recently_copied(index)
        show_copy_btn = bool(copy_text) and (is_hovered or is_copied)
        btn_rect = self.copy_button_rect(rect) if show_copy_btn else QRect()

        # 6. Cell content (group chevron / checkbox / icon / elided text)
        content_rect = rect.adjusted(10, 0, -10, 0)
        if show_copy_btn:
            content_rect.setRight(max(content_rect.left() + 8, btn_rect.left() - 6))

        # Group header chevron
        if is_group_hdr and visual_col == 0:
            expanded = True
            if isinstance(group_hdr, dict):
                expanded = bool(group_hdr.get("expanded", True))
            chevron_name = "chevron-down" if expanded else "chevron-right"
            chev_rect = QRectF(
                float(content_rect.left()),
                float(rect.center().y()) - 6.0,
                12.0,
                12.0,
            )
            paint_icon(painter, chevron_name, chev_rect, p["text_secondary"], stroke_width=1.6)
            content_rect.setLeft(content_rect.left() + 18)

        # Item checkbox (rendered via AppProxyStyle PE_IndicatorItemViewItemCheck)
        check_data = index.data(Qt.ItemDataRole.CheckStateRole)
        if check_data is not None:
            has_text = bool(opt.text)
            chk_size = 16
            if has_text:
                chk_x = content_rect.left()
                content_rect.setLeft(chk_x + chk_size + 8)
            else:
                chk_x = rect.x() + (rect.width() - chk_size) // 2
            chk_y = rect.y() + (rect.height() - chk_size) // 2

            chk_opt = QStyleOptionButton()
            chk_opt.rect = QRect(chk_x, chk_y, chk_size, chk_size)
            chk_opt.state = QStyle.StateFlag.State_Enabled
            if not (opt.state & QStyle.StateFlag.State_Enabled):
                chk_opt.state &= ~QStyle.StateFlag.State_Enabled
            if is_hovered:
                chk_opt.state |= QStyle.StateFlag.State_MouseOver

            state_val = (
                check_data.value if hasattr(check_data, "value") else int(check_data)
            )
            if state_val == Qt.CheckState.Checked.value:
                chk_opt.state |= QStyle.StateFlag.State_On
            elif state_val == Qt.CheckState.PartiallyChecked.value:
                chk_opt.state |= QStyle.StateFlag.State_NoChange
            else:
                chk_opt.state |= QStyle.StateFlag.State_Off

            style = (opt.widget.style() if opt.widget is not None else QApplication.style())
            if style is not None:
                style.drawPrimitive(
                    QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck,
                    chk_opt,
                    painter,
                    opt.widget,
                )

        # Decoration icon / pixmap
        deco = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(deco, QIcon) and not deco.isNull():
            ic_sz = 16
            ic_y = rect.y() + (rect.height() - ic_sz) // 2
            deco.paint(painter, QRect(content_rect.left(), ic_y, ic_sz, ic_sz))
            content_rect.setLeft(content_rect.left() + ic_sz + 7)
        elif isinstance(deco, QPixmap) and not deco.isNull():
            dpr = max(1.0, deco.devicePixelRatio())
            pw = int(round(deco.width() / dpr))
            ph = int(round(deco.height() / dpr))
            ic_y = rect.y() + (rect.height() - ph) // 2
            painter.drawPixmap(content_rect.left(), ic_y, deco)
            content_rect.setLeft(content_rect.left() + pw + 7)

        # Text drawing
        if opt.text and content_rect.width() > 4:
            font = QFont(opt.font)
            if is_group_hdr:
                font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)

            fg_data = index.data(Qt.ItemDataRole.ForegroundRole)
            if isinstance(fg_data, QBrush) and fg_data.color().isValid():
                text_color = fg_data.color()
            elif isinstance(fg_data, QColor) and fg_data.isValid():
                text_color = fg_data
            else:
                text_color = QColor(p["text_primary"])
            painter.setPen(text_color)

            align = opt.displayAlignment or (
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
            )
            fm = QFontMetrics(font)
            elided = fm.elidedText(opt.text, Qt.TextElideMode.ElideRight, content_rect.width())
            painter.drawText(content_rect, int(align), elided)

        # 7. Inline 1-click hover copy button
        if show_copy_btn:
            self._paint_copy_button(
                painter,
                btn_rect,
                is_copied=is_copied,
                is_btn_hovered=self._is_copy_btn_hovered(index),
                p=p,
            )

        painter.restore()


class TableCraftController(QObject):
    """Viewport event filter driving 60-FPS dirty-rect row hover & inline copy clicks."""

    def __init__(
        self,
        view: QAbstractItemView,
        delegate: ModernItemDelegate,
    ) -> None:
        super().__init__(view)
        self._view = view
        self._delegate = delegate
        self._hovered_row: int = -1
        self._hovered_parent: QPersistentModelIndex = QPersistentModelIndex()
        self._pressed_copy_index: QPersistentModelIndex = QPersistentModelIndex()
        self._pointer_active: bool = False

        view.setMouseTracking(True)
        vp = view.viewport()
        if vp is not None:
            vp.setMouseTracking(True)
            vp.installEventFilter(self)

    def _row_visual_rect(self, row: int, parent: QModelIndex) -> QRect:
        vp = self._view.viewport()
        model = self._view.model()
        if vp is None or model is None or row < 0:
            return QRect()
        if row >= model.rowCount(parent):
            return QRect()
        idx = model.index(row, 0, parent)
        if not idx.isValid():
            return QRect()
        r = self._view.visualRect(idx)
        if not r.isValid():
            return QRect()
        return QRect(0, r.top(), vp.width(), r.height() + 1)

    def _update_hover_row(self, index: QModelIndex) -> None:
        new_row = index.row() if index.isValid() else -1
        new_parent = index.parent() if index.isValid() else QModelIndex()
        old_row = self._hovered_row
        old_parent = QModelIndex(self._hovered_parent) if self._hovered_parent.isValid() else QModelIndex()

        if new_row == old_row and new_parent == old_parent:
            return

        vp = self._view.viewport()
        old_rect = self._row_visual_rect(old_row, old_parent) if old_row >= 0 else QRect()
        self._hovered_row = new_row
        self._hovered_parent = QPersistentModelIndex(new_parent) if new_parent.isValid() else QPersistentModelIndex()
        self._delegate.set_hovered_index(index)
        new_rect = self._row_visual_rect(new_row, new_parent) if new_row >= 0 else QRect()

        if vp is not None:
            if old_rect.isValid():
                vp.update(old_rect)
            if new_rect.isValid():
                vp.update(new_rect)

    def _hit_copy_index(self, pos) -> QModelIndex:
        idx = self._view.indexAt(pos)
        if not idx.isValid():
            return QModelIndex()
        if not self._delegate.get_copy_text(idx):
            return QModelIndex()
        cell_rect = self._view.visualRect(idx)
        if not cell_rect.isValid():
            return QModelIndex()
        btn_rect = self._delegate.copy_button_rect(cell_rect)
        if btn_rect.contains(pos):
            return idx
        return QModelIndex()

    def _set_cursor_pointing(self, pointing: bool) -> None:
        if pointing == self._pointer_active:
            return
        self._pointer_active = pointing
        vp = self._view.viewport()
        if vp is not None:
            vp.setCursor(
                Qt.CursorShape.PointingHandCursor
                if pointing
                else Qt.CursorShape.ArrowCursor
            )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        try:
            ev_type = event.type()
            if ev_type not in (
                QEvent.Type.MouseMove,
                QEvent.Type.Leave,
                QEvent.Type.HoverLeave,
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonRelease,
                QEvent.Type.MouseButtonDblClick,
            ):
                return False
            vp = self._view.viewport()
        except RuntimeError:
            return False

        if ev_type == QEvent.Type.MouseMove:
            if isinstance(event, QMouseEvent):
                pos = event.position().toPoint()
                idx = self._view.indexAt(pos)
                self._update_hover_row(idx)
                hit = self._hit_copy_index(pos)
                prev_hit = self._delegate._hovered_copy_index
                self._delegate.set_hovered_copy_index(hit)
                self._set_cursor_pointing(hit.isValid())
                if vp is not None and (
                    (prev_hit.isValid() != hit.isValid())
                    or (prev_hit.isValid() and QModelIndex(prev_hit) != hit)
                ):
                    if prev_hit.isValid():
                        vp.update(self._view.visualRect(QModelIndex(prev_hit)))
                    if hit.isValid():
                        vp.update(self._view.visualRect(hit))
            return False

        if ev_type in (QEvent.Type.Leave, QEvent.Type.HoverLeave):
            self._update_hover_row(QModelIndex())
            self._delegate.set_hovered_copy_index(QModelIndex())
            self._set_cursor_pointing(False)
            return False

        if ev_type == QEvent.Type.MouseButtonPress:
            if isinstance(event, QMouseEvent) and event.button() == Qt.MouseButton.LeftButton:
                hit = self._hit_copy_index(event.position().toPoint())
                if hit.isValid():
                    self._pressed_copy_index = QPersistentModelIndex(hit)
                    return True
            self._pressed_copy_index = QPersistentModelIndex()
            return False

        if ev_type == QEvent.Type.MouseButtonRelease:
            if isinstance(event, QMouseEvent) and event.button() == Qt.MouseButton.LeftButton:
                if self._pressed_copy_index.isValid():
                    pressed_idx = QModelIndex(self._pressed_copy_index)
                    self._pressed_copy_index = QPersistentModelIndex()
                    hit = self._hit_copy_index(event.position().toPoint())
                    if hit.isValid() and hit == pressed_idx:
                        self._delegate.trigger_copy(hit)
                    return True
            return False

        if ev_type == QEvent.Type.MouseButtonDblClick:
            if isinstance(event, QMouseEvent) and event.button() == Qt.MouseButton.LeftButton:
                hit = self._hit_copy_index(event.position().toPoint())
                if hit.isValid():
                    return True
            return False

        return False


def enhance_table_view(
    table_view: QTableView,
    theme: str = "light",
    on_code_copied: Optional[Callable[[str], None]] = None,
    *,
    row_height: int = 36,
    header_height: int = 34,
    show_left_accent: bool = True,
    copy_columns: Optional[set[int]] = None,
    hide_vertical_header: bool = True,
    slim_scrollbars: bool = True,
) -> ModernItemDelegate:
    """Apply Option B visual craft, hover copy, and slim scrollbars to a ``QTableView``."""
    table_view.setShowGrid(False)
    table_view.setAlternatingRowColors(False)
    table_view.setWordWrap(False)
    table_view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    table_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

    vh = table_view.verticalHeader()
    if vh is not None:
        vh.setDefaultSectionSize(row_height)
        vh.setMinimumSectionSize(max(24, row_height - 6))
        if hide_vertical_header:
            vh.setVisible(False)

    hh = table_view.horizontalHeader()
    if hh is not None:
        hh.setMinimumHeight(header_height)
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hh.setHighlightSections(False)
        hh.setSortIndicatorShown(True)
        if hh.sectionResizeMode(0) != QHeaderView.ResizeMode.Interactive:
            pass  # Respect caller / TableHeaderManager resize modes

    delegate = ModernItemDelegate(
        table_view,
        theme=theme,
        row_height=row_height,
        show_left_accent=show_left_accent,
        copy_columns=copy_columns,
    )
    delegate.bind_view(table_view)
    table_view.setItemDelegate(delegate)

    controller = TableCraftController(table_view, delegate)
    setattr(table_view, "_craft_delegate", delegate)
    setattr(table_view, "_craft_controller", controller)

    if slim_scrollbars:
        apply_slim_scrollbars(table_view, theme=theme)

    if on_code_copied is not None:
        delegate.code_copied.connect(on_code_copied)

    return delegate


def enhance_tree_view(
    tree_view: QTreeView,
    theme: str = "light",
    on_code_copied: Optional[Callable[[str], None]] = None,
    *,
    row_height: int = 34,
    show_left_accent: bool = True,
    copy_columns: Optional[set[int]] = None,
    slim_scrollbars: bool = True,
) -> ModernItemDelegate:
    """Apply Option B visual craft and slim scrollbars to a ``QTreeView``/``QTreeWidget``."""
    tree_view.setAlternatingRowColors(False)
    tree_view.setUniformRowHeights(True)
    tree_view.setAnimated(False)
    tree_view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    tree_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

    delegate = ModernItemDelegate(
        tree_view,
        theme=theme,
        row_height=row_height,
        show_left_accent=show_left_accent,
        copy_columns=copy_columns,
    )
    delegate.bind_view(tree_view)
    tree_view.setItemDelegate(delegate)

    controller = TableCraftController(tree_view, delegate)
    setattr(tree_view, "_craft_delegate", delegate)
    setattr(tree_view, "_craft_controller", controller)

    if slim_scrollbars:
        apply_slim_scrollbars(tree_view, theme=theme)

    if on_code_copied is not None:
        delegate.code_copied.connect(on_code_copied)

    return delegate


def sync_widget_craft_theme(
    root: Optional[QWidget] = None,
    theme: str = "light",
) -> None:
    """Synchronize all ``ModernItemDelegate`` and ``SlimScrollBar`` instances to ``theme``."""
    roots: list[QWidget]
    if root is not None:
        roots = [root]
    else:
        app = QApplication.instance()
        roots = list(app.topLevelWidgets()) if isinstance(app, QApplication) else []

    for top in roots:
        scrollbars = list(top.findChildren(SlimScrollBar))
        if isinstance(top, SlimScrollBar):
            scrollbars.insert(0, top)
        for sb in scrollbars:
            sb.set_theme(theme)

        views = list(top.findChildren(QAbstractItemView))
        if isinstance(top, QAbstractItemView):
            views.insert(0, top)
        for view in views:
            delegate = getattr(view, "_craft_delegate", None)
            if isinstance(delegate, ModernItemDelegate):
                delegate.set_theme(theme)
            else:
                item_del = view.itemDelegate()
                if isinstance(item_del, ModernItemDelegate):
                    item_del.set_theme(theme)
