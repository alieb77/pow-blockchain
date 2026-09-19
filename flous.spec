# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller — exécutable « clic-and-run » FLOUS (un seul fichier).

Construire :   pip install pyinstaller
               pyinstaller flous.spec --noconfirm --clean
Résultat :     dist/FLOUS.exe  (Windows)  /  dist/FLOUS (Linux, macOS)

Les pages web (powchain/web/*.html : explorateur, portefeuille) sont embarquées
dans « datas » : l'API les sert alors depuis les données extraites
(sys._MEIPASS), via powchain.api.load_explorer_html / load_wallet_html.
"""
import glob
import os

from PyInstaller.utils.hooks import collect_submodules

datas = [
    (path, os.path.join("powchain", "web"))
    for path in glob.glob(os.path.join("powchain", "web", "*.html"))
]
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
