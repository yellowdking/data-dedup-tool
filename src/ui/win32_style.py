"""Native Windows 11/10 DWM titlebar & window chrome styler.

Safely synchronizes native window titlebars, captions, text colors, rounded
corners, and borders across ``QMainWindow`` and all top-level ``QDialog``
windows using 64-bit safe ``ctypes`` bindings for ``dwmapi.DwmSetWindowAttribute``.

Strictly guarded for offscreen/CI/non-Windows platforms: never invokes Win32
APIs unless ``sys.platform == "win32"``, ``QGuiApplication.platformName() ==
"windows"``, and ``user32.IsWindow(hwnd)`` confirms a real native HWND.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Optional

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

from src.ui.theme import get_palette

# DWM window attributes (Windows 10 1809+ & Windows 11 22000+)
DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1: int = 19
DWMWA_USE_IMMERSIVE_DARK_MODE: int = 20
DWMWA_WINDOW_CORNER_PREFERENCE: int = 33
DWMWA_BORDER_COLOR: int = 34
DWMWA_CAPTION_COLOR: int = 35
DWMWA_TEXT_COLOR: int = 36

DWMWCP_ROUND: int = 2

# SetWindowPos flags for Win10 live theme repaint nudge
SWP_NOSIZE: int = 0x0001
SWP_NOMOVE: int = 0x0002
SWP_NOZORDER: int = 0x0004
SWP_NOACTIVATE: int = 0x0010
SWP_FRAMECHANGED: int = 0x0020

_STYLER_ATTR = "_win32_window_styler"
_DWMAPI_CACHE: Optional[object] = None
_USER32_CACHE: Optional[object] = None


def hex_to_colorref(hex_color: str) -> int:
    """Convert ``#RRGGBB`` (or ``#RGB``) hex string to Win32 BGR ``COLORREF``.

    Win32 ``COLORREF`` layout is ``0x00BBGGRR`` (blue in high byte, red in low
    byte). Raises ``ValueError`` for malformed color strings.
    """
    raw = str(hex_color or "").strip()
    if raw.startswith("#"):
        raw = raw[1:]
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6:
        raise ValueError(f"Expected 6-digit hex color, got {hex_color!r}")
    r = int(raw[0:2], 16)
    g = int(raw[2:4], 16)
    b = int(raw[4:6], 16)
    return (b << 16) | (g << 8) | r


def _get_windows_build() -> int:
    if sys.platform != "win32":
        return 0
    try:
        return int(sys.getwindowsversion().build)
    except Exception:
        return 0


def is_windows_native_platform() -> bool:
    """Return True only when running on Windows with the native ``windows`` QPA."""
    if sys.platform != "win32":
        return False
    if QGuiApplication.instance() is None:
        return False
    try:
        return QGuiApplication.platformName().lower() == "windows"
    except Exception:
        return False


def _load_win32_dlls() -> tuple[Optional[object], Optional[object]]:
    """Lazily initialize 64-bit safe ``dwmapi`` and ``user32`` ctypes handles."""
    global _DWMAPI_CACHE, _USER32_CACHE
    if sys.platform != "win32":
        return None, None
    if _DWMAPI_CACHE is not None and _USER32_CACHE is not None:
        return _DWMAPI_CACHE, _USER32_CACHE
    try:
        from ctypes import wintypes

        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwmapi.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL

        _DWMAPI_CACHE = dwmapi
        _USER32_CACHE = user32
        return dwmapi, user32
    except Exception:
        return None, None


def _is_styleable_window(widget: QWidget) -> bool:
    """Filter out popups, tooltips, menus, and frameless transient windows."""
    if not isinstance(widget, QWidget) or not widget.isWindow():
        return False
    flags = widget.windowFlags()
    # FramelessWindowHint is an independent flag bit outside WindowType_Mask
    if bool(flags & Qt.WindowType.FramelessWindowHint):
        return False
    # Window types share the low Window (0x01) bit, so compare via WindowType_Mask!
    wtype = flags & Qt.WindowType.WindowType_Mask
    if wtype in (
        Qt.WindowType.Popup,
        Qt.WindowType.ToolTip,
        Qt.WindowType.SplashScreen,
        Qt.WindowType.SubWindow,
    ):
        return False
    return True


def _dwm_set_dword(dwmapi: object, hwnd: int, attr: int, value: int) -> int:
    c_val = ctypes.c_uint32(value & 0xFFFFFFFF)
    return int(
        dwmapi.DwmSetWindowAttribute(  # type: ignore[attr-defined]
            hwnd,
            attr,
            ctypes.byref(c_val),
            ctypes.sizeof(c_val),
        )
    )


def apply_win32_window_theme(
    widget: QWidget,
    theme: str = "light",
    *,
    palette: Optional[dict[str, str]] = None,
) -> bool:
    """Apply native Windows 11/10 DWM titlebar styling to a top-level ``QWidget``.

    Returns ``True`` if DWM attributes were applied, or ``False`` when running
    offscreen, on a non-Windows platform, or when ``widget`` lacks a valid HWND.
    """
    if not is_windows_native_platform():
        return False
    if not _is_styleable_window(widget):
        return False
    # Never force premature native handle creation for hidden widgets
    if not widget.testAttribute(Qt.WidgetAttribute.WA_WState_Created) and not widget.isVisible():
        return False

    dwmapi, user32 = _load_win32_dlls()
    if dwmapi is None or user32 is None:
        return False

    try:
        hwnd = int(widget.winId())
    except Exception:
        return False
    if not hwnd or not bool(user32.IsWindow(hwnd)):  # type: ignore[attr-defined]
        return False

    norm_theme = "dark" if theme == "dark" else "light"
    p = palette if palette is not None else get_palette(norm_theme)
    build = _get_windows_build()
    dark_flag = 1 if norm_theme == "dark" else 0

    try:
        hr = _dwm_set_dword(dwmapi, hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, dark_flag)
        if hr != 0:
            _dwm_set_dword(dwmapi, hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1, dark_flag)

        if build >= 22000:
            _dwm_set_dword(dwmapi, hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
            caption_hex = p.get("win32_caption", p["bg_window"])
            text_hex = p.get("text_primary", "#F4F4F5" if norm_theme == "dark" else "#09090B")
            border_hex = p.get("win32_border", p["border_default"])
            _dwm_set_dword(dwmapi, hwnd, DWMWA_CAPTION_COLOR, hex_to_colorref(caption_hex))
            _dwm_set_dword(dwmapi, hwnd, DWMWA_TEXT_COLOR, hex_to_colorref(text_hex))
            _dwm_set_dword(dwmapi, hwnd, DWMWA_BORDER_COLOR, hex_to_colorref(border_hex))
        elif build >= 17763 and widget.isVisible():
            # Nudge Windows 10 non-client frame repaint on live theme switch
            user32.SetWindowPos(  # type: ignore[attr-defined]
                hwnd,
                0,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
            )
        return True
    except Exception:
        return False


class Win32WindowStyler(QObject):
    """App-wide event filter keeping MainWindow and every QDialog DWM-styled."""

    def __init__(self, app: QApplication, theme: str = "light") -> None:
        super().__init__(app)
        self._app = app
        self._theme = "dark" if theme == "dark" else "light"
        app.installEventFilter(self)

    @property
    def theme(self) -> str:
        return self._theme

    def set_theme(self, theme: str) -> int:
        """Update active theme and re-style all current top-level windows."""
        self._theme = "dark" if theme == "dark" else "light"
        return self.apply_to_all_windows()

    def apply_to_all_windows(self) -> int:
        """Apply current theme to every eligible top-level widget."""
        if not is_windows_native_platform():
            return 0
        count = 0
        for widget in self._app.topLevelWidgets():
            if apply_win32_window_theme(widget, self._theme):
                count += 1
        return count

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        try:
            ev_type = event.type()
            if ev_type in (QEvent.Type.Show, QEvent.Type.WinIdChange):
                if isinstance(watched, QWidget) and _is_styleable_window(watched):
                    apply_win32_window_theme(watched, self._theme)
        except RuntimeError:
            return False
        return False


def install_win32_window_styler(
    app: QApplication,
    theme: str = "light",
) -> Win32WindowStyler:
    """Install (or update) the singleton ``Win32WindowStyler`` on ``app``."""
    existing = getattr(app, _STYLER_ATTR, None)
    if isinstance(existing, Win32WindowStyler):
        existing.set_theme(theme)
        return existing
    styler = Win32WindowStyler(app, theme=theme)
    setattr(app, _STYLER_ATTR, styler)
    styler.apply_to_all_windows()
    return styler


def get_win32_window_styler(
    app: Optional[QApplication] = None,
) -> Optional[Win32WindowStyler]:
    """Return the installed ``Win32WindowStyler`` if present."""
    target = app if app is not None else QApplication.instance()
    if not isinstance(target, QApplication):
        return None
    styler = getattr(target, _STYLER_ATTR, None)
    return styler if isinstance(styler, Win32WindowStyler) else None


def sync_win32_theme(theme: str) -> int:
    """Synchronize native titlebars across all top-level windows to ``theme``."""
    styler = get_win32_window_styler()
    if styler is not None:
        return styler.set_theme(theme)
    app = QApplication.instance()
    if not isinstance(app, QApplication) or not is_windows_native_platform():
        return 0
    count = 0
    for widget in app.topLevelWidgets():
        if apply_win32_window_theme(widget, theme):
            count += 1
    return count
