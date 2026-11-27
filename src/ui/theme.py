"""Option B (Linear / Obsidian Pro) monochrome-first Light & Dark theme system.

Provides:
- Complete Light ("Paper Monochrome") and Dark ("Obsidian Graphite") design
  tokens with strict neutral surfaces, Ink/Ivory primary CTAs, surgical
  periwinkle accents, and subtle semantic row tints.
- Refined Windows typography helpers (Segoe UI Variable -> Microsoft YaHei UI,
  Cascadia Mono -> Consolas).
- Clean Qt6 QSS stylesheet deliberately free of conflicting indicator /
  arrow / single-cell-hover rules so ``AppProxyStyle`` and
  ``ModernItemDelegate`` render razor-sharp native-grade controls.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtGui import QFont, QFontDatabase

UI_FONT_FAMILIES: tuple[str, ...] = (
    "Segoe UI Variable Text",
    "Segoe UI",
    "Microsoft YaHei UI",
    "PingFang SC",
)

MONO_FONT_FAMILIES: tuple[str, ...] = (
    "Cascadia Mono",
    "JetBrains Mono",
    "Consolas",
    "Courier New",
)

UI_FONT_CSS: str = (
    '"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
)
MONO_FONT_CSS: str = '"Cascadia Mono", "JetBrains Mono", "Consolas", monospace'


LIGHT_PALETTE: dict[str, str] = {
    # Native Win11 DWM chrome
    "win32_caption": "#FAFAFA",
    "win32_border": "#E4E4E7",
    # Surface hierarchy (3 clear tonal layers)
    "bg_sidebar": "#F5F5F6",
    "bg_window": "#FAFAFA",
    "bg_surface": "#FFFFFF",
    "bg_surface_alt": "#F4F4F5",
    "bg_input": "#FFFFFF",
    "bg_hover": "#EFEFF1",
    "bg_selected": "#EBECEF",
    "bg_group_band": "#FAFAFB",
    # Hairline borders
    "border_subtle": "#EFEFF2",
    "border_default": "#E4E4E7",
    "border": "#E4E4E7",
    "border_strong": "#D4D4D8",
    # Typography
    "text_primary": "#09090B",
    "text_secondary": "#52525B",
    "text_muted": "#A1A1AA",
    # Monochrome Ink primary CTA
    "cta_bg": "#18181B",
    "cta_hover": "#27272A",
    "cta_pressed": "#09090B",
    "cta_text": "#FFFFFF",
    # Surgical Linear periwinkle accent (focus rings, active bars, checks)
    "accent": "#5E6AD2",
    "accent_hover": "#4F5BC4",
    "accent_pressed": "#434EB3",
    "accent_soft": "#EFF0FC",
    "accent_border": "#C7CCF4",
    "accent_text": "#FFFFFF",
    # Semantic tones (desaturated surgical)
    "success": "#15803D",
    "success_hover": "#166534",
    "success_pressed": "#14532D",
    "success_soft": "#F0FDF4",
    "success_border": "#BBF7D0",
    "success_cta_bg": "#15803D",
    "success_cta_hover": "#166534",
    "success_cta_pressed": "#14532D",
    "warning": "#B45309",
    "warning_soft": "#FFFBEB",
    "warning_border": "#FDE68A",
    "danger": "#B91C1C",
    "danger_hover": "#991B1B",
    "danger_pressed": "#7F1D1D",
    "danger_soft": "#FEF2F2",
    "danger_border": "#FECACA",
    "danger_cta_bg": "#B91C1C",
    "danger_cta_hover": "#991B1B",
    "danger_cta_pressed": "#7F1D1D",
    "purple": "#6D28D9",
    "purple_soft": "#F5F3FF",
    "purple_border": "#DDD6FE",
    # Subtle table row semantic washes
    "row_tint_matched": "#F6FBF7",
    "row_tint_missing": "#FEF7F7",
    "row_tint_checked": "#FEF2F2",
    # Controls & icon tokens
    "btn_disabled_bg": "#F4F4F5",
    "btn_disabled_border": "#E4E4E7",
    "btn_disabled_text": "#A1A1AA",
    "scrollbar_rest": "#D4D4D8",
    "scrollbar_hover": "#A1A1AA",
    "scrollbar": "#D4D4D8",
    "icon_default": "#52525B",
    "icon_hover": "#09090B",
    "icon_active": "#5E6AD2",
    "icon_muted": "#A1A1AA",
}


DARK_PALETTE: dict[str, str] = {
    # Native Win11 DWM chrome
    "win32_caption": "#0B0C0E",
    "win32_border": "#22242A",
    # Surface hierarchy (Obsidian Graphite — zero blue-navy cast)
    "bg_sidebar": "#0E0F12",
    "bg_window": "#111216",
    "bg_surface": "#16181D",
    "bg_surface_alt": "#1C1F26",
    "bg_input": "#0E0F12",
    "bg_hover": "#20232B",
    "bg_selected": "#232635",
    "bg_group_band": "#13151A",
    # Hairline borders
    "border_subtle": "#1E2028",
    "border_default": "#262933",
    "border": "#262933",
    "border_strong": "#353946",
    # Typography
    "text_primary": "#F4F4F5",
    "text_secondary": "#A1A1AA",
    "text_muted": "#63636E",
    # Monochrome Ivory primary CTA
    "cta_bg": "#F4F4F5",
    "cta_hover": "#FFFFFF",
    "cta_pressed": "#E4E4E7",
    "cta_text": "#09090B",
    # Surgical Linear periwinkle accent
    "accent": "#7C86E8",
    "accent_hover": "#929BF0",
    "accent_pressed": "#6772DE",
    "accent_soft": "#1D2038",
    "accent_border": "#343A68",
    "accent_text": "#FFFFFF",
    # Semantic tones
    "success": "#4ADE80",
    "success_hover": "#22C55E",
    "success_pressed": "#16A34A",
    "success_soft": "#102419",
    "success_border": "#1B432C",
    "success_cta_bg": "#15803D",
    "success_cta_hover": "#16A34A",
    "success_cta_pressed": "#166534",
    "warning": "#FBBF24",
    "warning_soft": "#261E0F",
    "warning_border": "#473616",
    "danger": "#FB7185",
    "danger_hover": "#F43F5E",
    "danger_pressed": "#E11D48",
    "danger_soft": "#2A1318",
    "danger_border": "#4E1F29",
    "danger_cta_bg": "#DC2626",
    "danger_cta_hover": "#EF4444",
    "danger_cta_pressed": "#B91C1C",
    "purple": "#A78BFA",
    "purple_soft": "#201833",
    "purple_border": "#382A5C",
    # Subtle table row semantic washes
    "row_tint_matched": "#111E17",
    "row_tint_missing": "#221417",
    "row_tint_checked": "#261418",
    # Controls & icon tokens
    "btn_disabled_bg": "#1C1F26",
    "btn_disabled_border": "#262933",
    "btn_disabled_text": "#52525B",
    "scrollbar_rest": "#2E313D",
    "scrollbar_hover": "#4B5062",
    "scrollbar": "#2E313D",
    "icon_default": "#A1A1AA",
    "icon_hover": "#F4F4F5",
    "icon_active": "#7C86E8",
    "icon_muted": "#52525B",
}


def get_palette(theme: str = "light") -> dict[str, str]:
    """Return Option B color tokens for ``'light'`` or ``'dark'`` theme."""
    return DARK_PALETTE if theme == "dark" else LIGHT_PALETTE


def _pick_installed_family(candidates: tuple[str, ...], fallback: str) -> str:
    try:
        available = set(QFontDatabase.families())
        for name in candidates:
            if name in available:
                return name
    except Exception:
        pass
    return fallback


def get_ui_font(
    point_size: float = 9.5,
    weight: Optional[QFont.Weight] = None,
) -> QFont:
    """Return the refined UI sans font cascading Segoe UI Variable -> YaHei UI."""
    primary = _pick_installed_family(UI_FONT_FAMILIES, "Microsoft YaHei UI")
    font = QFont(primary)
    font.setFamilies(list(UI_FONT_FAMILIES))
    font.setPointSizeF(point_size)
    if weight is not None:
        font.setWeight(weight)
    return font


def get_mono_font(
    point_size: float = 9.5,
    weight: QFont.Weight = QFont.Weight.DemiBold,
) -> QFont:
    """Return crisp monospace font (Cascadia Mono -> Consolas) for codes & counts."""
    primary = _pick_installed_family(MONO_FONT_FAMILIES, "Consolas")
    font = QFont(primary)
    font.setFamilies(list(MONO_FONT_FAMILIES))
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setFixedPitch(True)
    font.setPointSizeF(point_size)
    font.setWeight(weight)
    return font


def build_stylesheet(theme: str = "light") -> str:
    """Build complete Option B (Linear / Obsidian Pro) Qt6 QSS stylesheet."""
    p = get_palette(theme)
    return f"""
