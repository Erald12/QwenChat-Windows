"""
Qwen Chat for Windows - a fully offline chatbot (Kivy UI + llama.cpp).

Desktop version of QwenChat-Android. The model runs on the PC's CPU through chatbridge.dll, a small C
library built from llama.cpp (see native/). The internet is used only to download a model from the
Models menu - your chats never leave the PC.

Window: opens at a fixed, phone-like default size that always fits the screen (also on 1366x768
laptops and at 125-200% display scaling), can't be shrunk below a usable minimum, and remembers its
size and position - unless that would put it off-screen (monitor unplugged, resolution changed).
"""
import codecs
import ctypes
import gc
import glob
import json
import os
import queue
import re
import shutil
import sys
import threading
import time

IS_WINDOWS = sys.platform == "win32"
FROZEN = getattr(sys, "frozen", False)  # running as QwenChat.exe (cx_Freeze)

# Folder that holds QwenChat.exe (or main.py): a "models" folder here is used too (portable use)
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
# Files that ship with the app (assets, chatbridge.dll): next to QwenChat.exe / main.py
# (_MEIPASS: inside a PyInstaller build, should one ever be used again)
RESOURCE_DIR = getattr(sys, "_MEIPASS", APP_DIR)
# Per-user data: downloaded models, settings, logs.  %LOCALAPPDATA%\QwenChat
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                        "QwenChat" if IS_WINDOWS else ".qwenchat")
os.makedirs(DATA_DIR, exist_ok=True)

# The .exe has no console: sys.stdout / sys.stderr are None and Kivy's logger would crash on the
# first message. Send everything to a log file instead (also handy when something goes wrong).
if sys.stdout is None or sys.stderr is None:
    try:
        _log = open(os.path.join(DATA_DIR, "qwenchat.log"), "w", encoding="utf-8", buffering=1)
    except OSError:
        _log = open(os.devnull, "w")
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

# Kivy settings, before Kivy is imported. Its own config / logs go to our data folder, so an old
# %USERPROFILE%\.kivy\config.ini can't change the window.
os.environ.setdefault("KIVY_HOME", os.path.join(DATA_DIR, "kivy"))
os.environ.setdefault("KIVY_NO_ARGS", "1")           # don't treat command-line arguments as Kivy options
os.environ.setdefault("KIVY_NO_FILELOG", "1")
if IS_WINDOWS:
    # ANGLE draws through DirectX, which every Windows PC has working drivers for. Plain OpenGL fails
    # on some laptops, virtual machines and remote desktop. Override with KIVY_GL_BACKEND=glew.
    os.environ.setdefault("KIVY_GL_BACKEND", "angle_sdl2")
if FROZEN:
    # kivy_deps.sdl2/glew/angle run os.path.join(site.USER_BASE, ...) on import, but in the frozen
    # .exe site.USER_BASE is None -> TypeError before the window opens. Any real folder works:
    # their DLLs sit next to QwenChat.exe, where Windows finds them anyway.
    import site
    if site.USER_BASE is None:
        site.USER_BASE = APP_DIR


# ==== window geometry (Windows) ==============================================================
# All in physical pixels. The process is made per-monitor DPI aware *before* Kivy creates the window,
# so Windows never bitmap-stretches it (blurry text at 125-200%) and Kivy's dp()/sp() scale with
# the display setting instead.

WINDOW_SIZE = (440, 760)      # default window size at 100% scaling (logical pixels, like dp)
WINDOW_MIN_SIZE = (360, 480)  # smaller than this the header buttons and the input bar get squeezed


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _Rect), ("rcWork", _Rect),
                ("dwFlags", ctypes.c_ulong)]


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def make_dpi_aware():
    if not IS_WINDOWS:
        return
    user32 = ctypes.windll.user32
    try:  # Windows 10 1703+: per-monitor v2 (also scales the title bar and dialogs)
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except AttributeError:
        pass
    try:  # Windows 8.1
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            user32.SetProcessDPIAware()  # Windows 7
        except AttributeError:
            pass


def monitor_at(x, y, nearest=True):
    """(work area rect, scale) of the monitor at screen point x, y; None if no monitor is there."""
    user32 = ctypes.windll.user32
    user32.MonitorFromPoint.restype = ctypes.c_void_p
    monitor = user32.MonitorFromPoint(_Point(int(x), int(y)), 2 if nearest else 0)
    if not monitor:
        return None
    info = _MonitorInfo()
    info.cbSize = ctypes.sizeof(_MonitorInfo)
    if not user32.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(info)):
        return None
    scale = 1.0
    try:
        dpi_x, dpi_y = ctypes.c_uint(), ctypes.c_uint()
        if ctypes.windll.shcore.GetDpiForMonitor(ctypes.c_void_p(monitor), 0, ctypes.byref(dpi_x),
                                                 ctypes.byref(dpi_y)) == 0:
            scale = dpi_x.value / 96
    except (AttributeError, OSError):
        pass
    return info.rcWork, scale


def load_window_state():
    try:
        with open(os.path.join(DATA_DIR, "settings.json"), encoding="utf-8") as file:
            return json.load(file).get("window") or {}
    except (OSError, ValueError, AttributeError):
        return {}


