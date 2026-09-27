# -*- coding: utf-8 -*-
"""Build ccodex-sleep-plus.exe with PyInstaller."""
import PyInstaller.__main__
import sleep_plus as sp

ico = sp.icon_file()
print("icon:", ico)

PyInstaller.__main__.run([
    "sleep_plus.py",
    "--onefile", "--noconsole",
    "--name", "ccodex-sleep-plus",
    "--icon", str(ico),
    "--hidden-import", "pystray._win32",
    "--hidden-import", "webview.platforms.edgechromium",
    "--hidden-import", "webview.platforms.winforms",
    "--hidden-import", "clr_loader",
    "--collect-submodules", "compression",
    "--exclude-module", "tkinter",
    "--exclude-module", "PySide6",
    "--exclude-module", "PyQt6",
    "--clean", "--noconfirm",
])