/* ===== Global ===== */
QMainWindow, QDialog {{
    background-color: {p['bg_window']};
    color: {p['text_primary']};
    font-family: {UI_FONT_CSS};
    font-size: 13px;
}}

QWidget {{
    color: {p['text_primary']};
    font-family: {UI_FONT_CSS};
    font-size: 13px;
}}

QLabel {{
    background: transparent;
}}

QLabel[role="display"] {{
    font-size: 20px;
    font-weight: 700;
    color: {p['text_primary']};
}}

QLabel[role="title"], QLabel[role="page-title"] {{
    font-size: 16px;
    font-weight: 600;
    color: {p['text_primary']};
}}

QLabel[role="section"] {{
    font-size: 13px;
    font-weight: 600;
    color: {p['text_primary']};
}}

QLabel[role="subtitle"] {{
    font-size: 12px;
    color: {p['text_secondary']};
}}

QLabel[role="muted"] {{
    font-size: 12px;
    color: {p['text_muted']};
}}

QLabel[role="micro"] {{
    font-size: 11px;
    font-weight: 600;
    color: {p['text_muted']};
}}

QLabel[mono="true"], QLineEdit[mono="true"], QPlainTextEdit[mono="true"] {{
    font-family: {MONO_FONT_CSS};
}}

/* ===== Inline Badge Pills ===== */
QLabel[badge="true"], QLabel[badge="accent"], QLabel[badge="primary"], QLabel[badge="info"] {{
    background-color: {p['accent_soft']};
    color: {p['accent']};
    border: 1px solid {p['accent_border']};
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}}

