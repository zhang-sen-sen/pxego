# -*- coding: utf-8 -*-
"""打包 PXEGo 完整运行包（含 BOOT.WIM），排除 .git / logs / __pycache__。"""
import os
import sys
import zipfile

BASE = r"H:\豆包\PXE\PXEGo-github"
OUT = r"H:\豆包\PXE\PXEGo_完整运行包_20261018.zip"
EXCLUDE_DIRS = {".git", "logs", "__pycache__", ".pytest_cache"}

missing = []
for f in ("boot\\BOOT.WIM", "runtime\\python\\python313.zip", "launcher.ps1",
          "launcher.cmd", "main.py", "config.json"):
    if not os.path.isfile(os.path.join(BASE, f)):
        missing.append(f)
if missing:
    print("MISSING: " + ", ".join(missing))
    sys.exit(1)

parent = os.path.dirname(BASE)
count = 0
total = 0
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
    for root, dirs, files in os.walk(BASE):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for name in files:
            p = os.path.join(root, name)
            arc = os.path.relpath(p, parent)
            zf.write(p, arc)
            count += 1
            total += os.path.getsize(p)
print("OK files=%d total=%.1fMB zip=%.1fMB" % (count, total / 1048576,
      os.path.getsize(OUT) / 1048576))
