"""Main desktop window for 数据查重程序 (v2.0.0 Option B: Linear / Obsidian Pro)."""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
import tomllib
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QByteArray, QRect, QSize, Qt, QUrl
from PySide6.QtGui import (
    QCloseEvent,
    QDesktopServices,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QResizeEvent,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.config import APP_NAME, get_app_base_dir, get_resource_path, is_frozen
from src.ui.app_context import AppContext
from src.ui.delegates import sync_widget_craft_theme
from src.ui.icons import get_brand_pixmap, get_icon
from src.ui.proxy_style import sync_app_style_theme
from src.ui.theme import build_stylesheet, get_palette
from src.ui.view_compare import CompareView
from src.ui.view_duplicates import DuplicatesView
from src.ui.view_import import ImportView
from src.ui.view_library import LibraryView
from src.ui.view_settings import SettingsView
from src.ui.view_tables import TablesView
from src.ui.widgets import ScrollableDetailDialog, ToastNotification, WorkspaceSwitcher
from src.ui.win32_style import sync_win32_theme

_FALLBACK_VERSION = "2.0.0"


def _resolve_app_version() -> str:
    """Resolve version from pyproject.toml (source) or importlib.metadata (installed/frozen)."""
    version = ""
    if not is_frozen():
        pyproject_path = get_app_base_dir() / "pyproject.toml"
        if pyproject_path.is_file():
            try:
                data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
                ver = data.get("project", {}).get("version", "")
                if isinstance(ver, str) and ver.strip():
                    version = ver.strip()
            except Exception:
                pass

    if not version:
        try:
            ver = importlib.metadata.version("data-dedup-tool")
            if ver and ver.strip():
                version = ver.strip()
        except Exception:
            pass

    if not version:
        version = _FALLBACK_VERSION

    return version if version.startswith("v") else f"v{version}"


APP_VERSION = _resolve_app_version()


def _format_badge_count(count: int) -> str:
    if count <= 0:
        return ""
    if count > 9999:
        return "9999+"
    return str(count)


class MainWindow(QMainWindow):
    """Streamlined Linear/Obsidian main window with sidebar workspace switcher & Light/Dark craft."""

    _VIEW_TITLES = [
        "数据总库",
        "批量导入",
        "多表工作台",
        "查重与对比",
        "重复项中心",
        "规则与设置",
    ]

    # Stack index -> (label, icon_name, tooltip)
    _NAV_META: dict[int, tuple[str, str, str]] = {
        0: ("数据总库", "database", "数据总库 (Ctrl+1)"),
        1: ("批量导入", "upload", "批量导入 (Ctrl+2)"),
        2: ("多表工作台", "table", "多表工作台 (Ctrl+3)"),
        3: ("查重与对比", "git-compare", "查重与对比 (Ctrl+4)"),
        4: ("重复项中心", "layers", "重复项中心 (Ctrl+5)"),
        5: ("规则与设置", "settings", "规则与设置 (Ctrl+6)"),
    }

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._is_shut_down = False
        self._dirty_views: set[int] = set()
        self._shortcuts: list[QShortcut] = []
        self._nav_buttons: list[QPushButton] = []
        self._nav_by_index: dict[int, QPushButton] = {}
        self._nav_badges: dict[int, QLabel] = {}

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setMinimumSize(960, 620)

        icon_path = get_resource_path(Path("src") / "resources" / "app.ico")
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._setup_ui()
        self._setup_shortcuts()
        self._restore_geometry()
        self._apply_theme(self.ctx.config.theme)

        self.ctx.data_changed.connect(self._on_data_changed)
        self.ctx.db_switched.connect(self._on_db_switched)
        self.ctx.theme_changed.connect(self._apply_theme)
        self.ctx.toast_requested.connect(self._show_toast)

        self._refresh_workspace_and_badges()

    def _create_nav_button(
        self,
        index: int,
        *,
        badge_style: Optional[str] = None,
    ) -> QPushButton:
        label, _icon_name, tooltip = self._NAV_META[index]
        btn = QPushButton(label)
        btn.setProperty("nav", True)
        btn.setCheckable(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(tooltip)
        btn.setIconSize(QSize(16, 16))
        btn.setFixedHeight(34)

        if badge_style is not None:
            row = QHBoxLayout(btn)
            row.setContentsMargins(0, 0, 8, 0)
            row.setSpacing(0)
            row.addStretch(1)

            badge = QLabel("")
            badge.setProperty("navBadge", badge_style)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            badge.setVisible(False)
            row.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
            self._nav_badges[index] = badge

        self._nav_group.addButton(btn, index)
        self._nav_by_index[index] = btn
        return btn

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- Left Sidebar (212px, bg_sidebar, 1px right border) ----
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(212)
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(10, 14, 10, 12)
        sb_layout.setSpacing(4)

        # Crisp programmatic 26x26 vector brand mark + bold title
        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(4, 0, 4, 0)
        brand_row.setSpacing(9)

        self._brand_icon_label = QLabel()
        self._brand_icon_label.setFixedSize(26, 26)
        self._brand_icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_row.addWidget(self._brand_icon_label)

        title_lbl = QLabel(APP_NAME)
        title_lbl.setObjectName("AppBrandTitle")
        brand_row.addWidget(title_lbl)
        brand_row.addStretch()
        sb_layout.addLayout(brand_row)

        sb_layout.addSpacing(8)

        # Notion/Linear-style Workspace / DB Switcher
        self.workspace_switcher = WorkspaceSwitcher(
            theme=self.ctx.config.theme,
            parent=sidebar,
        )
        self._workspace_switcher = self.workspace_switcher
        self.workspace_switcher.database_selected.connect(self._on_db_selected)
        self.workspace_switcher.new_database_requested.connect(self._create_new_db)
        self.workspace_switcher.open_database_requested.connect(self._open_existing_db)
        self.workspace_switcher.open_folder_requested.connect(self._open_db_folder)
        sb_layout.addWidget(self.workspace_switcher)

        sb_layout.addSpacing(6)

        # Exclusive navigation button group (IDs match QStackedWidget indices 0..5)
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)

        # Group 1: 本地总库
        sec_local = QLabel("本地总库")
        sec_local.setProperty("sidebarSection", True)
        sb_layout.addWidget(sec_local)

        sb_layout.addWidget(self._create_nav_button(0, badge_style="muted"))
        sb_layout.addWidget(self._create_nav_button(4, badge_style="warning"))
        sb_layout.addWidget(self._create_nav_button(1))

        # Group 2: 独立工具
        sec_tools = QLabel("独立工具")
        sec_tools.setProperty("sidebarSection", True)
        sb_layout.addWidget(sec_tools)

        sb_layout.addWidget(self._create_nav_button(3))
        sb_layout.addWidget(self._create_nav_button(2))

        sb_layout.addStretch(1)

        # Bottom pinned hairline -> 规则与设置 -> quiet version pill + icon-only Theme Toggle
        divider = QFrame()
        divider.setObjectName("SidebarDivider")
        sb_layout.addWidget(divider)
        sb_layout.addSpacing(4)

        sb_layout.addWidget(self._create_nav_button(5))

        footer_row = QHBoxLayout()
        footer_row.setContentsMargins(4, 6, 2, 0)
        footer_row.setSpacing(6)

        ver_pill = QLabel(APP_VERSION)
        ver_pill.setProperty("badge", "muted")
        ver_pill.setToolTip(f"{APP_NAME} {APP_VERSION} · 本地离线运行")
        footer_row.addWidget(ver_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        footer_row.addStretch(1)

        self._theme_btn = QPushButton()
        self._theme_btn.setProperty("variant", "ghost")
        self._theme_btn.setProperty("iconOnly", True)
        self._theme_btn.setFixedSize(28, 28)
        self._theme_btn.setIconSize(QSize(15, 15))
        self._theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._theme_btn.clicked.connect(self._toggle_theme)
        footer_row.addWidget(self._theme_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        sb_layout.addLayout(footer_row)

        # Preserve index-ordered list 0..5 for compatibility
        self._nav_buttons = [self._nav_by_index[idx] for idx in range(6)]
        self._dup_nav_btn = self._nav_by_index[4]
        self._nav_by_index[0].setChecked(True)
        self._nav_group.idClicked.connect(self.switch_view)

        outer.addWidget(sidebar)

        # ---- Right Main Workspace (full-height QStackedWidget, zero global TopBar) ----
        self._stack = QStackedWidget()
        self.library_view = LibraryView(self.ctx, self)
        self.import_view = ImportView(self.ctx, self)
        self.tables_view = TablesView(self.ctx, self)
        self.compare_view = CompareView(self.ctx, self)
        self.duplicates_view = DuplicatesView(self.ctx, self)
        self.settings_view = SettingsView(self.ctx, self)

        self._views: list[QWidget] = [
            self.library_view,
            self.import_view,
            self.tables_view,
            self.compare_view,
            self.duplicates_view,
            self.settings_view,
        ]
        for view in self._views:
            self._stack.addWidget(view)

        outer.addWidget(self._stack, 1)

        # Hide permanent bottom status bar; transient feedback uses bottom-right toast overlay
        self.statusBar().hide()
        self._toast = ToastNotification(self, theme=self.ctx.config.theme)

    def _setup_shortcuts(self) -> None:
        """Bind global keyboard shortcuts (Ctrl+1..6, F5, Ctrl+F)."""
        for idx in range(len(self._views)):
            sc = QShortcut(QKeySequence(f"Ctrl+{idx + 1}"), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(lambda i=idx: self.switch_view(i))
            self._shortcuts.append(sc)

        sc_refresh = QShortcut(QKeySequence(Qt.Key.Key_F5), self)
        sc_refresh.setContext(Qt.ShortcutContext.WindowShortcut)
        sc_refresh.activated.connect(self._refresh_active_view)
        self._shortcuts.append(sc_refresh)

        sc_find = QShortcut(QKeySequence.StandardKey.Find, self)
        sc_find.setContext(Qt.ShortcutContext.WindowShortcut)
        sc_find.activated.connect(self._focus_active_search)
        self._shortcuts.append(sc_find)

    def _mark_inactive_views_dirty(self) -> None:
        current_idx = self._stack.currentIndex()
        for idx in range(len(self._views)):
            if idx != current_idx:
                self._dirty_views.add(idx)

    def _on_data_changed(self) -> None:
        self._refresh_stats()
        self._mark_inactive_views_dirty()

    def _on_db_switched(self, _path: str = "") -> None:
        self._refresh_workspace_and_badges()
        self._mark_inactive_views_dirty()

    def _refresh_active_view(self) -> None:
        self._refresh_stats()
        active = self._stack.currentWidget()
        if active is None:
            return
        if callable(getattr(active, "refresh_all", None)):
            active.refresh_all()
        elif callable(getattr(active, "refresh", None)):
            active.refresh()
        elif callable(getattr(active, "refresh_if_dirty", None)):
            active.refresh_if_dirty()

    def _focus_active_search(self) -> None:
        active = self._stack.currentWidget()
        if active is not None and callable(getattr(active, "focus_search", None)):
            active.focus_search()

    def switch_view(self, index: int) -> None:
        if not (0 <= index < self._stack.count()):
            return
        self._stack.setCurrentIndex(index)
        if index in self._nav_by_index:
            self._nav_by_index[index].setChecked(True)

        was_dirty = index in self._dirty_views
        self._dirty_views.discard(index)

        view = self._stack.widget(index)
        if view is None:
            return
        if callable(getattr(view, "on_activated", None)):
            view.on_activated()
        elif was_dirty and callable(getattr(view, "refresh_if_dirty", None)):
            view.refresh_if_dirty()

    def _refresh_workspace_and_badges(self) -> None:
        stats = self.ctx.db.get_stats()
        current_path = str(Path(self.ctx.db.db_path).resolve())
        recents = self.ctx.config_manager.get_recent_dbs(existing_only=False)
        self.workspace_switcher.set_workspace(
            current_path=current_path,
            record_count=stats.total_records,
            recent_paths=recents,
        )
        self._update_nav_badges(stats.total_records, stats.duplicate_groups)

    def _refresh_stats(self) -> None:
        stats = self.ctx.db.get_stats()
        self.workspace_switcher.set_record_count(stats.total_records)
        self._update_nav_badges(stats.total_records, stats.duplicate_groups)

    def _update_nav_badges(self, total_records: int, duplicate_groups: int) -> None:
        lib_badge = self._nav_badges.get(0)
        if lib_badge is not None:
            text = _format_badge_count(total_records)
            lib_badge.setText(text)
            lib_badge.setVisible(bool(text))

        dup_badge = self._nav_badges.get(4)
        if dup_badge is not None:
            text = _format_badge_count(duplicate_groups)
            dup_badge.setText(text)
            dup_badge.setVisible(bool(text))

        if duplicate_groups > 0:
            self._dup_nav_btn.setToolTip(
                f"重复项中心 (Ctrl+5) — 当前共有 {duplicate_groups:,} 组重复编码"
            )
        else:
            self._dup_nav_btn.setToolTip("重复项中心 (Ctrl+5)")

    def _on_db_selected(self, target: str) -> None:
        if not target:
            return
        target_path = Path(target)
        current_path = Path(self.ctx.db.db_path).resolve()
        try:
            if target_path.resolve() == current_path:
                return
        except OSError:
            pass

        if not target_path.is_file():
            dlg = ScrollableDetailDialog(
                title="数据库文件不存在",
                summary=f"找不到最近记录中的数据库文件：{target_path.name}",
                detail_text=(
                    f"目标路径：{target_path}\n\n"
                    "该文件可能已被移动、重命名或删除。为避免自动创建空白数据库，"
                    "本次切换已取消。\n\n是否将其从“最近数据库”列表中移除？"
                ),
                confirm_mode=True,
                confirm_label="从最近列表移除",
                danger=False,
                parent=self,
            )
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self.ctx.config_manager.remove_recent_db(target)
                self.ctx.config_manager.save()
                self.ctx.notify(f"已从最近列表移除失效路径：{target_path.name}", "info")
            self._refresh_workspace_and_badges()
            return

        try:
            self.ctx.switch_database(target)
            self.ctx.notify(f"已切换至数据库：{target_path.stem}", "success")
        except Exception as exc:
            ScrollableDetailDialog("切换数据库失败", str(exc), parent=self).exec()
            self._refresh_workspace_and_badges()

    def create_new_db(self) -> None:
        """Open file dialog to create and switch to a new SQLite database."""
        self._create_new_db()

    def open_existing_db(self) -> None:
        """Open file dialog to open an existing local SQLite database."""
        self._open_existing_db()

    def _create_new_db(self) -> None:
        default_dir = str(Path(self.ctx.db.db_path).parent)
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "新建数据库文件",
            str(Path(default_dir) / "archive_new.db"),
            "SQLite 数据库 (*.db);;所有文件 (*.*)",
        )
        if not save_path:
            return
        try:
            self.ctx.create_database(save_path, overwrite=True)
            self.ctx.notify(f"已新建并切换到库：{Path(save_path).stem}", "success")
        except Exception as exc:
            ScrollableDetailDialog("新建数据库失败", str(exc), parent=self).exec()

    def _open_existing_db(self) -> None:
        default_dir = str(Path(self.ctx.db.db_path).parent)
        path, _ = QFileDialog.getOpenFileName(
            self,
            "打开已有数据库文件",
            default_dir,
            "SQLite 数据库 (*.db *.sqlite *.sqlite3);;所有文件 (*.*)",
        )
        if not path:
            return
        try:
            self.ctx.switch_database(path)
            self.ctx.notify(f"已打开数据库：{Path(path).stem}", "success")
        except Exception as exc:
            ScrollableDetailDialog("打开数据库失败", str(exc), parent=self).exec()

    def _open_db_folder(self) -> None:
        """Safely open the local directory containing the active database file."""
        raw = str(Path(self.ctx.db.db_path).resolve())
        if raw.startswith("\\\\") or raw.startswith("//") or "://" in raw:
            self.ctx.notify("出于安全考虑，不支持直接打开网络共享 (UNC) 路径", "warning")
            return
        db_path = Path(raw)
        try:
            if db_path.is_file():
                if os.name == "nt":
                    subprocess.Popen(["explorer", f"/select,{os.path.normpath(str(db_path))}"])
                else:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(db_path.parent)))
            elif db_path.parent.is_dir():
                if os.name == "nt":
                    os.startfile(os.path.normpath(str(db_path.parent)))
                else:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(db_path.parent)))
            else:
                self.ctx.notify("未找到数据库所在目录", "warning")
        except OSError as exc:
            self.ctx.notify(f"无法打开目录：{exc}", "error")

    def _toggle_theme(self) -> None:
        next_theme = "light" if self.ctx.config.theme == "dark" else "dark"
        self.ctx.set_theme(next_theme)

    def _apply_theme(self, theme: str) -> None:
        norm = "dark" if theme == "dark" else "light"
        p = get_palette(norm)

        sync_app_style_theme(norm)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_stylesheet(norm))
        sync_win32_theme(norm)
        sync_widget_craft_theme(self, norm)

        self._brand_icon_label.setPixmap(get_brand_pixmap(size=26, theme=norm))
        self.workspace_switcher.apply_theme(norm)

        for idx, btn in self._nav_by_index.items():
            _label, icon_name, _tooltip = self._NAV_META[idx]
            btn.setIcon(
                get_icon(
                    icon_name,
                    16,
                    theme=norm,
                    active_color=p["icon_active"],
                )
            )

        if norm == "dark":
            self._theme_btn.setIcon(get_icon("sun", 15, theme=norm))
            self._theme_btn.setToolTip("切换浅色模式")
        else:
            self._theme_btn.setIcon(get_icon("moon", 15, theme=norm))
            self._theme_btn.setToolTip("切换深色模式")

        if hasattr(self, "_toast"):
            self._toast.apply_theme(norm)

    def _show_toast(self, message: str, level: str) -> None:
        self._toast.show_message(message, level=level, theme=self.ctx.config.theme)

    def _clamp_to_available_screens(self) -> None:
        """Ensure restored window coordinates and size remain visible on current monitors."""
        screens = QGuiApplication.screens()
        if not screens:
            return

        current_rect = self.frameGeometry()
        if not current_rect.isValid():
            current_rect = QRect(
                self.x(),
                self.y(),
                max(self.width(), 960),
                max(self.height(), 620),
            )

        best_avail: Optional[QRect] = None
        best_area = 0
        for screen in screens:
            avail = screen.availableGeometry()
            inter = current_rect.intersected(avail)
            area = max(0, inter.width()) * max(0, inter.height())
            if area > best_area:
                best_area = area
                best_avail = avail

        if best_avail is None or best_area < 120 * 80:
            primary = QGuiApplication.primaryScreen()
            best_avail = (
                primary.availableGeometry()
                if primary is not None
                else screens[0].availableGeometry()
            )

        if best_avail.width() <= 0 or best_avail.height() <= 0:
            return

        new_w = min(max(self.width(), self.minimumWidth()), best_avail.width())
        new_h = min(max(self.height(), self.minimumHeight()), best_avail.height())
        self.resize(new_w, new_h)

        max_x = max(best_avail.left(), best_avail.right() - new_w + 1)
        max_y = max(best_avail.top(), best_avail.bottom() - new_h + 1)
        new_x = min(max(self.x(), best_avail.left()), max_x)
        new_y = min(max(self.y(), best_avail.top()), max_y)
        self.move(new_x, new_y)

    def _restore_geometry(self) -> None:
        geom = self.ctx.config.window_geometry
        restored = False
        if geom.qt_geometry_b64:
            try:
                raw = QByteArray.fromBase64(geom.qt_geometry_b64.encode("ascii"))
                restored = bool(self.restoreGeometry(raw))
            except Exception:
                restored = False

        if not restored:
            self.resize(geom.width, geom.height)
            if geom.x is not None and geom.y is not None:
                self.move(geom.x, geom.y)
            if geom.maximized:
                self.showMaximized()

        if not self.isMaximized():
            self._clamp_to_available_screens()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_toast") and self._toast.isVisible():
            self._toast._reposition()

    @staticmethod
    def _flush_table_headers() -> None:
        """Flush any pending debounced TableHeaderManager layout saves."""
        try:
            from src.ui.table_header import TableHeaderManager

            TableHeaderManager.flush_all()
        except Exception:
            pass

    def shutdown(self) -> None:
        """Stop all view background workers and flush pending table layouts cleanly."""
        if self._is_shut_down:
            return
        self._is_shut_down = True
        for view in self._views:
            shutdown_fn = getattr(view, "shutdown", None)
            if callable(shutdown_fn):
                try:
                    shutdown_fn()
                except Exception:
                    pass
        self._flush_table_headers()
        self.ctx.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        try:
            self._flush_table_headers()
            b64 = bytes(self.saveGeometry().toBase64()).decode("ascii")
            self.ctx.config_manager.update_window_geometry(
                width=self.width(),
                height=self.height(),
                x=self.x(),
                y=self.y(),
                maximized=self.isMaximized(),
                qt_geometry_b64=b64,
            )
            self.shutdown()
        finally:
            super().closeEvent(event)