QLabel[badge="success"] {{
    background-color: {p['success_soft']};
    color: {p['success']};
    border: 1px solid {p['success_border']};
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}}

QLabel[badge="warning"] {{
    background-color: {p['warning_soft']};
    color: {p['warning']};
    border: 1px solid {p['warning_border']};
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}}

QLabel[badge="danger"] {{
    background-color: {p['danger_soft']};
    color: {p['danger']};
    border: 1px solid {p['danger_border']};
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}}

QLabel[badge="purple"] {{
    background-color: {p['purple_soft']};
    color: {p['purple']};
    border: 1px solid {p['purple_border']};
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}}

QLabel[badge="muted"], QLabel[badge="neutral"] {{
    background-color: {p['bg_surface_alt']};
    color: {p['text_secondary']};
    border: 1px solid {p['border_default']};
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}}

/* Clickable metric pill buttons (e.g. duplicate count link in Library header) */
QPushButton[metricPill="warning"] {{
    background-color: {p['warning_soft']};
    color: {p['warning']};
    border: 1px solid {p['warning_border']};
    border-radius: 11px;
    padding: 2px 9px;
    font-size: 11px;
    font-weight: 600;
    min-height: 18px;
}}

QPushButton[metricPill="warning"]:hover {{
    border-color: {p['warning']};
}}

/* ===== Left Sidebar ===== */
QFrame#Sidebar {{
    background-color: {p['bg_sidebar']};
    border-right: 1px solid {p['border_default']};
}}

QLabel#AppBrandTitle {{
    font-size: 14px;
    font-weight: 700;
    color: {p['text_primary']};
}}

QLabel#AppBrandSub {{
    font-size: 11px;
    color: {p['text_muted']};
}}

QLabel[sidebarSection="true"] {{
    font-size: 11px;
    font-weight: 600;
    color: {p['text_muted']};
    padding: 8px 8px 3px 8px;
}}

QFrame#SidebarDivider {{
    background-color: {p['border_default']};
    max-height: 1px;
    min-height: 1px;
}}

/* Notion / Linear style Workspace / DB Switcher */
QPushButton#WorkspaceSwitcher {{
    background-color: {p['bg_surface']};
    color: {p['text_primary']};
    border: 1px solid {p['border_default']};
    border-radius: 8px;
    padding: 6px 9px;
    text-align: left;
    min-height: 36px;
}}

QPushButton#WorkspaceSwitcher:hover {{
    background-color: {p['bg_hover']};
    border-color: {p['border_strong']};
}}

QPushButton#WorkspaceSwitcher:pressed {{
    background-color: {p['bg_selected']};
}}

QLabel#WorkspaceSwitcherTitle {{
    font-size: 12.5px;
    font-weight: 600;
    color: {p['text_primary']};
}}

QLabel#WorkspaceSwitcherSub {{
    font-size: 11px;
    color: {p['text_muted']};
}}

/* Sidebar Nav Items */
QPushButton[nav="true"] {{
    text-align: left;
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid transparent;
    background-color: transparent;
    color: {p['text_secondary']};
    font-size: 13px;
    font-weight: 500;
    min-height: 22px;
}}

QPushButton[nav="true"]:hover {{
    background-color: {p['bg_hover']};
    color: {p['text_primary']};
}}

QPushButton[nav="true"]:checked {{
    background-color: {p['bg_selected']};
    color: {p['text_primary']};
    font-weight: 600;
}}

QLabel[navBadge="muted"] {{
    background-color: {p['bg_surface_alt']};
    color: {p['text_secondary']};
    border: 1px solid {p['border_subtle']};
    border-radius: 9px;
    padding: 1px 6px;
    font-size: 11px;
    font-weight: 600;
}}

QLabel[navBadge="warning"] {{
    background-color: {p['warning_soft']};
    color: {p['warning']};
    border: 1px solid {p['warning_border']};
    border-radius: 9px;
    padding: 1px 6px;
    font-size: 11px;
    font-weight: 600;
}}

QLabel[navBadge="accent"] {{
    background-color: {p['accent_soft']};
    color: {p['accent']};
    border: 1px solid {p['accent_border']};
    border-radius: 9px;
    padding: 1px 6px;
    font-size: 11px;
    font-weight: 600;
}}

/* ===== Page Header Bar & Compact Strips ===== */
QFrame#PageHeaderBar, QWidget#PageHeaderBar {{
    background-color: {p['bg_window']};
    border: none;
    border-bottom: 1px solid {p['border_default']};
}}

QFrame[toolbar="true"] {{
    background-color: transparent;
    border: none;
}}

QFrame[summaryStrip="true"] {{
    background-color: {p['bg_surface_alt']};
    border: 1px solid {p['border_subtle']};
    border-radius: 7px;
}}

QFrame[actionBar="true"] {{
    background-color: {p['bg_surface']};
    border-top: 1px solid {p['border_default']};
}}

QFrame[actionBar="danger"] {{
    background-color: {p['danger_soft']};
    border-top: 1px solid {p['danger_border']};
}}