def initial_geometry():
    """
    Where the window opens: {"size", "pos" (None = centered), "min_size", "maximized"}, in pixels.
    The saved size and position are used only if the window is still fully reachable: its title bar
    on a connected monitor and the whole window inside that monitor's work area (above the taskbar).
    """
    saved = load_window_state()
    user32 = ctypes.windll.user32
    # Title bar height (the client-area position Kivy uses starts below it)
    caption = user32.GetSystemMetrics(4) + user32.GetSystemMetrics(33) + user32.GetSystemMetrics(92)

    monitor = None
    if saved.get("pos") and saved.get("size"):
        x, y = saved["pos"]
        monitor = monitor_at(x + saved["size"][0] // 2, y - caption // 2, nearest=False)
    if monitor is None:  # first start, or the saved monitor is gone: the primary monitor
        monitor = monitor_at(0, 0)
        saved.pop("pos", None)
    work, scale = monitor if monitor else (_Rect(0, 0, 1024, 768), 1.0)
    work_w = work.right - work.left
    work_h = work.bottom - work.top - caption  # room for the client area below the title bar
    margin = round(8 * scale)

    def fit(width, height):
        return (max(1, min(int(width), work_w - 2 * margin)), max(1, min(int(height), work_h - 2 * margin)))

    min_size = fit(WINDOW_MIN_SIZE[0] * scale, WINDOW_MIN_SIZE[1] * scale)
    # The size is saved in logical pixels, so it still looks the same after a display scaling change
    size = saved.get("size") or WINDOW_SIZE
    size = fit(max(size[0] * scale, min_size[0]), max(size[1] * scale, min_size[1]))

    pos = saved.get("pos")
    if pos:  # keep it on the monitor: move it back inside the work area if it sticks out
        x = min(max(pos[0], work.left + margin), work.right - margin - size[0])
        y = min(max(pos[1], work.top + caption + margin), work.bottom - margin - size[1])
        pos = (x, y)
    else:    # centered on the work area
        pos = (work.left + (work_w - size[0]) // 2, work.top + caption + (work_h - size[1]) // 2)
    return {"size": size, "pos": pos, "min_size": min_size, "maximized": bool(saved.get("maximized"))}


GEOMETRY = None
if IS_WINDOWS:
    make_dpi_aware()
    try:
        GEOMETRY = initial_geometry()
    except Exception as e:  # never keep the app from starting over the window position
        print(f"[QwenChat] window geometry failed: {e}")

from kivy.config import Config  # noqa: E402

Config.set("kivy", "exit_on_escape", "0")                   # Esc must not close the chat
Config.set("kivy", "desktop", "1")
Config.set("input", "mouse", "mouse,disable_multitouch")    # right click: no red touch dots
Config.set("graphics", "resizable", "1")
Config.set("graphics", "maxfps", "60")
if GEOMETRY:
    Config.set("graphics", "width", str(GEOMETRY["size"][0]))
    Config.set("graphics", "height", str(GEOMETRY["size"][1]))
    Config.set("graphics", "position", "custom")
    Config.set("graphics", "left", str(GEOMETRY["pos"][0]))
    Config.set("graphics", "top", str(GEOMETRY["pos"][1]))
    Config.set("graphics", "window_state", "maximized" if GEOMETRY["maximized"] else "visible")
else:
    Config.set("graphics", "width", str(WINDOW_SIZE[0]))
    Config.set("graphics", "height", str(WINDOW_SIZE[1]))

# ---- settings ---------------------------------------------------------------------------------

# Threads. Generating a token reads every weight once (~1 GB for the 1.5B model), so it is limited by
# memory bandwidth: a few threads already saturate it, more only make the PC hot and loud and slow
# down everything else. llama.cpp's threads also spin while waiting, so each one keeps a core busy.
# os.cpu_count() counts hyper-threads; half of it is about the number of real cores.
_CORES = max(1, (os.cpu_count() or 4) // 2)
N_THREADS = max(1, min(_CORES, 8))           # generation
N_THREADS_BATCH = max(1, min(_CORES, 16))    # prompt evaluation (compute-bound: all real cores)

# Memory. The weights are memory-mapped (file-backed, Windows can drop and re-read them); the KV cache
# and compute buffer are allocated once at load. After loading, the app measures itself and halves
# N_CTX if it is over the budget (e.g. a big model on a PC with little RAM).
N_CTX = 4096             # context window: ~50 messages. Every generated token reads the whole KV cache,
                         # so a fuller context is slower
N_BATCH = 512            # prompt tokens per step
MIN_CTX = 1024           # never shrink the context below this
LOW_SYSTEM_MEMORY_MB = 500   # Windows starts swapping hard below this much free memory

MAX_REPLY_TOKENS = 1024  # longest reply; also reserved in the context when trimming old messages
TRIM_TARGET = 0.6        # when the chat outgrows the context, forget old messages down to 60% of it,
                         # so this happens every ~10 messages instead of on every message
MAX_BUBBLES = 100        # messages kept on screen (each bubble holds its rendered text lines)
BUBBLES_UNDER_PRESSURE = 30  # kept when memory runs low

# Qwen2.5 recommended sampling settings
TEMPERATURE = 0.7
TOP_P = 0.8
TOP_K = 20
REPEAT_PENALTY = 1.05

SYSTEM_PROMPT = ("You are Erald Bot, a helpful assistant that runs offline on this computer. "
                 "Keep answers short and clear unless the user asks for detail. "
                 "Write math as plain text with Unicode symbols, like x² + 3x = 10, √2, ½, π, ≤, "
                 "never LaTeX.")

# Models live in a "models" folder next to QwenChat.exe / main.py (the extracted folder), so the app
# stays portable: copy or delete the whole folder and the models go with it.
BUNDLED_MODEL_DIR = os.path.join(APP_DIR, "models")
# Older versions downloaded into %LOCALAPPDATA%\QwenChat\models: still found there, never written to
# unless the app folder is read-only (e.g. extracted under Program Files).
LEGACY_MODEL_DIR = os.path.join(DATA_DIR, "models")


def _writable_dir(folder):
    try:
        os.makedirs(folder, exist_ok=True)
        probe = os.path.join(folder, ".write_test")
        with open(probe, "w") as file:
            file.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


MODEL_DIR = BUNDLED_MODEL_DIR if _writable_dir(BUNDLED_MODEL_DIR) else LEGACY_MODEL_DIR  # downloads go here
LIBRARY_NAME = "chatbridge.dll" if IS_WINDOWS else "libchatbridge.so"

def total_memory_mb():
    if IS_WINDOWS:
        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullTotalPhys // (1024 * 1024)
        return None
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024)
    except (ValueError, OSError, AttributeError):
        return None


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _ProcessMemory(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS_EX
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]


# Hard ceiling for the app (model + context + UI): 60% of the RAM, at least 2 GB
MEMORY_BUDGET_MB = max(2000, int((total_memory_mb() or 4000) * 0.6))


# ==== inference engine (no Kivy below until the UI section) ===================================

def external_model_dir():
    """Folder the Models menu downloads into: the "models" folder next to the app (see MODEL_DIR)."""
    return MODEL_DIR


def model_folders():
    """Folders searched for .gguf files: the download folder first, then the others (no duplicates)."""
    folders = []
    for folder in (MODEL_DIR, BUNDLED_MODEL_DIR, LEGACY_MODEL_DIR):
        if folder and os.path.normcase(os.path.abspath(folder)) not in \
                [os.path.normcase(os.path.abspath(f)) for f in folders]:
            folders.append(folder)
    return folders


# Models that work with this app: all use the ChatML chat format that chatml() writes.
# Q4_K_M is the best size / quality balance on a CPU. Sizes are the exact file sizes in bytes.
HF = "https://huggingface.co/"
MODELS = [
    {"name": "Qwen2.5 1.5B", "about": "Best all-round answers",
     "file": "qwen2.5-1.5b-instruct-q4_k_m.gguf", "size": 1117320736,
     "url": HF + "Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf"},
    {"name": "Qwen2.5 0.5B", "about": "2-3x faster, simpler answers",
     "file": "qwen2.5-0.5b-instruct-q4_k_m.gguf", "size": 491400032,
     "url": HF + "Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf"},
    {"name": "Qwen2.5 Math 1.5B", "about": "Math problems, step by step",
     "file": "Qwen2.5-Math-1.5B-Instruct-Q4_K_M.gguf", "size": 986048832,
     "url": HF + "bartowski/Qwen2.5-Math-1.5B-Instruct-GGUF/resolve/main/Qwen2.5-Math-1.5B-Instruct-Q4_K_M.gguf"},
    {"name": "Qwen2.5 Coder 1.5B", "about": "Programming and code",
     "file": "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf", "size": 1117320768,
     "url": HF + "Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF/resolve/main/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf"},
    {"name": "Qwen3 1.7B", "about": "Newest Qwen, smarter answers",
     "file": "Qwen3-1.7B-Q4_K_M.gguf", "size": 1107409472,
     "url": HF + "unsloth/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q4_K_M.gguf"},
    {"name": "SmolLM2 1.7B", "about": "English chat, by Hugging Face",
     "file": "smollm2-1.7b-instruct-q4_k_m.gguf", "size": 1055609536,
     "url": HF + "HuggingFaceTB/SmolLM2-1.7B-Instruct-GGUF/resolve/main/smollm2-1.7b-instruct-q4_k_m.gguf"},
    {"name": "SmolLM2 360M", "about": "Tiny and very fast, basic answers",
     "file": "SmolLM2-360M-Instruct-Q4_K_M.gguf", "size": 270590880,
     "url": HF + "bartowski/SmolLM2-360M-Instruct-GGUF/resolve/main/SmolLM2-360M-Instruct-Q4_K_M.gguf"},
]


def model_path(model):
    """Where this model's file is on the PC (downloaded, or copied next to the app), or None."""
    for folder in model_folders():
        if folder:
            path = os.path.join(folder, model["file"])
            if os.path.isfile(path):
                return path
    return None


def model_name(path):
    """Display name for a model file: the catalog name, or the file name for your own .gguf."""
    file = os.path.basename(path or "")
    return next((m["name"] for m in MODELS if m["file"] == file), file.replace(".gguf", "") or "demo")


def settings_path():
    return os.path.join(DATA_DIR, "settings.json")


def load_settings():
    try:
        with open(settings_path(), encoding="utf-8") as file:
            return json.load(file)
    except (OSError, TypeError, ValueError):
        return {}


def save_settings(**changes):
    path = settings_path()
    if not path:
        return
    settings = load_settings()
    settings.update(changes)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(settings, file)
    except OSError as e:
        print(f"[QwenChat] could not save settings: {e}")


def all_model_files():
    """Every .gguf in the model folders (see model_folders())."""
    files = []
    for folder in model_folders():
        if folder and os.path.isdir(folder):
            files += sorted(glob.glob(os.path.join(folder, "*.gguf")))
    return files


def find_model():
    """
    The model to load: the one chosen in the Models menu, else the first catalog model on the PC,
    else any other .gguf (one you copied into a models folder).
    """
    chosen = load_settings().get("model")
    files = all_model_files()
    for path in files:
        if os.path.basename(path) == chosen:
            return path
    for model in MODELS:
        path = model_path(model)
        if path:
            return path
    return files[0] if files else None


def free_storage_bytes(folder):
    try:
        while folder and not os.path.exists(folder):  # models folder not created yet: ask its parent
            folder = os.path.dirname(folder)
        return shutil.disk_usage(folder).free
    except (OSError, TypeError, ValueError):
        return None


def download_model(model, on_progress, cancel):
    """
    Downloads a model into the external folder (runs on a background thread). Resumes an interrupted
    download from its .part file. on_progress(done_bytes, total_bytes). Raises on error or cancel.
    """
    import ssl
    import urllib.request
    folder = external_model_dir()
    if not folder:
        raise RuntimeError("no storage folder available")
    os.makedirs(folder, exist_ok=True)
    final = os.path.join(folder, model["file"])
    part = final + ".part"
    done = os.path.getsize(part) if os.path.exists(part) else 0
    free = free_storage_bytes(folder)
    if free is not None and free < model["size"] - done + 200 * 1024 * 1024:
        raise RuntimeError(f"not enough storage: {model['size'] / 1e9:.1f} GB needed, "
                           f"{free / 1e9:.1f} GB free")
    try:  # the .exe has no CA certificates where Python looks for them: use certifi's
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    request = urllib.request.Request(model["url"], headers={"User-Agent": "offline-chat-windows/1.0"})
    if done:
        request.add_header("Range", f"bytes={done}-")
    with urllib.request.urlopen(request, context=context, timeout=30) as response:
        if done and response.status != 206:  # server ignored the resume: start over
            done = 0
        total = done + int(response.headers.get("Content-Length") or 0)
        with open(part, "ab" if done else "wb") as file:
            last = 0.0
            while True:
                if cancel.is_set():
                    raise RuntimeError("cancelled")
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                file.write(chunk)
                done += len(chunk)
                if time.time() - last > 0.3:
                    on_progress(done, total)
                    last = time.time()
    if done != model["size"]:
        raise RuntimeError(f"download incomplete ({done} of {model['size']} bytes) - click Download to resume")
    os.replace(part, final)
    on_progress(done, done)
    return final


def process_memory_mb():
    """
    This app's memory in MB: {"anon": heap and native buffers (not the memory-mapped model file),
    "rss": total resident}. Windows: private bytes and working set. None if it can't be measured.
    """
    if IS_WINDOWS:
        counters = _ProcessMemory()
        counters.cb = ctypes.sizeof(_ProcessMemory)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        try:
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(kernel32.GetCurrentProcess()),
                                                          ctypes.byref(counters), counters.cb)
        except (AttributeError, OSError):
            ok = False
        if not ok:
            return None
        return {"anon": counters.PrivateUsage // (1024 * 1024), "rss": counters.WorkingSetSize // (1024 * 1024)}
    fields = {"RssAnon:": "anon", "RssFile:": "file", "VmRSS:": "rss"}
    result = {}
    try:
        with open("/proc/self/status") as file:
            for line in file:
                key = line.split(":")[0] + ":"
                if key in fields:
                    result[fields[key]] = int(line.split()[1]) // 1024
    except (OSError, ValueError):
        return None
    return result if "anon" in result else None


def available_memory_mb():
    if IS_WINDOWS:
        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullAvailPhys // (1024 * 1024)
        return None
    try:
        with open("/proc/meminfo") as file:
            for line in file:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return None


def load_library():
    """
    Loads chatbridge.dll: from CHATBRIDGE_LIB, from inside the .exe, next to the .exe, or from
    libs\ (where native\build_windows.ps1 puts it) when running main.py.
    """
    candidates = [os.environ.get("CHATBRIDGE_LIB")]
    for folder in (RESOURCE_DIR, APP_DIR, os.path.join(APP_DIR, "libs")):
        path = os.path.join(folder, LIBRARY_NAME)
        if os.path.isfile(path):
            candidates.append(path)
    if not IS_WINDOWS:
        candidates.append(LIBRARY_NAME)
    errors = []
    for candidate in filter(None, candidates):
        try:
            return ctypes.CDLL(candidate)
        except OSError as e:
            errors.append(f"{candidate}: {e}")
    raise OSError("could not load " + LIBRARY_NAME + "\n" + "\n".join(errors))


def native_path(path):
    """
    A path the C library can open. Windows: a folder with non-English letters (e.g. in the user
    name) is passed as its short 8.3 name, which is plain ASCII, in case the C code opens it with the
    ANSI file functions.
    """
    if not IS_WINDOWS or path.isascii():
        return path
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer)):
        return buffer.value
    return path


