# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: сборка «Pinterest Downloader.app». Запускать через packaging/build_macos.sh —
# он заранее готовит build/icon.icns и build/build_info.json.

import re
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
BUILD = ROOT / "build"
TOOLS = ROOT / "upscale" / "tools"
APP_NAME = "Pinterest Downloader"
VERSION = re.search(
    r'APP_VERSION = "([^"]+)"', (ROOT / "pinterest_gui_mac.py").read_text(encoding="utf-8")
).group(1)

binaries = []
datas = [(str(BUILD / "build_info.json"), ".")]
# Real-ESRGAN и модели — внутрь приложения, чтобы Upscale работал без папки проекта
if (TOOLS / "realesrgan-ncnn-vulkan").is_file():
    binaries.append((str(TOOLS / "realesrgan-ncnn-vulkan"), "upscale/tools"))
if (TOOLS / "models").is_dir():
    datas.append((str(TOOLS / "models"), "upscale/tools/models"))

a = Analysis(
    [str(ROOT / "pinterest_gui_mac.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    # Tk в приложении не нужен (и на новых macOS системный Tk всё равно не рисует окна)
    excludes=["tkinter", "_tkinter", "PIL.ImageTk", "win10toast"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    argv_emulation=False,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)
app = BUNDLE(
    coll,
    name=f"{APP_NAME}.app",
    icon=str(BUILD / "icon.icns"),
    bundle_identifier="io.github.chika1305.pin-download",
    version=VERSION,
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "12.0",
        "LSApplicationCategoryType": "public.app-category.photography",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        "NSHumanReadableCopyright": "Pinterest Image Downloader",
        "NSAppleEventsUsageDescription": "Нужно, чтобы показывать уведомление о завершении загрузки.",
    },
)