/* Legacy TopBar / Card support */
QFrame#TopBar {{
    background-color: {p['bg_surface']};
    border-bottom: 1px solid {p['border_default']};
}}

QFrame[card="true"] {{
    background-color: {p['bg_surface']};
    border: 1px solid {p['border_default']};
    border-radius: 8px;
}}

QFrame[statCard="true"] {{
    background-color: {p['bg_surface']};
    border: 1px solid {p['border_default']};
    border-radius: 8px;
}}

QFrame[statCard="true"]:hover {{
    border-color: {p['border_strong']};
}}

QLabel[statTitle="true"] {{
    font-size: 12px;
    color: {p['text_secondary']};
}}

QLabel[statValue="true"] {{
    font-size: 20px;
    font-weight: 700;
    color: {p['text_primary']};
}}

/* ===== Empty State & Drop Zone ===== */
QFrame#EmptyStateIconWell {{
    background-color: {p['bg_surface_alt']};
    border: 1px solid {p['border_default']};
    border-radius: 24px;
}}

QFrame#DropZone {{
    background-color: {p['bg_surface']};
    border: 1.5px dashed {p['border_strong']};
    border-radius: 10px;
}}

QFrame#DropZone[compact="true"] {{
    background-color: {p['bg_surface']};
    border: 1px dashed {p['border_strong']};
    border-radius: 8px;
}}

QFrame#DropZone[dragActive="true"] {{
    background-color: {p['accent_soft']};
    border: 1.5px dashed {p['accent']};
}}

/* ===== Segmented Controls ===== */
QFrame[segmented="true"] {{
    background-color: {p['bg_surface_alt']};
    border: 1px solid {p['border_default']};
    border-radius: 7px;
    padding: 2px;
}}

QPushButton[segment="true"] {{
    background-color: transparent;
    color: {p['text_secondary']};
    border: 1px solid transparent;
    border-radius: 5px;
    padding: 3px 10px;
    font-size: 12px;
    font-weight: 500;
    min-height: 20px;
}}

QPushButton[segment="true"]:hover {{
    color: {p['text_primary']};
    background-color: transparent;
    border-color: transparent;
}}

QPushButton[segment="true"]:checked {{
    background-color: {p['bg_surface']};
    color: {p['text_primary']};
    border: 1px solid {p['border_default']};
    font-weight: 600;
}}

/* ===== Buttons (QPushButton & QToolButton) ===== */
QPushButton, QToolButton {{
    background-color: {p['bg_surface']};
    color: {p['text_primary']};
    border: 1px solid {p['border_default']};
    border-radius: 6px;
    padding: 5px 12px;
    font-weight: 500;
    min-height: 20px;
}}

QPushButton:hover, QToolButton:hover {{
    background-color: {p['bg_hover']};
    border-color: {p['border_strong']};
}}

QPushButton:pressed, QToolButton:pressed {{
    background-color: {p['bg_selected']};
}}

QPushButton:disabled, QToolButton:disabled {{
    color: {p['btn_disabled_text']};
    background-color: {p['btn_disabled_bg']};
    border-color: {p['btn_disabled_border']};
}}

QPushButton:focus, QToolButton:focus {{
    border-color: {p['accent']};
}}

/* Split QToolButton (MenuButtonPopup) */
QToolButton[popupMode="1"] {{
    padding-right: 26px;
}}

QToolButton::menu-button {{
    border: none;
    border-left: 1px solid {p['border_default']};
    border-top-right-radius: 6px;
    border-bottom-right-radius: 6px;
    width: 22px;
    background-color: transparent;
}}

QToolButton::menu-button:hover {{
    background-color: {p['bg_hover']};
}}

QToolButton::menu-indicator {{
    subcontrol-origin: padding;
    subcontrol-position: right center;
    right: 6px;
    width: 12px;
}}

QPushButton::menu-indicator {{
    subcontrol-origin: padding;
    subcontrol-position: right center;
    right: 8px;
    width: 12px;
}}

/* Option B Monochrome Ink (Light) / Ivory (Dark) Primary CTA */
QPushButton[variant="primary"], QToolButton[variant="primary"] {{
    background-color: {p['cta_bg']};
    color: {p['cta_text']};
    border: 1px solid {p['cta_bg']};
    font-weight: 600;
}}

QPushButton[variant="primary"]:hover, QToolButton[variant="primary"]:hover {{
    background-color: {p['cta_hover']};
    border-color: {p['cta_hover']};
}}

QPushButton[variant="primary"]:pressed, QToolButton[variant="primary"]:pressed {{
    background-color: {p['cta_pressed']};
    border-color: {p['cta_pressed']};
}}

QToolButton[variant="primary"]::menu-button {{
    border-left: 1px solid rgba(128, 128, 128, 0.35);
}}

QToolButton[variant="primary"]::menu-button:hover {{
    background-color: rgba(128, 128, 128, 0.16);
}}

QPushButton[variant="primary"]:disabled, QToolButton[variant="primary"]:disabled {{
    background-color: {p['btn_disabled_bg']};
    border-color: {p['btn_disabled_border']};
    color: {p['btn_disabled_text']};
}}