class LlamaBridge:
    """
    ctypes wrapper around native/chat_bridge.cpp. Not thread-safe: ChatEngine calls it from one thread
    (except abort(), which is safe from any thread).
    """

    def __init__(self, lib=None):
        self.lib = lib or load_library()
        lib = self.lib
        lib.chat_load.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        lib.chat_load.restype = ctypes.c_int
        lib.chat_start.argtypes = [ctypes.c_char_p, ctypes.c_float, ctypes.c_float, ctypes.c_int,
                                   ctypes.c_float, ctypes.c_uint]
        lib.chat_start.restype = ctypes.c_int
        lib.chat_next.argtypes = [ctypes.c_char_p, ctypes.c_int]
        lib.chat_next.restype = ctypes.c_int
        lib.chat_count_tokens.argtypes = [ctypes.c_char_p]
        lib.chat_count_tokens.restype = ctypes.c_int
        lib.chat_cached_tokens.argtypes = []
        lib.chat_cached_tokens.restype = ctypes.c_int
        for name in ("chat_abort", "chat_reset", "chat_free"):
            getattr(lib, name).argtypes = []
            getattr(lib, name).restype = None
        for name in ("chat_error", "chat_system_info"):
            getattr(lib, name).argtypes = []
            getattr(lib, name).restype = ctypes.c_char_p
        self.buffer = ctypes.create_string_buffer(1024)

    def error(self):
        return (self.lib.chat_error() or b"").decode("utf-8", "replace")

    def system_info(self):
        return (self.lib.chat_system_info() or b"").decode("utf-8", "replace")

    def load(self, path, n_ctx, n_threads, n_threads_batch, n_batch):
        if self.lib.chat_load(native_path(path).encode("utf-8"), n_ctx, n_threads, n_threads_batch,
                              n_batch) != 0:
            raise RuntimeError(self.error())

    def count_tokens(self, text):
        return self.lib.chat_count_tokens(text.encode("utf-8"))

    def cached_tokens(self):
        return self.lib.chat_cached_tokens()

    def start(self, prompt, temp, top_p, top_k, repeat_penalty, seed):
        """Evaluates the prompt (reusing the cached part). Returns the number of new prompt tokens."""
        result = self.lib.chat_start(prompt.encode("utf-8"), temp, top_p, top_k, repeat_penalty, seed)
        if result < 0:
            raise RuntimeError(self.error())
        return result

    def next(self):
        """Returns the raw bytes of the next text piece, or None at the end of the reply."""
        n = self.lib.chat_next(self.buffer, len(self.buffer))
        if n < 0:
            raise RuntimeError(self.error())
        return self.buffer.raw[:n] if n else None

    def abort(self):
        self.lib.chat_abort()

    def reset(self):
        self.lib.chat_reset()

    def free(self):
        self.lib.chat_free()


class DemoBridge:
    """
    Stand-in for LlamaBridge when libchatbridge.so isn't available (running main.py on a PC to work on
    the UI). Streams a canned reply.
    """

    def load(self, path, n_ctx, n_threads, n_threads_batch, n_batch):
        time.sleep(0.5)

    def cached_tokens(self):
        return 0

    def system_info(self):
        return "demo mode"

    def count_tokens(self, text):
        return len(text.encode("utf-8")) // 3 + 1

    def start(self, prompt, *args):
        question = prompt.rsplit("<|im_start|>user\n", 1)[-1].split("<|im_end|>")[0]
        self.reply = iter(f"(Demo mode - no model loaded.) You said: **{question}**".encode("utf-8"))
        return self.count_tokens(prompt)

    def next(self):
        time.sleep(0.03)
        byte = next(self.reply, None)
        return None if byte is None else bytes([byte])

    def abort(self):
        pass

    def reset(self):
        pass

    def free(self):
        pass


# Qwen3 first "thinks" out loud inside <think>...</think>, which on a CPU means a minute of
# hidden text before the answer. An empty think block after "assistant" switches that off (the same
# thing Qwen's own chat template does for enable_thinking=False).
THINK_OFF = "<think>\n\n</think>\n\n"


def needs_think_off(path):
    return "qwen3" in os.path.basename(path or "").lower()


def strip_thinking(text):
    """Hides <think>...</think> text if a model writes it anyway (unfinished: hide the rest)."""
    if "<think>" not in text:
        return text
    return re.sub(r"<think>.*?(</think>\s*|$)", "", text, flags=re.S)


def chatml(messages, no_think=False):
    """
    Qwen's chat format. messages: [{"role": "user" | "assistant", "content": str}].
    no_think: Qwen3 - every assistant turn starts with an empty think block (past turns too, so the
    prompt keeps matching the tokens already in the KV cache).
    """
    think = THINK_OFF if no_think else ""
    def clean(text):
        # A user typing "<|im_end|>" must not be able to inject control tokens
        return text.replace("<|", "<​|")

    parts = [f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n"]
    for message in messages:
        prefix = think if message["role"] == "assistant" else ""
        parts.append(f"<|im_start|>{message['role']}\n{prefix}{clean(message['content'])}<|im_end|>\n")
    parts.append("<|im_start|>assistant\n" + think)
    return "".join(parts)


# ==== math: LaTeX -> readable text ============================================================
# Qwen writes math as LaTeX (\frac{1}{2}, x^2, \( ... \)), which websites render as equations.
# Kivy labels only show plain text, so the common commands become Unicode: ½ x² √ × ≤ π Σ ...

SUPERSCRIPT = dict(zip("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ"))
SUBSCRIPT = dict(zip("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎"))
FRACTIONS = {("1", "2"): "½", ("1", "3"): "⅓", ("2", "3"): "⅔", ("1", "4"): "¼", ("3", "4"): "¾",
             ("1", "5"): "⅕", ("1", "6"): "⅙", ("1", "8"): "⅛"}
LATEX_SYMBOLS = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓", "ast": "*",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡",
    "sim": "~", "propto": "∝", "ll": "≪", "gg": "≫",
    "infty": "∞", "partial": "∂", "nabla": "∇", "sum": "Σ", "prod": "∏", "int": "∫", "oint": "∮",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "implies": "⇒",
    "Leftarrow": "⇐", "Leftrightarrow": "⇔", "iff": "⇔", "leftrightarrow": "↔", "mapsto": "↦",
    "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "supseteq": "⊇",
    "cup": "∪", "cap": "∩", "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃",
    "neg": "¬", "land": "∧", "wedge": "∧", "lor": "∨", "vee": "∨", "angle": "∠", "perp": "⊥",
    "parallel": "∥", "triangle": "△", "circ": "∘", "degree": "°", "therefore": "∴", "because": "∵",
    "ldots": "…", "cdots": "⋯", "dots": "…", "prime": "′", "mid": "|",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "θ", "iota": "ι", "kappa": "κ", "lambda": "λ",
    "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ",
    "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ",
    "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "quad": "  ", "qquad": "    ", "displaystyle": "", "left": "", "right": "", "big": "", "Big": "",
    "bigg": "", "Bigg": "", "limits": "", "nolimits": "",
}
for _name in ("sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "sinh", "cosh",
              "tanh", "log", "ln", "lg", "exp", "lim", "max", "min", "sup", "inf", "det", "gcd", "lcm",
              "mod", "deg", "dim", "ker", "arg"):
    LATEX_SYMBOLS[_name] = _name
LATEX_SYMBOLS["bmod"] = "mod"
LATEX_SYMBOLS["pmod"] = "mod"

_GROUP = r"\{([^{}]*)\}"  # innermost {...}: the loop below works from the inside out
_SYMBOL_RE = re.compile(r"\\([A-Za-z]+)(?![A-Za-z])")
_MATH_RE = re.compile(r"\\\[(.*?)(?:\\\]|$)|\$\$(.*?)(?:\$\$|$)|\\\((.*?)(?:\\\)|$)|\$([^$\n]+?)\$", re.S)


def _simple(text):
    """True when text needs no parentheses in a fraction or root: 12, x, 2x, x², π."""
    return bool(re.fullmatch(r"[\w.²³¹⁰⁴-⁹′]+|.", text.strip()))


def _wrap(text):
    text = text.strip()
    return text if _simple(text) else f"({text})"


def _script(text, table, mark):
    text = text.strip()
    if text and all(char in table for char in text):
        return "".join(table[char] for char in text)
    mark = "\x05" if mark == "^" else "\x06"  # restored as ^ / _ at the end, so they aren't converted again
    return mark + (text if len(text) == 1 else f"({text})")


def _fraction(match):
    top, bottom = match.group(1).strip(), match.group(2).strip()
    if (top, bottom) in FRACTIONS:
        return FRACTIONS[(top, bottom)]
    # a/2x would read as (a/2)·x, so a denominator is bare only when it is one number or symbol
    bottom = bottom if re.fullmatch(r"[\d.]+|.", bottom) else f"({bottom})"
    return f"{_wrap(top)}/{bottom}"


def _root(match):
    degree, body = (match.group(1) or "").strip(), match.group(2)
    sign = {"": "√", "2": "√", "3": "∛", "4": "∜"}.get(degree)
    if sign is None:
        sign = _script(degree, SUPERSCRIPT, "^") + "√"
    return sign + _wrap(body)


def _bounds(match):
    name, low, high = match.group(1), match.group(2), match.group(3) or match.group(4)
    sign = {"sum": "Σ", "prod": "∏", "lim": "lim"}[name]
    if all(char in SUBSCRIPT for char in low.strip()) and (not high or all(c in SUPERSCRIPT for c in high.strip())):
        return match.group(0)  # fits as small numbers: the general rules make Σ₁ⁿ
    return f"{sign}({low.strip()}..{high.strip()}) " if high else f"{sign}({low.strip()}) "


def _convert(math, full):
    """
    Converts LaTeX commands to Unicode. full=True inside math delimiters: also ^ _ { } & and \\\\.
    Outside them only backslash commands change, so code like a^b or my_var stays as it is.
    """
    math = (math.replace(r"\{", "\x01").replace(r"\}", "\x02").replace(r"\%", "%").replace(r"\$", "$")
            .replace(r"\&", "\x03").replace(r"\#", "#").replace(r"\_", "\x04"))
    if full:
        math = re.sub(r"\\\\(\[[^\]]*\])?", "\n", math)  # \\ (optionally \\[2pt]): new line
    math = re.sub(r"\\begin\{[a-z*]+\}(\{[^{}]*\})?|\\end\{[a-z*]+\}", "", math)
    math = re.sub(r"\\[,;:!> ]", lambda m: "" if m.group(0) == r"\!" else " ", math)
    math = re.sub(r"\^\s*\{?\s*\\circ\s*\}?", "°", math)
    math = re.sub(r"\\mathbb\s*\{?\s*([RNZQC])\s*\}?", lambda m: "ℝℕℤℚℂ"["RNZQC".index(m.group(1))], math)
    if full:
        math = re.sub(r"\\(sum|prod|lim)\s*_\s*" + _GROUP + r"(?:\s*\^\s*(?:" + _GROUP + r"|(\w)))?", _bounds, math)
    math = re.sub(r"\\[dt]?frac\s*(\d)\s*(\d)", r"\\frac{\1}{\2}", math)  # \frac12
    for _ in range(12):  # nested groups: innermost first
        before = math
        math = re.sub(r"\\[dt]?frac\s*" + _GROUP + r"\s*" + _GROUP, _fraction, math)
        math = re.sub(r"\\[dt]?binom\s*" + _GROUP + r"\s*" + _GROUP, r"C(\1, \2)", math)
        math = re.sub(r"\\sqrt\s*(?:\[([^\]]*)\])?\s*" + _GROUP, _root, math)
        math = re.sub(r"\\(?:text|textbf|textit|mathrm|mathbf|mathit|mathsf|mathbb|mathcal|operatorname"
                      r"|vec|hat|bar|overline|underline|tilde|dot|ddot|mbox)\s*" + _GROUP, r"\1", math)
        math = re.sub(r"\\boxed\s*" + _GROUP, r"**\1**", math)
        if full:
            math = re.sub(r"\^\s*" + _GROUP, lambda m: _script(m.group(1), SUPERSCRIPT, "^"), math)
            math = re.sub(r"_\s*" + _GROUP, lambda m: _script(m.group(1), SUBSCRIPT, "_"), math)
        if math == before:
            break
    math = _SYMBOL_RE.sub(lambda m: LATEX_SYMBOLS.get(m.group(1), m.group(0)), math)
    if full:
        math = re.sub(r"\^\s*([^\s{}\\])", lambda m: _script(m.group(1), SUPERSCRIPT, "^"), math)
        math = re.sub(r"_\s*([^\s{}\\])", lambda m: _script(m.group(1), SUBSCRIPT, "_"), math)
        math = math.replace("&", "").replace("{", "").replace("}", "")
    if full:
        math = re.sub(r" *\n *", "\n", re.sub(r" {2,}", " ", math))
    return (math.replace("\x01", "{").replace("\x02", "}").replace("\x03", "&").replace("\x04", "_")
            .replace("\x05", "^").replace("\x06", "_"))


