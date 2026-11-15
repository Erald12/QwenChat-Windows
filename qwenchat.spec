# PyInstaller recipe for QwenChat.exe:   pyinstaller --noconfirm qwenchat.spec
#
# Builds a folder app (dist\QwenChat\QwenChat.exe + its files), not a single .exe: a single .exe
# unpacks ~60 MB to a temp folder on every start (slow, and antivirus programs often flag it).
import os

from kivy_deps import angle, glew, sdl2
from kivy.tools.packaging.pyinstaller_hooks import get_deps_minimal, hookspath, runtime_hooks

binaries = []
if os.path.exists(os.path.join("libs", "chatbridge.dll")):
    binaries.append((os.path.join("libs", "chatbridge.dll"), "."))
else:
    print("WARNING: libs\\chatbridge.dll is missing - the .exe will run in demo mode. "
          "Build it first with native\\build_windows.ps1")

deps = get_deps_minimal(video=None, audio=None, camera=None, spelling=None)
deps["binaries"] = deps.get("binaries", []) + binaries
deps["hiddenimports"] = deps.get("hiddenimports", []) + ["win32com.client", "certifi"]

a = Analysis(
    ["main.py"],
    datas=[("assets", "assets")],
    hookspath=hookspath(),
    runtime_hooks=runtime_hooks(),
    noarchive=False,
    **deps,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="QwenChat",
    icon="assets/icon.ico",
    console=False,          # no black console window; logs go to %LOCALAPPDATA%\QwenChat\qwenchat.log
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    *[Tree(path) for path in (sdl2.dep_bins + glew.dep_bins + angle.dep_bins)],
    strip=False,
    upx=False,
    name="QwenChat",
)
