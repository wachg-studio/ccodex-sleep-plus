# -*- coding: utf-8 -*-
"""Build ccodex-sleep-plus with PyInstaller (onedir mode, stable for C extensions)."""
import PyInstaller.__main__
import sleep_plus as sp

ico = sp.icon_file()
print("icon:", ico)

PyInstaller.__main__.run([
    "sleep_plus.py",
    "--onedir", "--noconsole",
    "--name", "ccodex-sleep-plus",
    "--icon", str(ico),
    "--add-data", f"{sp.Path(__file__).parent / 'panel.html'};.",
    "--add-data", f"{sp.Path(__file__).parent / 'data' / 'gpt_bank.json'};data",
    "--hidden-import", "pystray._win32",
    "--collect-all", "PIL",
    "--collect-all", "pystray",
    "--hidden-import", "webview.platforms.edgechromium",
    "--hidden-import", "clr_loader",
    "--collect-submodules", "compression",
    "--exclude-module", "tkinter",
    "--exclude-module", "PySide6",
    "--exclude-module", "PyQt6",
    "--clean", "--noconfirm",
])