def _looks_like_math(text):
    return bool(re.search(r"[\\^_=+]", text)) and not re.fullmatch(r"\s*\d[\d,.]*\s*", text)


def latex_to_text(text):
    """Makes LaTeX math in a reply readable as plain text. Code blocks (```) are left untouched."""
    if "\\" not in text and "$" not in text:
        return text
    parts = text.split("```")
    for i in range(0, len(parts), 2):  # even parts are outside code blocks
        def math_region(match):
            display = match.group(1) is not None or match.group(2) is not None
            body = next(group for group in match.groups() if group is not None)
            if match.group(4) is not None and not _looks_like_math(body):
                return match.group(0)  # "$5 and $10": money, not math
            converted = _convert(body, True).strip()
            return f"\n{converted}\n" if display else converted
        part = _MATH_RE.sub(math_region, parts[i])
        part = _convert(part, False)
        parts[i] = re.sub(r"\n{3,}", "\n\n", part)
    return "```".join(parts)



def speech_text(text):
    """What to read aloud: the message without markdown marks, and code blocks left out."""
    text = re.sub(r"```.*?(```|$)", " (code) ", text, flags=re.S)
    text = re.sub(r"^\s*(#+|[-*]|\d+\.)\s+", "", text, flags=re.M)  # headings, bullets, numbers
    text = text.replace("**", "").replace("`", "").replace("__", "")
    return re.sub(r"[ \t]+", " ", text).strip()


def split_for_speech(text, limit):
    """Splits long text at sentence or line ends into pieces of at most `limit` characters."""
    pieces = []
    while len(text) > limit:
        cut = max(text.rfind(mark, 0, limit) for mark in (". ", "\n", "! ", "? "))
        cut = cut + 1 if cut > limit // 2 else limit
        pieces.append(text[:cut].strip())
        text = text[cut:]
    if text.strip():
        pieces.append(text.strip())
    return pieces or [""]


class ChatEngine:
    """
    Runs the model on one background thread so the UI never freezes.
    Callbacks are called from that thread - the UI wraps them to run on its own thread.
    """

    def __init__(self, bridge_factory=None):
        self.bridge_factory = bridge_factory or LlamaBridge
        self.bridge = None
        self.jobs = queue.Queue()
        self.stop_requested = threading.Event()
        self.busy = False
        self.ready = False
        self.model_path = None
        self.n_ctx = N_CTX
        self.no_think = False  # Qwen3: switch its thinking off (see THINK_OFF)
        self.mapped_mb = 0  # model weights that are file-backed (not copied into RAM), worst case
        self.demo = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            job()

    def load(self, path, on_ready, on_error):
        def job():
            self.ready = False
            try:
                if self.bridge is not None:  # switching models: unload the old one first, so the
                    self.bridge.free()       # memory measured below is the new model's alone
                    self.bridge = None
                    gc.collect()
                try:
                    self.bridge = self.bridge_factory()
                    self.demo = False
                except OSError as e:
                    print(f"[QwenChat] {e}")
                    self.bridge = DemoBridge()  # chatbridge.dll not built yet: UI demo
                    self.demo = True
                self.no_think = needs_think_off(path)
                memory = self._load_within_budget(path or "")
                self.model_path = path
                self.ready = True
                on_ready(self.bridge.system_info(), memory)
            except Exception as e:
                on_error(str(e))
        self.jobs.put(job)

    def _load_within_budget(self, path):
        """
        Loads the model, measures the app's real memory use and, if it is over MEMORY_BUDGET_MB,
        reloads with half the context. Returns the measurement ({"estimate_mb", "n_ctx", ...}).
        """
        model_mb = os.path.getsize(path) // (1024 * 1024) if path and os.path.exists(path) else 0
        before = process_memory_mb()
        n_ctx = N_CTX
        previous_anon = None
        while True:
            self.bridge.load(path, n_ctx, N_THREADS, N_THREADS_BATCH, N_BATCH)
            self.n_ctx = n_ctx
            # Warm-up: evaluating the system prompt now makes the first reply faster (it is cached),
            # and touches the compute buffers so the measurement below is real
            self.bridge.start(chatml([], self.no_think), TEMPERATURE, TOP_P, TOP_K, REPEAT_PENALTY, 0)
            memory = process_memory_mb()
            if memory is None:  # no /proc (desktop): nothing to measure
                return {"n_ctx": n_ctx}
            # Worst case: every page of the memory-mapped weights resident at once. But if loading
            # grew the anonymous memory by most of the model's size, llama.cpp copied the weights
            # into RAM (e.g. repacked them for ARM) and they are already counted in "anon".
            grown = memory["anon"] - (before["anon"] if before else 0)
            mapped_mb = 0 if grown > model_mb // 2 else model_mb
            self.mapped_mb = mapped_mb
            estimate = memory["anon"] + mapped_mb
            memory.update(n_ctx=n_ctx, model_mb=model_mb, estimate_mb=estimate)
            if estimate <= MEMORY_BUDGET_MB or n_ctx <= MIN_CTX:
                return memory
            # The context is a small part of the memory (56 MB of KV cache at 2048): stop shrinking
            # it once halving no longer saves anything worthwhile
            if previous_anon is not None and previous_anon - memory["anon"] < 32:
                return memory
            previous_anon = memory["anon"]
            n_ctx //= 2

    def reply_reserve(self):
        """Tokens kept free for the reply: MAX_REPLY_TOKENS, but never more than a quarter of a small context."""
        return min(MAX_REPLY_TOKENS, self.n_ctx // 4)

    def fit_history(self, messages):
        """
        Keeps the prompt inside the context, leaving room for a full reply. When it doesn't fit,
        forgets the oldest messages down to TRIM_TARGET of the space, so this happens only every few
        messages. The bridge then shifts the KV cache instead of re-reading the whole chat.
        Returns (prompt, messages that were kept).
        """
        budget = self.n_ctx - self.reply_reserve() - 16
        kept = list(messages)
        prompt = chatml(kept, self.no_think)
        if self.bridge.count_tokens(prompt) <= budget:
            return prompt, kept
        target = int(budget * TRIM_TARGET)
        while len(kept) > 1:
            kept = kept[2:] if len(kept) > 2 else kept[1:]  # drop the oldest question + answer
            prompt = chatml(kept, self.no_think)
            if self.bridge.count_tokens(prompt) <= target:
                break
        return prompt, kept

    def send(self, messages, on_text, on_done, on_error):
        """
        Generates the assistant's reply to `messages` (the last one is the user's new message).
        on_text(text so far) is called while streaming, on_done(text, stats) at the end.
        """
        self.stop_requested.clear()

        def job():
            self.busy = True
            text = ""
            try:
                prompt, kept = self.fit_history(messages)
                if self.bridge.count_tokens(prompt) > self.n_ctx - self.reply_reserve() - 16:
                    raise RuntimeError("Your message is too long for the model's context.")
                started = time.time()
                prompt_tokens = self.bridge.start(prompt, TEMPERATURE, TOP_P, TOP_K, REPEAT_PENALTY,
                                                  int(time.time()) & 0xFFFFFFFF)
                prompt_seconds = time.time() - started

                # Tokens can end in the middle of a multi-byte character (emoji, Chinese...):
                # the incremental decoder holds those bytes back until the character is complete
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                generated = 0
                gen_started = time.time()
                last_push = 0.0
                max_tokens = self.n_ctx - self.bridge.cached_tokens() - 8  # never overflow the context
                while generated < min(MAX_REPLY_TOKENS, max_tokens) and not self.stop_requested.is_set():
                    piece = self.bridge.next()
                    if piece is None:
                        break
                    generated += 1
                    text += decoder.decode(piece)
                    now = time.time()
                    if now - last_push > 0.08:  # ~12 UI updates per second is plenty
                        on_text(text)
                        last_push = now
                text += decoder.decode(b"", final=True)
                gen_seconds = max(time.time() - gen_started, 1e-6)
                stats = {
                    "prompt_tokens": prompt_tokens, "prompt_seconds": prompt_seconds,
                    "tokens": generated, "tokens_per_second": generated / gen_seconds,
                    "dropped_messages": len(messages) - len(kept),
                    "stopped": self.stop_requested.is_set(),
                    "context_used": self.bridge.cached_tokens() / self.n_ctx,
                }
                on_done(text, stats)
            except Exception as e:
                on_error(str(e), text)
            finally:
                self.busy = False
        self.jobs.put(job)

    def stop(self):
        self.stop_requested.set()
        if self.bridge is not None:
            self.bridge.abort()  # also interrupts a long prompt evaluation

    def reset(self):
        def job():
            if self.bridge is not None:
                self.bridge.reset()
        self.jobs.put(job)

    def shutdown(self):
        self.stop()

        def job():
            if self.bridge is not None:
                self.bridge.free()
        self.jobs.put(job)
        self.jobs.put(None)


# ==== UI (Kivy) =================================================================================

from kivy.app import App  # noqa: E402
from kivy.clock import Clock, mainthread  # noqa: E402
from kivy.core.clipboard import Clipboard  # noqa: E402
from kivy.core.text import Label as CoreLabel  # noqa: E402
from kivy.core.window import Window  # noqa: E402
from kivy.effects.scroll import ScrollEffect  # noqa: E402
from kivy.graphics import Color, Ellipse, Line, Quad, Rectangle, RoundedRectangle  # noqa: E402
from kivy.metrics import dp, sp  # noqa: E402
from kivy.uix.behaviors import ButtonBehavior  # noqa: E402
from kivy.uix.boxlayout import BoxLayout  # noqa: E402
from kivy.uix.gridlayout import GridLayout  # noqa: E402
from kivy.uix.label import Label  # noqa: E402
from kivy.uix.modalview import ModalView  # noqa: E402
from kivy.uix.scrollview import ScrollView  # noqa: E402
from kivy.uix.textinput import FL_IS_LINEBREAK, TextInput  # noqa: E402
from kivy.uix.widget import Widget  # noqa: E402
from kivy.utils import get_color_from_hex as hex_color  # noqa: E402

# WhatsApp-like colors
HEADER_GREEN = hex_color("#075E54")
SEND_GREEN = hex_color("#00A884")
CHAT_BACKGROUND = hex_color("#EFEAE2")
USER_BUBBLE = hex_color("#D9FDD3")
BOT_BUBBLE = hex_color("#FFFFFF")
TEXT_COLOR = hex_color("#111B21")
TIME_COLOR = hex_color("#667781")
NOTICE_BUBBLE = hex_color("#FFF3C4")

BUBBLE_MAX_WIDTH = 0.80          # fraction of the chat width...
BUBBLE_MAX_DP = 640              # ...but never wider than this in a big / maximized window
BUBBLE_PADDING_X = dp(10)


def pick_font():
    """
    Kivy's default font (Roboto) has no Chinese / Japanese / Korean glyphs, and Qwen often answers in
    Chinese when asked. Use Windows' own CJK font (it also has the math symbols Roboto lacks).
    """
    fonts = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
    for name in ("msyh.ttc", "msyh.ttf", "YuGothM.ttc", "malgun.ttf", "simsun.ttc", "seguisym.ttf"):
        path = os.path.join(fonts, name)
        if os.path.exists(path):
            return path
    for path in ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # Linux
                 "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"):
        if os.path.exists(path):
            return path
    return "Roboto"


