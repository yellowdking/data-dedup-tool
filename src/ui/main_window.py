"""Main desktop window for 数据查重程序."""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QByteArray, QRect, Qt
from PySide6.QtGui import (
    QCloseEvent,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPixmap,
    QResizeEvent,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
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
from src.ui.theme import build_stylesheet, get_palette
from src.ui.view_compare import CompareView
from src.ui.view_duplicates import DuplicatesView
from src.ui.view_import import ImportView
from src.ui.view_library import LibraryView
from src.ui.view_settings import SettingsView
from src.ui.view_tables import TablesView
from src.ui.widgets import ScrollableDetailDialog, StatCard, ToastNotification

_FALLBACK_VERSION = "1.2.0"


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


class MainWindow(QMainWindow):
    """Sleek sidebar-navigated main window with library switcher, live stats & Light/Dark theme."""

    _VIEW_TITLES = [
        "数据总库",
        "批量导入",
        "多表工作台",
        "快速查重",
        "重复项中心",
        "规则与设置",
    ]

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._is_shut_down = False
        self._dirty_views: set[int] = set()
        self._shortcuts: list[QShortcut] = []
        self._brand_fallback_icon: Optional[QLabel] = None

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setMinimumSize(960, 620)

        icon_path = get_resource_path(Path("src") / "resources" / "app.ico")
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._setup_ui(icon_path)
        self._setup_shortcuts()
        self._restore_geometry()
        self._apply_theme(self.ctx.config.theme)

        self.ctx.data_changed.connect(self._on_data_changed)
        self.ctx.db_switched.connect(self._on_db_switched)
        self.ctx.theme_changed.connect(self._apply_theme)
        self.ctx.toast_requested.connect(self._show_toast)
        self.ctx.status_message.connect(lambda msg: self.statusBar().showMessage(msg, 5000))

        self._refresh_db_combo()
        self._refresh_stats()

    def _setup_ui(self, icon_path: Path) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- Left Sidebar ----
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(216)
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(12, 16, 12, 14)
        sb_layout.setSpacing(6)

        # Brand block
        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        icon_lbl = QLabel()
        if icon_path.is_file():
            pm = QPixmap(str(icon_path)).scaled(
                34,
                34,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            icon_lbl.setPixmap(pm)
        else:
            icon_lbl.setText("◈")
            icon_lbl.setStyleSheet("font-size: 24px; color: #2563EB;")
            self._brand_fallback_icon = icon_lbl
        brand_row.addWidget(icon_lbl)

        brand_text = QVBoxLayout()
        brand_text.setSpacing(1)
        title_lbl = QLabel(APP_NAME)
        title_lbl.setObjectName("AppBrandTitle")
        sub_lbl = QLabel("多表与多格式智能查重")
        sub_lbl.setObjectName("AppBrandSub")
        brand_text.addWidget(title_lbl)
        brand_text.addWidget(sub_lbl)
        brand_row.addLayout(brand_text)
        brand_row.addStretch()
        sb_layout.addLayout(brand_row)

        sb_layout.addSpacing(14)

        # Navigation buttons
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        nav_defs = [
            ("📚  数据总库", 0, "Ctrl+1"),
            ("📥  批量导入", 1, "Ctrl+2"),
            ("📊  多表工作台", 2, "Ctrl+3"),
            ("🔍  快速查重", 3, "Ctrl+4"),
            ("🧩  重复项中心", 4, "Ctrl+5"),
            ("⚙️  规则与设置", 5, "Ctrl+6"),
        ]
        self._nav_buttons: list[QPushButton] = []
        for label, idx, shortcut_hint in nav_defs:
            btn = QPushButton(label)
            btn.setProperty("nav", True)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(f"{self._VIEW_TITLES[idx]} ({shortcut_hint})")
            self._nav_group.addButton(btn, idx)
            sb_layout.addWidget(btn)
            self._nav_buttons.append(btn)

        self._dup_nav_btn = self._nav_buttons[4]
        self._nav_buttons[0].setChecked(True)
        self._nav_group.idClicked.connect(self.switch_view)

        sb_layout.addStretch()

        # Theme toggle button
        self._theme_btn = QPushButton("🌙 切换深色模式")
        self._theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._theme_btn.clicked.connect(self._toggle_theme)
        sb_layout.addWidget(self._theme_btn)

        ver_lbl = QLabel(f"{APP_VERSION} · 本地离线安全运行")
        ver_lbl.setProperty("role", "muted")
        ver_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sb_layout.addWidget(ver_lbl)

        outer.addWidget(sidebar)

        # ---- Right Main Area ----
        right_col = QVBoxLayout()
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(0)

        # Top bar (library switcher + stats cards)
        top_bar = QFrame()
        top_bar.setObjectName("TopBar")
        top_layout = QVBoxLayout(top_bar)
        top_layout.setContentsMargins(20, 12, 20, 14)
        top_layout.setSpacing(12)

        # Row 1: View title & Database Switcher
        header_row = QHBoxLayout()
        header_row.setSpacing(10)

        self._page_title = QLabel("数据总库")
        self._page_title.setProperty("role", "title")
        header_row.addWidget(self._page_title)
        header_row.addStretch()

        db_lbl = QLabel("当前数据库：")
        db_lbl.setProperty("role", "subtitle")
        header_row.addWidget(db_lbl)

        self._db_combo = QComboBox()
        self._db_combo.setMinimumWidth(220)
        self._db_combo.setMaximumWidth(340)
        self._db_combo.activated.connect(self._on_db_combo_selected)
        header_row.addWidget(self._db_combo)

        btn_new_db = QPushButton("新建库")
        btn_new_db.clicked.connect(self._create_new_db)
        header_row.addWidget(btn_new_db)

        btn_open_db = QPushButton("打开库")
        btn_open_db.clicked.connect(self._open_existing_db)
        header_row.addWidget(btn_open_db)

        top_layout.addLayout(header_row)

        # Row 2: Live KPI Stat Cards
        stats_row = QHBoxLayout()
        stats_row.setSpacing(12)

        self._card_total = StatCard("总记录数", "0", accent_color="#2563EB")
        self._card_unique = StatCard("唯一标准编码", "0", accent_color="#059669")
        self._card_dups = StatCard("重复编码组", "0", accent_color="#D97706")
        self._card_multipart = StatCard("疑似分集组", "0", accent_color="#7C3AED")

        stats_row.addWidget(self._card_total)
        stats_row.addWidget(self._card_unique)
        stats_row.addWidget(self._card_dups)
        stats_row.addWidget(self._card_multipart)
        top_layout.addLayout(stats_row)

        right_col.addWidget(top_bar)

        # Stacked Views
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
        right_col.addWidget(self._stack, 1)

        outer.addLayout(right_col, 1)

        # Status bar & Toast notification overlay
        self.statusBar().showMessage("就绪")
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
        self._refresh_db_combo()
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
        self._page_title.setText(self._VIEW_TITLES[index])
        if 0 <= index < len(self._nav_buttons):
            self._nav_buttons[index].setChecked(True)

        was_dirty = index in self._dirty_views
        self._dirty_views.discard(index)

        view = self._stack.widget(index)
        if view is None:
            return
        if callable(getattr(view, "on_activated", None)):
            view.on_activated()
        elif was_dirty and callable(getattr(view, "refresh_if_dirty", None)):
            view.refresh_if_dirty()

    def _refresh_stats(self) -> None:
        stats = self.ctx.db.get_stats()
        self._card_total.set_value(f"{stats.total_records:,}")
        self._card_unique.set_value(f"{stats.unique_codes:,}")
        self._card_dups.set_value(
            f"{stats.duplicate_groups:,}",
            subtitle=f"涉及 {stats.duplicate_records:,} 条记录" if stats.duplicate_groups else "",
        )
        self._card_multipart.set_value(f"{stats.possible_multipart_groups:,}")

        if stats.duplicate_groups > 9999:
            dup_label = "🧩  重复项中心 (9999+)"
        elif stats.duplicate_groups > 0:
            dup_label = f"🧩  重复项中心 ({stats.duplicate_groups})"
        else:
            dup_label = "🧩  重复项中心"
        self._dup_nav_btn.setText(dup_label)
        if stats.duplicate_groups > 0:
            self._dup_nav_btn.setToolTip(
                f"重复项中心 (Ctrl+5) — 当前共有 {stats.duplicate_groups:,} 组重复编码"
            )
        else:
            self._dup_nav_btn.setToolTip("重复项中心 (Ctrl+5)")

    def _refresh_db_combo(self, *_args) -> None:
        current_path = str(Path(self.ctx.db.db_path).resolve())
        recents = self.ctx.config_manager.get_recent_dbs(existing_only=False)
        if current_path not in recents:
            recents.insert(0, current_path)

        self._db_combo.blockSignals(True)
        self._db_combo.clear()
        for p_str in recents:
            p = Path(p_str)
            missing_tag = "" if (p.is_file() or p_str == current_path) else " [缺失]"
            self._db_combo.addItem(f"{p.name}  ({p.parent.name}){missing_tag}", p_str)
        idx = self._db_combo.findData(current_path)
        self._db_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._db_combo.setToolTip(current_path)
        self._db_combo.blockSignals(False)

    def _on_db_combo_selected(self, index: int) -> None:
        target = self._db_combo.itemData(index)
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
                    "该文件可能已被移动、重命名或删除。为避免自动创建空白幽灵库，"
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
            self._refresh_db_combo()
            return

        try:
            self.ctx.switch_database(target)
            self.ctx.notify(f"已切换至数据库：{target_path.name}", "success")
        except Exception as exc:
            ScrollableDetailDialog("切换数据库失败", str(exc), parent=self).exec()
            self._refresh_db_combo()

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
            self.ctx.notify(f"已新建并切换到库：{Path(save_path).name}", "success")
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
            self.ctx.notify(f"已打开数据库：{Path(path).name}", "success")
        except Exception as exc:
            ScrollableDetailDialog("打开数据库失败", str(exc), parent=self).exec()

    def _toggle_theme(self) -> None:
        next_theme = "light" if self.ctx.config.theme == "dark" else "dark"
        self.ctx.set_theme(next_theme)

    def _apply_theme(self, theme: str) -> None:
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_stylesheet(theme))
        p = get_palette(theme)
        if hasattr(self, "_toast"):
            self._toast.apply_theme(theme)
        if self._brand_fallback_icon is not None:
            self._brand_fallback_icon.setStyleSheet(f"font-size: 24px; color: {p['accent']};")
        self._card_total.set_accent_color(p["accent"])
        self._card_unique.set_accent_color(p["success"])
        self._card_dups.set_accent_color(p["warning"])
        self._card_multipart.set_accent_color(p["purple"])

        if theme == "dark":
            self._theme_btn.setText("☀️ 切换浅色模式")
        else:
            self._theme_btn.setText("🌙 切换深色模式")

    def _show_toast(self, message: str, level: str) -> None:
        self._toast.show_message(message, level=level, theme=self.ctx.config.theme)

    def _clamp_to_available_screens(self) -> None:
        """Ensure restored window coordinates and size remain visible on current monitors."""
        screens = QGuiApplication.screens()
        if not screens:
            return

        current_rect = self.frameGeometry()
        if not current_rect.isValid():
            current_rect = QRect(self.x(), self.y(), max(self.width(), 960), max(self.height(), 620))

        # Pick the screen with the largest intersection area, else primary screen
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
            best_avail = primary.availableGeometry() if primary is not None else screens[0].availableGeometry()

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

    def shutdown(self) -> None:
        """Stop all view background workers cleanly before closing DB/config."""
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
        self.ctx.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        try:
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
