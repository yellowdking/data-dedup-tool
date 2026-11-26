"""Modern minimalist Light & Dark themes (Qt6 QSS) for 数据查重程序.

Uses a clean slate/indigo palette with crisp 1px hairline borders, generous
padding, high-contrast typography, and zero heavy OS-default chrome.
"""

from __future__ import annotations

LIGHT_PALETTE: dict[str, str] = {
    "bg_window": "#F6F8FB",
    "bg_sidebar": "#FFFFFF",
    "bg_surface": "#FFFFFF",
    "bg_surface_alt": "#F1F5F9",
    "bg_hover": "#EFF6FF",
    "bg_selected": "#DBEAFE",
    "bg_input": "#FFFFFF",
    "border": "#E2E8F0",
    "border_strong": "#CBD5E1",
    "text_primary": "#0F172A",
    "text_secondary": "#475569",
    "text_muted": "#64748B",
    "accent": "#2563EB",
    "accent_hover": "#1D4ED8",
    "accent_pressed": "#1E40AF",
    "accent_soft": "#EFF6FF",
    "accent_text": "#FFFFFF",
    "success": "#059669",
    "success_hover": "#047857",
    "success_pressed": "#065F46",
    "success_soft": "#ECFDF5",
    "warning": "#D97706",
    "warning_soft": "#FFFBEB",
    "danger": "#DC2626",
    "danger_hover": "#B91C1C",
    "danger_pressed": "#991B1B",
    "danger_soft": "#FEF2F2",
    "purple": "#7C3AED",
    "purple_soft": "#F5F3FF",
    "btn_disabled_bg": "#E2E8F0",
    "btn_disabled_border": "#CBD5E1",
    "btn_disabled_text": "#64748B",
    "scrollbar": "#CBD5E1",
    "scrollbar_hover": "#94A3B8",
}

DARK_PALETTE: dict[str, str] = {
    "bg_window": "#0F172A",
    "bg_sidebar": "#131C31",
    "bg_surface": "#1E293B",
    "bg_surface_alt": "#162032",
    "bg_hover": "#26354D",
    "bg_selected": "#1E3A5F",
    "bg_input": "#0F172A",
    "border": "#334155",
    "border_strong": "#475569",
    "text_primary": "#F8FAFC",
    "text_secondary": "#CBD5E1",
    "text_muted": "#94A3B8",
    "accent": "#3B82F6",
    "accent_hover": "#60A5FA",
    "accent_pressed": "#2563EB",
    "accent_soft": "#172554",
    "accent_text": "#FFFFFF",
    "success": "#10B981",
    "success_hover": "#059669",
    "success_pressed": "#047857",
    "success_soft": "#064E3B",
    "warning": "#F59E0B",
    "warning_soft": "#451A03",
    "danger": "#EF4444",
    "danger_hover": "#DC2626",
    "danger_pressed": "#B91C1C",
    "danger_soft": "#450A0A",
    "purple": "#A78BFA",
    "purple_soft": "#2E1065",
    "btn_disabled_bg": "#334155",
    "btn_disabled_border": "#475569",
    "btn_disabled_text": "#94A3B8",
    "scrollbar": "#334155",
    "scrollbar_hover": "#475569",
}


def get_palette(theme: str) -> dict[str, str]:
    """Return color tokens for 'light' or 'dark' theme."""
    return DARK_PALETTE if theme == "dark" else LIGHT_PALETTE


