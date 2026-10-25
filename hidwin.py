# -*- coding: utf-8 -*-
"""Windows HID 底层访问（纯 ctypes，零第三方依赖）。

只做三件事：

1. 用 SetupAPI 枚举本机全部 HID 接口（2.4G 接收器、蓝牙外设、有线外设都在里面）；
2. 读出每个接口的 VID/PID、产品名、UsagePage/Usage、各类报告长度与电量相关的值能力；
3. 提供 Feature Report / Output Report 写入，以及带超时的 Input Report 读取。

设计上刻意把「写」和「读」放在两个独立的句柄里：在同一个 HID 句柄上
一边阻塞 ReadFile 一边 WriteFile，实测会让进程直接消失，连异常都抓不到。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

if sys.platform != "win32":  # pragma: no cover - 只支持 Windows
    raise RuntimeError("本项目依赖 Windows HID API，只能在 Windows 上运行")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_hid = ctypes.WinDLL("hid", use_last_error=True)
_setup = ctypes.WinDLL("setupapi", use_last_error=True)

# ---------------------------------------------------------------- 常量

DIGCF_PRESENT = 0x00000002
DIGCF_DEVICEINTERFACE = 0x00000010

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_IO_PENDING = 997

HidP_Input = 0
HidP_Output = 1
HidP_Feature = 2

USAGE_PAGE_GENERIC_DEVICE = 0x0006
USAGE_BATTERY_STRENGTH = 0x0020

#: {4d1e55b2-f16f-11cf-88cb-001111000030}
GUID_DEVINTERFACE_HID = (0x4D1E55B2, 0xF16F, 0x11CF,
                         (0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30))


class HidError(OSError):
    """HID 调用失败。"""


# ---------------------------------------------------------------- 结构体


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, d1: int = 0, d2: int = 0, d3: int = 0, d4=None):
        super().__init__(d1, d2, d3, (ctypes.c_ubyte * 8)(*(d4 or [0] * 8)))


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD),
                ("InterfaceClassGuid", GUID),
                ("Flags", wt.DWORD),
                ("Reserved", ctypes.c_size_t)]


class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("DevicePath", wt.WCHAR * 1)]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", wt.ULONG),
                ("VendorID", wt.USHORT),
                ("ProductID", wt.USHORT),
                ("VersionNumber", wt.USHORT)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", wt.USHORT),
                ("UsagePage", wt.USHORT),
                ("InputReportByteLength", wt.USHORT),
                ("OutputReportByteLength", wt.USHORT),
                ("FeatureReportByteLength", wt.USHORT),
                ("Reserved", wt.USHORT * 17),
                ("NumberLinkCollectionNodes", wt.USHORT),
                ("NumberInputButtonCaps", wt.USHORT),
                ("NumberInputValueCaps", wt.USHORT),
                ("NumberInputDataIndices", wt.USHORT),
                ("NumberOutputButtonCaps", wt.USHORT),
                ("NumberOutputValueCaps", wt.USHORT),
                ("NumberOutputDataIndices", wt.USHORT),
                ("NumberFeatureButtonCaps", wt.USHORT),
                ("NumberFeatureValueCaps", wt.USHORT),
                ("NumberFeatureDataIndices", wt.USHORT)]


class _UsageRange(ctypes.Structure):
    _fields_ = [("UsageMin", wt.USHORT), ("UsageMax", wt.USHORT),
                ("StringMin", wt.USHORT), ("StringMax", wt.USHORT),
                ("DesignatorMin", wt.USHORT), ("DesignatorMax", wt.USHORT),
                ("DataIndexMin", wt.USHORT), ("DataIndexMax", wt.USHORT)]


class _UsageNotRange(ctypes.Structure):
    _fields_ = [("Usage", wt.USHORT), ("Reserved1", wt.USHORT),
                ("StringIndex", wt.USHORT), ("Reserved2", wt.USHORT),
                ("DesignatorIndex", wt.USHORT), ("Reserved3", wt.USHORT),
                ("DataIndex", wt.USHORT), ("Reserved4", wt.USHORT)]


class _UsageUnion(ctypes.Union):
    _fields_ = [("Range", _UsageRange), ("NotRange", _UsageNotRange)]


class HIDP_VALUE_CAPS(ctypes.Structure):
    # 注意字段顺序：HasNull/BitSize/ReportCount 在 Units 之前，
    # Usage 联合体在最后。结构体总长在 x64 上是 72 字节。
    _fields_ = [("UsagePage", wt.USHORT),
                ("ReportID", ctypes.c_ubyte),
                ("IsAlias", ctypes.c_ubyte),
                ("BitField", wt.USHORT),
                ("LinkCollection", wt.USHORT),
                ("LinkUsage", wt.USHORT),
                ("LinkUsagePage", wt.USHORT),
                ("IsRange", ctypes.c_ubyte),
                ("IsStringRange", ctypes.c_ubyte),
                ("IsDesignatorRange", ctypes.c_ubyte),
                ("IsAbsolute", ctypes.c_ubyte),
                ("HasNull", ctypes.c_ubyte),
                ("Reserved", ctypes.c_ubyte),
                ("BitSize", wt.USHORT),
                ("ReportCount", wt.USHORT),
                ("Reserved2", wt.USHORT * 5),
                ("UnitsExp", ctypes.c_uint32),
                ("Units", ctypes.c_uint32),
                ("LogicalMin", ctypes.c_int32),
                ("LogicalMax", ctypes.c_int32),
                ("PhysicalMin", ctypes.c_int32),
                ("PhysicalMax", ctypes.c_int32),
                ("Usage", _UsageUnion)]


# ---------------------------------------------------------------- 原型声明

_setup.SetupDiGetClassDevsW.restype = ctypes.c_void_p
_setup.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wt.LPCWSTR,
                                       wt.HWND, wt.DWORD]
_setup.SetupDiEnumDeviceInterfaces.restype = wt.BOOL
_setup.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p,
                                               ctypes.c_void_p,
                                               ctypes.POINTER(GUID), wt.DWORD,
                                               ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
_setup.SetupDiGetDeviceInterfaceDetailW.restype = wt.BOOL
_setup.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p,
                                                    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                    ctypes.c_void_p, wt.DWORD,
                                                    ctypes.POINTER(wt.DWORD),
                                                    ctypes.c_void_p]
_setup.SetupDiDestroyDeviceInfoList.restype = wt.BOOL
_setup.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]

_hid.HidD_GetAttributes.restype = wt.BOOLEAN
_hid.HidD_GetAttributes.argtypes = [wt.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]

for _name in ("HidD_GetManufacturerString", "HidD_GetProductString",
              "HidD_GetSerialNumberString"):
    _fn = getattr(_hid, _name)
    _fn.restype = wt.BOOLEAN
    _fn.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.ULONG]

_hid.HidD_GetPreparsedData.restype = wt.BOOLEAN
_hid.HidD_GetPreparsedData.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
_hid.HidD_FreePreparsedData.restype = wt.BOOLEAN
_hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
_hid.HidP_GetCaps.restype = ctypes.c_long
_hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
_hid.HidP_GetValueCaps.restype = ctypes.c_long
_hid.HidP_GetValueCaps.argtypes = [ctypes.c_int,
                                   ctypes.POINTER(HIDP_VALUE_CAPS),
                                   ctypes.POINTER(wt.USHORT), ctypes.c_void_p]
_hid.HidP_GetUsageValue.restype = ctypes.c_long
_hid.HidP_GetUsageValue.argtypes = [ctypes.c_int, wt.USHORT, wt.USHORT, wt.USHORT,
                                    ctypes.POINTER(wt.ULONG), ctypes.c_void_p,
                                    ctypes.c_char_p, wt.ULONG]
_hid.HidP_GetScaledUsageValue.restype = ctypes.c_long
_hid.HidP_GetScaledUsageValue.argtypes = [ctypes.c_int, wt.USHORT, wt.USHORT, wt.USHORT,
                                          ctypes.POINTER(ctypes.c_long), ctypes.c_void_p,
                                          ctypes.c_char_p, wt.ULONG]
_hid.HidD_GetFeature.restype = wt.BOOLEAN
_hid.HidD_GetFeature.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.ULONG]
_hid.HidD_SetFeature.restype = wt.BOOLEAN
_hid.HidD_SetFeature.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.ULONG]
_hid.HidD_GetInputReport.restype = wt.BOOLEAN
_hid.HidD_GetInputReport.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.ULONG]

_k32.CreateFileW.restype = wt.HANDLE
_k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                             wt.DWORD, wt.DWORD, wt.HANDLE]
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.CloseHandle.restype = wt.BOOL
_k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                          ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_k32.ReadFile.restype = wt.BOOL
_k32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                           ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_k32.WriteFile.restype = wt.BOOL
_k32.CreateEventW.restype = wt.HANDLE
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
_k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
_k32.WaitForSingleObject.restype = wt.DWORD
_k32.GetOverlappedResult.argtypes = [wt.HANDLE, ctypes.c_void_p,
                                     ctypes.POINTER(wt.DWORD), wt.BOOL]
_k32.GetOverlappedResult.restype = wt.BOOL
_k32.CancelIoEx.restype = wt.BOOL
_k32.CancelIoEx.argtypes = [wt.HANDLE, ctypes.c_void_p]
_k32.ResetEvent.argtypes = [wt.HANDLE]
_k32.ResetEvent.restype = wt.BOOL


# ---------------------------------------------------------------- 设备描述


@dataclass
class ValueCap:
    """一条「值能力」：某个 Report 里的某个 usage，可用来定位电量字段。"""
    report_type: int          # HidP_Input / HidP_Output / HidP_Feature
    report_id: int
    usage_page: int
    usage: int
    link_collection: int
    logical_min: int
    logical_max: int
    bit_size: int
    report_count: int = 1

    @property
    def report_type_name(self) -> str:
        return {HidP_Input: "Input", HidP_Output: "Output",
                HidP_Feature: "Feature"}.get(self.report_type, "?")


@dataclass
class HidDevice:
    path: str
    vendor_id: int = 0
    product_id: int = 0
    version: int = 0
    manufacturer: str = ""
    product: str = ""
    serial: str = ""
    usage_page: int = 0
    usage: int = 0
    input_len: int = 0
    output_len: int = 0
    feature_len: int = 0
    value_caps: list[ValueCap] = field(default_factory=list)
    error: str = ""

    # -------------------------------------------------------------- 便捷属性
    @property
    def vid_pid(self) -> str:
        return f"{self.vendor_id:04X}:{self.product_id:04X}"

    @property
    def key(self) -> str:
        return f"{self.vid_pid}#{self.path.rsplit('#', 1)[-1].split('&')[0]}"

    @property
    def name(self) -> str:
        if self.product.strip():
            return self.product.strip()
        if self.manufacturer.strip():
            return f"{self.manufacturer.strip()} HID 设备"
        return f"HID 设备 {self.vid_pid}"

    @property
    def is_top_level_mouse_or_keyboard(self) -> bool:
        """系统独占的顶层集合：鼠标(0x02)、键盘(0x06)、笔、触摸屏等。"""
        return self.usage_page == 0x01 and self.usage in (0x01, 0x02, 0x05, 0x06)

    def battery_caps(self, report_type: int | None = None) -> list[ValueCap]:
        """标准 HID 电量字段（Generic Device Controls / Battery Strength）。"""
        return [c for c in self.value_caps
                if c.usage_page == USAGE_PAGE_GENERIC_DEVICE
                and c.usage == USAGE_BATTERY_STRENGTH
                and (report_type is None or c.report_type == report_type)]


# ---------------------------------------------------------------- 打开句柄


def _open_path(path: str, access: int, overlapped: bool = False):
    flags = FILE_FLAG_OVERLAPPED if overlapped else 0
    handle = _k32.CreateFileW(path, access,
                              FILE_SHARE_READ | FILE_SHARE_WRITE,
                              None, OPEN_EXISTING, flags, None)
    if handle == INVALID_HANDLE_VALUE or handle is None:
        raise HidError(ctypes.get_last_error(), "CreateFileW 失败",
                       path)
    return handle


class HidHandle:
    """一个已打开的 HID 句柄。

    读、写请在独立句柄上进行：`HidDevice.open(write=True)` 拿写句柄，
    `HidDevice.open(overlapped=True)` 拿带超时的读句柄。
    """

    def __init__(self, handle, device: HidDevice, overlapped: bool):
        self.handle = handle
        self.device = device
        self.overlapped = overlapped
        self._closed = False

    # -- 关闭 ---------------------------------------------------------
    def close(self) -> None:
        if not self._closed:
            self._closed = True
            _k32.CloseHandle(self.handle)

    def __enter__(self) -> "HidHandle":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- Feature Report ----------------------------------------------
    def get_feature(self, report_id: int, length: int) -> bytes:
        """读 Feature Report，返回含 report id 首字节的原始数据。"""
        size = max(int(length), 2)
        buf = ctypes.create_string_buffer(size)
        ctypes.memset(buf, 0, size)
        buf[0] = bytes([report_id & 0xFF])
        if _hid.HidD_GetFeature(self.handle, buf, size):
            return buf.raw[:size]
        err = ctypes.get_last_error()
        # 无 Report ID 的设备：首字节就是数据，重试一次
        if report_id == 0:
            buf2 = ctypes.create_string_buffer(size)
            ctypes.memset(buf2, 0, size)
            if _hid.HidD_GetFeature(self.handle, buf2, size):
                return buf2.raw[:size]
        raise HidError(err, "HidD_GetFeature 失败", self.device.path)

    def set_feature(self, data: bytes) -> None:
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        if not _hid.HidD_SetFeature(self.handle, buf, len(data)):
            raise HidError(ctypes.get_last_error(), "HidD_SetFeature 失败",
                           self.device.path)

    # -- Output Report ------------------------------------------------
    def write_output(self, data: bytes) -> None:
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        written = wt.DWORD(0)
        if not _k32.WriteFile(self.handle, buf, len(data),
                              ctypes.byref(written), None):
            raise HidError(ctypes.get_last_error(), "WriteFile 失败",
                           self.device.path)

    # -- Input Report -------------------------------------------------
    def read_inputs(self, duration_ms: int = 400,
                    max_reports: int = 64) -> list[bytes]:
        """在 duration_ms 时间窗内尽量多地读 Input Report。"""
        if not self.overlapped:
            raise RuntimeError("read_inputs 需要 overlapped 句柄")
        size = max(int(self.device.input_len), 2)
        reports: list[bytes] = []
        deadline = time.monotonic() + duration_ms / 1000.0
        while len(reports) < max_reports:
            remain = int((deadline - time.monotonic()) * 1000)
            if remain <= 0:
                break
            data = self._read_once(size, remain)
            if data is None:
                continue
            reports.append(data)
        return reports

    def _read_once(self, size: int, timeout_ms: int) -> bytes | None:
        ov = _OVERLAPPED()
        ov.hEvent = _k32.CreateEventW(None, True, False, None)
        if not ov.hEvent:
            raise HidError(ctypes.get_last_error(), "CreateEventW 失败")
        buf = ctypes.create_string_buffer(size)
        read = wt.DWORD(0)
        try:
            ok = _k32.ReadFile(self.handle, buf, size, ctypes.byref(read),
                               ctypes.byref(ov))
            if not ok:
                err = ctypes.get_last_error()
                if err != ERROR_IO_PENDING:
                    return None
                wait = _k32.WaitForSingleObject(ov.hEvent, timeout_ms)
                if wait != 0:  # WAIT_OBJECT_0 == 0
                    _k32.CancelIoEx(self.handle, ctypes.byref(ov))
                    _k32.WaitForSingleObject(ov.hEvent, 50)
                    return None
                if not _k32.GetOverlappedResult(self.handle, ctypes.byref(ov),
                                                ctypes.byref(read), False):
                    return None
            n = int(read.value)
            if n <= 0:
                return None
            return buf.raw[:n]
        finally:
            _k32.CloseHandle(ov.hEvent)


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wt.DWORD),
                ("OffsetHigh", wt.DWORD),
                ("hEvent", wt.HANDLE)]


def _hid_device_open(dev: HidDevice, *, write: bool = False,
                     read: bool = False) -> HidHandle:
    """打开设备。

    - write=True：拿到可写句柄（用于 Output / Feature Report 发命令）；
    - read=True：拿到带 FILE_FLAG_OVERLAPPED 的读句柄（带超时读 Input Report）；
    - 都关掉时按只读打开（查询用）。
    """
    if read:
        return HidHandle(_open_path(dev.path, GENERIC_READ, overlapped=True),
                         dev, overlapped=True)
    if write:
        try:
            handle = _open_path(dev.path, GENERIC_READ | GENERIC_WRITE)
        except HidError:
            # 有些设备允许发 Output Report 但拒绝 GENERIC_WRITE，退回只读
            handle = _open_path(dev.path, GENERIC_READ)
        return HidHandle(handle, dev, overlapped=False)
    return HidHandle(_open_path(dev.path, 0), dev, overlapped=False)


def _string(handle, fn, limit: int = 256) -> str:
    buf = ctypes.create_unicode_buffer(limit)
    try:
        if fn(handle, buf, ctypes.sizeof(buf)):
            return buf.value.strip("\x00 ").strip()
    except Exception:
        pass
    return ""


def _describe(path: str) -> HidDevice:
    dev = HidDevice(path=path)
    try:
        with _hid_device_open(dev) as h:
            attrs = HIDD_ATTRIBUTES()
            attrs.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
            if _hid.HidD_GetAttributes(h.handle, ctypes.byref(attrs)):
                dev.vendor_id = int(attrs.VendorID)
                dev.product_id = int(attrs.ProductID)
                dev.version = int(attrs.VersionNumber)
            dev.manufacturer = _string(h.handle, _hid.HidD_GetManufacturerString)
            dev.product = _string(h.handle, _hid.HidD_GetProductString)
            dev.serial = _string(h.handle, _hid.HidD_GetSerialNumberString)

            preparsed = ctypes.c_void_p()
            if _hid.HidD_GetPreparsedData(h.handle, ctypes.byref(preparsed)):
                try:
                    caps = HIDP_CAPS()
                    if _hid.HidP_GetCaps(preparsed, ctypes.byref(caps)) >= 0:
                        dev.usage = int(caps.Usage)
                        dev.usage_page = int(caps.UsagePage)
                        dev.input_len = int(caps.InputReportByteLength)
                        dev.output_len = int(caps.OutputReportByteLength)
                        dev.feature_len = int(caps.FeatureReportByteLength)
                    dev.value_caps = _value_caps(preparsed)
                finally:
                    _hid.HidD_FreePreparsedData(preparsed)
    except HidError as exc:
        dev.error = str(exc)
    return dev


def _value_caps(preparsed) -> list[ValueCap]:
    out: list[ValueCap] = []
    for report_type in (HidP_Input, HidP_Feature, HidP_Output):
        length = wt.USHORT(256)
        arr = (HIDP_VALUE_CAPS * 256)()
        status = _hid.HidP_GetValueCaps(report_type, arr, ctypes.byref(length),
                                        preparsed)
        if status < 0:
            continue
        for i in range(int(length.value)):
            cap = arr[i]
            if cap.IsRange:
                usages = range(cap.Usage.Range.UsageMin,
                               cap.Usage.Range.UsageMax + 1)
            else:
                usages = [cap.Usage.NotRange.Usage]
            for usage in usages:
                out.append(ValueCap(
                    report_type=report_type,
                    report_id=int(cap.ReportID),
                    usage_page=int(cap.UsagePage),
                    usage=int(usage),
                    link_collection=int(cap.LinkCollection),
                    logical_min=int(cap.LogicalMin),
                    logical_max=int(cap.LogicalMax),
                    bit_size=int(cap.BitSize),
                    report_count=int(cap.ReportCount),
                ))
    return out


def list_devices() -> list[HidDevice]:
    """枚举本机当前存在的所有 HID 接口。"""
    guid = GUID(*GUID_DEVINTERFACE_HID)
    hdev = _setup.SetupDiGetClassDevsW(ctypes.byref(guid), None, None,
                                       DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not hdev or hdev == INVALID_HANDLE_VALUE:
        raise HidError(ctypes.get_last_error(), "SetupDiGetClassDevsW 失败")
    devices: list[HidDevice] = []
    seen: set[str] = set()
    try:
        index = 0
        while index < 512:
            did = SP_DEVICE_INTERFACE_DATA()
            did.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not _setup.SetupDiEnumDeviceInterfaces(hdev, None,
                                                      ctypes.byref(guid), index,
                                                      ctypes.byref(did)):
                break
            index += 1
            needed = wt.DWORD(0)
            _setup.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did),
                                                    None, 0, ctypes.byref(needed), None)
            if not needed.value:
                continue
            size = int(needed.value) + 64
            raw = ctypes.create_string_buffer(size)
            ctypes.memset(raw, 0, size)
            # cbSize 必须是 SP_DEVICE_INTERFACE_DETAIL_DATA_W 的结构体大小：
            # x64 因对齐是 8，x86 是 6。DevicePath 实际从偏移 4 开始。
            ctypes.cast(raw, ctypes.POINTER(wt.DWORD)).contents.value = (
                8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6)
            if not _setup.SetupDiGetDeviceInterfaceDetailW(
                    hdev, ctypes.byref(did), ctypes.cast(raw, ctypes.c_void_p),
                    size, ctypes.byref(needed), None):
                continue
            base = ctypes.addressof(raw)
            path = ctypes.wstring_at(base + 4)
            if not path.startswith("\\\\"):
                other = ctypes.wstring_at(base + 8)
                if other.startswith("\\\\"):
                    path = other
            if not path or path in seen:
                continue
            seen.add(path)
            devices.append(_describe(path))
    finally:
        _setup.SetupDiDestroyDeviceInfoList(hdev)
    return devices


def read_value(dev: HidDevice, cap: ValueCap, raw: bytes) -> int | None:
    """从一个原始报告里取出 cap 对应的值（未归一化，按逻辑范围原样返回）。"""
    if not raw:
        return None
    buf = bytearray(raw)
    if cap.report_id:
        if not buf or buf[0] != cap.report_id:
            return None
    else:
        buf = bytearray([0]) + buf  # HidP 要求首字节是 report id
    try:
        with _hid_device_open(dev) as h:
            preparsed = ctypes.c_void_p()
            if not _hid.HidD_GetPreparsedData(h.handle, ctypes.byref(preparsed)):
                return None
            try:
                value = wt.ULONG(0)
                data = ctypes.create_string_buffer(bytes(buf), len(buf))
                status = _hid.HidP_GetUsageValue(cap.report_type, cap.usage_page,
                                                 cap.link_collection, cap.usage,
                                                 ctypes.byref(value), preparsed,
                                                 data, len(buf))
                if status < 0:
                    return None
                return int(value.value)
            finally:
                _hid.HidD_FreePreparsedData(preparsed)
    except HidError:
        return None


if __name__ == "__main__":
    for d in list_devices():
        print(f"{d.vid_pid}  usage={d.usage_page:04X}/{d.usage:04X}  "
              f"in/out/feat={d.input_len}/{d.output_len}/{d.feature_len}  "
              f"{d.name!r}  {d.path}")