CJK_FONT = pick_font()
CJK_RANGES = ((0x2E80, 0x9FFF), (0xAC00, 0xD7AF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF),
              (0x2070, 0x23FF))  # also small numbers, letterlike symbols, arrows and math (² ₁ ℝ → ∫ ≤)


def font_for(text):
    # Roboto (which has a real bold face) unless the text contains Chinese / Japanese / Korean or
    # math symbols that Kivy's Roboto doesn't have; the system CJK font covers both
    for char in text:
        code = ord(char)
        if code >= 0x2070 and any(low <= code <= high for low, high in CJK_RANGES):
            return CJK_FONT
    return "Roboto"


def window_handle():
    try:
        return Window.get_window_info().window
    except Exception:
        return None


class WindowsSpeech:
    """Reads text aloud with the voices built into Windows (SAPI), without blocking the UI."""

    def __init__(self):
        import win32com.client  # pywin32
        self.voice = win32com.client.Dispatch("SAPI.SpVoice")

    def speak(self, text):
        self.voice.Speak(text, 1 | 2)  # SVSFlagsAsync | SVSFPurgeBeforeSpeak

    def stop(self):
        self.voice.Speak("", 1 | 2)    # purging with an empty text stops the current speech

    def is_speaking(self):
        return self.voice.Status.RunningState == 2  # SRSEIsSpeaking


class RoundButton(ButtonBehavior, Label):
    def __init__(self, color, **kwargs):
        super().__init__(**kwargs)
        self.fill = color
        with self.canvas.before:
            self.fill_color = Color(*color)
            self.circle = Ellipse()
        self.bind(pos=self._redraw, size=self._redraw)

    def _redraw(self, *_):
        self.circle.pos, self.circle.size = self.pos, self.size

    def set_fill(self, color):
        self.fill_color.rgba = color


class MicButton(RoundButton):
    """Round button with a microphone drawn on it (Kivy's font has no microphone symbol)."""

    def __init__(self, color, **kwargs):
        super().__init__(color, **kwargs)
        with self.canvas.after:
            Color(1, 1, 1, 1)
            self.body = RoundedRectangle()
            self.cup = Line(width=dp(1.6))
            self.stem = Line(width=dp(1.6))
            self.base = Line(width=dp(1.6))

    def _redraw(self, *_):
        super()._redraw()
        cx, cy = self.center
        self.body.pos, self.body.size = (cx - dp(4.5), cy - dp(3)), (dp(9), dp(13))
        self.body.radius = [dp(4.5)]
        # U-shaped holder around the bottom of the mic (Kivy angles: 0 = top, clockwise)
        self.cup.ellipse = (cx - dp(7.5), cy - dp(5), dp(15), dp(14), 90, 270)
        self.stem.points = [cx, cy - dp(5), cx, cy - dp(9)]
        self.base.points = [cx - dp(4.5), cy - dp(9), cx + dp(4.5), cy - dp(9)]


class SpeakerButton(ButtonBehavior, Widget):
    """Small speaker icon under a bot message: grey, or green while that message is being read."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        with self.canvas:
            self.color = Color(*TIME_COLOR)
            self.box = Rectangle()
            self.cone = Quad()
            self.wave1 = Line(width=dp(1.2))
            self.wave2 = Line(width=dp(1.2))
        self.bind(pos=self._redraw, size=self._redraw)

    def _redraw(self, *_):
        cx, cy = self.center_x - dp(3), self.center_y
        self.box.pos, self.box.size = (cx - dp(6), cy - dp(2.5)), (dp(3), dp(5))
        self.cone.points = [cx - dp(3), cy - dp(2.5), cx + dp(2), cy - dp(6.5),
                            cx + dp(2), cy + dp(6.5), cx - dp(3), cy + dp(2.5)]
        # sound waves: arcs on the right (Kivy angles: 0 = top, clockwise)
        self.wave1.ellipse = (cx - dp(2), cy - dp(4), dp(8), dp(8), 50, 130)
        self.wave2.ellipse = (cx - dp(5.5), cy - dp(7.5), dp(15), dp(15), 50, 130)

    def set_active(self, active):
        self.color.rgba = SEND_GREEN if active else TIME_COLOR


class TextButton(ButtonBehavior, Label):
    """A small clickable word, like "Copy" under a message."""


class PillButton(ButtonBehavior, Label):
    """Rounded-rectangle button (RoundButton is a circle, too small for words like "Download")."""

    def __init__(self, color, **kwargs):
        kwargs.setdefault("font_size", sp(13))
        kwargs.setdefault("bold", True)
        super().__init__(**kwargs)
        with self.canvas.before:
            self.fill_color = Color(*color)
            self.shape = RoundedRectangle()
        self.bind(pos=self._redraw, size=self._redraw)

    def _redraw(self, *_):
        self.shape.pos, self.shape.size = self.pos, self.size
        self.shape.radius = [min(self.height / 2, dp(18))]

    def set_fill(self, color):
        self.fill_color.rgba = color


class ChatInput(TextInput):
    """
    Message box: Enter sends, Shift+Enter starts a new line (like WhatsApp Desktop).
    The focused box gets the keys before the window does, so the app's shortcuts are passed on here.
    """

    def __init__(self, submit, shortcut, **kwargs):
        super().__init__(**kwargs)
        self.submit = submit
        self.shortcut = shortcut

    def keyboard_on_key_down(self, window, keycode, text, modifiers):
        if keycode[1] in ("enter", "numpadenter") and not {"shift", "ctrl", "alt"} & set(modifiers):
            self.submit()
            return True
        if keycode[1] == "escape" or ("ctrl" in modifiers and keycode[1] == "n"):
            return self.shortcut(keycode[0], modifiers)  # Esc keeps the focus here, unlike Kivy's default
        return super().keyboard_on_key_down(window, keycode, text, modifiers)


def chat_scroll_view():
    """
    A ScrollView that behaves like a desktop one: mouse wheel, draggable scrollbar, no bounce.
    Dragging the content doesn't scroll, so a mouse drag selects message text instead.
    """
    return ScrollView(do_scroll_x=False, bar_width=dp(10), scroll_type=["bars"],
                      scroll_wheel_distance=dp(60), effect_cls=ScrollEffect,
                      bar_color=(0.45, 0.5, 0.53, 0.8), bar_inactive_color=(0.45, 0.5, 0.53, 0.35))


class ModelsMenu(ModalView):
    """
    Lists the models that work with the app. Each one is downloaded once (then used offline) and
    can be switched to or deleted. Downloads keep running when the menu is closed.
    """

    def __init__(self, app, **kwargs):
        # A share of the window, but never a huge dialog in a maximized window
        super().__init__(size_hint=(0.94, 0.9), size_hint_max=(dp(560), dp(700)), background="",
                         background_color=(0, 0, 0, 0), overlay_color=(0, 0, 0, 0.5), **kwargs)
        self.app = app
        root = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(10))
        with root.canvas.before:
            Color(1, 1, 1, 1)
            panel = RoundedRectangle(radius=[dp(14)])
        root.bind(pos=lambda w, _: setattr(panel, "pos", w.pos), size=lambda w, _: setattr(panel, "size", w.size))

        title = Label(text="Models", bold=True, font_size=sp(20), color=TEXT_COLOR,
                      size_hint_y=None, height=dp(30), halign="left", valign="middle")
        title.bind(size=lambda w, _: setattr(w, "text_size", w.size))
        self.info = Label(font_size=sp(12), color=TIME_COLOR, size_hint_y=None, halign="left", valign="top")
        self.info.bind(width=lambda w, _: setattr(w, "text_size", (w.width, None)),
                       texture_size=lambda w, size: setattr(w, "height", size[1]))
        self.rows = GridLayout(cols=1, size_hint_y=None, spacing=dp(8))
        self.rows.bind(minimum_height=self.rows.setter("height"))
        scroll = chat_scroll_view()
        scroll.add_widget(self.rows)
        buttons = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(10))
        folder = PillButton(hex_color("#128C7E"), text="Open folder")
        folder.bind(on_release=lambda *_: self.app.open_model_folder())
        close = PillButton(SEND_GREEN, text="Close")
        close.bind(on_release=lambda *_: self.dismiss())
        buttons.add_widget(folder)
        buttons.add_widget(close)
        for widget in (title, self.info, scroll, buttons):
            root.add_widget(widget)
        self.add_widget(root)
        self.row_widgets = {}
        self.refresh()
        self.ticker = Clock.schedule_interval(lambda dt: self.update_progress(), 0.5)
        self.bind(on_dismiss=lambda *_: self.ticker.cancel())

    def refresh(self):
        """Rebuilds every row (after a download, a switch or a delete)."""
        free = free_storage_bytes(external_model_dir())
        self.info.text = ("Download a model once, then it works offline. Only the download uses the "
                          "internet. You can also put your own .gguf file in the models folder."
                          + (f"  Free disk space: {free / 1e9:.1f} GB." if free is not None else ""))
        self.rows.clear_widgets()
        self.row_widgets = {}
        active = os.path.basename(self.app.engine.model_path or "")
        for model in MODELS:
            self.rows.add_widget(self.make_row(model, model_path(model), model["file"] == active))
        # .gguf files you copied yourself (not in the list): can be used, maybe with odd replies
        known = {m["file"] for m in MODELS}
        for path in all_model_files():
            if os.path.basename(path) not in known:
                own = {"name": os.path.basename(path), "about": "Your own file (must use the ChatML format)",
                       "file": os.path.basename(path), "size": os.path.getsize(path)}
                self.rows.add_widget(self.make_row(own, path, own["file"] == active, deletable=False))

    def make_row(self, model, path, active, deletable=True):
        row = BoxLayout(size_hint_y=None, height=dp(78), padding=(dp(12), dp(8)), spacing=dp(8))
        with row.canvas.before:
            Color(*(USER_BUBBLE if active else hex_color("#F0F2F5")))
            bg = RoundedRectangle(radius=[dp(10)])
        row.bind(pos=lambda w, _: setattr(bg, "pos", w.pos), size=lambda w, _: setattr(bg, "size", w.size))
        texts = BoxLayout(orientation="vertical")
        name = Label(text=model["name"], bold=True, font_size=sp(15), color=TEXT_COLOR, halign="left",
                     valign="bottom", shorten=True, shorten_from="right")
        about = Label(text=f"{model['about']} - {model['size'] / 1e9:.1f} GB", font_size=sp(12),
                      color=TIME_COLOR, halign="left", valign="top", shorten=True, shorten_from="right")
        for label in (name, about):
            label.bind(size=lambda w, _: setattr(w, "text_size", w.size))
            texts.add_widget(label)
        buttons = BoxLayout(orientation="vertical", size_hint_x=None, width=dp(104), spacing=dp(6))
        row.add_widget(texts)
        row.add_widget(buttons)

        def button(text, color, action):
            widget = PillButton(color, text=text)
            if action:
                widget.bind(on_release=lambda *_: action())
            buttons.add_widget(widget)
            return widget

        downloading = model["file"] in self.app.downloads
        if active:
            button("In use", hex_color("#8696A0"), None)
        elif path:
            button("Use", SEND_GREEN, lambda: self.app.switch_model(path))
            if deletable and any(os.path.normcase(path).startswith(os.path.normcase(f)) for f in model_folders()):
                button("Delete", hex_color("#D9534F"), lambda: self.app.delete_model(path))
        elif downloading:
            self.row_widgets[model["file"]] = button("...", hex_color("#8696A0"), None)
            button("Cancel", hex_color("#D9534F"), lambda: self.app.cancel_download(model))
        elif "url" in model:
            button("Download", hex_color("#128C7E"), lambda: self.app.start_download(model))
        return row

    def update_progress(self):
        for file, widget in self.row_widgets.items():
            state = self.app.downloads.get(file)
            if state:
                done, total = state["progress"]
                widget.text = f"{done / total:.0%}" if total else "starting"


def plain_text(text):
    """What a message shows: **bold** marks removed (selectable text can't render bold)."""
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)


