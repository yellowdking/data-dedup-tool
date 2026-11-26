"""Application configuration management for 数据查重程序.

Handles persistent JSON settings, recent database history, theme preferences,
companion-file extension filters, custom rule paths, window geometry, and
PyInstaller frozen-bundle path resolution.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

APP_NAME = "数据查重程序"
APP_DIR_NAME = "DataDedupTool"

DEFAULT_COMPANION_EXTENSIONS: List[str] = [
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".gif",
    ".webp",
    ".ico",
    ".tiff",
    ".nfo",
    ".srt",
    ".ass",
    ".ssa",
    ".sub",
    ".idx",
    ".vtt",
    ".sup",
    ".lrc",
    ".txt",
    ".url",
    ".lnk",
    ".ini",
    ".db",
    ".torrent",
    ".json",
    ".xml",
    ".log",
    ".md5",
    ".sha1",
    ".sha256",
    ".sfv",
    ".par2",
    ".mht",
    ".html",
    ".htm",
]


def is_frozen() -> bool:
    """Return True when running inside a PyInstaller frozen bundle."""
    return bool(getattr(sys, "frozen", False))


def get_app_base_dir() -> Path:
    """Return the application base directory.

    When packaged with PyInstaller, returns the directory containing the
    executable. In source/development mode, returns the repository root.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    # src/core/config.py -> repo root is two levels above src/
    return Path(__file__).resolve().parent.parent.parent


def get_resource_path(relative_path: str | Path) -> Path:
    """Resolve a bundled resource path in both frozen and source modes."""
    rel = Path(relative_path)
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidate = Path(meipass) / rel
            if candidate.exists():
                return candidate
        return get_app_base_dir() / rel
    return get_app_base_dir() / rel


def get_user_data_dir(app_dir_name: str = APP_DIR_NAME) -> Path:
    """Return a writable per-user data directory for configs, rules, and databases.

    Supports portable mode if a ``portable.flag`` file exists next to the
    frozen executable; otherwise uses ``%APPDATA%`` on Windows or
    ``~/.local/share`` on POSIX platforms.
    """
    base_dir = get_app_base_dir()
    if is_frozen() and (base_dir / "portable.flag").exists():
        data_dir = base_dir / "user_data"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if appdata:
            data_dir = Path(appdata) / app_dir_name
        else:
            data_dir = Path.home() / "AppData" / "Roaming" / app_dir_name
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            data_dir = Path(xdg) / app_dir_name
        else:
            data_dir = Path.home() / ".local" / "share" / app_dir_name

    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_default_config_path(data_dir: Optional[Path] = None) -> Path:
    """Return the default ``config.json`` path."""
    root = Path(data_dir) if data_dir is not None else get_user_data_dir()
    return root / "config.json"


def get_default_db_path(data_dir: Optional[Path] = None) -> Path:
    """Return the default SQLite database file path."""
    root = Path(data_dir) if data_dir is not None else get_user_data_dir()
    db_dir = root / "databases"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "default.db"


def get_default_rules_path(data_dir: Optional[Path] = None) -> Path:
    """Return the default custom rules JSON file path."""
    root = Path(data_dir) if data_dir is not None else get_user_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root / "custom_rules.json"


def normalize_extension(ext: str) -> str:
    """Normalize a file extension to lowercase with a leading dot."""
    cleaned = ext.strip().lower()
    if not cleaned:
        return ""
    if not cleaned.startswith("."):
        cleaned = f".{cleaned}"
    return cleaned


@dataclass
class WindowGeometry:
    """Stored desktop window dimensions and position."""

    width: int = 1280
    height: int = 800
    x: Optional[int] = None
    y: Optional[int] = None
    maximized: bool = False
    qt_geometry_b64: str = ""

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "WindowGeometry":
        if not isinstance(data, dict):
            return cls()
        return cls(
            width=max(640, int(data.get("width", 1280) or 1280)),
            height=max(480, int(data.get("height", 800) or 800)),
            x=data.get("x") if isinstance(data.get("x"), int) else None,
            y=data.get("y") if isinstance(data.get("y"), int) else None,
            maximized=bool(data.get("maximized", False)),
            qt_geometry_b64=str(data.get("qt_geometry_b64", "") or ""),
        )