/* Surgical Periwinkle Accent Button */
QPushButton[variant="accent"], QToolButton[variant="accent"] {{
    background-color: {p['accent']};
    color: {p['accent_text']};
    border: 1px solid {p['accent']};
    font-weight: 600;
}}

QPushButton[variant="accent"]:hover, QToolButton[variant="accent"]:hover {{
    background-color: {p['accent_hover']};
    border-color: {p['accent_hover']};
}}

QPushButton[variant="accent"]:pressed, QToolButton[variant="accent"]:pressed {{
    background-color: {p['accent_pressed']};
    border-color: {p['accent_pressed']};
}}

QPushButton[variant="accent"]:disabled, QToolButton[variant="accent"]:disabled {{
    background-color: {p['btn_disabled_bg']};
    border-color: {p['btn_disabled_border']};
    color: {p['btn_disabled_text']};
}}

/* Success Button */
QPushButton[variant="success"], QToolButton[variant="success"] {{
    background-color: {p['success_cta_bg']};
    color: #FFFFFF;
    border: 1px solid {p['success_cta_bg']};
    font-weight: 600;
}}

QPushButton[variant="success"]:hover, QToolButton[variant="success"]:hover {{
    background-color: {p['success_cta_hover']};
    border-color: {p['success_cta_hover']};
}}

QPushButton[variant="success"]:pressed, QToolButton[variant="success"]:pressed {{
    background-color: {p['success_cta_pressed']};
    border-color: {p['success_cta_pressed']};
}}

QPushButton[variant="success"]:disabled, QToolButton[variant="success"]:disabled {{
    background-color: {p['btn_disabled_bg']};
    border-color: {p['btn_disabled_border']};
    color: {p['btn_disabled_text']};
}}

/* Danger Button */
QPushButton[variant="danger"], QToolButton[variant="danger"] {{
    background-color: {p['danger_cta_bg']};
    color: #FFFFFF;
    border: 1px solid {p['danger_cta_bg']};
    font-weight: 600;
}}

QPushButton[variant="danger"]:hover, QToolButton[variant="danger"]:hover {{
    background-color: {p['danger_cta_hover']};
    border-color: {p['danger_cta_hover']};
}}

QPushButton[variant="danger"]:pressed, QToolButton[variant="danger"]:pressed {{
    background-color: {p['danger_cta_pressed']};
    border-color: {p['danger_cta_pressed']};
}}

QPushButton[variant="danger"]:disabled, QToolButton[variant="danger"]:disabled {{
    background-color: {p['btn_disabled_bg']};
    border-color: {p['btn_disabled_border']};
    color: {p['btn_disabled_text']};
}}

QPushButton[variant="danger-outline"], QToolButton[variant="danger-outline"] {{
    background-color: transparent;
    color: {p['danger']};
    border: 1px solid {p['danger_border']};
    font-weight: 600;
}}

QPushButton[variant="danger-outline"]:hover, QToolButton[variant="danger-outline"]:hover {{
    background-color: {p['danger_soft']};
    border-color: {p['danger']};
}}

QPushButton[variant="danger-ghost"], QToolButton[variant="danger-ghost"] {{
    background-color: transparent;
    color: {p['danger']};
    border: 1px solid transparent;
}}

QPushButton[variant="danger-ghost"]:hover, QToolButton[variant="danger-ghost"]:hover {{
    background-color: {p['danger_soft']};
    border-color: {p['danger_border']};
}}

/* Ghost & Outline Buttons */
QPushButton[variant="ghost"], QToolButton[variant="ghost"] {{
    background-color: transparent;
    border: 1px solid transparent;
    color: {p['text_secondary']};
}}

QPushButton[variant="ghost"]:hover, QToolButton[variant="ghost"]:hover {{
    background-color: {p['bg_hover']};
    color: {p['text_primary']};
    border-color: transparent;
}}

QPushButton[variant="ghost"]:disabled, QToolButton[variant="ghost"]:disabled {{
    background-color: transparent;
    border-color: transparent;
    color: {p['btn_disabled_text']};
}}

QPushButton[variant="outline"], QToolButton[variant="outline"] {{
    background-color: transparent;
    border: 1px solid {p['border_strong']};
    color: {p['text_primary']};
    font-weight: 600;
}}

QPushButton[variant="outline"]:hover, QToolButton[variant="outline"]:hover {{
    background-color: {p['bg_hover']};
    border-color: {p['text_secondary']};
}}

QPushButton[variant="outline"]:disabled, QToolButton[variant="outline"]:disabled {{
    background-color: transparent;
    border-color: {p['btn_disabled_border']};
    color: {p['btn_disabled_text']};
}}

QPushButton[size="sm"], QPushButton[compact="true"],
QToolButton[size="sm"], QToolButton[compact="true"] {{
    padding: 3px 9px;
    font-size: 12px;
    min-height: 18px;
}}

QPushButton[iconOnly="true"], QToolButton[iconOnly="true"] {{
    padding: 4px;
    min-width: 24px;
    min-height: 24px;
}}