class SelectableText(TextInput):
    """
    Message text you can select with the mouse and copy: drag to select, double-click selects a
    word, triple-click a line, Ctrl+A everything in the message, Ctrl+C or right-click copies.
    It is a read-only, borderless TextInput that looks like a plain label.
    """

    def __init__(self, **kwargs):
        super().__init__(readonly=True, multiline=True, do_wrap=True, write_tab=False,
                         background_normal="", background_active="", background_color=(0, 0, 0, 0),
                         foreground_color=TEXT_COLOR, cursor_color=(0, 0, 0, 0), cursor_width=0,
                         selection_color=(0.13, 0.59, 0.95, 0.35),
                         padding=(0, 0, dp(2), 0),
                         use_bubble=False, use_handles=False, scroll_from_swipe=False, **kwargs)

    def _split_smart(self, text):
        """
        Kivy wraps by adding up the widths of single words, which comes out a few pixels short on
        long lines, so the end of a line got cut off. Re-measure every wrapped line as a whole and
        move its last word to the next line while it is too wide.
        """
        lines, flags = super()._split_smart(text)
        width = self.width - self.padding[0] - self.padding[2]
        if not (self.multiline and self.do_wrap) or width < 1:
            return lines, flags
        label = self._label_cached
        tab = " " * self.tab_width

        def too_wide(line):
            return label.get_extents(line.rstrip(" ").replace("\t", tab))[0] > width

        lines, flags = list(lines), list(flags)
        i = 0
        while i < len(lines):
            cut = lines[i].rstrip(" ").rfind(" ")
            if cut > 0 and too_wide(lines[i]):
                tail = lines[i][cut + 1:]
                lines[i] = lines[i][:cut + 1]
                if i + 1 < len(lines) and not flags[i + 1] & FL_IS_LINEBREAK:
                    lines[i + 1] = tail + lines[i + 1]  # same paragraph: the word starts the next line
                else:
                    lines.insert(i + 1, tail)          # last line of a paragraph: add a line
                    flags.insert(i + 1, 0)
                continue  # check this line again
            i += 1
        return lines, flags

    def on_touch_down(self, touch):
        if not self.collide_point(*touch.pos):
            return False
        if touch.button and touch.button.startswith("scroll"):
            return False  # the mouse wheel scrolls the chat, not the message
        if touch.button == "right":  # right-click copies the selection
            if self.selection_text:
                Clipboard.copy(self.selection_text)
                App.get_running_app().screen.show_status_hint("copied")
            return True
        return super().on_touch_down(touch)

    def keyboard_on_textinput(self, window, text):
        # Typing while a message is selected goes to the message box, as you'd expect
        box = App.get_running_app().screen.input
        box.focus = True
        box.insert_text(text)


class Bubble(BoxLayout):
    """
    One chat message: a rounded bubble, right-aligned for the user, left-aligned for the bot,
    with the time in the corner. The text can be selected and copied; bot replies also get a Copy
    button (whole message) and a speaker (read aloud).
    """

    SPEAKER_WIDTH = dp(30)
    COPY_WIDTH = dp(40)
    TEXT_SLACK = dp(6)  # the text box's right margin (2) + rounding: one-line texts never wrap

    def __init__(self, text, mine, notice=False, **kwargs):
        super().__init__(size_hint_y=None, padding=(dp(10), dp(2)), **kwargs)
        self.mine = mine
        self.raw_text = ""
        self.layout_key = None
        self.footer_height = 0
        self.box = BoxLayout(orientation="vertical", size_hint=(None, None),
                             padding=(BUBBLE_PADDING_X, dp(6), BUBBLE_PADDING_X, dp(4)), spacing=dp(1))
        with self.box.canvas.before:
            Color(*(NOTICE_BUBBLE if notice else USER_BUBBLE if mine else BOT_BUBBLE))
            self.background = RoundedRectangle(radius=[dp(10)])
        self.box.bind(pos=self._redraw, size=self._redraw)

        self.label = SelectableText(font_size=sp(15), size_hint=(None, None))
        self.label.bind(minimum_height=self._fit_height)
        self.time = Label(text=time.strftime("%H:%M"), color=TIME_COLOR, font_size=sp(10),
                          size_hint=(None, None), halign="right", valign="bottom")
        self.time.texture_update()
        self.time_width = self.time.texture_size[0]  # measured once - text_size changes it later
        self.footer = BoxLayout(size_hint=(None, None))
        self.speaker = None
        self.copy = None
        if not mine and not notice:  # bot replies can be copied and read aloud
            self.copy = TextButton(text="Copy", font_size=sp(11), color=TIME_COLOR,
                                   size_hint=(None, 1), width=self.COPY_WIDTH)
            self.copy.bind(on_release=lambda *_: self.copy_text())
            self.speaker = SpeakerButton(size_hint=(None, 1), width=self.SPEAKER_WIDTH)
            self.speaker.bind(on_release=lambda *_: App.get_running_app().toggle_speech(self))
            self.footer.add_widget(self.copy)
            self.footer.add_widget(self.speaker)
        self.footer.add_widget(self.time)
        self.box.add_widget(self.label)
        self.box.add_widget(self.footer)
        if mine:
            self.add_widget(Widget())
            self.add_widget(self.box)
        else:
            self.add_widget(self.box)
            self.add_widget(Widget())
        self.bind(width=lambda *_: self.relayout())
        self.set_text(text)

    def _redraw(self, *_):
        self.background.pos, self.background.size = self.box.pos, self.box.size

    def copy_text(self):
        Clipboard.copy(self.raw_text)
        self.copy.text = "Copied"
        Clock.schedule_once(lambda dt: setattr(self.copy, "text", "Copy"), 1.5)

    def set_text(self, text):
        text = plain_text(latex_to_text(strip_thinking(text)).strip())
        self.raw_text = text
        self.label.font_name = font_for(text)
        self.label.text = text if text else "..."
        self.relayout()

    def natural_width(self, text):
        # Width of the longest line, measured without rendering (cheap even for long replies)
        measure = CoreLabel(font_size=self.label.font_size, font_name=self.label.font_name)
        widest = 0
        for line in text.split("\n")[:40]:
            widest = max(widest, measure.get_extents(line[:300].replace("\t", "    "))[0])
        return widest

    def relayout(self):
        # The bubble is at most 80% of the chat width (and at most BUBBLE_MAX_DP), and always fits
        # the row it sits in. Resizing the window re-wraps the text to the new width.
        row_width = self.width if self.width > 100 else Window.width
        inner_row = row_width - self.padding[0] - self.padding[2]
        bubble_max = min(row_width * BUBBLE_MAX_WIDTH, dp(BUBBLE_MAX_DP), inner_row)
        inner_max = max(dp(40), bubble_max - BUBBLE_PADDING_X * 2)
        text_width = min(self.natural_width(self.label.text) + self.TEXT_SLACK, inner_max)
        key = (self.label.text, self.label.font_name, round(text_width))
        if key == self.layout_key:  # same text, same wrap width: nothing to redo (keeps resizing smooth)
            return
        self.layout_key = key
        self.label.width = text_width  # the text box wraps at its width (long words are broken too)
        tools_width = (self.SPEAKER_WIDTH + self.COPY_WIDTH) if self.speaker else 0
        bubble_width = max(text_width, self.time_width + dp(4) + tools_width)
        self.footer_height = dp(24) if self.speaker else self.time.texture_size[1]  # room to click
        self.footer.size = (bubble_width, self.footer_height)
        self.time.size = (bubble_width - tools_width, self.footer_height)
        self.time.text_size = self.time.size
        self.box.width = bubble_width + BUBBLE_PADDING_X * 2
        self._fit_height()

    def _fit_height(self, *_):
        # The text box knows its height only after it has wrapped the text (minimum_height)
        self.label.height = self.label.minimum_height
        self.box.height = self.label.height + self.footer_height + dp(11)
        self.height = self.box.height + dp(4)


