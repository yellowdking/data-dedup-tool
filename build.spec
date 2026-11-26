# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build specification for data-dedup-tool (Windows single-file GUI)."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

PROJECT_ROOT = Path(SPECPATH).resolve()
ICON_PATH = PROJECT_ROOT / "src" / "resources" / "app.ico"
RESOURCES_DIR = PROJECT_ROOT / "src" / "resources"

datas = []
if RESOURCES_DIR.exists():
    datas.append((str(RESOURCES_DIR), "src/resources"))

# Include python-docx package templates (e.g. docx/templates/default.docx) so
# creating and exporting .docx documents works cleanly in the frozen single-file EXE.
datas += collect_data_files("docx")

hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "openpyxl",
    "openpyxl.cell._writer",
    "docx",
    "docx.oxml",
    "docx.oxml.ns",
    "docx.opc",
    "docx.opc.constants",
    "docx.shared",
    "docx.enum.text",
    "docx.enum.table",
    "lxml",
    "lxml.etree",
    "lxml._elementpath",
    "sqlite3",
    "csv",
]

excludes = [
    "PySide6.QtWebEngine",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQml",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtDBus",
    "tkinter",
    "PIL",
    "unittest",
    "pytest",
    "matplotlib",
    "numpy",
    "scipy",
    "pandas",
]

a = Analysis(
    [str(PROJECT_ROOT / "src" / "app.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="data-dedup-tool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_PATH) if ICON_PATH.exists() else None,
)