/* ===== Inputs (No subcontrol overrides — AppProxyStyle paints crisp chevrons) ===== */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {{
    background-color: {p['bg_input']};
    color: {p['text_primary']};
    border: 1px solid {p['border_default']};
    border-radius: 6px;
    padding: 4px 10px;
    selection-background-color: {p['accent']};
    selection-color: #FFFFFF;
}}

QLineEdit {{
    padding: 3px 10px;
    min-height: 24px;
}}

QLineEdit QToolButton {{
    background-color: transparent;
    border: none;
    border-radius: 0px;
    padding: 0px;
    margin: 0px;
    min-height: 0px;
    min-width: 0px;
}}

QLineEdit QToolButton:hover {{
    background-color: transparent;
    border: none;
}}

QSpinBox, QComboBox {{
    min-height: 22px;
    padding-right: 12px;
}}

QLineEdit:hover, QPlainTextEdit:hover, QTextEdit:hover, QSpinBox:hover, QComboBox:hover {{
    border-color: {p['border_strong']};
}}

QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border: 1px solid {p['accent']};
}}

QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    background-color: {p['bg_surface_alt']};
    color: {p['btn_disabled_text']};
    border-color: {p['border_subtle']};
}}

QComboBox QAbstractItemView {{
    background-color: {p['bg_surface']};
    color: {p['text_primary']};
    border: 1px solid {p['border_default']};
    border-radius: 6px;
    padding: 3px;
    selection-background-color: {p['bg_selected']};
    selection-color: {p['text_primary']};
    outline: none;
}}

/* ===== Checkboxes & Radio (Indicators delegated cleanly to AppProxyStyle) ===== */
QCheckBox, QRadioButton {{
    spacing: 7px;
    color: {p['text_primary']};
    background: transparent;
}}

QCheckBox:disabled, QRadioButton:disabled {{
    color: {p['btn_disabled_text']};
}}

/* ===== Tabs ===== */
QTabWidget::pane {{
    border: 1px solid {p['border_default']};
    border-radius: 8px;
    background-color: {p['bg_surface']};
    top: -1px;
}}

QTabWidget[borderless="true"]::pane {{
    border: none;
    background-color: transparent;
    top: 0px;
}}

QTabBar::tab {{
    background-color: transparent;
    color: {p['text_secondary']};
    padding: 7px 14px;
    border: none;
    border-bottom: 2px solid transparent;
    font-size: 13px;
    font-weight: 500;
    margin-right: 2px;
}}

QTabBar::tab:hover {{
    color: {p['text_primary']};
    background-color: {p['bg_hover']};
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}}

QTabBar::tab:selected {{
    color: {p['text_primary']};
    border-bottom: 2px solid {p['accent']};
    font-weight: 600;
}}

QTabWidget[subtabs="true"]::pane {{
    border: 1px solid {p['border_default']};
    border-radius: 8px;
    background-color: {p['bg_surface']};
    top: -1px;
}}

QTabWidget[subtabs="true"] QTabBar::tab {{
    padding: 5px 12px;
    font-size: 12px;
    margin-right: 2px;
}}

QTabBar QToolButton {{
    background-color: {p['bg_surface']};
    color: {p['text_secondary']};
    border: 1px solid {p['border_default']};
    border-radius: 5px;
    margin: 2px;
    padding: 2px;
}}

QTabBar QToolButton:hover {{
    background-color: {p['bg_hover']};
    color: {p['text_primary']};
    border-color: {p['border_strong']};
}}

/* ===== Group Box & Scroll Area ===== */
QScrollArea {{
    background: transparent;
    border: none;
}}

QScrollArea > QWidget > QWidget {{
    background: transparent;
}}

QAbstractScrollArea::corner {{
    background: transparent;
    border: none;
}}

QGroupBox {{
    background-color: {p['bg_surface']};
    border: 1px solid {p['border_default']};
    border-radius: 8px;
    margin-top: 10px;
    padding: 12px 12px 10px 12px;
    font-weight: 600;
    color: {p['text_primary']};
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 6px;
    color: {p['text_secondary']};
}}

/* ===== Lists ===== */
QListWidget, QListView {{
    background-color: {p['bg_surface']};
    color: {p['text_primary']};
    border: 1px solid {p['border_default']};
    border-radius: 8px;
    padding: 4px;
    outline: none;
}}

QListWidget::item, QListView::item {{
    padding: 6px 9px;
    border-radius: 6px;
    margin: 1px 0px;
    color: {p['text_primary']};
}}

QListWidget::item:hover, QListView::item:hover {{
    background-color: {p['bg_hover']};
}}

QListWidget::item:selected, QListView::item:selected {{
    background-color: {p['bg_selected']};
    color: {p['text_primary']};
    font-weight: 600;
}}

/* ===== Multi-Table Studio Panel ===== */
QFrame#TablesSourcePanel {{
    background-color: {p['bg_surface']};
    border: 1px solid {p['border_default']};
    border-radius: 8px;
}}

QListWidget#TablesSourceList {{
    background-color: {p['bg_surface']};
    border: none;
    border-radius: 6px;
    padding: 2px;
}}

QListWidget#TablesSourceList::item {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 6px 8px;
    margin: 1px 0px;
}}

QListWidget#TablesSourceList::item:hover {{
    background-color: {p['bg_hover']};
}}