class ChatScreen(BoxLayout):
    """
    Header / messages / input bar stacked vertically. Everything is placed by the layouts
    (size_hint) - no absolute positions - so any window size works: the header and the input bar
    keep their height and the message list takes the rest.
    """

    def __init__(self, app, **kwargs):
        super().__init__(orientation="vertical", **kwargs)
        self.app = app

        # Header: avatar, name + status, "Models", "New chat"
        header = BoxLayout(size_hint_y=None, height=dp(58), padding=(dp(10), dp(8)), spacing=dp(10))
        with header.canvas.before:
            Color(*HEADER_GREEN)
            header_bg = Rectangle()
        header.bind(pos=lambda w, _: setattr(header_bg, "pos", w.pos),
                    size=lambda w, _: setattr(header_bg, "size", w.size))
        avatar = RoundButton(hex_color("#25D366"), text="E", bold=True, font_size=sp(20),
                             size_hint=(None, None), size=(dp(42), dp(42)))
        avatar.bind(on_release=lambda *_: self.app.open_models())
        titles = BoxLayout(orientation="vertical")
        self.title = Label(text="Erald Bot", bold=True, font_size=sp(17), halign="left", valign="bottom",
                           shorten=True, shorten_from="right")
        self.status = Label(text="starting...", font_size=sp(12), color=(1, 1, 1, 0.8),
                            halign="left", valign="top", shorten=True, shorten_from="right")
        for label in (self.title, self.status):
            label.bind(size=lambda w, _: setattr(w, "text_size", w.size))
            titles.add_widget(label)
        new_chat = RoundButton(hex_color("#128C7E"), text="New", font_size=sp(13),
                               size_hint=(None, None), size=(dp(42), dp(42)))
        new_chat.bind(on_release=lambda *_: self.app.new_chat())
        models = PillButton(hex_color("#128C7E"), text="Models", size_hint=(None, None), size=(dp(72), dp(42)))
        models.bind(on_release=lambda *_: self.app.open_models())
        header.add_widget(avatar)
        header.add_widget(titles)
        header.add_widget(models)
        header.add_widget(new_chat)

        # Messages
        self.scroll = chat_scroll_view()
        with self.scroll.canvas.before:
            Color(*CHAT_BACKGROUND)
            chat_bg = Rectangle()
        self.scroll.bind(pos=lambda w, _: setattr(chat_bg, "pos", w.pos),
                         size=lambda w, _: setattr(chat_bg, "size", w.size))
        self.messages = GridLayout(cols=1, size_hint_y=None, spacing=dp(2), padding=(0, dp(8)))
        self.messages.bind(minimum_height=self.messages.setter("height"))
        # Column = empty space + messages, at least as tall as the view: a short chat sits at the
        # bottom, right above the input bar (like WhatsApp), and scroll_y = 0 is always the newest message
        self.column = BoxLayout(orientation="vertical", size_hint_y=None)
        self.column.add_widget(Widget())
        self.column.add_widget(self.messages)
        self.messages.bind(height=self._fit_column)
        self.scroll.bind(height=self._fit_column)
        self.scroll.add_widget(self.column)

        # Input bar
        bar = BoxLayout(size_hint_y=None, height=dp(60), padding=(dp(8), dp(8)), spacing=dp(8))
        with bar.canvas.before:
            Color(*CHAT_BACKGROUND)
            bar_bg = Rectangle()
        bar.bind(pos=lambda w, _: setattr(bar_bg, "pos", w.pos),
                 size=lambda w, _: setattr(bar_bg, "size", w.size))
        self.input = ChatInput(self.app.send_or_stop, self.app.shortcut, hint_text="Message  (Enter sends, Shift+Enter new line)",
                               multiline=True, font_size=sp(15), font_name=CJK_FONT,
                               background_normal="", background_active="", background_color=(1, 1, 1, 1),
                               foreground_color=TEXT_COLOR, cursor_color=SEND_GREEN,
                               padding=(dp(14), dp(11)))
        self.input.bind(minimum_height=self._grow_input)
        self.send_button = RoundButton(SEND_GREEN, text="Send", bold=True, font_size=sp(13),
                                       size_hint=(None, None), size=(dp(44), dp(44)))
        self.send_button.bind(on_release=lambda *_: self.app.send_or_stop())
        self.mic_button = MicButton(hex_color("#128C7E"), size_hint=(None, None), size=(dp(44), dp(44)))
        self.mic_button.bind(on_release=lambda *_: self.app.start_voice_input())
        bar.add_widget(self.input)
        bar.add_widget(self.mic_button)
        bar.add_widget(self.send_button)
        self.bar = bar

        self.add_widget(header)
        self.add_widget(self.scroll)
        self.add_widget(bar)

    def _fit_column(self, *_):
        self.column.height = max(self.messages.height, self.scroll.height)

    def _grow_input(self, *_):
        # Grow with the text up to ~5 lines
        height = min(max(dp(44), self.input.minimum_height), dp(130))
        self.bar.height = height + dp(16)

    def add_bubble(self, text, mine, notice=False):
        bubble = Bubble(text, mine, notice=notice, width=self.messages.width)
        self.messages.add_widget(bubble)
        self.prune_bubbles(MAX_BUBBLES)
        self.scroll_to_end()
        return bubble

    def prune_bubbles(self, keep):
        """Removes the oldest bubbles from the screen (each one holds a rendered text texture)."""
        removed = 0
        while len(self.messages.children) > keep:
            self.messages.remove_widget(self.messages.children[-1])  # children[-1] is the oldest
            removed += 1
        return removed

    def near_end(self):
        return self.scroll.scroll_y <= 0.02 or self.column.height <= self.scroll.height + 1

    def scroll_to_end(self, *_):
        """Shows the newest message. Runs again after the next layout pass, once new heights are known."""
        def scroll(dt):
            self.scroll.scroll_y = 0
        Clock.schedule_once(scroll, 0)
        Clock.schedule_once(scroll, 0.05)

    def focus_input(self, *_):
        Clock.schedule_once(lambda dt: setattr(self.input, "focus", True), 0)

    def show_status_hint(self, text):
        """Shows a word like "copied" in the status line for a moment."""
        previous = self.status.text
        self.status.text = text
        Clock.schedule_once(lambda dt: self.status.text == text and setattr(self.status, "text", previous), 1.2)

    def set_generating(self, generating):
        self.send_button.text = "Stop" if generating else "Send"
        self.send_button.set_fill(hex_color("#D9534F") if generating else SEND_GREEN)


