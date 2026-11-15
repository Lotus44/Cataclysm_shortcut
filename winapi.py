# -*- coding: utf-8 -*-
"""Cataclysm Shortcut 的 Win32 底层封装。

这里集中所有 ctypes 调用：全局热键、进程/窗口探测、按键投递、以及**零依赖的托盘图标**
（直接用 Shell_NotifyIconW + 自建消息循环，不需要 pystray / pywin32）。

设计原则：本模块只依赖标准库；不可用时（非 Windows）所有函数安静降级。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import os
import subprocess
import threading
import time
from typing import Any, Callable, Optional

IS_WINDOWS = os.name == "nt"

USER32 = ctypes.WinDLL("user32", use_last_error=True) if IS_WINDOWS else None
KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True) if IS_WINDOWS else None
SHELL32 = ctypes.WinDLL("shell32", use_last_error=True) if IS_WINDOWS else None
GDI32 = ctypes.WinDLL("gdi32", use_last_error=True) if IS_WINDOWS else None
# ImmGetDefaultIMEWnd 属于 imm32，不在 user32；必须先加载 imm32，user32 才能解析到它
IMM32 = ctypes.WinDLL("imm32", use_last_error=True) if IS_WINDOWS else None

# ---------------------------------------------------------------- 常量

WM_HOTKEY = 0x0312
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_QUIT = 0x0012
WM_TRAYICON = 0x0400 + 1  # WM_APP + 1

VK_ESCAPE = 0x1B
VK_RETURN = 0x0D
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

ERROR_ALREADY_EXISTS = 183
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

GW_OWNER = 4
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002

MOD_MAP = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
    "cmd": MOD_WIN,
}

NAMED_VK = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "esc": VK_ESCAPE, "escape": VK_ESCAPE,
    "insert": 0x2D, "ins": 0x2D, "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22, "backspace": 0x08,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
}

# 托盘
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001
NIIF_WARNING = 0x00000002
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040
IDI_APPLICATION = 32512


# ---------------------------------------------------------------- 热键解析


def parse_hotkey(spec: str) -> tuple[int, int]:
    """``'ctrl+alt+s'`` -> ``(modifiers, virtual_key)``；无法解析时抛 ValueError。"""
    mods = 0
    key = ""
    for part in str(spec).lower().split("+"):
        part = part.strip()
        if not part:
            continue
        if part in MOD_MAP:
            mods |= MOD_MAP[part]
        else:
            key = part
    if not key:
        raise ValueError(f"热键缺少主键: {spec!r}")
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif key in NAMED_VK:
        vk = NAMED_VK[key]
    else:
        raise ValueError(f"看不懂的按键: {key!r}")
    return mods, vk


def format_hotkey(spec: str) -> str:
    """``'ctrl+alt+f5'`` -> ``'Ctrl+Alt+F5'``（仅用于显示）。"""
    parts = []
    for part in str(spec).split("+"):
        part = part.strip()
        if not part:
            continue
        low = part.lower()
        if low in ("ctrl", "control"):
            parts.append("Ctrl")
        elif low == "alt":
            parts.append("Alt")
        elif low == "shift":
            parts.append("Shift")
        elif low in ("win", "super", "cmd"):
            parts.append("Win")
        else:
            parts.append(part.upper() if len(part) <= 3 else part.capitalize())
    return "+".join(parts)


def hotkey_candidates(spec: Any) -> list[str]:
    """把配置值统一成候选列表（允许写单个字符串）。"""
    if isinstance(spec, str):
        return [spec] if spec.strip() else []
    if isinstance(spec, (list, tuple)):
        return [str(s) for s in spec if isinstance(s, str) and s.strip()]
    return []


def probe_hotkey(spec: str) -> tuple[bool, str]:
    """试探某个组合键当前能否注册（注册成功后立刻注销）。"""
    if not IS_WINDOWS or USER32 is None:
        return False, "仅支持 Windows"
    try:
        mods, vk = parse_hotkey(spec)
    except ValueError as exc:
        return False, str(exc)
    hid = 0xD000 + (abs(hash(spec)) % 0x0FFF)
    if USER32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
        USER32.UnregisterHotKey(None, hid)
        return True, "可用"
    err = ctypes.get_last_error()
    if err == ERROR_HOTKEY_ALREADY_REGISTERED:
        return False, "已被其它程序占用"
    return False, f"注册失败（错误 {err}）"


# ---------------------------------------------------------------- 全局热键监听


class HotkeyListener(threading.Thread):
    """独占式全局热键监听（RegisterHotKey）。

    每个动作给一串候选组合键，按顺序尝试注册，第一个成功的生效；这样即使某个组合
    被别的常驻软件占用，工具仍然可用。实际生效的绑定放在 :attr:`resolved` 里。
    """

    def __init__(self, bindings: dict[str, Any], post: Callable[[str], None]):
        super().__init__(daemon=True, name="cdda-hotkeys")
        self.bindings = bindings
        self.post = post
        self.resolved: dict[str, str] = {}
        self.fallbacks: dict[str, tuple[str, str]] = {}
        self._thread_id: Optional[int] = None
        self._stop = threading.Event()
        self._errors: list[str] = []
        self._ready = threading.Event()
        self._actions: dict[int, str] = {}

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    def wait_ready(self, timeout: float = 3.0) -> bool:
        return self._ready.wait(timeout)

    def label(self, action: str, default: str = "") -> str:
        spec = self.resolved.get(action)
        return format_hotkey(spec) if spec else default

    def stop(self) -> None:
        self._stop.set()
        if self._thread_id and USER32:
            USER32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def run(self) -> None:  # pragma: no cover - 线程体
        if not IS_WINDOWS or USER32 is None:
            self._errors.append("本工具的快照功能需要 Windows。")
            self._ready.set()
            return
        self._thread_id = KERNEL32.GetCurrentThreadId()
        msg = wintypes.MSG()
        registered: list[int] = []
        hid = 0xA000
        for action, spec in self.bindings.items():
            candidates = hotkey_candidates(spec)
            if not candidates:
                self._errors.append(f"动作 {action} 没有配置有效的热键。")
                continue
            last_error = "（无候选）"
            for index, candidate in enumerate(candidates):
                try:
                    mods, vk = parse_hotkey(candidate)
                except ValueError as exc:
                    self._errors.append(str(exc))
                    continue
                hid += 1
                if USER32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
                    registered.append(hid)
                    self._actions[hid] = action
                    self.resolved[action] = candidate
                    if index > 0:
                        self.fallbacks[action] = (candidates[0], candidate)
                        logging.warning(
                            "热键 %s 被占用，%s 改用备用键 %s",
                            candidates[0], action, candidate,
                        )
                    break
                err = ctypes.get_last_error()
                last_error = (
                    f"{candidate} 已被其它程序占用" if err == ERROR_HOTKEY_ALREADY_REGISTERED
                    else f"{candidate} 注册失败（错误 {err}）"
                )
            else:
                self._errors.append(
                    f"动作 {action} 的所有候选热键都不可用（{last_error}），"
                    f"请在配置里换一组。"
                )
        self._ready.set()
        try:
            while not self._stop.is_set():
                got = USER32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):
                    break
                if msg.message == WM_HOTKEY:
                    action = self._actions.get(int(msg.wParam))
                    if action:
                        try:
                            self.post(action)
                        except Exception:
                            logging.exception("热键回调失败")
        finally:
            for hid_value in registered:
                USER32.UnregisterHotKey(None, hid_value)


# ---------------------------------------------------------------- 进程 / 窗口


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


def any_process_named(exe_name: str) -> bool:
    """用 Toolhelp 快照按可执行文件名查找进程（不依赖 tasklist）。"""
    if not IS_WINDOWS or KERNEL32 is None:
        return False
    snap = KERNEL32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == -1:
        return False
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if not KERNEL32.Process32First(snap, ctypes.byref(entry)):
            return False
        while True:
            if entry.szExeFile.decode("ascii", "replace").lower() == exe_name.lower():
                return True
            if not KERNEL32.Process32Next(snap, ctypes.byref(entry)):
                break
        return False
    finally:
        KERNEL32.CloseHandle(snap)


def is_process_running(exe_name: str) -> bool:
    """进程是否在跑。

    **先用进程内 Toolhelp 快照，失败才退回 tasklist 子进程。**

    顺序不能反：tasklist 要 spawn 一个子进程，实测 **55 ms**，而进程内快照
    **7.8 ms** —— 这个方法在 F5 链路上会被调好几次（每次快照、还原判定、
    回退路径），55 ms 是实打实拉长"按下 F5 到开始写入"的。而且子进程 + 管道
    在受限环境（沙箱/杀软拦截）里还可能直接失败。
    """
    if not IS_WINDOWS:
        return False
    try:
        return any_process_named(exe_name)
    except Exception:
        logging.debug("进程内快照探测失败，退回 tasklist", exc_info=True)
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode == 0 and proc.stdout:
            if exe_name.lower() in proc.stdout.lower():
                return True
            lowered = proc.stdout.lower()
            if "no tasks" in lowered or "信息:" in proc.stdout:
                return False
    except Exception:
        pass
    return False


def process_name(pid: int) -> str:
    if not IS_WINDOWS or KERNEL32 is None:
        return ""
    handle = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if KERNEL32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        KERNEL32.CloseHandle(handle)


def find_window_by_process(exe_name: str) -> int:
    """返回该可执行文件名对应的可见顶层窗口句柄（0 = 没找到）。"""
    if not IS_WINDOWS or USER32 is None:
        return 0
    handles: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        pid = wintypes.DWORD()
        USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if USER32.IsWindowVisible(hwnd) and not USER32.GetWindow(hwnd, GW_OWNER) and pid.value:
            if process_name(pid.value).lower() == exe_name.lower():
                handles.append(int(hwnd))
        return True

    try:
        USER32.EnumWindows(_cb, 0)
    except Exception:
        return 0
    return handles[0] if handles else 0


def send_key_to_window(hwnd: int, vk: int) -> bool:
    """向指定窗口投递按键（PostMessage，不抢焦点）。

    ⚠️ 对 SDL 程序（CDDA 就是）**无效**：SDL 的输入循环不处理合成消息，
    实测游戏完全收不到。需要真的按键请用 :func:`send_key_global`。
    """
    if not hwnd or USER32 is None:
        return False
    scan = USER32.MapVirtualKeyW(vk, 0)
    lp = (scan << 16) | 1
    up = lp | (1 << 30) | (1 << 31)
    try:
        USER32.PostMessageW(hwnd, WM_KEYDOWN, vk, lp)
        time.sleep(0.05)
        USER32.PostMessageW(hwnd, WM_KEYUP, vk, up)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 真实按键注入

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt

_MOD_VK = {"ctrl": VK_CONTROL, "control": VK_CONTROL, "shift": VK_SHIFT, "alt": VK_MENU}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


# ---------------------------------------------------------------- 输入法（IME）处理
#
# 关键坑：微软拼音等输入法处于**中文输入状态**时会拦截注入的按键去做组字，
# 游戏根本收不到 Shift+S / Y，于是"自动存档"永远不生效。
# 送按键前必须先把 IME 切到英文/关闭状态。

IME_CMODE_ALPHANUMERIC = 0x0000
IME_CMODE_NATIVE = 0x0001
GCS_COMPSTR = 0x0008
WM_IME_CONTROL = 0x0283
IMC_GETCONVERSIONMODE = 0x0001
IMC_SETCONVERSIONMODE = 0x0002
WM_IME_SETCONTEXT = 0x0281


def ime_window(hwnd: int) -> int:
    """取指定窗口的默认 IME 窗口（0 = 没有，通常说明没在用输入法）。"""
    if not hwnd:
        return 0
    try:
        if IMM32 is not None:
            # ImmGetDefaultIMEWnd 实际定义在 imm32.dll
            fn = ctypes.WINFUNCTYPE(wintypes.HWND, wintypes.HWND)(
                ("ImmGetDefaultIMEWnd", IMM32))
            return int(fn(hwnd))
    except Exception:
        pass
    # 兜底：找该窗口下类名为 IME 的子窗口
    try:
        child = USER32.GetWindow(hwnd, 5)  # GW_CHILD
        while child:
            cls = ctypes.create_unicode_buffer(64)
            USER32.GetClassNameW(child, cls, 64)
            if cls.value == "IME":
                return int(child)
            child = USER32.GetWindow(child, 2)  # GW_HWNDNEXT
    except Exception:
        pass
    return 0


def _send_ime_control(hwnd: int, action: int, value: int) -> int:
    if USER32 is None:
        return -1
    try:
        return int(USER32.SendMessageW(hwnd, WM_IME_CONTROL, action, value))
    except Exception:
        return -1


def ime_is_native(hwnd: int) -> Optional[bool]:
    """当前是否处于中文（native）输入状态；无法判断时返回 None。"""
    ime = ime_window(hwnd)
    if not ime:
        return None
    mode = _send_ime_control(ime, IMC_GETCONVERSIONMODE, 0)
    if mode < 0:
        return None
    return bool(mode & IME_CMODE_NATIVE)


def ime_to_ascii(hwnd: int) -> bool:
    """把输入法切到英文/半角状态（关掉组字），让按键直达游戏。

    优先用 IMC_SETCONVERSIONMODE；失败则退回发送 VK_CONVERT(Shift 切换) 等效做法。
    """
    if USER32 is None or not hwnd:
        return False
    ime = ime_window(hwnd)
    if not ime:
        return True  # 没有输入法窗口，本来就不会拦
    try:
        mode = _send_ime_control(ime, IMC_GETCONVERSIONMODE, 0)
        if mode >= 0:
            if not (mode & IME_CMODE_NATIVE):
                return True  # 已经是英文状态
            new_mode = mode & ~IME_CMODE_NATIVE
            _send_ime_control(ime, IMC_SETCONVERSIONMODE, new_mode)
            # 通知窗口上下文变化，让输入法状态真正刷新
            USER32.SendMessageW(ime, WM_IME_SETCONTEXT, 1, 0)
            time.sleep(0.15)
            after = _send_ime_control(ime, IMC_GETCONVERSIONMODE, 0)
            if after >= 0 and not (after & IME_CMODE_NATIVE):
                return True
            logging.info("IMC_SETCONVERSIONMODE 未生效（mode %s -> %s），尝试按键切换", mode, after)
    except Exception:
        logging.debug("IME 模式切换异常", exc_info=True)

    # 退回：VK_CONVERT(0x1C, 即 Shift 短按) 切换中英；再不行用 VK_IME_OFF(0x1A)
    try:
        _tap(0x1C)
        time.sleep(0.25)
        state = ime_is_native(hwnd)
        if state is False:
            return True
        _tap(0x1A)  # VK_IME_OFF：直接关掉输入法
        time.sleep(0.25)
        return ime_is_native(hwnd) is not True
    except Exception:
        return False


def _kb(vk: int, up: bool = False, extended: bool = False) -> INPUT:
    ev = INPUT(type=INPUT_KEYBOARD)
    flags = (KEYEVENTF_EXTENDEDKEY if extended else 0) | (KEYEVENTF_KEYUP if up else 0)
    ev.union.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)
    return ev


def _tap(vk: int, hold_open: float = 0.05, hold_close: float = 0.05) -> None:
    """敲一次按键：按下 -> 停顿 -> 抬起。

    停顿很重要：CDDA 的输入循环需要时间看到按键的按下状态，
    一口气连发按下/抬起可能被合并掉。
    """
    arr = (INPUT * 1)(_kb(vk))
    USER32.SendInput(1, ctypes.byref(arr), ctypes.sizeof(INPUT))
    time.sleep(hold_open)
    arr = (INPUT * 1)(_kb(vk, up=True))
    USER32.SendInput(1, ctypes.byref(arr), ctypes.sizeof(INPUT))
    time.sleep(hold_close)


def _chord(vks: list[int], gap: float = 0.08,
           hold: float = 0.12) -> None:
    """按住修饰键 -> 稍等 -> 敲主键 -> 稍等 -> 逆序松开。

    每一步都用**独立的 SendInput 调用**并留出间隔。实测把整串塞进一次
    SendInput 时，CDDA 收不到正确的大小写组合（Shift+S 会失效）。
    """
    def _send(vk: int, up: bool = False) -> None:
        arr = (INPUT * 1)(_kb(vk, up=up))
        USER32.SendInput(1, ctypes.byref(arr), ctypes.sizeof(INPUT))

    for vk in vks[:-1]:
        _send(vk)
        time.sleep(gap)
    _send(vks[-1])
    time.sleep(hold)
    _send(vks[-1], up=True)
    time.sleep(gap)
    for vk in reversed(vks[:-1]):
        _send(vk, up=True)
        time.sleep(gap)


def set_foreground(hwnd: int) -> bool:
    """把窗口切到前台（真实按键只送到前台窗口）。"""
    if not hwnd or USER32 is None:
        return False
    try:
        USER32.ShowWindow(hwnd, 9)  # SW_RESTORE
        return bool(USER32.SetForegroundWindow(hwnd))
    except Exception:
        return False


def foreground_window() -> int:
    if USER32 is None:
        return 0
    try:
        return int(USER32.GetForegroundWindow())
    except Exception:
        return 0


def send_key_global(hwnd: int, vk: int, *, focus: bool = True,
                    modifiers: Optional[list[str]] = None,
                    handle_ime: bool = True) -> bool:
    """用 SendInput 注入**真实按键**——SDL 程序唯一认的方式。

    ``modifiers`` 可给 ``"shift"`` / ``"ctrl"`` / ``"alt"``。
    这点对 CDDA 至关重要：默认键位里裸 ``s`` 是「砸碎」，**存盘是 Shift+s**。

    ``handle_ime``：送键前先关掉中文输入法的组字状态。微软拼音在中文状态下会把
    注入的按键吃掉去做组字，游戏完全收不到（实测踩过）。
    """
    if USER32 is None:
        return False
    try:
        if focus and hwnd:
            set_foreground(hwnd)
            time.sleep(0.2)
        if handle_ime and hwnd:
            ime_to_ascii(hwnd)
        mods = [_MOD_VK[m] for m in (modifiers or []) if m in _MOD_VK]
        if mods:
            _chord(mods + [vk])
        else:
            _tap(vk)
        return True
    except Exception:
        logging.warning("SendInput 按键失败", exc_info=True)
        return False


def sav_mtime(world_dir: str) -> float:
    """世界目录里 .sav / .sav.zzip 的最新修改时间（游戏存盘的硬证据）。

    **只扫世界目录本级**：CDDA 的角色存档就放在 ``save/<世界>/#角色.sav.zzip``，
    子目录里是地图（maps/）、超图（overmaps/）、地图记忆（.mm1/），不会有 .sav。

    实测（139 文件的世界）：整树 ``os.walk`` 0.42 ms，只扫本级 0.06 ms —— 快 7 倍。
    这个方法在一次快照里会被调用十几次（预扫 + 轮询），轮询粒度从此可以压到 50 ms
    而不必心疼开销。

    万一某个世界的 .sav 真不在本级（非常规布局），本级扫不到东西时会退回整树扫描，
    不会因此把"游戏写盘了"漏判成"没写"。
    """
    best = 0.0
    found = False
    try:
        with os.scandir(world_dir) as entries:
            for entry in entries:
                if ".sav" not in entry.name.lower():
                    continue
                found = True
                try:
                    if entry.is_file():
                        best = max(best, entry.stat().st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    if found:
        return best

    # 退路：本级没有 .sav，按老办法整树找一遍
    try:
        for dirpath, _dirnames, filenames in os.walk(world_dir):
            for name in filenames:
                if ".sav" not in name.lower():
                    continue
                try:
                    best = max(best, os.path.getmtime(os.path.join(dirpath, name)))
                except OSError:
                    continue
    except OSError:
        pass
    return best


def kill_processes_named(exe_name: str) -> int:
    """结束所有名为 ``exe_name`` 的进程，返回成功结束的数量。

    用 Win32 API 直接 TerminateProcess，而不是 ``taskkill``：
    后者在受限环境里常常失败且拿不到原因，速度也慢。
    """
    if not IS_WINDOWS or KERNEL32 is None:
        return 0
    PROCESS_TERMINATE = 0x0001
    killed = 0
    snap = KERNEL32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == -1:
        return 0
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        pids: list[int] = []
        if KERNEL32.Process32First(snap, ctypes.byref(entry)):
            while True:
                if entry.szExeFile.decode("ascii", "replace").lower() == exe_name.lower():
                    pids.append(int(entry.th32ProcessID))
                if not KERNEL32.Process32Next(snap, ctypes.byref(entry)):
                    break
    finally:
        KERNEL32.CloseHandle(snap)

    for pid in pids:
        handle = KERNEL32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not handle:
            logging.info("无法打开进程 %s（可能已退出或权限不足）", pid)
            continue
        try:
            if KERNEL32.TerminateProcess(handle, 1):
                killed += 1
            else:
                logging.info("TerminateProcess(%s) 失败: %s", pid, ctypes.get_last_error())
        finally:
            KERNEL32.CloseHandle(handle)
    if pids:
        logging.info("已请求结束 %d 个 %s 进程（成功 %d）", len(pids), exe_name, killed)
    return killed


def _tap_post(hwnd: int, vk: int) -> bool:
    """用 PostMessage 投递一次按键。

    为什么单独做一条：SDL 程序**收不到 SendInput 注入的 ESC**
    （系统/输入法把 ESC 当保留键吃掉了），但 PostMessage 能送达。
    """
    if not hwnd or USER32 is None:
        return False
    scan = USER32.MapVirtualKeyW(vk, 0)
    down = (scan << 16) | 1
    up = down | (1 << 30) | (1 << 31)
    USER32.PostMessageW(hwnd, WM_KEYDOWN, vk, down)
    time.sleep(0.05)
    USER32.PostMessageW(hwnd, WM_KEYUP, vk, up)
    return True


def press_enter(hwnd: int) -> bool:
    """向游戏窗口投递一次回车（PostMessage）。

    用途：CCB（Cataclysm: Cleanwater Bomb）每次启动都会先显示一个「Lua 模组是
    可执行程序……」的安全提示，**必须按任意键才能继续**，不按游戏会一直停在那里。
    与 :func:`quicksave_game` 里的 ESC 是同一条通道：PostMessage 送得到，
    SendInput 注入的 ESC 反而会被系统/输入法吃掉。

    返回 True 只表示消息已投递，是否真的关掉了提示由调用方用 debug.log 里的
    模组扫描日志确认（见 cdda_snapshot.dismiss_startup_prompt）。
    """
    return _tap_post(hwnd, VK_RETURN)


def quicksave_game(hwnd: int = 0, menu_delay: float = 0.6) -> bool:
    """CDDA「快速保存」：ESC 打开菜单 → 小写 ``e`` 保存，**不退出游戏**。

    实测确认（2026-09-19 版本，逐条用截图 + 存档 mtime 验证）：

    * 游戏里按 ESC 弹出的是**浮层菜单**（不是退回主屏幕），其中一项是
      「快速保存」→ 小写 ``e``；「保存并退出」则是大写 ``S``。
    * **两者都必须用 PostMessage**：``SendInput`` 注入的 ESC 游戏收不到；
      而 ``e`` 用 SendInput 会被当成大写，结果只是关掉菜单、并不保存。
    * 菜单快捷键**区分大小写**（与"小写 s=砸碎 / 大写 S=存档"同一规律）。

    ``menu_delay`` 是"ESC 之后等浮层出现"的时间，**这是盲目等待**：没有可观测的
    "菜单已弹出"信号。所以它必须**大于**浮层的实际弹出时间，否则那个 ``e`` 会落到
    游戏世界上 —— 而世界里裸 ``e`` 是 **EXAMINE（查看/搜索）**，不是无害键：
    会弹出查看界面，这次存档等于没发生（调用方最终只会看到"目录毫无动静"）。
    默认 0.6 秒是留足余量的保守值；实测浮层弹出约 0.1~0.2 秒，
    想更快可以在配置里调小 ``quicksave_menu_delay``。

    返回 True 表示按键已投递；是否真的写盘由调用方用 ``.sav`` mtime 判定。
    """
    if not hwnd:
        hwnd = find_window_by_process("cataclysm-tiles.exe")
    if not hwnd or USER32 is None:
        return False
    try:
        _tap_post(hwnd, VK_ESCAPE)
        time.sleep(max(0.0, float(menu_delay)))
        _tap_post(hwnd, ord("E"))      # 小写 e = 快速保存
        return True
    except Exception:
        logging.warning("发送快速保存按键失败", exc_info=True)
        return False


def save_and_quit_game(hwnd: int, world_dir: str = "", *,
                       focus: bool = True,
                       dialog_wait: float = 3.0,
                       handle_ime: bool = False) -> bool:
    """让 CDDA 存档并退出：``Shift+S`` → 确认框 → ``Shift+Y``。

    实测确认的完整链条（2026-09-19 版本，逐条都用截图/文件时间验证过）：

    1. 裸 ``s`` 是「砸碎附近地形」，**存档是 Shift+S**
       （keybindings.json: ``save`` -> ``{key:'S'}` / `{key:'s', mod:['shift']}`）。
    2. Shift+S 弹出确认框「存档退出？（区分大小写）[Y]是 [N]否」，
       **必须按 Shift+Y**（大写 Y）；裸 ``y`` 无效，``Enter`` 也无效（默认光标在 [N]）。
    3. **修饰键与主键必须分开发送**：把 Shift 和 S 塞进同一次 ``SendInput``
       时 CDDA 收不到正确的大小写组合（见 :func:`_chord`）。
    4. 中文输入法在中文状态下有可能吃掉按键，所以留了 ``handle_ime`` 开关——
       实测正常游戏内不需要，默认关闭，避免对 IME 状态做无谓改动。
    """
    if USER32 is None:
        return False
    if focus and hwnd:
        set_foreground(hwnd)
        time.sleep(0.2)
    before = sav_mtime(world_dir) if world_dir else 0.0
    try:
        if handle_ime:
            ime_to_ascii(hwnd)
        _chord([VK_SHIFT, 0x53])                  # Shift+S -> 弹出确认框
        # 等确认框出现：以 .sav 是否开始变动为准，最长 dialog_wait 秒
        deadline = time.time() + max(0.4, dialog_wait)
        dialog_seen = False
        while time.time() < deadline:
            time.sleep(0.15)
            if world_dir and sav_mtime(world_dir) != before:
                dialog_seen = True
                break
        if not dialog_seen:
            time.sleep(0.4)
        if handle_ime:
            ime_to_ascii(hwnd)
        _chord([VK_SHIFT, 0x59])                  # Shift+Y -> 选「是」
        return True
    except Exception:
        logging.warning("发送存档退出按键失败", exc_info=True)
        return False


def confirm_save_dialog(hwnd: int) -> None:
    """兜底确认方式：把光标从 [N] 移到 [Y] 再按 Enter。

    有些界面下 Shift+Y 可能不被接受，这条路径走方向键导航，更接近真人操作。
    """
    if USER32 is None:
        return
    try:
        if hwnd:
            set_foreground(hwnd)
        ime_to_ascii(hwnd)
        _tap(0x25)          # Left：从 [N] 挪到 [Y]
        time.sleep(0.15)
        _tap(0x0D)          # Enter 确认
    except Exception:
        logging.debug("兜底确认失败", exc_info=True)


def close_menus(hwnd: int) -> None:
    """按一次 ESC 关掉可能开着的子界面（背包/菜单等）。"""
    if USER32 is None:
        return
    try:
        if hwnd:
            set_foreground(hwnd)
            time.sleep(0.2)
        _tap(0x1B)
    except Exception:
        pass


def single_instance(lock_name: str = "Local\\CDDA_Snapshot_Tool") -> tuple[bool, Any]:
    """命名互斥体单实例保护；返回 (是否为首个实例, 句柄)。"""
    if not IS_WINDOWS or KERNEL32 is None:
        return True, None
    try:
        handle = KERNEL32.CreateMutexW(None, False, lock_name)
        if not handle:
            return True, None
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            KERNEL32.CloseHandle(handle)
            return False, None
        return True, handle
    except Exception:
        return True, None


# ---------------------------------------------------------------- 托盘图标

#: 窗口过程类型。64 位下返回值必须是有符号指针宽度，否则 DefWindowProcW
#: 之类的返回值会触发 OverflowError。
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", wintypes.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


#: NOTIFYICONDATAW 的 cbSize。不要手算常量：结构体按指针对齐，手算会少算尾部填充，
#: Shell_NotifyIconW 会直接失败。这里以 ctypes 实测值为准。
NOTIFYICONDATA_SIZE = ctypes.sizeof(NOTIFYICONDATAW)

# ---------------------------------------------------------------- 函数签名声明
#
# 64 位下必须显式声明 argtypes/restype：ctypes 默认按 32 位 int 处理，
# 窗口消息的 wParam/lParam 常带高位（例如 0x80000000），会直接抛
# "OverflowError: int too long to convert"。


def _declare_functions() -> None:
    if not IS_WINDOWS or USER32 is None or KERNEL32 is None or SHELL32 is None:
        return
    WNDPROC_T = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM
    )
    globals()["WNDPROC"] = WNDPROC_T

    sigs = [
        (USER32, "DefWindowProcW",
         [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t),
        (USER32, "RegisterClassW", [ctypes.c_void_p], wintypes.ATOM),
        (USER32, "CreateWindowExW",
         [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
          ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
          wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p],
         wintypes.HWND),
        (USER32, "DestroyWindow", [wintypes.HWND], wintypes.BOOL),
        (USER32, "GetMessageW", [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint, ctypes.c_uint],
         ctypes.c_int),
        (USER32, "TranslateMessage", [ctypes.c_void_p], wintypes.BOOL),
        (USER32, "DispatchMessageW", [ctypes.c_void_p], ctypes.c_ssize_t),
        (USER32, "PostQuitMessage", [ctypes.c_int], None),
        (USER32, "PostThreadMessageW",
         [wintypes.DWORD, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
        (USER32, "RegisterHotKey",
         [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint], wintypes.BOOL),
        (USER32, "UnregisterHotKey", [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        (USER32, "PostMessageW",
         [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
        (USER32, "MapVirtualKeyW", [ctypes.c_uint, ctypes.c_uint], ctypes.c_uint),
        (USER32, "CreatePopupMenu", [], wintypes.HMENU),
        (USER32, "DestroyMenu", [wintypes.HMENU], wintypes.BOOL),
        (USER32, "AppendMenuW",
         [wintypes.HMENU, ctypes.c_uint, ctypes.c_size_t, wintypes.LPCWSTR], wintypes.BOOL),
        (USER32, "TrackPopupMenu",
         [wintypes.HMENU, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int,
          wintypes.HWND, ctypes.c_void_p], ctypes.c_int),
        (USER32, "GetCursorPos", [ctypes.c_void_p], wintypes.BOOL),
        (USER32, "LoadImageW",
         [wintypes.HINSTANCE, wintypes.LPCWSTR, ctypes.c_uint,
          ctypes.c_int, ctypes.c_int, ctypes.c_uint], wintypes.HANDLE),
        (USER32, "LoadIconW", [wintypes.HINSTANCE, wintypes.LPCWSTR], wintypes.HICON),
        (USER32, "GetWindow", [wintypes.HWND, ctypes.c_uint], wintypes.HWND),
        (USER32, "GetParent", [wintypes.HWND], wintypes.HWND),
        (USER32, "IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
        (USER32, "IsWindowEnabled", [wintypes.HWND], wintypes.BOOL),
        (USER32, "GetForegroundWindow", [], wintypes.HWND),
        (USER32, "SetForegroundWindow", [wintypes.HWND], wintypes.BOOL),
        (USER32, "ShowWindow", [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        (USER32, "SendInput", [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int], ctypes.c_uint),
        (USER32, "SendMessageW",
         [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t),
        (USER32, "GetClassNameW",
         [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        (USER32, "GetWindowTextW",
         [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        (USER32, "GetWindowRect", [wintypes.HWND, ctypes.c_void_p], wintypes.BOOL),
        (USER32, "GetWindowThreadProcessId", [wintypes.HWND, ctypes.c_void_p], wintypes.DWORD),
        (KERNEL32, "GetModuleHandleW", [wintypes.LPCWSTR], wintypes.HMODULE),
        (KERNEL32, "GetCurrentThreadId", [], wintypes.DWORD),
        (KERNEL32, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        (KERNEL32, "CreateMutexW",
         [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        (KERNEL32, "CreateToolhelp32Snapshot", [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
        (KERNEL32, "OpenProcess",
         [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        (KERNEL32, "TerminateProcess", [wintypes.HANDLE, ctypes.c_uint], wintypes.BOOL),
        (KERNEL32, "Process32First", [wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL),
        (KERNEL32, "Process32Next", [wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL),
        (KERNEL32, "QueryFullProcessImageNameW",
         [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.c_void_p], wintypes.BOOL),
        (SHELL32, "Shell_NotifyIconW", [wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
    ]
    for lib, name, argtypes, restype in sigs:
        try:
            func = getattr(lib, name)
            func.argtypes = argtypes
            func.restype = restype
        except Exception:  # pragma: no cover - 某些精简系统可能缺函数
            logging.debug("声明 %s 签名失败", name)


_declare_functions()


class TrayIcon:
    """零依赖的托盘图标（Shell_NotifyIconW + 自建消息窗口与消息循环）。

    在独立线程里跑自己的消息循环，对外只暴露回调；所有 UI 回调都通过 ``post``
    派发回主线程（本模块不直接碰 tkinter）。
    """

    def __init__(
        self,
        tooltip: str,
        menu_items: list[tuple[int, str]],
        on_command: Callable[[int], None],
        *,
        on_left_click: Optional[Callable[[], None]] = None,
        icon_path: Optional[str] = None,
    ):
        self.tooltip = tooltip[:127]
        self.menu_items = menu_items
        self.on_command = on_command
        self.on_left_click = on_left_click
        self.icon_path = icon_path
        self.available = False
        self.last_error = ""
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._hwnd: int = 0
        self._hicon: int = 0
        self._wndproc_ref = None
        self._menu_id_base = 0xE000
        self._added = False

    # -------------------------------------------------- 生命周期

    def start(self) -> bool:
        if not IS_WINDOWS or USER32 is None or SHELL32 is None:
            self.last_error = "仅支持 Windows"
            return False
        self._thread = threading.Thread(target=self._run, daemon=True, name="cdda-tray")
        self._thread.start()
        for _ in range(50):
            if self.available or self.last_error:
                break
            time.sleep(0.05)
        return self.available

    def stop(self) -> None:
        if self._thread_id:
            try:
                USER32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass

    # -------------------------------------------------- 气泡通知

    def notify(self, title: str, message: str, *, warning: bool = False) -> bool:
        if not self._added or not self._hwnd:
            return False
        try:
            nid = self._make_nid()
            nid.uFlags = NIF_INFO
            nid.szInfoTitle = (title or "")[:63]
            nid.szInfo = (message or "")[:255]
            nid.dwInfoFlags = NIIF_WARNING if warning else NIIF_INFO
            return bool(SHELL32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))
        except Exception:
            return False

    def _make_nid(self) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = NOTIFYICONDATA_SIZE
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uCallbackMessage = WM_TRAYICON
        return nid

    # -------------------------------------------------- 线程体

    def _load_icon(self) -> int:
        if self.icon_path and os.path.isfile(self.icon_path):
            handle = USER32.LoadImageW(
                None, self.icon_path, IMAGE_ICON, 0, 0,
                LR_LOADFROMFILE | LR_DEFAULTSIZE,
            )
            if handle:
                return handle
        return USER32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))

    def _run(self) -> None:  # pragma: no cover - 线程体
        try:
            self._thread_id = KERNEL32.GetCurrentThreadId()
            hinstance = KERNEL32.GetModuleHandleW(None)
            class_name = f"CDDASnapshotTray{os.getpid()}"

            def _wndproc(hwnd, msg, wparam, lparam):
                try:
                    if msg == WM_TRAYICON:
                        if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                            if self.on_left_click:
                                self.on_left_click()
                        elif lparam == WM_RBUTTONUP:
                            self._show_menu(hwnd)
                        return 0
                    if msg == WM_COMMAND:
                        cmd = int(wparam) & 0xFFFF
                        if cmd == self._menu_id_base + 0x7FFF:
                            USER32.PostQuitMessage(0)
                            return 0
                        if self.on_command:
                            self.on_command(cmd)
                        return 0
                    if msg == WM_DESTROY:
                        USER32.PostQuitMessage(0)
                        return 0
                except Exception:
                    logging.exception("托盘窗口过程异常")
                return USER32.DefWindowProcW(hwnd, msg, wparam, lparam)

            self._wndproc_ref = WNDPROC(_wndproc)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinstance
            wc.lpszClassName = class_name
            if not USER32.RegisterClassW(ctypes.byref(wc)):
                err = ctypes.get_last_error()
                if err not in (0, 1410):  # 1410 = class already exists
                    raise OSError(f"RegisterClassW 失败: {err}")

            hwnd = USER32.CreateWindowExW(
                0, class_name, class_name, 0, 0, 0, 0, 0, None, None, hinstance, None
            )
            if not hwnd:
                raise OSError(f"CreateWindowExW 失败: {ctypes.get_last_error()}")
            self._hwnd = int(hwnd)

            self._hicon = self._load_icon()
            nid = self._make_nid()
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.hIcon = self._hicon
            nid.szTip = self.tooltip
            if not SHELL32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                raise OSError("Shell_NotifyIconW 添加失败")
            self._added = True
            self.available = True
            logging.info("托盘图标已就绪")

            msg = wintypes.MSG()
            while True:
                got = USER32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):
                    break
                USER32.TranslateMessage(ctypes.byref(msg))
                USER32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            self.last_error = str(exc)
            logging.warning("托盘图标不可用: %s", exc)
        finally:
            if self._added:
                try:
                    SHELL32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._make_nid()))
                except Exception:
                    pass
                self._added = False
            if self._hwnd:
                try:
                    USER32.DestroyWindow(self._hwnd)
                except Exception:
                    pass
            self.available = False

    def _show_menu(self, hwnd) -> None:
        menu = USER32.CreatePopupMenu()
        if not menu:
            return
        try:
            for cmd, label in self.menu_items:
                USER32.AppendMenuW(menu, 0x0000, cmd, label)  # MF_STRING
            USER32.SetForegroundWindow(hwnd)
            point = wintypes.POINT()
            USER32.GetCursorPos(ctypes.byref(point))
            # TPM_RIGHTBUTTON | TPM_RETURNCMD
            choice = USER32.TrackPopupMenu(
                menu, 0x0002 | 0x0100, point.x, point.y, 0, hwnd, None
            )
            if choice:
                if choice == self._menu_id_base + 0x7FFF:
                    USER32.PostQuitMessage(0)
                elif self.on_command:
                    self.on_command(int(choice))
        finally:
            USER32.DestroyMenu(menu)
