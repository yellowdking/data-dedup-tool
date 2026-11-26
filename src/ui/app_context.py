"""Shared application state and signal hub for 数据查重程序."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal

from src.core.config import ConfigManager
from src.core.database import DatabaseRepository
from src.core.parser import CodeParser
from src.core.rules import RuleRegistry


class AppContext(QObject):
    """Central service container shared across all views."""

    data_changed = Signal()
    db_switched = Signal(str)
    rules_changed = Signal()
    settings_changed = Signal()
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

    def get_companion_extensions(self) -> set[str]:
        return self.config_manager.get_companion_extension_set()

    def close(self) -> None:
        try:
            self.config_manager.save()
        finally:
            self.db.close()