class QwenChatApp(App):
    title = "Erald Bot"
    icon = os.path.join(RESOURCE_DIR, "assets", "icon.png")

    def build(self):
        Window.clearcolor = CHAT_BACKGROUND
        self.engine = ChatEngine()
        self.history = []        # [{"role", "content"}] sent to the model
        self.reply_bubble = None
        self.generating = False
        self.clear_after_reply = False
        self.downloads = {}      # file -> {"progress": (done, total), "cancel": Event}
        self.tts = None          # WindowsSpeech, created on first use
        self.speaking_bubble = None
        self.speech_watch = None
        self.speech_started = 0.0
        self.voice_hint_shown = False
        self.menu = None
        self.normal_geometry = None  # last size / position while not maximized or minimized
        self.screen = ChatScreen(self)
        if GEOMETRY:
            # Can't be dragged smaller than this: the header buttons and input bar stay usable
            Window.minimum_width, Window.minimum_height = GEOMETRY["min_size"]
        Window.bind(on_resize=self.track_geometry, on_move=self.track_geometry,
                    on_key_down=self.on_key_down)
        Clock.schedule_once(self.track_geometry, 0.5)
        Clock.schedule_once(self.screen.focus_input, 0.2)
        Clock.schedule_once(lambda dt: self.load_model(), 0.3)
        Clock.schedule_interval(lambda dt: self.check_memory(), 30)
        return self.screen

    # ---- window ------------------------------------------------------------------------------

    def track_geometry(self, *_):
        """Remembers the window's normal size and position (not while it is maximized or minimized)."""
        hwnd = window_handle()
        if not IS_WINDOWS or not hwnd:
            return
        user32 = ctypes.windll.user32
        if user32.IsIconic(ctypes.c_void_p(hwnd)) or user32.IsZoomed(ctypes.c_void_p(hwnd)):
            return
        scale = (user32.GetDpiForWindow(ctypes.c_void_p(hwnd)) or 96) / 96
        width, height = Window.system_size
        self.normal_geometry = {"size": [round(width / scale), round(height / scale)],  # logical pixels
                                "pos": [int(Window.left), int(Window.top)]}

    def save_geometry(self):
        hwnd = window_handle()
        if not IS_WINDOWS or not hwnd:
            return
        # Never un-maximized during this run: keep the normal size saved last time
        state = dict(self.normal_geometry or load_window_state())
        state["maximized"] = bool(ctypes.windll.user32.IsZoomed(ctypes.c_void_p(hwnd)))
        save_settings(window=state)

    def on_key_down(self, window, key, scancode, text, modifiers):
        return self.shortcut(key, modifiers)

    def shortcut(self, key, modifiers):
        """Esc stops the reply, Ctrl+N starts a new chat. Returns True when the key was used."""
        if self.menu is not None:
            return False  # the Models menu is open: Esc closes it (ModalView does that itself)
        if key == 27:
            if self.generating:
                self.engine.stop()
            return True
        if "ctrl" in modifiers and key == ord("n"):
            self.new_chat()
            return True
        return False

    # ---- model -------------------------------------------------------------------------------

    def load_model(self):
        path = find_model()
        if path is None and library_available():
            self.screen.status.text = "no model - click Models"
            self.screen.add_bubble("No model on this PC yet. Click Models and download one.", False, notice=True)
            self.open_models()
            return
        self.start_loading(path)

    def start_loading(self, path):
        size_mb = os.path.getsize(path) // (1024 * 1024) if path else 0
        free_mb = available_memory_mb()
        if free_mb is not None and size_mb and free_mb < size_mb + 600:
            self.screen.add_bubble(
                f"Low memory: {free_mb} MB free, the model needs about {size_mb + 400} MB. "
                "Close other programs if the chat is very slow.", False, notice=True)
        self.screen.status.text = f"loading {model_name(path)}..."
        self.engine.load(path, self.on_model_ready, self.on_model_error)

    @mainthread
    def on_model_ready(self, system_info, memory):
        name = os.path.basename(self.engine.model_path or "demo")
        self.screen.status.text = self.online_status()
        if self.menu:
            self.menu.refresh()
        print(f"[QwenChat] model {name} loaded - context {self.engine.n_ctx} - threads {N_THREADS}/"
              f"{N_THREADS_BATCH} - memory {memory} - {system_info}")
        if self.engine.demo:
            self.screen.status.text = "demo mode - no chatbridge.dll"
            self.screen.add_bubble(
                f"Demo mode: {LIBRARY_NAME} was not found, so replies are only echoes. Build it with "
                "native\\build_windows.ps1, or use the QwenChat.exe from the GitHub build.", False, notice=True)
        elif self.engine.n_ctx < N_CTX:
            self.screen.add_bubble(
                f"To stay under {MEMORY_BUDGET_MB} MB of memory, the context was reduced to "
                f"{self.engine.n_ctx} tokens, so the chat remembers fewer messages.", False, notice=True)
        elif memory.get("estimate_mb", 0) > MEMORY_BUDGET_MB:
            self.screen.add_bubble(
                f"This model needs about {memory['estimate_mb']} MB, more than the {MEMORY_BUDGET_MB} MB "
                "budget. The PC may get slow - a smaller model is recommended.", False, notice=True)

    def check_memory(self):
        """
        Frees what the app can free when memory gets tight: old bubbles (text textures) and Python
        garbage. The model's own memory is fixed and memory-mapped, so it isn't the part that grows.
        """
        memory = process_memory_mb()
        free = available_memory_mb()
        own = memory["anon"] + self.engine.mapped_mb if memory else 0
        if (memory and own > MEMORY_BUDGET_MB) or (free is not None and free < LOW_SYSTEM_MEMORY_MB):
            removed = self.screen.prune_bubbles(BUBBLES_UNDER_PRESSURE)
            gc.collect()
            print(f"[QwenChat] low memory (app {own} MB, system free {free} MB) - "
                  f"removed {removed} old bubbles")

    # ---- reading replies aloud ---------------------------------------------------------------

    def toggle_speech(self, bubble):
        """Speaker button: reads the message aloud, or stops if that message is being read."""
        if self.speaking_bubble is bubble:
            self.stop_speech()
            return
        self.stop_speech()
        text = speech_text(bubble.raw_text)
        if not text:
            return
        try:
            if self.tts is None:
                self.tts = WindowsSpeech()
            self.tts.speak(text)
        except Exception as e:
            print(f"[QwenChat] text to speech failed: {e}")
            self.screen.add_bubble(
                "Reading aloud isn't available. Check that a voice is installed in Windows Settings > "
                "Time & language > Speech.", False, notice=True)
            return
        self.speaking_bubble = bubble
        bubble.speaker.set_active(True)
        self.speech_started = time.time()
        if self.speech_watch is None:
            self.speech_watch = Clock.schedule_interval(lambda dt: self._watch_speech(), 0.5)

    def _watch_speech(self):
        # Turns the speaker icon grey again when Windows has finished reading
        try:
            done = time.time() - self.speech_started > 1 and not self.tts.is_speaking()
        except Exception:
            done = True
        if self.speaking_bubble is None or done:
            self.stop_speech()

    def stop_speech(self):
        if self.tts is not None:
            try:
                self.tts.stop()
            except Exception:
                pass
        if self.speaking_bubble is not None and self.speaking_bubble.speaker:
            self.speaking_bubble.speaker.set_active(False)
        self.speaking_bubble = None
        if self.speech_watch is not None:
            self.speech_watch.cancel()
            self.speech_watch = None

    # ---- voice typing ------------------------------------------------------------------------

    def start_voice_input(self):
        """
        Opens Windows' own voice typing (the same as pressing Win+H) with the cursor in the message
        box, so the recognized words are typed there, to check before sending.
        """
        if not IS_WINDOWS:
            self.screen.add_bubble("Voice typing works on Windows only.", False, notice=True)
            return
        self.screen.input.focus = True

        def press_win_h(dt):
            keybd_event = ctypes.windll.user32.keybd_event
            keybd_event(0x5B, 0, 0, 0)  # left Windows key down
            keybd_event(0x48, 0, 0, 0)  # H down
            keybd_event(0x48, 0, 2, 0)  # H up
            keybd_event(0x5B, 0, 2, 0)  # Windows key up
        Clock.schedule_once(press_win_h, 0.1)  # after the message box has the keyboard focus
        if not self.voice_hint_shown:
            self.voice_hint_shown = True
            self.screen.add_bubble(
                "Windows voice typing opened (also Win+H). If nothing appears, turn on "
                "\"Online speech recognition\" in Windows Settings > Privacy > Speech.", False, notice=True)

    def online_status(self):
        return f"online - {model_name(self.engine.model_path)}"

    # ---- models menu -------------------------------------------------------------------------

    def open_models(self):
        if self.menu is None:
            self.menu = ModelsMenu(self)
            self.menu.bind(on_dismiss=lambda *_: setattr(self, "menu", None))
            self.menu.open()

    def open_model_folder(self):
        os.makedirs(external_model_dir(), exist_ok=True)
        try:
            os.startfile(external_model_dir())  # Windows Explorer
        except (AttributeError, OSError) as e:
            self.screen.add_bubble(f"Models folder: {external_model_dir()} ({e})", False, notice=True)

    def switch_model(self, path):
        if self.menu:
            self.menu.dismiss()
        if path == self.engine.model_path and self.engine.ready:
            return
        save_settings(model=os.path.basename(path))
        if self.generating:
            self.engine.stop()
        self.new_chat()  # another model can't continue this chat's cached tokens
        self.screen.add_bubble(f"Switched to {model_name(path)}.", False, notice=True)
        self.start_loading(path)

    def delete_model(self, path):
        if path == self.engine.model_path:
            return
        try:
            os.remove(path)
        except OSError as e:
            self.screen.add_bubble(f"Could not delete {model_name(path)}: {e}", False, notice=True)
        if self.menu:
            self.menu.refresh()

    def start_download(self, model):
        if model["file"] in self.downloads:
            return
        cancel = threading.Event()
        state = {"progress": (0, model["size"]), "cancel": cancel}
        self.downloads[model["file"]] = state

        def progress(done, total):
            state["progress"] = (done, total)

        def run():
            try:
                path = download_model(model, progress, cancel)
                self.download_finished(model, path, None)
            except Exception as e:
                self.download_finished(model, None, str(e))
        threading.Thread(target=run, daemon=True).start()
        if self.menu:
            self.menu.refresh()

    def cancel_download(self, model):
        state = self.downloads.get(model["file"])
        if state:
            state["cancel"].set()

    @mainthread
    def download_finished(self, model, path, error):
        self.downloads.pop(model["file"], None)
        if error == "cancelled":
            pass
        elif error:
            self.screen.add_bubble(f"Download of {model['name']} failed: {error}", False, notice=True)
        else:
            self.screen.add_bubble(f"{model['name']} downloaded. Click Models > Use to switch to it.",
                                   False, notice=True)
            if not self.engine.model_path:  # first model on a PC that had none
                self.switch_model(path)
        if self.menu:
            self.menu.refresh()

    @mainthread
    def on_model_error(self, error):
        self.screen.status.text = "model failed to load"
        self.screen.add_bubble(f"Could not load the model:\n{error}", False, notice=True)

    # ---- chat --------------------------------------------------------------------------------

    def send_or_stop(self):
        if self.generating:
            self.engine.stop()
            return
        text = self.screen.input.text.strip()
        if not text:
            return
        if not self.engine.ready:
            self.screen.add_bubble("The model is still loading - try again in a moment.", False, notice=True)
            return
        self.screen.input.text = ""
        self.screen.focus_input()  # clicking Send would otherwise leave the message box without focus
        self.screen.add_bubble(text, True)
        self.history.append({"role": "user", "content": text})
        self.reply_bubble = self.screen.add_bubble("", False)
        self.generating = True
        self.screen.set_generating(True)
        self.screen.status.text = "typing..."
        self.engine.send(list(self.history), self.on_text, self.on_done, self.on_error)

    @mainthread
    def on_text(self, text):
        if self.reply_bubble is not None:
            follow = self.screen.near_end()  # scrolled up to read something? then don't jump down
            self.reply_bubble.set_text(text.strip())
            if follow:
                self.screen.scroll_to_end()

    @mainthread
    def on_done(self, text, stats):
        # Messages the model could no longer fit are forgotten for good, so the history (and the
        # prompt built from it) never grows past the context
        del self.history[:stats["dropped_messages"]]
        self.finish_reply(text)
        status = f"{stats['tokens_per_second']:.1f} tokens/s - context {stats['context_used']:.0%}"
        if stats["dropped_messages"]:
            status += " - oldest messages forgotten"
        self.screen.status.text = status
        print(f"[QwenChat] {stats}")
        self.check_memory()

    @mainthread
    def on_error(self, error, partial_text):
        self.finish_reply(partial_text)
        if not error.startswith("stopped"):
            self.screen.add_bubble(f"Error: {error}", False, notice=True)
        self.screen.status.text = self.online_status()

    def finish_reply(self, text):
        follow = self.screen.near_end()
        if text.strip():
            # Keep the exact text the model produced, so the next prompt matches the cached tokens
            self.history.append({"role": "assistant", "content": text})
            self.reply_bubble.set_text(text.strip())
        else:
            self.history.pop()  # nothing came back - forget the question too
            self.screen.messages.remove_widget(self.reply_bubble)
        self.reply_bubble = None
        self.generating = False
        self.screen.set_generating(False)
        if follow:
            self.screen.scroll_to_end()
        if self.clear_after_reply:
            self.new_chat()

    def new_chat(self):
        if self.generating:
            self.clear_after_reply = True  # finish_reply() clears once the model has stopped
            self.engine.stop()
            return
        self.clear_after_reply = False
        self.stop_speech()
        self.history = []
        self.screen.messages.clear_widgets()
        self.engine.reset()  # clears the KV cache
        gc.collect()         # and releases the old bubbles' textures
        self.screen.status.text = self.online_status() if self.engine.ready else self.screen.status.text
        self.screen.focus_input()

    # ---- lifecycle ---------------------------------------------------------------------------

    def on_request_close(self, *_):
        self.save_geometry()  # while the window still exists
        return False

    def on_start(self):
        Window.bind(on_request_close=self.on_request_close)

    def on_stop(self):
        self.save_geometry()
        self.stop_speech()
        self.engine.shutdown()


def library_available():
    try:
        load_library()
        return True
    except OSError:
        return False


if __name__ == "__main__":
    QwenChatApp().run()
