"""Shared application state and signal hub for 数据查重程序."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal

from src.core.config import ConfigManager, normalize_extension
from src.core.database import DatabaseRepository
from src.core.parser import CodeParser
from src.core.rules import RuleRegistry


class AppContext(QObject):
    """Central service container shared across all views."""

    data_changed = Signal()
    db_switched = Signal(str)
    rules_changed = Signal()
    settings_changed = Signal()
    table_layouts_reset = Signal()
    theme_changed = Signal(str)
    toast_requested = Signal(str, str)  # (message, level)
    status_message = Signal(str)

    def __init__(
        self,
        config_manager: Optional[ConfigManager] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.config_manager = config_manager or ConfigManager()
        cfg = self.config_manager.config
        self._closed = False

        # Ensure current DB is recorded in recent list
        db_path = Path(cfg.current_db_path)
        self.config_manager.add_recent_db(db_path, set_current=True)
        self.config_manager.save()

        self.db = DatabaseRepository(
            db_path,
            auto_backup=cfg.auto_backup,
            backup_keep_count=cfg.backup_keep_count,
        )
        self.rule_registry = RuleRegistry(cfg.custom_rules_path)
        self.parser = CodeParser(self.rule_registry)

    @property
    def config(self):
        return self.config_manager.config

    def notify(self, message: str, level: str = "info") -> None:
        self.toast_requested.emit(message, level)
        self.status_message.emit(message)

    def switch_database(self, db_path: str | Path) -> Path:
        target = Path(db_path).resolve()
        self.db.switch_database(target)
        self.config_manager.add_recent_db(target, set_current=True)
        self.config_manager.save()
        self.db_switched.emit(str(target))
        self.data_changed.emit()
        return target

    def create_database(self, db_path: str | Path, overwrite: bool = False) -> Path:
        target = Path(db_path).resolve()
        self.db.create_database(target, overwrite=overwrite)
        self.config_manager.add_recent_db(target, set_current=True)
        self.config_manager.save()
        self.db_switched.emit(str(target))
        self.data_changed.emit()
        return target

    def save_rules(self) -> None:
        self.rule_registry.save_custom_rules(self.config.custom_rules_path)
        self.rules_changed.emit()

    def set_theme(self, theme: str) -> None:
        normalized = "dark" if theme == "dark" else "light"
        if self.config.theme != normalized:
            self.config.theme = normalized
            self.config_manager.save()
        self.theme_changed.emit(normalized)

    def get_companion_extensions(self, respect_global_toggle: bool = True) -> set[str]:
        """Return configured companion extensions as a normalized lowercase set.

        When ``respect_global_toggle`` is True (default), returns an empty set if
        ``config.ignore_companion_files`` is False. Pass ``respect_global_toggle=False``
        when a view's local companion-filter checkbox is explicitly enabled.
        """
        if respect_global_toggle:
            return self.config_manager.get_companion_extension_set()
        return {
            normalize_extension(ext)
            for ext in self.config.companion_extensions
            if normalize_extension(ext)
        }

    def sync_db_backup_settings(self) -> None:
        """Apply current config backup preferences to the active DatabaseRepository."""
        self.db.auto_backup = bool(self.config.auto_backup)
        self.db.backup_keep_count = max(1, int(self.config.backup_keep_count))

    def reset_all_table_layouts(self) -> None:
        """Reset every persisted table layout to factory defaults and notify all views."""
        self.config_manager.reset_table_layouts()
        self.config_manager.save()
        self.table_layouts_reset.emit()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            try:
                from src.ui.table_header import TableHeaderManager

                TableHeaderManager.flush_all()
            except Exception:
                pass
            self.config_manager.save()
        finally:
            self.db.close()
