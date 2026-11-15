# cx_Freeze recipe for QwenChat.exe:   python setup_exe.py build_exe
#
# Result: dist\QwenChat\QwenChat.exe plus its files (copy the whole folder to use it elsewhere).
# Layout:
#   QwenChat.exe, python312.dll, chatbridge.dll, SDL2/GLEW/ANGLE DLLs   <- next to the .exe, so Windows
#                                                                        finds them when Kivy loads
#   assets\                                                             <- icon
#   lib\                                                                <- Python packages (kivy, ...)
#
# Unlike PyInstaller's Kivy helpers, nothing here imports Kivy's window module while building, so no
# window (and no "OpenGL version" error box) is ever opened on the GitHub runner.
import glob
import os

from cx_Freeze import Executable, setup
from kivy_deps import angle, glew, sdl2  # only puts their bin folders on PATH; opens nothing

include_files = [("assets", "assets")]
if os.path.exists(os.path.join("libs", "chatbridge.dll")):
    include_files.append((os.path.join("libs", "chatbridge.dll"), "chatbridge.dll"))
else:
    print("WARNING: libs\\chatbridge.dll is missing - the .exe will run in demo mode. "
          "Build it first with native\\build_windows.ps1")

# SDL2 (window, text, images), GLEW (OpenGL) and ANGLE (DirectX) DLLs
for folder in sdl2.dep_bins + glew.dep_bins + angle.dep_bins:
    for path in glob.glob(os.path.join(folder, "*")):
        if os.path.isfile(path):
            include_files.append((path, os.path.basename(path)))

build_exe_options = {
    "build_exe": os.path.join("dist", "QwenChat"),
    # Kivy picks its window/text/image providers by name at runtime, so ship the whole package
    "packages": ["kivy", "kivy_deps", "win32com", "certifi"],
    "excludes": [
        "tkinter", "kivy.tests", "kivy.tools", "kivy.lib.gstplayer", "PyInstaller",
        "kivy.core.video", "kivy.core.audio", "kivy.core.camera", "kivy.core.spelling",
    ],
    "include_files": include_files,
    "include_msvcr": True,  # Visual C++ runtime, so the PC needs nothing installed
}

setup(
    name="QwenChat",
    version="1.0",
    description="QwenChat",
    options={"build_exe": build_exe_options},
    executables=[
        Executable(
            "main.py",
            base="gui",            # no black console window; logs go to %LOCALAPPDATA%\QwenChat\qwenchat.log
            target_name="QwenChat",
            icon="assets/icon.ico",
        )
    ],
)