def build_stylesheet(theme: str = "light") -> str:
    """Build complete Qt6 QSS stylesheet for the chosen theme."""
    p = get_palette(theme)
    return f"""
/* ===== Global ===== */
QMainWindow, QDialog {{
    background-color: {p['bg_window']};
    color: {p['text_primary']};
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 13px;
}}

QWidget {{
    color: {p['text_primary']};
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 13px;
}}

QLabel {{
    background: transparent;
}}

QLabel[role="title"] {{
    font-size: 18px;
    font-weight: 700;
    color: {p['text_primary']};
}}

QLabel[role="subtitle"] {{
    font-size: 12px;
    color: {p['text_secondary']};
}}

QLabel[role="section"] {{
    font-size: 14px;
    font-weight: 600;
    color: {p['text_primary']};
}}

QLabel[role="muted"] {{
    font-size: 12px;
    color: {p['text_muted']};
}}

/* ===== Badge Pills ===== */
QLabel[badge="true"], QLabel[badge="accent"], QLabel[badge="primary"], QLabel[badge="info"] {{
    background-color: {p['accent_soft']};
    color: {p['accent']};
    border: 1px solid {p['border']};
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}}

QLabel[badge="success"] {{
    background-color: {p['success_soft']};
    color: {p['success']};
    border: 1px solid {p['border']};
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}}

QLabel[badge="warning"] {{
    background-color: {p['warning_soft']};
    color: {p['warning']};
    border: 1px solid {p['border']};
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}}

QLabel[badge="danger"] {{
    background-color: {p['danger_soft']};
    color: {p['danger']};
    border: 1px solid {p['border']};
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}}

QLabel[badge="purple"] {{
    background-color: {p['purple_soft']};
    color: {p['purple']};
    border: 1px solid {p['border']};
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}}

QLabel[badge="muted"], QLabel[badge="neutral"] {{
    background-color: {p['bg_surface_alt']};
    color: {p['text_secondary']};
    border: 1px solid {p['border']};
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}}

/* ===== Sidebar ===== */
QFrame#Sidebar {{
    background-color: {p['bg_sidebar']};
    border-right: 1px solid {p['border']};
}}

QLabel#AppBrandTitle {{
    font-size: 16px;
    font-weight: 700;
    color: {p['text_primary']};
}}

QLabel#AppBrandSub {{
    font-size: 11px;
    color: {p['text_muted']};
}}

QPushButton[nav="true"] {{
    text-align: left;
    padding: 10px 14px;
    border-radius: 8px;
    border: none;
    background-color: transparent;
    color: {p['text_secondary']};
    font-size: 13px;
    font-weight: 500;
}}

QPushButton[nav="true"]:hover {{
    background-color: {p['bg_hover']};
    color: {p['text_primary']};
}}

QPushButton[nav="true"]:checked {{
    background-color: {p['accent_soft']};
    color: {p['accent']};
    font-weight: 700;
}}

/* ===== Top Header & Cards ===== */
QFrame#TopBar {{
    background-color: {p['bg_surface']};
    border-bottom: 1px solid {p['border']};
}}

QFrame[card="true"] {{
    background-color: {p['bg_surface']};
    border: 1px solid {p['border']};
    border-radius: 10px;
}}

QFrame[statCard="true"] {{
    background-color: {p['bg_surface']};
    border: 1px solid {p['border']};
    border-radius: 10px;
}}

QFrame[statCard="true"]:hover {{
    border-color: {p['border_strong']};
}}

QLabel[statTitle="true"] {{
    font-size: 12px;
    color: {p['text_secondary']};
}}

QLabel[statValue="true"] {{
    font-size: 22px;
    font-weight: 700;
    color: {p['text_primary']};
}}

/* ===== Drop Zone ===== */
QFrame#DropZone {{
    background-color: {p['bg_surface']};
    border: 2px dashed {p['border_strong']};
    border-radius: 12px;
}}

QFrame#DropZone[dragActive="true"] {{
    background-color: {p['accent_soft']};
    border: 2px dashed {p['accent']};
}}

/* ===== Buttons ===== */
QPushButton {{
    background-color: {p['bg_surface']};
    color: {p['text_primary']};
    border: 1px solid {p['border']};
    border-radius: 7px;
    padding: 6px 14px;
    font-weight: 500;
    min-height: 20px;
}}

QPushButton:hover {{
    background-color: {p['bg_hover']};
    border-color: {p['border_strong']};
}}

QPushButton:pressed {{
    background-color: {p['bg_selected']};
}}

QPushButton:disabled {{
    color: {p['btn_disabled_text']};
    background-color: {p['btn_disabled_bg']};
    border-color: {p['btn_disabled_border']};
}}

QPushButton[variant="primary"] {{
    background-color: {p['accent']};
    color: {p['accent_text']};
    border: 1px solid {p['accent']};
    font-weight: 600;
}}

QPushButton[variant="primary"]:hover {{
    background-color: {p['accent_hover']};
    border-color: {p['accent_hover']};
}}

QPushButton[variant="primary"]:pressed {{
    background-color: {p['accent_pressed']};
    border-color: {p['accent_pressed']};
}}

QPushButton[variant="primary"]:disabled {{
    background-color: {p['btn_disabled_bg']};
    border-color: {p['btn_disabled_border']};
    color: {p['btn_disabled_text']};
}}

QPushButton[variant="success"] {{
    background-color: {p['success']};
    color: #FFFFFF;
    border: 1px solid {p['success']};
    font-weight: 600;
}}

QPushButton[variant="success"]:hover {{
    background-color: {p['success_hover']};
    border-color: {p['success_hover']};
}}

QPushButton[variant="success"]:pressed {{
    background-color: {p['success_pressed']};
    border-color: {p['success_pressed']};
}}

QPushButton[variant="success"]:disabled {{
    background-color: {p['btn_disabled_bg']};
    border-color: {p['btn_disabled_border']};
    color: {p['btn_disabled_text']};
}}

QPushButton[variant="danger"] {{
    background-color: {p['danger']};
    color: #FFFFFF;
    border: 1px solid {p['danger']};
    font-weight: 600;
}}

QPushButton[variant="danger"]:hover {{
    background-color: {p['danger_hover']};
    border-color: {p['danger_hover']};
}}

QPushButton[variant="danger"]:pressed {{
    background-color: {p['danger_pressed']};
    border-color: {p['danger_pressed']};
}}

QPushButton[variant="danger"]:disabled {{
    background-color: {p['btn_disabled_bg']};
    border-color: {p['btn_disabled_border']};
    color: {p['btn_disabled_text']};
}}

QPushButton[variant="ghost"] {{
    background-color: transparent;
    border: 1px solid transparent;
    color: {p['text_secondary']};
}}

QPushButton[variant="ghost"]:hover {{
    background-color: {p['bg_hover']};
    color: {p['text_primary']};
}}

QPushButton[variant="ghost"]:disabled {{
    background-color: transparent;
    border-color: transparent;
    color: {p['btn_disabled_text']};
}}

QPushButton[variant="outline"] {{
    background-color: transparent;
    border: 1px solid {p['accent']};
    color: {p['accent']};
    font-weight: 600;
}}

QPushButton[variant="outline"]:hover {{
    background-color: {p['accent_soft']};
}}

QPushButton[variant="outline"]:disabled {{
    background-color: transparent;
    border-color: {p['btn_disabled_border']};
    color: {p['btn_disabled_text']};
}}

QPushButton[size="sm"], QPushButton[compact="true"] {{
    padding: 4px 10px;
    font-size: 12px;
    min-height: 18px;
}}

/* ===== Inputs ===== */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {{
    background-color: {p['bg_input']};
    color: {p['text_primary']};
    border: 1px solid {p['border']};
    border-radius: 7px;
    padding: 6px 10px;
    selection-background-color: {p['accent']};
    selection-color: #FFFFFF;
}}

QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border: 1px solid {p['accent']};
}}

QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    background-color: {p['bg_surface_alt']};
    color: {p['btn_disabled_text']};
    border-color: {p['border']};
}}

QSpinBox {{
    padding-right: 22px;
}}

QSpinBox::up-button, QSpinBox::down-button {{
    subcontrol-origin: border;
    width: 20px;
    background-color: {p['bg_surface_alt']};
    border-left: 1px solid {p['border']};
}}

QSpinBox::up-button {{
    subcontrol-position: top right;
    border-top-right-radius: 6px;
    border-bottom: 1px solid {p['border']};
}}

QSpinBox::down-button {{
    subcontrol-position: bottom right;
    border-bottom-right-radius: 6px;
}}

QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
    background-color: {p['bg_hover']};
}}

QSpinBox::up-button:pressed, QSpinBox::down-button:pressed {{
    background-color: {p['bg_selected']};
}}

QSpinBox::up-arrow {{
    width: 0px;
    height: 0px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid {p['text_secondary']};
}}

QSpinBox::down-arrow {{
    width: 0px;
    height: 0px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {p['text_secondary']};
}}

QComboBox::drop-down {{
    border: none;
    width: 24px;
}}

QComboBox::down-arrow {{
    width: 0px;
    height: 0px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {p['text_secondary']};
}}

QComboBox QAbstractItemView {{
    background-color: {p['bg_surface']};
    color: {p['text_primary']};
    border: 1px solid {p['border']};
    selection-background-color: {p['bg_selected']};
    selection-color: {p['text_primary']};
    outline: none;
}}

/* ===== Checkboxes & Radio ===== */
QCheckBox, QRadioButton {{
    spacing: 7px;
    color: {p['text_primary']};
}}

QCheckBox:disabled, QRadioButton:disabled {{
    color: {p['btn_disabled_text']};
}}

QCheckBox::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {p['border_strong']};
    border-radius: 4px;
    background-color: {p['bg_input']};
}}

QCheckBox::indicator:hover {{
    border-color: {p['accent']};
}}

QCheckBox::indicator:checked {{
    background-color: #FFFFFF;
    border: 4px solid {p['accent']};
}}

QCheckBox::indicator:checked:hover {{
    border-color: {p['accent_hover']};
}}

QCheckBox::indicator:indeterminate {{
    background-color: {p['accent_soft']};
    border: 4px solid {p['accent']};
}}

QCheckBox::indicator:disabled {{
    background-color: {p['btn_disabled_bg']};
    border-color: {p['btn_disabled_border']};
}}

QRadioButton::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {p['border_strong']};
    border-radius: 8px;
    background-color: {p['bg_input']};
}}

QRadioButton::indicator:hover {{
    border-color: {p['accent']};
}}

QRadioButton::indicator:checked {{
    background-color: #FFFFFF;
    border: 5px solid {p['accent']};
}}

QRadioButton::indicator:disabled {{
    background-color: {p['btn_disabled_bg']};
    border-color: {p['btn_disabled_border']};
}}

/* ===== Tabs ===== */
QTabWidget::pane {{
    border: 1px solid {p['border']};
    border-radius: 8px;
    background-color: {p['bg_surface']};
    top: -1px;
}}

QTabBar::tab {{
    background-color: transparent;
    color: {p['text_secondary']};
    padding: 8px 16px;
    border: 1px solid transparent;
    border-bottom: 2px solid transparent;
    font-weight: 500;
    margin-right: 4px;
}}

QTabBar::tab:hover {{
    color: {p['text_primary']};
    background-color: {p['bg_hover']};
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}}

QTabBar::tab:selected {{
    color: {p['accent']};
    border-bottom: 2px solid {p['accent']};
    font-weight: 700;
}}

QTabWidget[subtabs="true"]::pane {{
    border: 1px solid {p['border']};
    border-radius: 8px;
    background-color: {p['bg_surface']};
    top: -1px;
}}

QTabWidget[subtabs="true"] QTabBar::tab {{
    padding: 6px 12px;
    font-size: 12px;
    margin-right: 3px;
}}

QTabBar QToolButton {{
    background-color: {p['bg_surface']};
    color: {p['text_secondary']};
    border: 1px solid {p['border']};
    border-radius: 5px;
    margin: 2px;
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

QGroupBox {{
    background-color: {p['bg_surface']};
    border: 1px solid {p['border']};
    border-radius: 8px;
    margin-top: 10px;
    padding: 12px 10px 10px 10px;
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

/* ===== Lists (Checkable Table List, etc.) ===== */
QListWidget, QListView {{
    background-color: {p['bg_surface']};
    color: {p['text_primary']};
    border: 1px solid {p['border']};
    border-radius: 8px;
    padding: 4px;
    outline: none;
}}

QListWidget::item, QListView::item {{
    padding: 7px 10px;
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

/* ===== Multi-Table Studio Specific Polish ===== */
QFrame#TablesSourcePanel {{
    background-color: {p['bg_surface']};
    border: 1px solid {p['border']};
    border-radius: 10px;
}}

QListWidget#TablesSourceList {{
    background-color: {p['bg_surface_alt']};
    border: 1px solid {p['border']};
    border-radius: 8px;
    padding: 4px;
}}

QListWidget#TablesSourceList::item {{
    background-color: {p['bg_surface']};
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 7px 8px;
    margin: 2px 0px;
}}

QListWidget#TablesSourceList::item:hover {{
    background-color: {p['bg_hover']};
    border-color: {p['border']};
}}

QListWidget#TablesSourceList::item:selected {{
    background-color: {p['bg_selected']};
    border-color: {p['accent']};
    color: {p['text_primary']};
    font-weight: 600;
}}

QTabWidget#TablesWorkTabs::pane {{
    border: 1px solid {p['border']};
    border-radius: 10px;
    background-color: {p['bg_surface']};
    top: -1px;
}}

QTabWidget#TablesWorkTabs QTabBar::tab {{
    padding: 8px 16px;
    font-size: 13px;
}}

/* ===== Tables & Trees ===== */
QTableView, QTableWidget, QTreeWidget {{
    background-color: {p['bg_surface']};
    alternate-background-color: {p['bg_surface_alt']};
    color: {p['text_primary']};
    border: 1px solid {p['border']};
    border-radius: 8px;
    gridline-color: {p['border']};
    selection-background-color: {p['bg_selected']};
    selection-color: {p['text_primary']};
    outline: none;
}}

QTableView::item, QTableWidget::item, QTreeWidget::item {{
    padding: 5px 8px;
    border: none;
}}

QTableView::item:hover, QTableWidget::item:hover, QTreeWidget::item:hover {{
    background-color: {p['bg_hover']};
}}

QHeaderView {{
    background-color: {p['bg_surface_alt']};
    border: none;
}}

QHeaderView::section {{
    background-color: {p['bg_surface_alt']};
    color: {p['text_secondary']};
    border: none;
    border-right: 1px solid {p['border']};
    border-bottom: 1px solid {p['border']};
    padding: 7px 10px;
    font-weight: 600;
    font-size: 12px;
}}

QHeaderView::section:hover {{
    background-color: {p['bg_hover']};
    color: {p['text_primary']};
}}

QTableCornerButton::section {{
    background-color: {p['bg_surface_alt']};
    border: none;
    border-right: 1px solid {p['border']};
    border-bottom: 1px solid {p['border']};
}}

/* ===== Progress Bar ===== */
QProgressBar {{
    background-color: {p['bg_surface_alt']};
    border: 1px solid {p['border']};
    border-radius: 6px;
    text-align: center;
    height: 16px;
    color: {p['text_primary']};
    font-size: 11px;
}}

QProgressBar::chunk {{
    background-color: {p['accent']};
    border-radius: 5px;
}}

/* ===== Menus ===== */
QMenu {{
    background-color: {p['bg_surface']};
    color: {p['text_primary']};
    border: 1px solid {p['border']};
    border-radius: 8px;
    padding: 4px;
}}

QMenu::item {{
    padding: 7px 18px;
    border-radius: 5px;
}}

QMenu::item:selected {{
    background-color: {p['bg_hover']};
    color: {p['accent']};
}}

QMenu::separator {{
    height: 1px;
    background-color: {p['border']};
    margin: 4px 8px;
}}

/* ===== Scrollbars ===== */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}

QScrollBar::handle:vertical {{
    background: {p['scrollbar']};
    min-height: 30px;
    border-radius: 5px;
}}

QScrollBar::handle:vertical:hover {{
    background: {p['scrollbar_hover']};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
    height: 0px;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}

QScrollBar::handle:horizontal {{
    background: {p['scrollbar']};
    min-width: 30px;
    border-radius: 5px;
}}

QScrollBar::handle:horizontal:hover {{
    background: {p['scrollbar_hover']};
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
    width: 0px;
}}

/* ===== Splitter & Status Bar ===== */
QSplitter::handle {{
    background-color: {p['border']};
    border-radius: 2px;
}}

QSplitter::handle:horizontal {{
    width: 5px;
    margin: 4px 2px;
    background-color: transparent;
    border-left: 1px solid {p['border']};
}}

QSplitter::handle:vertical {{
    height: 5px;
    margin: 2px 4px;
    background-color: transparent;
    border-top: 1px solid {p['border']};
}}

QSplitter::handle:hover {{
    background-color: {p['accent_soft']};
    border-color: {p['accent']};
}}

QStatusBar {{
    background-color: {p['bg_surface']};
    border-top: 1px solid {p['border']};
    color: {p['text_secondary']};
    font-size: 12px;
}}

QToolTip {{
    background-color: {p['bg_surface']};
    color: {p['text_primary']};
    border: 1px solid {p['border_strong']};
    border-radius: 6px;
    padding: 5px 8px;
}}
"""