QListWidget#TablesSourceList::item:selected {{
    background-color: {p['bg_selected']};
    border-color: {p['border_strong']};
    color: {p['text_primary']};
    font-weight: 600;
}}

QTabWidget#TablesWorkTabs::pane {{
    border: 1px solid {p['border_default']};
    border-radius: 8px;
    background-color: {p['bg_surface']};
    top: -1px;
}}

QTabWidget#TablesWorkTabs QTabBar::tab {{
    padding: 7px 14px;
    font-size: 13px;
}}

/* ===== Tables & Trees (Full-row hover handled by ModernItemDelegate) ===== */
QTableView, QTableWidget, QTreeView, QTreeWidget {{
    background-color: {p['bg_surface']};
    alternate-background-color: {p['bg_group_band']};
    color: {p['text_primary']};
    border: 1px solid {p['border_default']};
    border-radius: 8px;
    gridline-color: transparent;
    selection-background-color: {p['bg_selected']};
    selection-color: {p['text_primary']};
    outline: none;
}}

QTableView::item, QTableWidget::item, QTreeView::item, QTreeWidget::item {{
    padding: 4px 8px;
    border: none;
}}

QHeaderView {{
    background-color: {p['bg_surface_alt']};
    border: none;
}}

QHeaderView::section {{
    background-color: {p['bg_surface_alt']};
    color: {p['text_secondary']};
    border: none;
    border-right: 1px solid {p['border_subtle']};
    border-bottom: 1px solid {p['border_default']};
    padding: 6px 10px;
    font-weight: 600;
    font-size: 12px;
}}

QHeaderView::section:hover {{
    background-color: {p['bg_hover']};
    color: {p['text_primary']};
}}

QHeaderView::section:last {{
    border-right: none;
}}

QTableCornerButton::section {{
    background-color: {p['bg_surface_alt']};
    border: none;
    border-right: 1px solid {p['border_subtle']};
    border-bottom: 1px solid {p['border_default']};
}}

/* ===== Progress Bar ===== */
QProgressBar {{
    background-color: {p['bg_surface_alt']};
    border: 1px solid {p['border_default']};
    border-radius: 5px;
    text-align: center;
    height: 12px;
    color: {p['text_primary']};
    font-size: 11px;
}}

QProgressBar::chunk {{
    background-color: {p['accent']};
    border-radius: 4px;
}}

/* ===== Dual-List Coverage Comparison (Mode B) ===== */
QFrame[sourceCard="true"] {{
    background-color: {p['bg_surface']};
    border: 1px solid {p['border_default']};
    border-radius: 8px;
}}

QFrame[sourceCard="true"]:hover {{
    border-color: {p['border_strong']};
}}

QPlainTextEdit[dragActive="true"] {{
    background-color: {p['accent_soft']};
    border: 1.5px dashed {p['accent']};
}}

QPushButton#SwapABButton {{
    background-color: {p['bg_surface']};
    color: {p['text_secondary']};
    border: 1px solid {p['border_default']};
    border-radius: 15px;
    padding: 4px;
    min-width: 30px;
    max-width: 30px;
    min-height: 30px;
    max-height: 30px;
}}

QPushButton#SwapABButton:hover {{
    background-color: {p['bg_hover']};
    color: {p['text_primary']};
    border-color: {p['border_strong']};
}}

QFrame#CoverageBanner {{
    background-color: {p['bg_surface']};
    border: 1px solid {p['border_default']};
    border-radius: 8px;
}}

QFrame#CoverageBanner[coverageState="full"] {{
    border: 1px solid {p['success_border']};
}}

QLabel#CoverageVerdict {{
    font-size: 13px;
    font-weight: 600;
    color: {p['text_primary']};
}}

QLabel#CoverageVerdict[coverageState="full"] {{
    color: {p['success']};
}}

QLabel#CoveragePercent {{
    font-size: 20px;
    font-weight: 700;
    font-family: {MONO_FONT_CSS};
    color: {p['accent']};
}}

QLabel#CoveragePercent[coverageLevel="full"] {{
    color: {p['success']};
}}

QLabel#CoveragePercent[coverageLevel="high"] {{
    color: {p['accent']};
}}

QLabel#CoveragePercent[coverageLevel="mid"] {{
    color: {p['warning']};
}}

QLabel#CoveragePercent[coverageLevel="low"] {{
    color: {p['danger']};
}}

QLabel#CoveragePercent[coverageLevel="idle"] {{
    color: {p['text_muted']};
}}

QProgressBar#CoverageBar {{
    background-color: {p['bg_surface_alt']};
    border: none;
    border-radius: 4px;
    min-height: 8px;
    max-height: 8px;
    text-align: center;
    color: transparent;
}}

QProgressBar#CoverageBar::chunk {{
    background-color: {p['accent']};
    border-radius: 4px;
}}

QProgressBar#CoverageBar[coverageLevel="full"]::chunk {{
    background-color: {p['success']};
}}

QProgressBar#CoverageBar[coverageLevel="high"]::chunk {{
    background-color: {p['accent']};
}}

QProgressBar#CoverageBar[coverageLevel="mid"]::chunk {{
    background-color: {p['warning']};
}}

