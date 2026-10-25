# -*- coding: utf-8 -*-
"""系统托盘图标：用 GDI 现画圆环电量图标，左键刷新、右键打开面板。

纯 ctypes 调 user32 / shell32 / gdi32，不依赖 pystray / Pillow。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import queue
import threading
from dataclasses import dataclass

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_UPDATE = WM_APP + 2
WM_QUIT_APP = WM_APP + 3
WM_DESTROY = 0x0002
WM_CONTEXTMENU = 0x007B
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
NIN_SELECT = 0x0400

NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIM_ADD = 0x00
NIM_MODIFY = 0x01
NIM_DELETE = 0x02
NIM_SETVERSION = 0x04
NOTIFYICON_VERSION_4 = 4

SM_CXSMICON = 49

#: 颜色（RGB）
GREEN = (61, 220, 132)
AMBER = (255, 176, 46)
RED = (255, 90, 95)
GREY = (150, 158, 176)
TRACK = (74, 84, 110)

#: 3x5 点阵字体
GLYPHS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "010", "010", "010", "010"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "001", "001"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "?": ("111", "001", "011", "000", "010"),
}


def level_color(percent: int) -> tuple[int, int, int]:
    if percent >= 60:
        return GREEN
    if percent >= 30:
        return AMBER
    return RED


# ------------------------------------------------------------------ 画图


def render_rgba(size: int, percent: int | None, charging: bool = False) -> bytearray:
    """画一个圆环：外圈是进度，中心是数字（0-99，100 显示满环）。"""
    buf = bytearray(size * size * 4)
    cx = cy = size / 2.0
    outer = size / 2.0 - 0.6
    thickness = max(2.0, size * 0.13)
    inner = outer - thickness
    mid = (outer + inner) / 2.0
    half = thickness / 2.0
    frac = 0.0 if percent is None else max(0.0, min(1.0, percent / 100.0))
    color = GREY if percent is None else level_color(percent)

    for y in range(size):
        for x in range(size):
            dx = x + 0.5 - cx
            dy = y + 0.5 - cy
            r = math.hypot(dx, dy)
            cov = half + 0.5 - abs(r - mid)
            if cov <= 0:
                continue
            cov = min(1.0, cov)
            ang = math.degrees(math.atan2(dy, dx))
            t = ((ang + 90.0) % 360.0) / 360.0  # 0 = 正上方，顺时针
            if percent is None:
                rgb, alpha = TRACK, 160
            elif t <= frac:
                rgb, alpha = color, 255
            else:
                rgb, alpha = TRACK, 150
            a = int(alpha * cov)
            i = (y * size + x) * 4
            buf[i] = rgb[2] * a // 255      # B
            buf[i + 1] = rgb[1] * a // 255  # G
            buf[i + 2] = rgb[0] * a // 255  # R
            buf[i + 3] = a

    # 中心数字
    text = "?" if percent is None else str(percent)
    if percent is not None and percent >= 100:
        text = ""  # 满环本身就是 100
    if text:
        limit = inner * 2 - 0.5
        scale = 1
        while True:
            nxt = scale + 1
            width = (len(text) * 3 + len(text) - 1) * nxt
            if width <= limit and 5 * nxt <= limit:
                scale = nxt
            else:
                break
        tw = (len(text) * 3 + len(text) - 1) * scale
        th = 5 * scale
        x0 = int(round(cx - tw / 2.0))
        y0 = int(round(cy - th / 2.0))
        text_rgb = AMBER if charging else color
        for index, ch in enumerate(text):
            glyph = GLYPHS.get(ch)
            if glyph is None:
                continue
            for gy, row in enumerate(glyph):
                for gx, bit in enumerate(row):
                    if bit != "1":
                        continue
                    px = x0 + (index * 4 + gx) * scale
                    py = y0 + gy * scale
                    for oy in range(scale):
                        for ox in range(scale):
                            x, y = px + ox, py + oy
                            if not (0 <= x < size and 0 <= y < size):
                                continue
                            i = (y * size + x) * 4
                            buf[i] = text_rgb[2]
                            buf[i + 1] = text_rgb[1]
                            buf[i + 2] = text_rgb[0]
                            buf[i + 3] = 255
    return buf


# ------------------------------------------------------------------ GDI


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD),
                ("yHotspot", wt.DWORD), ("hbmMask", wt.HBITMAP),
                ("hbmColor", wt.HBITMAP)]


gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wt.HANDLE,
                                   wt.DWORD]
gdi32.CreateBitmap.restype = wt.HBITMAP
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wt.UINT, wt.UINT,
                               ctypes.c_void_p]
gdi32.SelectObject.restype = wt.HANDLE
gdi32.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
gdi32.DeleteObject.argtypes = [wt.HANDLE]
gdi32.DeleteDC.argtypes = [wt.HDC]
user32.CreateIconIndirect.restype = wt.HICON
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.DestroyIcon.argtypes = [wt.HICON]


def make_icon(size: int, percent: int | None, charging: bool = False):
    """把圆环画进 32 位 DIB，再转成 HICON。返回的 HICON 需要 DestroyIcon。"""
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = size
    bmi.bmiHeader.biHeight = -size
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB

    hdc = gdi32.CreateCompatibleDC(None)
    bits = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits),
                                 None, 0)
    if not hbm or not bits:
        gdi32.DeleteDC(hdc)
        return None

    pixels = render_rgba(size, percent, charging)
    ctypes.memmove(bits, bytes(pixels), len(pixels))

    mask = gdi32.CreateBitmap(size, size, 1, 1, None)
    info = ICONINFO(True, 0, 0, mask, hbm)
    icon = user32.CreateIconIndirect(ctypes.byref(info))

    gdi32.DeleteObject(mask)
    gdi32.DeleteObject(hbm)
    gdi32.DeleteDC(hdc)
    return icon


# ------------------------------------------------------------------ 托盘


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD),
                ("hWnd", wt.HWND),
                ("uID", wt.UINT),
                ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT),
                ("hIcon", wt.HICON),
                ("szTip", ctypes.c_wchar * 128),
                ("dwState", wt.DWORD),
                ("dwStateMask", wt.DWORD),
                ("szInfo", ctypes.c_wchar * 256),
                ("uVersion", wt.UINT),
                ("szInfoTitle", ctypes.c_wchar * 64),
                ("dwInfoFlags", wt.DWORD),
                ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", wt.HICON)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM,
                             wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wt.HWND, wt.HMENU,
                                   wt.HINSTANCE, ctypes.c_void_p]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.RegisterWindowMessageW.restype = wt.UINT
user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
shell32.Shell_NotifyIconW.restype = wt.BOOL
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]

#: Explorer 重启后广播的消息：需要重新注册图标
TASKBAR_CREATED = user32.RegisterWindowMessageW("TaskbarCreated")


@dataclass
class TrayItem:
    key: str            # 设备唯一键
    label: str          # 提示文字里的设备名
    percent: int | None
    charging: bool = False


class Tray:
    """托盘图标管理器：一个设备一个图标。"""

    def __init__(self, on_refresh=None, on_show=None, on_quit=None):
        self.on_refresh = on_refresh
        self.on_show = on_show
        self.on_quit = on_quit
        self.events: "queue.Queue[str]" = queue.Queue()

        self._items: list[TrayItem] = []
        self._lock = threading.Lock()
        self._hwnd = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="dsh-tray")
        self._size = 16
        self._slots: dict[str, int] = {}
        self._icons: dict[str, wt.HICON] = {}
        self._garbage: list = []
        self._next_id = 1
        self._proc = None
        self.debug: list[str] = []
        #: 是否真的注册上了托盘图标（低完整性进程会被 UIPI 拦下）
        self.available = False

    # -- 对外接口（任意线程） -----------------------------------------
    def start(self) -> None:
        self._thread.start()
        self._ready.wait(3.0)

    def update(self, items: list[TrayItem]) -> None:
        with self._lock:
            self._items = list(items)
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_UPDATE, 0, 0)

    def stop(self) -> None:
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_QUIT_APP, 0, 0)

    # -- 内部 ---------------------------------------------------------
    def _run(self) -> None:
        self._size = max(16, int(user32.GetSystemMetrics(SM_CXSMICON)))
        self._proc = WNDPROC(self._wnd_proc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.lpszClassName = "DshBatteryTray"
        wc.hInstance = kernel32.GetModuleHandleW(None)
        if not user32.RegisterClassW(ctypes.byref(wc)):
            self._ready.set()
            return
        hwnd = user32.CreateWindowExW(0, "DshBatteryTray", "tray", 0,
                                      0, 0, 0, 0, None, None, wc.hInstance, None)
        if not hwnd:
            self._ready.set()
            return
        self._hwnd = hwnd
        self._ready.set()

        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        self._remove_all()
        user32.DestroyWindow(hwnd)

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            inner = lparam & 0xFFFF
            if inner in (NIN_SELECT, WM_LBUTTONUP):
                self.events.put("refresh")
            elif inner in (WM_CONTEXTMENU, WM_RBUTTONUP):
                self.events.put("show")
            return 0
        if msg == WM_UPDATE:
            self._sync_icons()
            return 0
        if msg == TASKBAR_CREATED:
            # 资源管理器重启后图标会全部丢失，重新注册
            for icon in list(self._icons.values()) + self._garbage:
                if icon:
                    user32.DestroyIcon(icon)
            self._icons.clear()
            self._garbage.clear()
            self._slots.clear()
            self.available = False
            self._sync_icons()
            return 0
        if msg == WM_QUIT_APP:
            user32.PostQuitMessage(0)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _sync_icons(self) -> None:
        with self._lock:
            items = list(self._items)
        if not items:
            items = [TrayItem("none", "未发现设备", None)]

        alive = {item.key for item in items}
        for key in list(self._slots):
            if key in alive:
                continue
            nid = self._nid(self._slots.pop(key))
            nid.hIcon = None
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            icon = self._icons.pop(key, None)
            if icon:
                self._garbage.append(icon)

        for item in items:
            icon = make_icon(self._size, item.percent, item.charging)
            if icon is None:
                continue
            tip = f"{item.label} {item.percent}%" if item.percent is not None \
                else item.label
            if item.charging:
                tip += "（充电中）"

            if item.key in self._slots:
                nid = self._nid(self._slots[item.key])
                nid.uFlags = NIF_ICON | NIF_TIP
                nid.hIcon = icon
                nid.szTip = tip[:127]
                ok = shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
                self.debug.append(f"modify {item.key} -> {int(bool(ok))}")
            else:
                icon_id = self._next_id
                self._next_id += 1
                self._slots[item.key] = icon_id
                nid = self._nid(icon_id)
                nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
                nid.uCallbackMessage = WM_TRAY
                nid.hIcon = icon
                nid.szTip = tip[:127]
                ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
                self.debug.append(f"add {item.key} -> {int(bool(ok))}")
                if ok:
                    self.available = True
                nid.uVersion = NOTIFYICON_VERSION_4
                shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))

            old = self._icons.get(item.key)
            if old:
                self._garbage.append(old)
            self._icons[item.key] = icon

    def _nid(self, icon_id: int) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = icon_id
        return nid

    def _remove_all(self) -> None:
        for icon_id in list(self._slots.values()):
            nid = self._nid(icon_id)
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        for icon in list(self._icons.values()) + self._garbage:
            if icon:
                user32.DestroyIcon(icon)
        self._icons.clear()
        self._garbage.clear()
        self._slots.clear()