@dataclass
class AppConfig:
    """Persistent application settings."""

    current_db_path: str = ""
    recent_db_paths: List[str] = field(default_factory=list)
    max_recent_dbs: int = 10
    theme: str = "light"
    ignore_companion_files: bool = True
    companion_extensions: List[str] = field(
        default_factory=lambda: list(DEFAULT_COMPANION_EXTENSIONS)
    )
    custom_rules_path: str = ""
    auto_backup: bool = True
    backup_keep_count: int = 5
    default_dedup_strategy: str = "keep_highest_tag_score"
    default_import_dir: str = ""
    default_export_dir: str = ""
    window_geometry: WindowGeometry = field(default_factory=WindowGeometry)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize configuration to a plain JSON-compatible dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        """Create an ``AppConfig`` from a parsed JSON dictionary safely."""
        if not isinstance(data, dict):
            return cls()

        theme = str(data.get("theme", "light") or "light").lower()
        if theme not in ("light", "dark", "system"):
            theme = "light"

        raw_exts = data.get("companion_extensions")
        if isinstance(raw_exts, list):
            normalized_exts: List[str] = []
            seen = set()
            for item in raw_exts:
                ext = normalize_extension(str(item))
                if ext and ext not in seen:
                    seen.add(ext)
                    normalized_exts.append(ext)
        else:
            normalized_exts = list(DEFAULT_COMPANION_EXTENSIONS)

        raw_recent = data.get("recent_db_paths")
        recent: List[str] = []
        if isinstance(raw_recent, list):
            seen_paths = set()
            for item in raw_recent:
                path_str = str(item).strip()
                if path_str and path_str not in seen_paths:
                    seen_paths.add(path_str)
                    recent.append(path_str)

        max_recent = max(1, min(50, int(data.get("max_recent_dbs", 10) or 10)))
        backup_keep = max(1, min(50, int(data.get("backup_keep_count", 5) or 5)))

        return cls(
            current_db_path=str(data.get("current_db_path", "") or ""),
            recent_db_paths=recent[:max_recent],
            max_recent_dbs=max_recent,
            theme=theme,
            ignore_companion_files=bool(data.get("ignore_companion_files", True)),
            companion_extensions=normalized_exts,
            custom_rules_path=str(data.get("custom_rules_path", "") or ""),
            auto_backup=bool(data.get("auto_backup", True)),
            backup_keep_count=backup_keep,
            default_dedup_strategy=str(
                data.get("default_dedup_strategy", "keep_highest_tag_score")
                or "keep_highest_tag_score"
            ),
            default_import_dir=str(data.get("default_import_dir", "") or ""),
            default_export_dir=str(data.get("default_export_dir", "") or ""),
            window_geometry=WindowGeometry.from_dict(data.get("window_geometry")),
        )


