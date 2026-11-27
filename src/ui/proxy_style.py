"""Global deterministic Fusion proxy style eliminating all QSS indicator defects.

Replaces broken/native OS indicators with razor-sharp antialiased vector craft:
- Solid-fill white-checkmark ``PE_IndicatorCheckBox`` &
  ``PE_IndicatorItemViewItemCheck`` (fixes hollow checkboxes & dark-mode item
  check visibility)
- True circular ``PE_IndicatorRadioButton`` with crisp inner dot
- 1.5px vector chevrons for ``PE_IndicatorArrowDown/Up/Left/Right``,
  ``PE_IndicatorSpinUp/Down``, ``PE_IndicatorHeaderArrow``,
  ``PE_IndicatorBranch``, and ``PE_IndicatorMenuCheckMark``
- Suppresses dotted ``PE_FrameFocusRect`` and Fusion disabled-text etching
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QProxyStyle,
    QStyle,
    QStyleFactory,
    QStyleOption,
    QStyleOptionHeader,
    QWidget,
)

from src.ui.icons import paint_icon
from src.ui.theme import get_palette

_STYLE_ATTR = "_app_proxy_style"


class AppProxyStyle(QProxyStyle):
    """Deterministic Fusion-based proxy style with theme-aware vector controls."""

    def __init__(self, theme: str = "light", base_style: str = "Fusion") -> None:
        base = QStyleFactory.create(base_style)
        super().__init__(base if base is not None else base_style)
        self._theme: str = "dark" if theme == "dark" else "light"

    def theme(self) -> str:
        return self._theme

    def set_theme(self, theme: str) -> None:
        self._theme = "dark" if theme == "dark" else "light"

    def _palette(self) -> dict[str, str]:
        return get_palette(self._theme)

    def _arrow_color(
        self,
        option: QStyleOption,
        widget: Optional[QWidget],
    ) -> QColor:
        p = self._palette()
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        if not enabled:
            return QColor(p["text_muted"])

        if widget is not None:
            try:
                variant = str(widget.property("variant") or "")
            except RuntimeError:
                variant = ""
            if variant == "primary":
                return QColor(p["cta_text"])
            if variant in ("danger", "success", "accent"):
                return QColor("#FFFFFF")

        hovered = bool(
            option.state
            & (QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_Sunken)
        )
        return QColor(p["text_primary"] if hovered else p["text_secondary"])

    def _draw_checkbox(
        self,
        option: QStyleOption,
        painter: QPainter,
        *,
        is_item_view: bool = False,
    ) -> None:
        p = self._palette()
        r = option.rect
        side = min(16.0, float(min(r.width(), r.height())))
        if side < 8.0:
            side = 14.0
        box = QRectF(
            r.x() + (r.width() - side) * 0.5,
            r.y() + (r.height() - side) * 0.5,
            side,
            side,
        ).adjusted(0.5, 0.5, -0.5, -0.5)

        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        checked = bool(option.state & QStyle.StateFlag.State_On)
        indeterminate = bool(option.state & QStyle.StateFlag.State_NoChange)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if checked or indeterminate:
            if not enabled:
                fill = QColor(p["btn_disabled_bg"])
                border = QColor(p["btn_disabled_border"])
                glyph_col = QColor(p["btn_disabled_text"])
            else:
                fill = QColor(p["accent_hover"] if hovered else p["accent"])
                border = fill
                glyph_col = QColor("#FFFFFF")

            painter.setPen(QPen(border, 1.0))
            painter.setBrush(QBrush(fill))
            painter.drawRoundedRect(box, 4.0, 4.0)

            pen = QPen(
                glyph_col,
                1.85,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)

            if indeterminate:
                y_mid = box.center().y()
                painter.drawLine(
                    QPointF(box.left() + box.width() * 0.25, y_mid),
                    QPointF(box.right() - box.width() * 0.25, y_mid),
                )
            else:
                ck = QPainterPath()
                ck.moveTo(box.left() + box.width() * 0.22, box.top() + box.height() * 0.53)
                ck.lineTo(box.left() + box.width() * 0.43, box.top() + box.height() * 0.74)
                ck.lineTo(box.left() + box.width() * 0.79, box.top() + box.height() * 0.28)
                painter.drawPath(ck)
        else:
            if not enabled:
                fill = QColor(p["btn_disabled_bg"])
                border = QColor(p["btn_disabled_border"])
            else:
                fill = QColor(p["bg_surface"] if is_item_view else p["bg_input"])
                border = QColor(p["accent"] if hovered else p["border_strong"])
            painter.setPen(QPen(border, 1.25))
            painter.setBrush(QBrush(fill))
            painter.drawRoundedRect(box, 4.0, 4.0)

        painter.restore()

    def _draw_radio(self, option: QStyleOption, painter: QPainter) -> None:
        p = self._palette()
        r = option.rect
        side = min(16.0, float(min(r.width(), r.height())))
        if side < 8.0:
            side = 14.0
        circle = QRectF(
            r.x() + (r.width() - side) * 0.5,
            r.y() + (r.height() - side) * 0.5,
            side,
            side,
        ).adjusted(0.6, 0.6, -0.6, -0.6)

        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        checked = bool(option.state & QStyle.StateFlag.State_On)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if checked:
            if not enabled:
                fill = QColor(p["btn_disabled_bg"])
                border = QColor(p["btn_disabled_border"])
                dot = QColor(p["btn_disabled_text"])
            else:
                fill = QColor(p["accent_hover"] if hovered else p["accent"])
                border = fill
                dot = QColor("#FFFFFF")
            painter.setPen(QPen(border, 1.0))
            painter.setBrush(QBrush(fill))
            painter.drawEllipse(circle)

            dot_r = circle.width() * 0.21
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(dot))
            painter.drawEllipse(circle.center(), dot_r, dot_r)
        else:
            if not enabled:
                fill = QColor(p["btn_disabled_bg"])
                border = QColor(p["btn_disabled_border"])
            else:
                fill = QColor(p["bg_input"])
                border = QColor(p["accent"] if hovered else p["border_strong"])
            painter.setPen(QPen(border, 1.25))
            painter.setBrush(QBrush(fill))
            painter.drawEllipse(circle)

        painter.restore()

    def _draw_chevron(
        self,
        icon_name: str,
        rect: QRectF,
        painter: QPainter,
        color: QColor,
        max_side: float = 12.0,
        min_side: float = 8.0,
        stroke_width: float = 1.6,
    ) -> None:
        side = max(min_side, min(max_side, max(rect.width(), rect.height())))
        target = QRectF(
            rect.center().x() - side * 0.5,
            rect.center().y() - side * 0.5,
            side,
            side,
        )
        paint_icon(painter, icon_name, target, color, stroke_width=stroke_width)

    def drawPrimitive(
        self,
        element: QStyle.PrimitiveElement,
        option: QStyleOption,
        painter: QPainter,
        widget: Optional[QWidget] = None,
    ) -> None:
        pe = QStyle.PrimitiveElement
        if element == pe.PE_FrameFocusRect:
            # Suppress dotted Qt focus rectangles globally
            return

        if element == pe.PE_IndicatorCheckBox:
            self._draw_checkbox(option, painter, is_item_view=False)
            return

        if element == pe.PE_IndicatorItemViewItemCheck:
            self._draw_checkbox(option, painter, is_item_view=True)
            return

        if element == pe.PE_IndicatorRadioButton:
            self._draw_radio(option, painter)
            return

        if element in (
            pe.PE_IndicatorArrowDown,
            pe.PE_IndicatorArrowUp,
            pe.PE_IndicatorArrowLeft,
            pe.PE_IndicatorArrowRight,
        ):
            name_map = {
                pe.PE_IndicatorArrowDown: "chevron-down",
                pe.PE_IndicatorArrowUp: "chevron-up",
                pe.PE_IndicatorArrowLeft: "chevron-left",
                pe.PE_IndicatorArrowRight: "chevron-right",
            }
            if element == pe.PE_IndicatorArrowDown and isinstance(widget, QComboBox):
                # Erase Fusion's default bevel on the unstyled drop-down area
                p = self._palette()
                enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
                fill_col = QColor(p["bg_input"] if enabled else p["bg_surface_alt"])
                wr = widget.rect()
                left_x = float(max(2, option.rect.left() - 8))
                cover = QRectF(
                    left_x,
                    1.0,
                    float(max(14, wr.width() - left_x - 1.0)),
                    float(max(4, wr.height() - 2.0)),
                )
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(fill_col))
                painter.drawRoundedRect(cover, 5.0, 5.0)
                painter.restore()
            col = self._arrow_color(option, widget)
            self._draw_chevron(
                name_map[element],
                QRectF(option.rect),
                painter,
                col,
                max_side=11.5,
                stroke_width=1.6,
            )
            return

        if element in (pe.PE_IndicatorSpinUp, pe.PE_IndicatorSpinDown):
            if isinstance(widget, QAbstractSpinBox):
                # Erase Fusion's default spin-button bevel cleanly inside the rounded border
                p = self._palette()
                enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
                hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
                if not enabled:
                    fill_col = QColor(p["bg_surface_alt"])
                elif hovered:
                    fill_col = QColor(p["bg_hover"])
                else:
                    fill_col = QColor(p["bg_input"])
                wr = widget.rect()
                left_x = float(max(2, option.rect.left() - 7))
                right_x = float(wr.width() - 1.0)
                mid_y = float(wr.height()) * 0.5
                if element == pe.PE_IndicatorSpinUp:
                    cover = QRectF(left_x, 1.0, max(12.0, right_x - left_x), max(4.0, mid_y - 1.0))
                else:
                    cover = QRectF(left_x, mid_y, max(12.0, right_x - left_x), max(4.0, wr.height() - mid_y - 1.0))
                painter.save()
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(fill_col))
                painter.drawRoundedRect(cover, 4.0, 4.0)
                painter.setPen(QPen(QColor(p["border_subtle"]), 1.0))
                painter.drawLine(QPointF(left_x, cover.top() + 0.5), QPointF(left_x, cover.bottom() - 0.5))
                if element == pe.PE_IndicatorSpinDown:
                    painter.drawLine(QPointF(left_x, mid_y), QPointF(right_x - 1.0, mid_y))
                painter.restore()
                chev_rect = cover
            else:
                chev_rect = QRectF(option.rect)
            icon_name = "chevron-up" if element == pe.PE_IndicatorSpinUp else "chevron-down"
            col = self._arrow_color(option, widget)
            self._draw_chevron(
                icon_name,
                chev_rect,
                painter,
                col,
                max_side=9.0,
                min_side=8.0,
                stroke_width=1.55,
            )
            return

        if element == pe.PE_IndicatorMenuCheckMark:
            p = self._palette()
            enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
            col = QColor(p["accent"] if enabled else p["text_muted"])
            self._draw_chevron(
                "check",
                QRectF(option.rect),
                painter,
                col,
                max_side=13.0,
                stroke_width=1.8,
            )
            return

        if element == pe.PE_IndicatorHeaderArrow:
            p = self._palette()
            col = QColor(p["accent"])
            icon_name = "chevron-down"
            if isinstance(option, QStyleOptionHeader):
                if option.sortIndicator == QStyleOptionHeader.SortIndicator.SortDown:
                    icon_name = "chevron-up"
                elif option.sortIndicator == QStyleOptionHeader.SortIndicator.SortUp:
                    icon_name = "chevron-down"
            self._draw_chevron(
                icon_name,
                QRectF(option.rect),
                painter,
                col,
                max_side=10.5,
                stroke_width=1.7,
            )
            return

        if element == pe.PE_IndicatorBranch:
            # Clean minimal chevron without legacy dotted tree lines
            if bool(option.state & QStyle.StateFlag.State_Children):
                p = self._palette()
                hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
                col = QColor(p["text_primary"] if hovered else p["text_secondary"])
                opened = bool(option.state & QStyle.StateFlag.State_Open)
                icon_name = "chevron-down" if opened else "chevron-right"
                self._draw_chevron(
                    icon_name,
                    QRectF(option.rect),
                    painter,
                    col,
                    max_side=12.0,
                    stroke_width=1.6,
                )
            return

        super().drawPrimitive(element, option, painter, widget)

    def pixelMetric(
        self,
        metric: QStyle.PixelMetric,
        option: Optional[QStyleOption] = None,
        widget: Optional[QWidget] = None,
    ) -> int:
        pm = QStyle.PixelMetric
        if metric in (
            pm.PM_IndicatorWidth,
            pm.PM_IndicatorHeight,
            pm.PM_ExclusiveIndicatorWidth,
            pm.PM_ExclusiveIndicatorHeight,
        ):
            return 16
        if metric == pm.PM_MenuButtonIndicator:
            return 14
        if metric in (pm.PM_FocusFrameHMargin, pm.PM_FocusFrameVMargin):
            return 0
        return super().pixelMetric(metric, option, widget)

    def styleHint(
        self,
        hint: QStyle.StyleHint,
        option: Optional[QStyleOption] = None,
        widget: Optional[QWidget] = None,
        returnData=None,
    ) -> int:
        sh = QStyle.StyleHint
        if hint == sh.SH_EtchDisabledText:
            return 0
        if hint == sh.SH_ItemView_ShowDecorationSelected:
            return 1
        if hint == sh.SH_DrawMenuBarSeparator:
            return 0
        return super().styleHint(hint, option, widget, returnData)


def install_app_style(
    app: QApplication,
    theme: str = "light",
) -> AppProxyStyle:
    """Install (or update) ``AppProxyStyle`` on ``app``."""
    existing = getattr(app, _STYLE_ATTR, None)
    if isinstance(existing, AppProxyStyle):
        existing.set_theme(theme)
        return existing
    proxy = AppProxyStyle(theme=theme, base_style="Fusion")
    app.setStyle(proxy)
    setattr(app, _STYLE_ATTR, proxy)
    return proxy


def sync_app_style_theme(theme: str) -> None:
    """Update the active ``AppProxyStyle`` theme if installed."""
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        return
    proxy = getattr(app, _STYLE_ATTR, None)
    if isinstance(proxy, AppProxyStyle):
        proxy.set_theme(theme)
