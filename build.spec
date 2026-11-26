# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build specification for data-dedup-tool (Windows single-file GUI).

Slims unused Qt/OpenGL/Network/SVG/translation/lxml bloat (~30% smaller EXE)
while keeping PySide6 QtCore/QtGui/QtWidgets, qwindows + qoffscreen platforms,
qico.dll, qtbase_zh_CN.qm, openpyxl, python-docx (+ templates), lxml.etree,
and sqlite3 100% intact, and embeds Windows PE VSVersionInfo metadata.
"""

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files

PROJECT_ROOT = Path(SPECPATH).resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.version_info import write_version_info_file

ICON_PATH = PROJECT_ROOT / "src" / "resources" / "app.ico"
RESOURCES_DIR = PROJECT_ROOT / "src" / "resources"
VERSION_INFO_PATH = write_version_info_file(
    output_path=PROJECT_ROOT / "build_version_info.txt",
    project_root=PROJECT_ROOT,
)

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
    # Unused PySide6 modules (keep QtCore, QtGui, QtWidgets only)
    "PySide6.QtNetwork",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtPrintSupport",
    "PySide6.QtXml",
    "PySide6.QtConcurrent",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtUiTools",
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
    # Unused lxml submodules (python-docx only requires lxml.etree + lxml._elementpath)
    "lxml.objectify",
    "lxml.html",
    "lxml.sax",
    "lxml.builder",
    "lxml.isoschematron",
    "lxml.cssselect",
    "lxml.doctestcompare",
    # Unused standard / dev / scientific packages
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

# --- Post-Analysis binary & data slimming ---
_DROP_BINARY_EXACT = {
    "opengl32sw.dll",
    "qt6network.dll",
    "qtnetwork.pyd",
    "qt6svg.dll",
    "qt6svgwidgets.dll",
    "qtsvg.pyd",
    "qtsvgwidgets.pyd",
    "qt6opengl.dll",
    "qt6openglwidgets.dll",
    "qtopengl.pyd",
    "qtopenglwidgets.pyd",
    "qt6pdf.dll",
    "qt6qml.dll",
    "qt6qmlmodels.dll",
    "qt6quick.dll",
    "qt6sql.dll",
    "qt6test.dll",
    "qt6dbus.dll",
    "qt6printsupport.dll",
    "qt6xml.dll",
    "qdirect2d.dll",
}

_DROP_PLUGIN_DIRS = (
    "/plugins/tls/",
    "/plugins/networkinformation/",
    "/plugins/iconengines/",
    "/plugins/generic/",
    "/plugins/platforminputcontexts/",
    "/plugins/sqldrivers/",
    "/plugins/designer/",
    "/plugins/qmltooling/",
    "/plugins/vectorimageformats/",
)

_KEEP_IMAGEFORMATS = {
    "qico.dll",
}

_DROP_LXML_PREFIXES = (
    "lxml/objectify",
    "lxml/html/",
    "lxml/sax",
    "lxml/builder",
    "lxml/isoschematron",
)


def _norm_path(value: str) -> str:
    return value.replace("\\", "/").lower()


def _should_drop_binary(dest_name: str, src_path: str) -> bool:
    dest = _norm_path(dest_name)
    base = dest.rsplit("/", 1)[-1]

    if base in _DROP_BINARY_EXACT:
        return True

    for plugin_dir in _DROP_PLUGIN_DIRS:
        if plugin_dir in dest:
            return True

    if "/plugins/imageformats/" in dest and base not in _KEEP_IMAGEFORMATS:
        return True

    for lxml_prefix in _DROP_LXML_PREFIXES:
        if dest.startswith(lxml_prefix):
            return True

    return False


def _should_drop_data(dest_name: str, src_path: str) -> bool:
    dest = _norm_path(dest_name)
    base = dest.rsplit("/", 1)[-1]

    # Keep only Simplified Chinese Qt base translation
    if "/translations/" in dest and base.endswith(".qm"):
        return base != "qtbase_zh_cn.qm"

    if dest.startswith("lxml/isoschematron/"):
        return True

    return False


a.binaries = [
    entry for entry in a.binaries if not _should_drop_binary(entry[0], entry[1])
]
a.datas = [
    entry for entry in a.datas if not _should_drop_data(entry[0], entry[1])
]

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
    version=str(VERSION_INFO_PATH) if VERSION_INFO_PATH.exists() else None,
)