QProgressBar#CoverageBar[coverageLevel="low"]::chunk {{
    background-color: {p['danger']};
}}

QFrame[coverageStat="true"] {{
    background-color: {p['bg_surface_alt']};
    border: 1px solid {p['border_subtle']};
    border-radius: 6px;
}}

QFrame[coverageStat="success"] {{
    background-color: {p['success_soft']};
    border: 1px solid {p['success_border']};
    border-radius: 6px;
}}

QFrame[coverageStat="danger"] {{
    background-color: {p['danger_soft']};
    border: 1px solid {p['danger_border']};
    border-radius: 6px;
}}

QFrame[coverageStat="accent"] {{
    background-color: {p['accent_soft']};
    border: 1px solid {p['accent_border']};
    border-radius: 6px;
}}

QLabel[coverageStatTitle="true"] {{
    font-size: 11px;
    color: {p['text_secondary']};
}}

QLabel[coverageStatValue="true"] {{
    font-size: 14px;
    font-weight: 700;
    color: {p['text_primary']};
}}

QLabel[coverageStatValue="success"] {{
    font-size: 14px;
    font-weight: 700;
    color: {p['success']};
}}

QLabel[coverageStatValue="danger"] {{
    font-size: 14px;
    font-weight: 700;
    color: {p['danger']};
}}

QLabel[coverageStatValue="accent"] {{
    font-size: 14px;
    font-weight: 700;
    color: {p['accent']};
}}

/* ===== Filter Pills ===== */
QPushButton[filterPill="true"] {{
    background-color: {p['bg_surface_alt']};
    color: {p['text_secondary']};
    border: 1px solid {p['border_default']};
    border-radius: 13px;
    padding: 3px 11px;
    font-size: 12px;
    font-weight: 600;
    min-height: 18px;
}}

QPushButton[filterPill="true"]:hover {{
    background-color: {p['bg_hover']};
    color: {p['text_primary']};
    border-color: {p['border_strong']};
}}

QPushButton[filterPill="true"]:checked {{
    background-color: {p['accent_soft']};
    color: {p['accent']};
    border-color: {p['accent_border']};
}}

QPushButton[filterPill="true"][pillTone="danger"]:checked {{
    background-color: {p['danger_soft']};
    color: {p['danger']};
    border-color: {p['danger_border']};
}}

QPushButton[filterPill="true"][pillTone="success"]:checked {{
    background-color: {p['success_soft']};
    color: {p['success']};
    border-color: {p['success_border']};
}}

/* ===== Menus ===== */
QMenu {{
    background-color: {p['bg_surface']};
    color: {p['text_primary']};
    border: 1px solid {p['border_default']};
    border-radius: 8px;
    padding: 4px;
}}

QMenu::item {{
    padding: 6px 24px 6px 12px;
    border-radius: 5px;
    color: {p['text_primary']};
}}

QMenu::item:selected {{
    background-color: {p['bg_hover']};
    color: {p['text_primary']};
}}

QMenu::item:disabled {{
    color: {p['text_muted']};
}}

QMenu::separator {{
    height: 1px;
    background-color: {p['border_subtle']};
    margin: 4px 8px;
}}

QMenu::indicator {{
    width: 14px;
    height: 14px;
    margin-left: 6px;
}}

/* ===== Default Slim Scrollbars (fallback when SlimScrollBar is not attached) ===== */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px 2px 2px 2px;
}}

QScrollBar::handle:vertical {{
    background: {p['scrollbar_rest']};
    min-height: 28px;
    border-radius: 3px;
    margin: 0px 2px 0px 2px;
}}

QScrollBar::handle:vertical:hover {{
    background: {p['scrollbar_hover']};
    margin: 0px 1px 0px 1px;
    border-radius: 4px;
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
    height: 0px;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px 2px 2px 2px;
}}

QScrollBar::handle:horizontal {{
    background: {p['scrollbar_rest']};
    min-width: 28px;
    border-radius: 3px;
    margin: 2px 0px 2px 0px;
}}

QScrollBar::handle:horizontal:hover {{
    background: {p['scrollbar_hover']};
    margin: 1px 0px 1px 0px;
    border-radius: 4px;
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
    width: 0px;
}}

/* ===== Splitter, Status Bar & Tooltips ===== */
QSplitter::handle {{
    background-color: transparent;
}}

QSplitter::handle:horizontal {{
    width: 5px;
    margin: 4px 2px;
    background-color: transparent;
    border-left: 1px solid {p['border_subtle']};
}}

QSplitter::handle:vertical {{
    height: 5px;
    margin: 2px 4px;
    background-color: transparent;
    border-top: 1px solid {p['border_subtle']};
}}

QSplitter::handle:hover {{
    background-color: {p['accent_soft']};
    border-color: {p['accent']};
}}

QStatusBar {{
    background-color: {p['bg_window']};
    border-top: 1px solid {p['border_default']};
    color: {p['text_secondary']};
    font-size: 12px;
}}

QToolTip {{
    background-color: {p['bg_surface']};
    color: {p['text_primary']};
    border: 1px solid {p['border_strong']};
    border-radius: 6px;
    padding: 5px 8px;
    font-size: 12px;
}}
"""
