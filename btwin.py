# -*- coding: utf-8 -*-
"""蓝牙（BLE）设备电量：读 GATT Battery Service（0x180F / 0x2A19）。

Windows 把 BLE 外设的每个 GATT 服务挂在 BTHLEDevice#{服务UUID}_Dev_<地址> 下，
用服务句柄调用 Win32 GATT API 就能直接读电量 —— 不需要 WinRT，也不需要第三方库。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
from dataclasses import dataclass

import hidwin as H

bt = ctypes.WinDLL("bluetoothapis", use_last_error=True)

#: GATT 服务设备接口（BTHLEDevice#{uuid}_Dev_<addr>）
GUID_GATT_SERVICE = (0x6E3BB679, 0x4372, 0x40C8,
                     (0x9E, 0xAA, 0x45, 0x09, 0xDF, 0x26, 0x0C, 0xD8))
#: BLE 设备接口（备用，BTHLE#Dev_<addr>）
GUID_BTHLE_DEVICE = (0x781AEE18, 0x7733, 0x4CE4,
                     (0xAD, 0xD0, 0x91, 0xF4, 0x1C, 0x67, 0xB5, 0x92))

BATTERY_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_CHAR = "00002a19-0000-1000-8000-00805f9b34fb"

BLUETOOTH_GATT_FLAG_NONE = 0
ERROR_MORE_DATA = -2147483639  # 0x80070005? 实际值只用来判断“还有更多数据”


class _UuidValue(ctypes.Union):
    _fields_ = [("ShortUuid", wt.USHORT), ("LongUuid", H.GUID)]


class BTH_LE_UUID(ctypes.Structure):
    _fields_ = [("IsShortUuid", ctypes.c_ubyte), ("Value", _UuidValue)]


class BTH_LE_GATT_SERVICE(ctypes.Structure):
    _fields_ = [("ServiceUuid", BTH_LE_UUID), ("AttributeHandle", wt.USHORT)]


class BTH_LE_GATT_CHARACTERISTIC(ctypes.Structure):
    _fields_ = [("ServiceHandle", wt.USHORT),
                ("CharacteristicUuid", BTH_LE_UUID),
                ("AttributeHandle", wt.USHORT),
                ("CharacteristicProperties", wt.USHORT)]


class BTH_LE_GATT_CHARACTERISTIC_VALUE(ctypes.Structure):
    #: Data 固定 64 字节，够放任何电池特征值，避免再 cast 动态缓冲区
    _fields_ = [("DataSize", wt.ULONG), ("Data", ctypes.c_ubyte * 64)]


bt.BluetoothGATTGetServices.restype = ctypes.c_long
bt.BluetoothGATTGetServices.argtypes = [wt.HANDLE, wt.USHORT,
                                        ctypes.POINTER(BTH_LE_GATT_SERVICE),
                                        ctypes.POINTER(wt.USHORT), wt.ULONG]
bt.BluetoothGATTGetCharacteristics.restype = ctypes.c_long
bt.BluetoothGATTGetCharacteristics.argtypes = [
    wt.HANDLE, ctypes.POINTER(BTH_LE_GATT_SERVICE), wt.USHORT,
    ctypes.POINTER(BTH_LE_GATT_CHARACTERISTIC), ctypes.POINTER(wt.USHORT),
    wt.ULONG]
bt.BluetoothGATTGetCharacteristicValue.restype = ctypes.c_long
bt.BluetoothGATTGetCharacteristicValue.argtypes = [
    wt.HANDLE, ctypes.POINTER(BTH_LE_GATT_CHARACTERISTIC), wt.ULONG,
    ctypes.POINTER(BTH_LE_GATT_CHARACTERISTIC_VALUE),
    ctypes.POINTER(wt.USHORT), wt.ULONG]


@dataclass
class BtDevice:
    address: str          # 12 位大写十六进制
    name: str
    battery: int | None
    error: str = ""

    @property
    def key(self) -> str:
        return f"bt:{self.address}"

    @property
    def subtitle(self) -> str:
        addr = ":".join(self.address[i:i + 2] for i in range(0, 12, 2))
        return f"{addr} · 蓝牙"


# ------------------------------------------------------------------ 工具


def guid_to_str(guid: H.GUID) -> str:
    d4 = bytes(guid.Data4)
    tail = "".join(f"{b:02x}" for b in d4[2:])
    return (f"{guid.Data1:08x}-{guid.Data2:04x}-{guid.Data3:04x}-"
            f"{d4[0]:02x}{d4[1]:02x}-{tail}")


def uuid_str(uuid: BTH_LE_UUID) -> str:
    if uuid.IsShortUuid:
        return f"0000{uuid.Value.ShortUuid:04x}-0000-1000-8000-00805f9b34fb"
    return guid_to_str(uuid.Value.LongUuid)


def address_from_path(path: str) -> str:
    low = path.lower()
    marker = "_dev_"
    start = low.find(marker)
    if start < 0:
        return ""
    rest = low[start + len(marker):]
    for token in rest.replace("#", "_").split("_"):
        token = token.strip()
        if len(token) >= 12:
            candidate = token[-12:]
            if all(c in "0123456789abcdef" for c in candidate):
                return candidate.upper()
    return ""


def connected_addresses() -> set[str] | None:
    """当前处于「已连接」状态的 BLE 设备地址（WinRT 枚举，只读）。

    返回 None 表示查询失败（此时调用方按“全部设备”处理）。
    Windows 会为**已配对**的设备保留 GATT 缓存，所以光看服务接口会把
    没连上的设备也列出来（还会读到上次的旧电量）——必须再问一次连接状态。
    """
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "bt_connected.ps1")
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", script],
            capture_output=True, timeout=15, creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or b"").decode("utf-8-sig", "replace").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, str):
        data = [data]
    if not isinstance(data, list):
        return None
    return {str(item).strip().upper().replace(":", "")[-12:]
            for item in data if item}


_registry = __import__("winreg")


def device_name(address: str) -> str:
    path = (r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices\\"
            + address.lower())
    try:
        with _registry.OpenKey(_registry.HKEY_LOCAL_MACHINE, path) as key:
            value, _ = _registry.QueryValueEx(key, "Name")
            if isinstance(value, bytes):
                text = value.decode("utf-8", "ignore").strip("\x00").strip()
            else:
                text = str(value).strip("\x00").strip()
            if text:
                return text
    except OSError:
        pass
    return ""


# ------------------------------------------------------------------ 接口枚举


def _enum_paths(guid_tuple) -> list[str]:
    guid = H.GUID(*guid_tuple)
    hdev = H._setup.SetupDiGetClassDevsW(ctypes.byref(guid), None, None,
                                         H.DIGCF_PRESENT | H.DIGCF_DEVICEINTERFACE)
    if not hdev or hdev == H.INVALID_HANDLE_VALUE:
        return []
    paths: list[str] = []
    index = 0
    try:
        while index < 256:
            did = H.SP_DEVICE_INTERFACE_DATA()
            did.cbSize = ctypes.sizeof(H.SP_DEVICE_INTERFACE_DATA)
            if not H._setup.SetupDiEnumDeviceInterfaces(
                    hdev, None, ctypes.byref(guid), index, ctypes.byref(did)):
                break
            index += 1
            needed = wt.DWORD(0)
            H._setup.SetupDiGetDeviceInterfaceDetailW(
                hdev, ctypes.byref(did), None, 0, ctypes.byref(needed), None)
            if not needed.value:
                continue
            size = int(needed.value) + 64
            raw = ctypes.create_string_buffer(size)
            ctypes.memset(raw, 0, size)
            ctypes.cast(raw, ctypes.POINTER(wt.DWORD)).contents.value = (
                8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6)
            if not H._setup.SetupDiGetDeviceInterfaceDetailW(
                    hdev, ctypes.byref(did), ctypes.cast(raw, ctypes.c_void_p),
                    size, ctypes.byref(needed), None):
                continue
            base = ctypes.addressof(raw)
            path = ctypes.wstring_at(base + 4)
            if not path.startswith("\\\\"):
                path = ctypes.wstring_at(base + 8)
            if path:
                paths.append(path)
    finally:
        H._setup.SetupDiDestroyDeviceInfoList(hdev)
    return paths


def battery_service_paths() -> dict[str, str]:
    """地址 → Battery 服务的设备接口路径。"""
    found: dict[str, str] = {}
    for path in _enum_paths(GUID_GATT_SERVICE):
        if BATTERY_SERVICE not in path.lower():
            continue
        address = address_from_path(path)
        if address and address not in found:
            found[address] = path
    return found


# ------------------------------------------------------------------ 读电量


def _read_battery_path(path: str,
                       flag: int = BLUETOOTH_GATT_FLAG_NONE) -> int | None:
    try:
        handle = H._open_path(path, 0x80000000)  # GENERIC_READ
    except H.HidError:
        return None
    try:
        count = wt.USHORT(0)
        bt.BluetoothGATTGetServices(handle, 0, None, ctypes.byref(count),
                                    BLUETOOTH_GATT_FLAG_NONE)
        if count.value == 0:
            return None
        services = (BTH_LE_GATT_SERVICE * count.value)()
        actual = wt.USHORT(0)
        if bt.BluetoothGATTGetServices(handle, count.value, services,
                                       ctypes.byref(actual),
                                       BLUETOOTH_GATT_FLAG_NONE) != 0:
            return None
        for service in services[:actual.value]:
            if uuid_str(service.ServiceUuid) != BATTERY_SERVICE:
                continue
            char_count = wt.USHORT(0)
            bt.BluetoothGATTGetCharacteristics(
                handle, ctypes.byref(service), 0, None,
                ctypes.byref(char_count), BLUETOOTH_GATT_FLAG_NONE)
            if char_count.value == 0:
                continue
            chars = (BTH_LE_GATT_CHARACTERISTIC * char_count.value)()
            char_actual = wt.USHORT(0)
            if bt.BluetoothGATTGetCharacteristics(
                    handle, ctypes.byref(service), char_count.value, chars,
                    ctypes.byref(char_actual), BLUETOOTH_GATT_FLAG_NONE) != 0:
                continue
            for char in chars[:char_actual.value]:
                if uuid_str(char.CharacteristicUuid) != BATTERY_LEVEL_CHAR:
                    continue
                required = wt.USHORT(0)
                bt.BluetoothGATTGetCharacteristicValue(
                    handle, ctypes.byref(char), 0, None,
                    ctypes.byref(required), flag)
                if required.value == 0:
                    continue
                value = BTH_LE_GATT_CHARACTERISTIC_VALUE()
                size = ctypes.sizeof(value)
                filled = wt.USHORT(0)
                hr = bt.BluetoothGATTGetCharacteristicValue(
                    handle, ctypes.byref(char), size, ctypes.byref(value),
                    ctypes.byref(filled), flag)
                if hr != 0 or value.DataSize < 1:
                    continue
                level = int(value.Data[0])
                if 0 <= level <= 100:
                    return level
        return None
    finally:
        H._k32.CloseHandle(handle)


def read_devices() -> list[BtDevice]:
    """枚举当前已连接的 BLE 外设并读电量（顺序读，GATT 调用很快）。"""
    paths = battery_service_paths()
    connected = connected_addresses()
    if connected is not None:
        # 只保留真正连着的设备，避免已配对但离线的设备显示旧缓存电量
        paths = {addr: path for addr, path in paths.items()
                 if addr in connected}
    devices: list[BtDevice] = []
    for address, path in paths.items():
        level = _read_battery_path(path)
        name = device_name(address) or "蓝牙设备"
        devices.append(BtDevice(address=address, name=name, battery=level,
                                error="" if level is not None else "读不到电量"))
    devices.sort(key=lambda d: d.name.lower())
    return devices


if __name__ == "__main__":
    devices = read_devices()
    if "--json" in sys.argv:
        print(json.dumps(
            [{"address": d.address, "name": d.name, "battery": d.battery}
             for d in devices], ensure_ascii=False), flush=True)
        # GATT 句柄在解释器退出时可能触发驱动侧访问冲突，直接跳过清理
        os._exit(0)
    for device in devices:
        print(f"{device.address}  {device.name}  {device.battery}", flush=True)
    os._exit(0)
