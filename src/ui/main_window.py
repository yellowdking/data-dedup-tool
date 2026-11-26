"""Main desktop window for 数据查重程序."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QCloseEvent, QIcon, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
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

from src.core.config import APP_NAME, get_resource_path
from src.ui.app_context import AppContext
from src.ui.theme import build_stylesheet
from src.ui.view_compare import CompareView
from src.ui.view_duplicates import DuplicatesView
from src.ui.view_import import ImportView
from src.ui.view_library import LibraryView
from src.ui.view_settings import SettingsView
from src.ui.widgets import ScrollableDetailDialog, StatCard, ToastNotification


class MainWindow(QMainWindow):
    """Sleek sidebar-navigated main window with library switcher, live stats & Light/Dark theme."""

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1080, 700)

        icon_path = get_resource_path(Path("src") / "resources" / "app.ico")
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._setup_ui(icon_path)
        self._restore_geometry()
        self._apply_theme(self.ctx.config.theme)

        self.ctx.data_changed.connect(self._refresh_stats)
        self.ctx.db_switched.connect(self._refresh_db_combo)
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
        sidebar.setFixedWidth(212)
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(14, 18, 14, 16)
        sb_layout.setSpacing(6)

        # Brand block
        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        icon_lbl = QLabel()
        if icon_path.is_file():
            pm = QPixmap(str(icon_path)).scaled(
                34, 34, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            icon_lbl.setPixmap(pm)
        else:
            icon_lbl.setText("◈")
            icon_lbl.setStyleSheet("font-size: 24px; color: #2563EB;")
        brand_row.addWidget(icon_lbl)

        brand_text = QVBoxLayout()
        brand_text.setSpacing(1)
        title_lbl = QLabel(APP_NAME)
        title_lbl.setObjectName("AppBrandTitle")
        sub_lbl = QLabel("智能编码查重工作台")
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
            ("📚  数据总库", 0),
            ("📥  批量导入", 1),
            ("🔍  快速查重", 2),
            ("🧩  重复项中心", 3),
            ("⚙️  规则测试 && 设置", 4),
        ]
        self._nav_buttons: list[QPushButton] = []
        for label, idx in nav_defs:
            btn = QPushButton(label)
            btn.setProperty("nav", True)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._nav_group.addButton(btn, idx)
            sb_layout.addWidget(btn)
            self._nav_buttons.append(btn)

        self._nav_buttons[0].setChecked(True)
        self._nav_group.idClicked.connect(self.switch_view)

        sb_layout.addStretch()

        # Theme toggle button
        self._theme_btn = QPushButton("🌙 切换深色模式")
        self._theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._theme_btn.clicked.connect(self._toggle_theme)
        sb_layout.addWidget(self._theme_btn)

        ver_lbl = QLabel("v1.0.0 · 本地离线安全运行")
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
        self._db_combo.setMinimumWidth(240)
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
        self.compare_view = CompareView(self.ctx, self)
        self.duplicates_view = DuplicatesView(self.ctx, self)
        self.settings_view = SettingsView(self.ctx, self)

        self._stack.addWidget(self.library_view)
        self._stack.addWidget(self.import_view)
        self._stack.addWidget(self.compare_view)
        self._stack.addWidget(self.duplicates_view)
        self._stack.addWidget(self.settings_view)
        right_col.addWidget(self._stack, 1)

        outer.addLayout(right_col, 1)

        # Status bar & Toast notification overlay
        self.statusBar().showMessage("就绪")
        self._toast = ToastNotification(self)

    def switch_view(self, index: int) -> None:
        titles = ["数据总库", "批量导入", "快速查重", "重复项中心", "规则测试 & 设置"]
        if 0 <= index < self._stack.count():
            self._stack.setCurrentIndex(index)
            self._page_title.setText(titles[index])
            if 0 <= index < len(self._nav_buttons):
                self._nav_buttons[index].setChecked(True)

    def _refresh_stats(self) -> None:
        stats = self.ctx.db.get_stats()
        self._card_total.set_value(f"{stats.total_records:,}")
        self._card_unique.set_value(f"{stats.unique_codes:,}")
        self._card_dups.set_value(
            f"{stats.duplicate_groups:,}",
            subtitle=f"涉及 {stats.duplicate_records:,} 条记录" if stats.duplicate_groups else "",
        )
        self._card_multipart.set_value(f"{stats.possible_multipart_groups:,}")

        dup_label = (
            f"🧩  重复项中心 ({stats.duplicate_groups})"
            if stats.duplicate_groups > 0
            else "🧩  重复项中心"
        )
        self._nav_buttons[3].setText(dup_label)

    def _refresh_db_combo(self, *_args) -> None:
        current_path = str(Path(self.ctx.db.db_path).resolve())
        recents = self.ctx.config_manager.get_recent_dbs(existing_only=False)
        if current_path not in recents:
            recents.insert(0, current_path)

        self._db_combo.blockSignals(True)
        self._db_combo.clear()
        for p_str in recents:
            p = Path(p_str)
            self._db_combo.addItem(f"{p.name}  ({p.parent.name})", p_str)
        idx = self._db_combo.findData(current_path)
        self._db_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._db_combo.setToolTip(current_path)
        self._db_combo.blockSignals(False)

    def _on_db_combo_selected(self, index: int) -> None:
        target = self._db_combo.itemData(index)
        if not target:
            return
        try:
            self.ctx.switch_database(target)
            self.ctx.notify(f"已切换至数据库：{Path(target).name}", "success")
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
        if theme == "dark":
            self._theme_btn.setText("☀️ 切换浅色模式")
        else:
            self._theme_btn.setText("🌙 切换深色模式")

    def _show_toast(self, message: str, level: str) -> None:
        self._toast.show_message(message, level=level)

    def _restore_geometry(self) -> None:
        geom = self.ctx.config.window_geometry
        if geom.qt_geometry_b64:
            try:
                raw = QByteArray.fromBase64(geom.qt_geometry_b64.encode("ascii"))
                if self.restoreGeometry(raw):
                    return
            except Exception:
                pass
        self.resize(geom.width, geom.height)
        if geom.x is not None and geom.y is not None:
            self.move(geom.x, geom.y)
        if geom.maximized:
            self.showMaximized()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_toast") and self._toast.isVisible():
            self._toast._reposition()

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
            self.ctx.close()
        finally:
            super().closeEvent(event)
