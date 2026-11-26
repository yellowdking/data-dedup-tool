"""Application configuration management for 数据查重程序.

Handles persistent JSON settings, recent database history, theme preferences,
companion-file extension filters, custom rule paths, window geometry, and
PyInstaller frozen-bundle path resolution.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .io_utils import DEFAULT_COMPANION_EXTENSIONS_LIST

APP_NAME = "数据查重程序"
APP_DIR_NAME = "DataDedupTool"

DEFAULT_COMPANION_EXTENSIONS: List[str] = list(DEFAULT_COMPANION_EXTENSIONS_LIST)

_VALID_THEMES = {"light", "dark", "system"}
_VALID_DEDUP_STRATEGIES = {
    "keep_earliest",
    "keep_latest",
    "keep_highest_tag_score",
    "keep_longest_name",
}
_PORTABLE_PATH_FIELDS = (
    "current_db_path",
    "custom_rules_path",
    "default_import_dir",
    "default_export_dir",
)


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


def _safe_int(
    value: Any,
    default: int,
    *,
    min_val: Optional[int] = None,
    max_val: Optional[int] = None,
) -> int:
    """Safely parse an integer field, rejecting booleans and invalid values."""
    if value is None or isinstance(value, bool):
        parsed = default
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
    if min_val is not None and parsed < min_val:
        parsed = min_val
    if max_val is not None and parsed > max_val:
        parsed = max_val
    return parsed


def _safe_opt_int(value: Any) -> Optional[int]:
    """Safely parse an optional integer (rejects bools and non-int types)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    return None