class ConfigManager:
    """Loads, updates, and atomically persists ``AppConfig`` as JSON."""

    def __init__(
        self,
        config_path: Optional[str | Path] = None,
        data_dir: Optional[str | Path] = None,
    ) -> None:
        self._data_dir = Path(data_dir) if data_dir is not None else get_user_data_dir()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = (
            Path(config_path)
            if config_path is not None
            else get_default_config_path(self._data_dir)
        )
        self.config: AppConfig = self.load()

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def _apply_default_paths(self, cfg: AppConfig) -> AppConfig:
        if not cfg.current_db_path:
            cfg.current_db_path = str(get_default_db_path(self._data_dir))
        if not cfg.custom_rules_path:
            cfg.custom_rules_path = str(get_default_rules_path(self._data_dir))
        return cfg

    def load(self) -> AppConfig:
        """Load configuration from disk, recovering gracefully on corruption."""
        if not self.config_path.exists():
            cfg = self._apply_default_paths(AppConfig())
            self.config = cfg
            return cfg

        try:
            raw_text = self.config_path.read_text(encoding="utf-8")
            parsed = json.loads(raw_text)
            cfg = self._apply_default_paths(AppConfig.from_dict(parsed))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            # Preserve corrupt file for troubleshooting and fall back to defaults.
            corrupt_path = self.config_path.with_suffix(".corrupt.json")
            try:
                if self.config_path.exists():
                    os.replace(self.config_path, corrupt_path)
            except OSError:
                pass
            cfg = self._apply_default_paths(AppConfig())

        self.config = cfg
        return cfg

    def save(self, config: Optional[AppConfig] = None) -> Path:
        """Atomically persist the configuration to disk."""
        if config is not None:
            self.config = config
        self._apply_default_paths(self.config)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.config_path.with_suffix(".json.tmp")
        payload = json.dumps(self.config.to_dict(), ensure_ascii=False, indent=2)
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, self.config_path)
        return self.config_path

    def add_recent_db(self, db_path: str | Path, set_current: bool = True) -> List[str]:
        """Record a database path at the front of the recent list."""
        resolved = str(Path(db_path).resolve())
        norm_target = os.path.normcase(resolved)
        updated = [
            p for p in self.config.recent_db_paths if os.path.normcase(p) != norm_target
        ]
        updated.insert(0, resolved)
        self.config.recent_db_paths = updated[: self.config.max_recent_dbs]
        if set_current:
            self.config.current_db_path = resolved
        return list(self.config.recent_db_paths)

    def remove_recent_db(self, db_path: str | Path) -> List[str]:
        """Remove a database path from the recent list."""
        resolved = str(Path(db_path).resolve())
        norm_target = os.path.normcase(resolved)
        self.config.recent_db_paths = [
            p for p in self.config.recent_db_paths if os.path.normcase(p) != norm_target
        ]
        return list(self.config.recent_db_paths)

    def get_recent_dbs(self, existing_only: bool = False) -> List[str]:
        """Return recent database paths, optionally filtering out missing files."""
        if not existing_only:
            return list(self.config.recent_db_paths)
        return [p for p in self.config.recent_db_paths if Path(p).is_file()]

    def set_companion_extensions(self, extensions: Sequence[str]) -> List[str]:
        """Replace companion extensions with a deduplicated normalized list."""
        result: List[str] = []
        seen = set()
        for raw in extensions:
            ext = normalize_extension(raw)
            if ext and ext not in seen:
                seen.add(ext)
                result.append(ext)
        self.config.companion_extensions = result
        return list(result)

    def get_companion_extension_set(self) -> set[str]:
        """Return companion extensions as a lowercase set for fast lookup."""
        if not self.config.ignore_companion_files:
            return set()
        return {
            normalize_extension(ext)
            for ext in self.config.companion_extensions
            if normalize_extension(ext)
        }

    def update_window_geometry(
        self,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        maximized: Optional[bool] = None,
        qt_geometry_b64: Optional[str] = None,
    ) -> WindowGeometry:
        """Update stored window geometry fields."""
        geom = self.config.window_geometry
        if width is not None:
            geom.width = max(640, int(width))
        if height is not None:
            geom.height = max(480, int(height))
        if x is not None:
            geom.x = int(x)
        if y is not None:
            geom.y = int(y)
        if maximized is not None:
            geom.maximized = bool(maximized)
        if qt_geometry_b64 is not None:
            geom.qt_geometry_b64 = qt_geometry_b64
        return geom

    def reset_to_defaults(self) -> AppConfig:
        """Reset settings to factory defaults while keeping data_dir paths."""
        self.config = self._apply_default_paths(AppConfig())
        return self.config
