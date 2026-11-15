# Qwen Chat for Windows: offline AI chat on your PC

The Windows version of [QwenChat-Android](../QwenChat-Android). It is the same WhatsApp-style chat and
the same llama.cpp engine, running **Qwen2.5-Instruct** on the PC's CPU. There's no account and no
server. The internet is used only to download a model from the in-app **Models** menu.

```
main.py  (Kivy UI + ChatEngine)          QwenChat.exe (cx_Freeze, Python 3.12)
   │  ctypes
   ▼
chatbridge.dll  (native/chat_bridge.cpp + llama.cpp, statically linked, MSVC x64, AVX2)
   │
   ▼
%LOCALAPPDATA%\QwenChat\models\*.gguf    memory-mapped
```

`native/chat_bridge.cpp` is the Android bridge with one change: Windows exports via
`__declspec(dllexport)`. Both apps pin the same llama.cpp tag (`b11963`).

## A stable window on Windows

| Problem on Windows | What the app does |
|---|---|
| Blurry text at 125–200% display scaling | Makes itself per-monitor DPI-aware before the window exists, so Kivy's `dp`/`sp` follow the Windows scale setting and Windows never bitmap-stretches the window. |
| Window taller than the screen (1366×768 laptops, 1024×768) | The default 440×760 (logical) is shrunk to fit the monitor's **work area** (above the taskbar), title bar included, then centered. |
| Window dragged too small, so buttons overlap | Minimum size of 360×480 (logical), enforced by Windows while dragging. |
| Reopening off-screen (monitor unplugged, resolution changed) | Size, position and maximized state are saved on exit (`settings.json`). They are reused only if the title bar is still on a connected monitor, and the window is moved back inside the work area if it sticks out. |
| Size wrong after changing display scaling | The size is saved in logical pixels and multiplied by the current scale. |
| Bubbles stretched across a maximized window | Bubbles are at most 80% of the chat width **and** at most 640 dp. Resizing re-wraps the text. Bubbles whose wrap width didn't change are skipped, so resizing stays smooth. |
| Black window / crash on some GPUs, VMs, Remote Desktop | Draws through **ANGLE (DirectX)** instead of OpenGL. To try OpenGL: `set KIVY_GL_BACKEND=glew`. |
| Right-click leaves red dots; Esc closes the app | Kivy's multitouch emulation and exit-on-Escape are turned off. |
| An old `%USERPROFILE%\.kivy\config.ini` changes the window | Kivy's config lives in the app's own folder (`KIVY_HOME`). |
| No console in the .exe, so Kivy's logger crashes | Output goes to `%LOCALAPPDATA%\QwenChat\qwenchat.log`. |

Known limit: Kivy sizes widgets once. If you drag the window to a monitor with a **different**
scale setting, it keeps the first monitor's scale until you restart the app (it then opens there with
the right scale).

### Desktop behavior
- **Enter** sends, **Shift+Enter** starts a new line, **Esc** stops a reply, **Ctrl+N** starts a new chat.
- **Select text in any message** with the mouse: drag to select, double-click for a word,
  triple-click for a line, Ctrl+A for the whole message. Copy with **Ctrl+C** or a **right-click**.
  Selection stays inside one message. **Copy** under a reply copies all of it. Messages are
  read-only text boxes (`SelectableText`), so `**bold**` shows as plain text.
- Scroll with the mouse wheel or the scrollbar. Dragging the content selects text instead of
  scrolling. While a reply streams in, the chat follows it, unless you scrolled up to read something.
- The speaker reads a reply aloud with the Windows voices (SAPI).
- The microphone opens Windows voice typing (same as **Win+H**) into the message box.
- **Models → Open folder** opens the models folder in Explorer.

## Settings for a PC (top of `main.py`)

| | Android | Windows |
|---|---|---|
| `N_THREADS` / `N_THREADS_BATCH` | 4 / 6 | half the logical CPUs (≈ real cores), max 8 / 16 |
| `N_CTX` | 2048 | 4096 |
| `N_BATCH` | 256 | 512 |
| `MAX_REPLY_TOKENS` | 512 | 1024 |
| Memory budget | 2 GB | 60% of RAM (at least 2 GB) |

Memory is measured with `GetProcessMemoryInfo` / `GlobalMemoryStatusEx` instead of `/proc`. The same
logic as on the phone halves the context if a model doesn't fit the budget.

## Where things are

| | Path |
|---|---|
| Downloaded models, `settings.json`, log | `%LOCALAPPDATA%\QwenChat\` |
| Your own `.gguf` (alternative) | a `models` folder next to `QwenChat.exe` (or `main.py`) |
| chatbridge.dll | inside the app folder, or `libs\` when running `main.py` |

## Build

### With GitHub Actions (no tools needed on your PC)
Push this folder as its own repository. `.github/workflows/build-windows.yml` builds `chatbridge.dll`
with Visual Studio 2022, checks that it loads, builds `QwenChat.exe` with cx_Freeze and uploads
**QwenChat-windows-x64.zip** (Actions → the run → Artifacts). The first run takes about 15–25 minutes.
Later runs reuse the cached DLL. *Run workflow* lets you uncheck AVX2 for PCs from before ~2013.

### On your PC
1. Install **Build Tools for Visual Studio 2022** with *Desktop development with C++*, plus Git
   and Python 3.10–3.12.
2. `powershell -ExecutionPolicy Bypass -File build_exe.ps1`

   This builds `libs\chatbridge.dll` if it's missing, creates a clean `.venv-build`, and runs
   cx_Freeze (`setup_exe.py`). The result is `dist\QwenChat\QwenChat.exe`.

### Just run it
```
pip install -r requirements.txt
python main.py
```
Without `chatbridge.dll` it runs in **demo mode** and echoes your messages, which is enough for UI work.

## Notes
- The app folder is built in one-folder mode, not as a single .exe. A single .exe unpacks itself to a
  temp folder on every start, which is slow and often flagged by antivirus programs.
- The .exe isn't code-signed, so Windows SmartScreen may say "Windows protected your PC". Click
  **More info → Run anyway**.
- Small models make mistakes. Don't rely on the answers for anything important.