def _safe_bool(value: Any, default: bool) -> bool:
    """Safely parse a boolean field with sensible string/numeric handling."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
    return default


def _safe_str(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value.strip()
    return default


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
        raw_b64 = data.get("qt_geometry_b64")
        return cls(
            width=_safe_int(data.get("width"), 1280, min_val=640),
            height=_safe_int(data.get("height"), 800, min_val=480),
            x=_safe_opt_int(data.get("x")),
            y=_safe_opt_int(data.get("y")),
            maximized=_safe_bool(data.get("maximized"), False),
            qt_geometry_b64=raw_b64 if isinstance(raw_b64, str) else "",
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
        """Create an ``AppConfig`` from a parsed JSON dictionary with per-field fallbacks."""
        if not isinstance(data, dict):
            return cls()

        raw_theme = _safe_str(data.get("theme"), "light").lower()
        theme = raw_theme if raw_theme in _VALID_THEMES else "light"

        raw_exts = data.get("companion_extensions")
        if isinstance(raw_exts, list):
            normalized_exts: List[str] = []
            seen: set[str] = set()
            for item in raw_exts:
                if not isinstance(item, str):
                    continue
                ext = normalize_extension(item)
                if ext and ext not in seen:
                    seen.add(ext)
                    normalized_exts.append(ext)
        else:
            normalized_exts = list(DEFAULT_COMPANION_EXTENSIONS)

        raw_recent = data.get("recent_db_paths")
        recent: List[str] = []
        if isinstance(raw_recent, list):
            seen_paths: set[str] = set()
            for item in raw_recent:
                if not isinstance(item, str):
                    continue
                path_str = item.strip()
                if path_str and path_str not in seen_paths:
                    seen_paths.add(path_str)
                    recent.append(path_str)

        max_recent = _safe_int(data.get("max_recent_dbs"), 10, min_val=1, max_val=50)
        backup_keep = _safe_int(data.get("backup_keep_count"), 5, min_val=1, max_val=50)

        raw_strategy = _safe_str(
            data.get("default_dedup_strategy"), "keep_highest_tag_score"
        )
        strategy = (
            raw_strategy
            if raw_strategy in _VALID_DEDUP_STRATEGIES
            else "keep_highest_tag_score"
        )

        return cls(
            current_db_path=_safe_str(data.get("current_db_path"), ""),
            recent_db_paths=recent[:max_recent],
            max_recent_dbs=max_recent,
            theme=theme,
            ignore_companion_files=_safe_bool(data.get("ignore_companion_files"), True),
            companion_extensions=normalized_exts,
            custom_rules_path=_safe_str(data.get("custom_rules_path"), ""),
            auto_backup=_safe_bool(data.get("auto_backup"), True),
            backup_keep_count=backup_keep,
            default_dedup_strategy=strategy,
            default_import_dir=_safe_str(data.get("default_import_dir"), ""),
            default_export_dir=_safe_str(data.get("default_export_dir"), ""),
            window_geometry=WindowGeometry.from_dict(data.get("window_geometry")),
        )


class ConfigManager:
    """Loads, updates, and atomically persists ``AppConfig`` as JSON."""

    def __init__(
        self,
        config_path: Optional[str | Path] = None,
        data_dir: Optional[str | Path] = None,
    ) -> None:
        if data_dir is not None:
            self._data_dir = Path(data_dir).expanduser().resolve()
            self._data_dir.mkdir(parents=True, exist_ok=True)
            self.config_path = (
                Path(config_path).expanduser().resolve()
                if config_path is not None
                else get_default_config_path(self._data_dir)
            )
        elif config_path is not None:
            self.config_path = Path(config_path).expanduser().resolve()
            self._data_dir = self.config_path.parent
            self._data_dir.mkdir(parents=True, exist_ok=True)
        else:
            self._data_dir = get_user_data_dir().resolve()
            self._data_dir.mkdir(parents=True, exist_ok=True)
            self.config_path = get_default_config_path(self._data_dir)

        self.config: AppConfig = self.load()

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def is_portable(self) -> bool:
        """Return True if running in portable mode (``portable.flag`` present)."""
        if is_frozen() and (get_app_base_dir() / "portable.flag").exists():
            return True
        if (self._data_dir / "portable.flag").exists():
            return True
        if (self._data_dir.parent / "portable.flag").exists():
            return True
        return False

    def _resolve_stored_path(self, raw_path: str) -> str:
        """Resolve a stored path (converting relative paths against ``self._data_dir``)."""
        cleaned = str(raw_path or "").strip()
        if not cleaned:
            return ""
        p = Path(cleaned)
        if not p.is_absolute():
            return str((self._data_dir / p).resolve())
        return cleaned

    def _to_portable_path(self, raw_path: str) -> str:
        """Convert paths inside ``self._data_dir`` to POSIX relative paths in portable mode."""
        cleaned = str(raw_path or "").strip()
        if not cleaned or not self.is_portable():
            return cleaned
        try:
            resolved = Path(cleaned).resolve()
            rel = resolved.relative_to(self._data_dir)
            return rel.as_posix()
        except (ValueError, OSError):
            return cleaned

    def _resolve_config_paths(self, cfg: AppConfig) -> AppConfig:
        for attr in _PORTABLE_PATH_FIELDS:
            val = getattr(cfg, attr, "")
            if val:
                setattr(cfg, attr, self._resolve_stored_path(val))
        if cfg.recent_db_paths:
            resolved_recent: List[str] = []
            seen: set[str] = set()
            for item in cfg.recent_db_paths:
                rp = self._resolve_stored_path(item)
                norm = os.path.normcase(rp)
                if rp and norm not in seen:
                    seen.add(norm)
                    resolved_recent.append(rp)
            cfg.recent_db_paths = resolved_recent[: cfg.max_recent_dbs]
        return cfg

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
        except json.JSONDecodeError:
            # Quarantine syntactically corrupt JSON for troubleshooting and fall back to defaults.
            corrupt_path = self.config_path.with_suffix(".corrupt.json")
            try:
                if self.config_path.exists():
                    os.replace(self.config_path, corrupt_path)
            except OSError:
                pass
            cfg = self._apply_default_paths(AppConfig())
            self.config = cfg
            return cfg
        except OSError:
            # Transient read/permission error: do not destroy config file on disk.
            cfg = self._apply_default_paths(AppConfig())
            self.config = cfg
            return cfg

        cfg = AppConfig.from_dict(parsed)
        cfg = self._resolve_config_paths(cfg)
        cfg = self._apply_default_paths(cfg)
        self.config = cfg
        return cfg

    def save(self, config: Optional[AppConfig] = None) -> Path:
        """Atomically persist the configuration to disk using a unique temp file + fsync."""
        if config is not None:
            self.config = config
        self._apply_default_paths(self.config)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        data = self.config.to_dict()
        if self.is_portable():
            for attr in _PORTABLE_PATH_FIELDS:
                if data.get(attr):
                    data[attr] = self._to_portable_path(str(data[attr]))
            if isinstance(data.get("recent_db_paths"), list):
                data["recent_db_paths"] = [
                    self._to_portable_path(str(p)) for p in data["recent_db_paths"]
                ]

        payload = json.dumps(data, ensure_ascii=False, indent=2)
        tmp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self.config_path.parent),
                prefix=f".{self.config_path.stem}_",
                suffix=".tmp",
                delete=False,
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
                tmp_file.write(payload)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp_path, self.config_path)
        except Exception:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
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
