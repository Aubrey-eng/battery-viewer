# -*- coding: utf-8 -*-
"""电量读取策略：标准 HID 电量 → 厂商私有协议 → 罗技 HID++ → 手动标定。

每个策略都是「试探式」的：拿不到合理结果就返回 None，交给下一个策略。
所有写操作都只发只读查询帧，不修改设备设置。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

import hidwin as H

#: 已知会用 BY Tech / Pulsar 系协议（0x08 帧 + 0x55 校验）的厂商
BYTECH_VIDS = {
    0x3554,  # BY Tech（Pulsar、VGN 等）
    0x373B,  # ATK / VXE
    0x3367,  # Endgame Gear
    0x3057,  # VAXEE
    0x1915,  # Nordic 方案公版
    0x391D,  # VGN
}

#: 罗技
LOGITECH_VID = 0x046D


@dataclass
class Reading:
    percent: int
    source: str
    charging: bool | None = None
    voltage_mv: int | None = None
    note: str = ""

    def detail(self) -> str:
        parts = [self.source]
        if self.voltage_mv:
            parts.append(f"{self.voltage_mv / 1000:.2f} V")
        if self.charging is True:
            parts.append("充电中")
        elif self.charging is False:
            parts.append("放电中")
        if self.note:
            parts.append(self.note)
        return " · ".join(parts)


# ------------------------------------------------------------------ 工具


def _clean_percent(value: int | None, logical_max: int = 100) -> int | None:
    if value is None:
        return None
    if logical_max and logical_max > 0 and logical_max != 100:
        value = round(value * 100 / logical_max)
    if 0 <= value <= 100:
        return int(value)
    return None


def _read_matching(reader: H.HidHandle, match: Callable[[bytes], bool],
                   timeout: float = 0.8, chunk: float = 0.12) -> bytes | None:
    """在一小段时间窗里读输入报告，命中就立刻返回，避免干等满超时。"""
    deadline = time.monotonic() + timeout
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            return None
        for raw in reader.read_inputs(int(min(chunk, remain) * 1000),
                                      max_reports=16):
            if match(raw):
                return raw
        if time.monotonic() >= deadline:
            return None


def _candidates(devices: Iterable[H.HidDevice]) -> list[H.HidDevice]:
    """挑出值得尝试读电量的接口，避免对 40 个 HID 接口挨个探测。"""
    out = []
    for d in devices:
        if d.error or not d.path:
            continue
        known = d.vendor_id in BYTECH_VIDS or d.vendor_id == LOGITECH_VID
        vendor_page = d.usage_page >= 0xFF00 or d.usage_page == 0x0001
        has_battery = bool(d.battery_caps())
        if known or has_battery or (vendor_page and d.input_len >= 4):
            out.append(d)
    return out


# ------------------------------------------------------------------ 1. 标准 HID 电量


def probe_standard_hid(dev: H.HidDevice) -> Reading | None:
    """Generic Device Controls / Battery Strength（0x0006/0x0020）。

    这是 HID 规范里唯一的「标准电量」，不少正规接收器会实现。
    """
    caps = dev.battery_caps()
    if not caps:
        return None

    # 优先 Feature Report：一次读取，不用等事件
    for cap in [c for c in caps if c.report_type == H.HidP_Feature]:
        if dev.feature_len < 2:
            continue
        try:
            with H._hid_device_open(dev) as h:
                raw = h.get_feature(cap.report_id, dev.feature_len)
            value = H.read_value(dev, cap, raw)
            percent = _clean_percent(value, cap.logical_max)
            if percent is not None:
                return Reading(percent=percent, source="标准 HID 电量",
                               note="Feature Report")
        except H.HidError:
            continue

    # 其次 Input Report：有些设备只在状态变化时上报
    input_caps = [c for c in caps if c.report_type == H.HidP_Input]
    if input_caps and dev.input_len:
        try:
            with H._hid_device_open(dev, read=True) as h:
                reports = h.read_inputs(500, max_reports=32)
        except H.HidError:
            reports = []
        for raw in reports:
            for cap in input_caps:
                if cap.report_id and (not raw or raw[0] != cap.report_id):
                    continue
                value = H.read_value(dev, cap, raw)
                percent = _clean_percent(value, cap.logical_max)
                if percent is not None:
                    return Reading(percent=percent, source="标准 HID 电量",
                                   note="Input Report")
    return None


# ------------------------------------------------------------------ 2. BY Tech / Pulsar 系


def _checksum(values: Iterable[int]) -> int:
    return (0x55 - sum(values)) & 0xFF


def build_query(size: int, command: int = 0x04) -> bytes:
    """构造 BY Tech 帧：0x08 + 命令 + 填充 + 0x55 补和校验。"""
    body = [0x08, command] + [0] * (size - 3)
    return bytes(body + [_checksum(body)])


def _query(dev: H.HidDevice, frame: bytes,
           match: Callable[[bytes], bool], timeout: float = 0.35) -> bytes | None:
    """先挂读句柄，再在后台线程发命令，最后等符合条件的回复。

    HID 的输入报告只会投递给「那一刻已经打开」的句柄；先写后开读句柄
    会漏掉设备的即时应答（VGN 键盘就是这样丢回复的）。
    """
    def send() -> None:
        try:
            with H._hid_device_open(dev, write=True) as handle:
                handle.write_output(frame)
        except H.HidError:
            pass

    try:
        with H._hid_device_open(dev, read=True) as reader:
            threading.Thread(target=send, daemon=True).start()
            return _read_matching(reader, match, timeout)
    except H.HidError:
        return None


def probe_bytech(dev: H.HidDevice) -> Reading | None:
    """BY Tech / Pulsar 私有协议：17 字节报告，命令 0x04 返回电量。

    已在 VGN DragonFly（3554:FB50）上实测：`08 04 00...49` →
    `08 04 00 00 00 02 4b 00 0f 92 ...`，第 6 字节是电量、第 8/9 字节是大端毫伏。
    """
    if dev.usage_page < 0xFF00:
        return None
    if dev.output_len < 17 or dev.input_len < 17:
        return None
    frame = build_query(min(dev.output_len, 32))
    raw = _query(dev, frame, lambda r: len(r) >= 10 and r[1] == 0x04)
    if raw is None:
        return None
    percent = raw[6]
    if not 0 <= percent <= 100:
        return None
    voltage = int.from_bytes(raw[8:10], "big")
    return Reading(percent=percent, source="BY Tech / Pulsar 协议",
                   charging=bool(raw[7]),
                   voltage_mv=voltage if 2000 <= voltage <= 5000 else None)


# ------------------------------------------------------------------ 3. VGN 键盘（2.4G 接收器）


#: VGN 键盘接收器（20 字节 vendor 报告，Report ID 0x13）
VGN_KEYBOARD_VIDS = {0x391D}


def probe_vgn_keyboard(dev: H.HidDevice) -> Reading | None:
    """VGN N75 V2 这类键盘接收器：发 0x4A 查询，第 6/7 字节是电量与状态。

    实测（391D:2A01）：
        发送: 13 4A 00 00 ... 00
        回复: 13 4A 01 00 02 63 00 ...   ← 0x63 = 99%，flags 0x00 = 放电
        充电时 flags = 0x10，电量从 100 掉到 99。
    """
    if dev.vendor_id not in VGN_KEYBOARD_VIDS:
        return None
    if dev.usage_page != 0xFF02 or dev.output_len != 20 or dev.input_len != 20:
        return None

    frame = bytes([0x13, 0x4A]) + bytes(18)
    raw = _query(dev, frame, lambda r: len(r) >= 7 and r[1] == 0x4A, timeout=0.35)
    if raw is None:
        return None
    level = raw[5]
    if not 0 <= level <= 100:
        return None
    flags = raw[6]
    return Reading(percent=level, source="VGN 键盘 2.4G 协议",
                   charging=bool(flags & 0x10),
                   note="接收器在线" if raw[2] == 0x01 else "")


# ------------------------------------------------------------------ 4. ATK / VXE 373B


ATK_QUERY = bytes([0x08, 0x7D, 0x72, 0x02, 0x00, 0x01, 0x07, 0x01])


def probe_atk(dev: H.HidDevice) -> Reading | None:
    """ATK / VXE 系（usage page 0xFF05，Report ID 0x08，64 字节）。

    参考社区逆向结果：请求 `08 7D 72 02 00 01 07 01`，
    回复需满足 resp[1]==0x72 且 resp[5]==0x07，电量在 resp[7]。
    """
    if dev.usage_page != 0xFF05:
        return None
    if dev.output_len < 8 or dev.input_len < 8:
        return None
    frame = ATK_QUERY + bytes(max(0, dev.output_len - len(ATK_QUERY)))
    raw = _query(dev, frame[:dev.output_len],
                 lambda r: len(r) >= 8 and r[1] == 0x72 and r[5] == 0x07)
    if raw is not None and 0 <= raw[7] <= 100:
        return Reading(percent=raw[7], source="ATK / VXE 协议")
    return None


# ------------------------------------------------------------------ 4. 罗技 HID++ 2.0


def _hidpp_exchange(dev: H.HidDevice, payload: bytes,
                    expect_feature: int | None = None) -> bytes | None:
    size = dev.output_len or 20
    frame = payload[:size] + bytes(max(0, size - len(payload)))

    def match(raw: bytes) -> bool:
        if len(raw) < 5 or raw[0] not in (0x10, 0x11):
            return False
        return expect_feature is None or raw[2] == expect_feature

    return _query(dev, frame, match, timeout=0.35)


def probe_logitech(dev: H.HidDevice) -> Reading | None:
    """罗技 HID++ 2.0：特性 0x1004（Unified Battery）优先，退回 0x1000（电压）。"""
    if dev.vendor_id != LOGITECH_VID or dev.usage_page < 0xFF00:
        return None
    if dev.output_len < 7 or dev.input_len < 7:
        return None

    for device_index in (0x01, 0x02, 0x03, 0x04, 0x05, 0x06):
        # 1) getFeature(0x1004)
        resp = _hidpp_exchange(dev, bytes([0x10, device_index, 0x00, 0x10, 0x04, 0x00]))
        if not resp or resp[1] != device_index or resp[2] == 0 or resp[3:5] != b"\x10\x04":
            continue
        feature_index = resp[2]
        # 2) getBatteryLevelStatus
        resp = _hidpp_exchange(
            dev, bytes([0x10, device_index, feature_index, 0x10, 0x00, 0x00]),
            expect_feature=feature_index)
        if resp and len(resp) >= 7 and resp[3] == 0x10:
            percent = resp[4]
            status = resp[5]
            if 0 <= percent <= 100:
                charging = None
                if status & 0x02:
                    charging = True
                elif status & 0x01:
                    charging = False
                return Reading(percent=percent, source="罗技 HID++ 2.0",
                               charging=charging, note=f"设备 {device_index}")
    return None


# ------------------------------------------------------------------ 5. 手动标定


@dataclass
class Calibration:
    """用户为「不认识的设备」手写的读取规则。

    devices.json 示例::

        {
          "3554:FB50": {
            "report_id": 8,
            "command": "04",
            "value_index": 6,
            "scale": 1,
            "source": "我的鼠标"
          }
        }
    """
    report_id: int
    command: bytes
    value_index: int
    scale: float = 1.0
    source: str = "手动标定"


def load_calibrations(path: str) -> dict[str, Calibration]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    out: dict[str, Calibration] = {}
    for key, item in raw.items():
        try:
            out[key.upper()] = Calibration(
                report_id=int(item.get("report_id", 0)),
                command=bytes.fromhex(str(item.get("command", ""))),
                value_index=int(item["value_index"]),
                scale=float(item.get("scale", 1.0)),
                source=str(item.get("source", "手动标定")),
            )
        except (KeyError, ValueError, TypeError):
            continue
    return out


def probe_calibrated(dev: H.HidDevice, cal: Calibration) -> Reading | None:
    if dev.output_len < 2 or dev.input_len < 2:
        return None
    size = dev.output_len
    body = [cal.report_id] + list(cal.command)
    if len(body) > size:
        return None
    frame = bytes(body + [0] * (size - len(body)))
    try:
        with H._hid_device_open(dev, write=True) as h:
            h.write_output(frame)
        with H._hid_device_open(dev, read=True) as h:
            reports = h.read_inputs(700, max_reports=16)
    except H.HidError:
        return None
    for raw in reports:
        if len(raw) <= cal.value_index:
            continue
        value = raw[cal.value_index]
        percent = _clean_percent(round(value * cal.scale))
        if percent is not None:
            return Reading(percent=percent, source=cal.source)
    return None


# ------------------------------------------------------------------ 汇总


def read_device(dev: H.HidDevice,
                calibrations: dict[str, Calibration] | None = None) -> Reading | None:
    """按优先级尝试所有策略。"""
    cal = (calibrations or {}).get(dev.vid_pid.upper())
    if cal is not None:
        reading = probe_calibrated(dev, cal)
        if reading is not None:
            return reading
    for probe in (probe_standard_hid, probe_vgn_keyboard, probe_bytech,
                  probe_atk, probe_logitech):
        reading = probe(dev)
        if reading is not None:
            return reading
    return None


def collect(devices: Iterable[H.HidDevice],
            calibrations: dict[str, Calibration] | None = None) -> list[tuple[H.HidDevice, Reading]]:
    results: list[tuple[H.HidDevice, Reading]] = []
    seen: set[str] = set()
    for dev in _candidates(devices):
        reading = read_device(dev, calibrations)
        if reading is None:
            continue
        # 一个物理接收器的多个 collection 可能都返回同一结果，按 VID:PID + 名称去重
        key = f"{dev.vid_pid}:{dev.name}"
        if key in seen:
            continue
        seen.add(key)
        results.append((dev, reading))
    return results
