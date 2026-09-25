# PyInstaller one-directory build for the standalone Windows desktop program.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata
import os

hidden = []
for package in ("keyring.backends", "dashscope", "openpyxl", "docx", "jieba", "instructor", "pydantic"):
    hidden += collect_submodules(package)

datas = [("config.yaml", ".")]
if os.environ.get("MEDEXPLAIN_BUILD_INFO"):
    datas += [(os.environ["MEDEXPLAIN_BUILD_INFO"], ".")]
datas += collect_data_files("jieba")
datas += collect_data_files("keyring")
datas += [("frontend/dist", "frontend/dist")]

hidden += [
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebChannel",
]

a = Analysis(
    ["desktop_app.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "docling", "docling_core", "docling_parse", "rapidocr", "onnxruntime", "torch", "torchvision", "transformers"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [('X utf8', None, 'OPTION')],
    exclude_binaries=True,
    name="MedExplainStudio",
    icon="assets/app_icon.ico",
    version="version_info.txt",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="MedExplainStudio",
)
