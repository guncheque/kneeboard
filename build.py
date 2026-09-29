#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 GracelessDev
"""Build a self-contained kneeboard folder with PyInstaller.

    python build.py

Produces dist/kneeboard/ (the program plus its _internal folder) and, on
Windows, the helper .bat files next to it. Zip that folder to ship it.
PyInstaller can't cross-compile: build on the OS you're targeting (the
GitHub Actions workflow does the Windows build for you).
"""
import os
import shutil
import sys
from pathlib import Path

import PyInstaller.__main__

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist" / "kneeboard"

args = [
    str(HERE / "server.py"),
    "--name", "kneeboard",
    "--onedir",       # folder, not one-file: starts faster, fewer antivirus false positives
    "--console",      # the console shows the tablet URL, QR code and log
    "--noconfirm",
    "--clean",
    "--add-data", f"{HERE / 'client'}{os.pathsep}client",
    "--collect-all", "pypdfium2",
    "--collect-all", "pypdfium2_raw",
    "--hidden-import", "inputs",
]
if sys.platform == "win32":
    args += ["--collect-all", "sdl2dll", "--icon", str(HERE / "packaging" / "icon.ico")]
elif sys.platform == "darwin":
    args += ["--collect-all", "sdl2dll"]

PyInstaller.__main__.run(args)

for f in ("README.md", "LICENSE", "config.example.toml"):
    shutil.copy(HERE / f, DIST / f)
if sys.platform == "win32":
    for bat in (HERE / "packaging").glob("*.bat"):
        shutil.copy(bat, DIST / bat.name)
print(f"\nBuilt {DIST}")
