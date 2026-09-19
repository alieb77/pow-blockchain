# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller — exécutable « clic-and-run » FLOUS (un seul fichier).

Construire :   pip install pyinstaller
               pyinstaller flous.spec --noconfirm --clean
Résultat :     dist/FLOUS.exe  (Windows)  /  dist/FLOUS (Linux, macOS)

Le fichier de l'explorateur (powchain/web/explorer.html) est embarqué dans
« datas » : l'API le sert alors depuis les données extraites (sys._MEIPASS),
via powchain.api.load_explorer_html.
"""
import os

from PyInstaller.utils.hooks import collect_submodules

datas = [(os.path.join("powchain", "web", "explorer.html"), os.path.join("powchain", "web"))]
hiddenimports = collect_submodules("powchain")

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="FLOUS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX n'est pas garanti présent ; on ne compresse pas pour rester fiable
    console=True,
    disable_windowed_traceback=False,
    icon=None,
)
