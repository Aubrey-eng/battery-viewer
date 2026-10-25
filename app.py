# -*- coding: utf-8 -*-
"""设备电量 —— 2.4G 与蓝牙无线外设电量面板 + 任务栏圆环图标。

用法：
    python app.py              纯托盘（只在任务栏显示圆环）
    python app.py --show       打开面板 + 托盘
    python app.py --once       命令行读一次
    python app.py --json       命令行输出 JSON
    python app.py --dump       列出所有 HID 接口
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import ctypes
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import hidwin as H
import battery as B
import startup as S
import tray as T

# ------------------------------------------------------------------ 外观

BG = "#0f1115"
CARD = "#171a21"
CARD_BORDER = "#232833"
TEXT = "#e8ecf3"
MUTED = "#8b94a7"
ACCENT = "#5b8cff"
GREEN = "#3ddc84"
AMBER = "#ffb02e"
RED = "#ff5a5f"

FONT = "Microsoft YaHei UI"
FONT_MONO = "Consolas"

APP_TITLE = "设备电量"


def level_color(percent: int | None) -> str:
    if percent is None:
        return MUTED
    if percent >= 60:
        return GREEN
    if percent >= 30:
        return AMBER
    return RED


def set_dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _display_name(dev: H.HidDevice) -> str:
    name = dev.name
    for suffix in (" 2.4G Dongle", " 2.4G dongle", " Dongle"):
        if name.endswith(suffix):
            name = name[:-len(suffix)].strip()
            break
    return name or dev.name


def _device_kind(dev: H.HidDevice) -> str:
    if "Dongle" in dev.name or dev.usage_page == 0xFF02:
        return "2.4G 接收器"
    if dev.usage_page >= 0xFF00:
        return "厂商通道"
    return "HID 接口"


def _calibration_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "devices.json")


def _collect_bluetooth() -> list[dict] | None:
    """蓝牙电量放到子进程里读：GATT 句柄在退出时可能让驱动崩，隔离掉。

    失败时返回 None（调用方保留上一次的结果，避免设备“闪没”）。
    """
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "btwin.py")
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    try:
        proc = subprocess.run([sys.executable, script, "--json"],
                              capture_output=True, timeout=20,
                              creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or b"").decode("utf-8", "replace")
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, list):
            return data
    return None


@dataclass
class CardModel:
    key: str
    title: str
    subtitle: str
    percent: int | None
    charging: bool | None
    detail: str


# ------------------------------------------------------------------ 卡片


class BatteryCard:
    """一张设备卡片：名称 + 大号电量 + 进度条 + 来源。"""

    def __init__(self, master, model: CardModel):
        import tkinter as tk

        self.tk = tk
        self.model = model
        self.frame = tk.Frame(master, bg=CARD, highlightthickness=1,
                              highlightbackground=CARD_BORDER)
        self.frame.pack(fill="x", padx=2, pady=6)

        body = tk.Frame(self.frame, bg=CARD)
        body.pack(fill="x", padx=16, pady=10)

        top = tk.Frame(body, bg=CARD)
        top.pack(fill="x")

        left = tk.Frame(top, bg=CARD)
        left.pack(side="left", fill="x", expand=True)

        self.name = tk.Label(left, text=model.title, bg=CARD, fg=TEXT,
                             font=(FONT, 11, "bold"), anchor="w")
        self.name.pack(anchor="w")

        self.meta = tk.Label(left, text=model.subtitle, bg=CARD, fg=MUTED,
                             font=(FONT, 8), anchor="w")
        self.meta.pack(anchor="w", pady=(2, 0))

        right = tk.Frame(top, bg=CARD)
        right.pack(side="right")
        self.percent = tk.Label(right, text="--", bg=CARD, fg=TEXT,
                                font=(FONT, 26, "bold"))
        self.percent.pack(side="left")
        self.unit = tk.Label(right, text="%", bg=CARD, fg=MUTED,
                             font=(FONT, 10))
        self.unit.pack(side="left", anchor="s", pady=(0, 7))

        self.bar = tk.Canvas(body, height=8, bg=CARD, highlightthickness=0)
        self.bar.pack(fill="x", pady=(8, 6))

        bottom = tk.Frame(body, bg=CARD)
        bottom.pack(fill="x")
        self.source = tk.Label(bottom, text="", bg=CARD, fg=MUTED,
                               font=(FONT, 8), anchor="w")
        self.source.pack(side="left")
        self.badge = tk.Label(bottom, text="", bg=CARD, fg=GREEN,
                              font=(FONT, 8, "bold"), anchor="e")
        self.badge.pack(side="right")

        self.bar.bind("<Configure>", lambda _e: self._draw_bar())
        self._color = MUTED
        self._percent = 0
        self.update(model)

    # -- 数据刷新 -----------------------------------------------------
    def update(self, model: CardModel) -> None:
        self.model = model
        color = level_color(model.percent)
        self.percent.configure(text="--" if model.percent is None
                               else str(model.percent), fg=color)
        self.source.configure(text=model.detail)
        if model.charging is True:
            self.badge.configure(text="⚡ 充电中", fg=AMBER)
        else:
            self.badge.configure(text="")
        self._color = color
        self._percent = model.percent or 0
        self._draw_bar()

    def _draw_bar(self) -> None:
        bar = self.bar
        bar.delete("all")
        width = bar.winfo_width() or 360
        height = 8
        radius = height / 2
        self._round_rect(0, 0, width, height, radius, CARD_BORDER)
        if self._percent:
            filled = max(radius * 2, width * self._percent / 100)
            self._round_rect(0, 0, filled, height, radius, self._color)

    def _round_rect(self, x1, y1, x2, y2, r, color) -> None:
        bar = self.bar
        points = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r,
                  x2, y2, x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r,
                  x1, y1 + r, x1, y1]
        bar.create_polygon(points, smooth=True, splinesteps=24, fill=color,
                           outline=color)


# ------------------------------------------------------------------ 选项卡


class DeviceTab:
    """一个可滚动的设备卡片列表。"""

    def __init__(self, master, empty_text: str):
        import tkinter as tk

        self.tk = tk
        self.frame = tk.Frame(master, bg=BG)
        self.cards: dict[str, BatteryCard] = {}

        wrap = tk.Frame(self.frame, bg=BG)
        wrap.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        self.scrollbar = tk.Scrollbar(wrap, orient="vertical",
                                      command=self.canvas.yview,
                                      width=6, bd=0, relief="flat",
                                      troughcolor=BG, bg="#2a3242",
                                      activebackground=MUTED,
                                      highlightthickness=0)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._window = self.canvas.create_window((0, 0), window=self.inner,
                                                 anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", lambda _e: self.canvas.bind_all(
            "<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all(
            "<MouseWheel>"))

        self.empty = tk.Label(self.inner, text=empty_text, bg=BG, fg=MUTED,
                              font=(FONT, 9), justify="center")
        self.empty.pack(pady=60)

    def _on_inner(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._sync_scrollbar()

    def _on_canvas(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)
        self._sync_scrollbar()

    def _sync_scrollbar(self) -> None:
        need = self.inner.winfo_height() > self.canvas.winfo_height()
        if need and not self.scrollbar.winfo_manager():
            self.scrollbar.pack(side="right", fill="y")
        elif not need and self.scrollbar.winfo_manager():
            self.scrollbar.pack_forget()

    def _on_wheel(self, event) -> None:
        try:
            if self.inner.winfo_height() > self.canvas.winfo_height():
                self.canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass

    def clear(self) -> None:
        for card in self.cards.values():
            card.frame.destroy()
        self.cards.clear()

    def render(self, models: list[CardModel]) -> None:
        if not models:
            self.clear()
            self.empty.pack(pady=60)
            return
        self.empty.pack_forget()
        order = []
        for model in models:
            order.append(model.key)
            card = self.cards.get(model.key)
            if card is None:
                self.cards[model.key] = BatteryCard(self.inner, model)
            else:
                card.update(model)
        for key in list(self.cards):
            if key not in order:
                self.cards.pop(key).frame.destroy()


# ------------------------------------------------------------------ 主窗口


class App:
    def __init__(self, root, start_hidden: bool = True):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.queue: queue.Queue = queue.Queue()
        self.calibrations = B.load_calibrations(_calibration_path())
        self.busy = False
        self._timer = None
        self._hidden_hint = True
        self._start_hidden = start_hidden
        self._first_render = True
        self._last_bt: list[dict] = []

        root.title(APP_TITLE)
        root.configure(bg=BG)
        root.geometry("480x600")
        root.minsize(430, 460)
        root.protocol("WM_DELETE_WINDOW", self.hide_window)

        self._build()
        self.tray = T.Tray()
        self.tray.start()
        if start_hidden:
            root.withdraw()

        root.after(120, self._pump)
        root.after(50, self.refresh)

    # -- 界面 ---------------------------------------------------------
    def _build(self) -> None:
        tk = self.tk
        ttk = self.ttk

        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=18, pady=(16, 8))

        title_box = tk.Frame(header, bg=BG)
        title_box.pack(side="left")
        tk.Label(title_box, text=APP_TITLE, bg=BG, fg=TEXT,
                 font=(FONT, 16, "bold")).pack(anchor="w")
        tk.Label(title_box, text="2.4G / 蓝牙 无线外设电量", bg=BG, fg=MUTED,
                 font=(FONT, 8)).pack(anchor="w", pady=(1, 0))

        self.refresh_btn = tk.Button(
            header, text="刷新", command=self.refresh, bg="#1c2130", fg=TEXT,
            activebackground="#252c3d", activeforeground=TEXT, bd=0,
            font=(FONT, 9), padx=16, pady=6, cursor="hand2",
            highlightthickness=0)
        self.refresh_btn.pack(side="right")

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TNotebook", background=BG, borderwidth=0,
                        tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", background=CARD, foreground=MUTED,
                        padding=(18, 7), borderwidth=0, font=(FONT, 9))
        style.map("TNotebook.Tab",
                  background=[("selected", "#222a38"), ("active", "#1c2431")],
                  foreground=[("selected", TEXT)])

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=14, pady=(2, 0))

        frame_24g = tk.Frame(self.notebook, bg=BG)
        frame_bt = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(frame_24g, text="2.4G 设备")
        self.notebook.add(frame_bt, text="蓝牙设备")

        self.tab_24g = DeviceTab(
            frame_24g,
            "没有发现 2.4G 设备\n\n"
            "· 接收器插好了吗？\n"
            "· 设备是不是已经切到蓝牙模式了？")
        self.tab_24g.frame.pack(fill="both", expand=True)
        self.tab_bt = DeviceTab(
            frame_bt,
            "没有发现蓝牙设备\n\n"
            "· 在 Windows 蓝牙设置里确认显示「已连接」\n"
            "· 设备需要支持标准电量服务（0x180F）")
        self.tab_bt.frame.pack(fill="both", expand=True)

        footer = tk.Frame(self.root, bg=BG)
        footer.pack(fill="x", padx=18, pady=(8, 6))

        self.status = tk.Label(footer, text="", bg=BG, fg=MUTED,
                               font=(FONT, 8), anchor="w")
        self.status.pack(side="left")

        self.auto = tk.BooleanVar(value=True)
        tk.Checkbutton(footer, text="自动刷新", variable=self.auto, bg=BG,
                       fg=MUTED, selectcolor=BG, activebackground=BG,
                       activeforeground=TEXT, font=(FONT, 8), bd=0,
                       highlightthickness=0,
                       command=self._on_auto_toggle).pack(side="right")

        self.interval = tk.StringVar(value="60 秒")
        picker = tk.OptionMenu(footer, self.interval, "15 秒", "30 秒", "60 秒",
                               "120 秒", "300 秒")
        picker.configure(bg=BG, fg=MUTED, activebackground=CARD,
                         activeforeground=TEXT, bd=0, highlightthickness=0,
                         font=(FONT, 8), indicatoron=False, padx=6)
        picker["menu"].configure(bg=CARD, fg=TEXT, activebackground=ACCENT,
                                 activeforeground="#ffffff", font=(FONT, 8))
        picker.pack(side="right")

        tools = tk.Frame(self.root, bg=BG)
        tools.pack(fill="x", padx=18, pady=(0, 12))

        self.topmost = tk.BooleanVar(value=False)
        tk.Checkbutton(tools, text="窗口置顶", variable=self.topmost, bg=BG,
                       fg=MUTED, selectcolor=BG, activebackground=BG,
                       activeforeground=TEXT, font=(FONT, 8), bd=0,
                       highlightthickness=0,
                       command=self._on_topmost).pack(side="left")

        self.autostart = tk.BooleanVar(value=S.is_enabled())
        tk.Checkbutton(tools, text="开机自启", variable=self.autostart, bg=BG,
                       fg=MUTED, selectcolor=BG, activebackground=BG,
                       activeforeground=TEXT, font=(FONT, 8), bd=0,
                       highlightthickness=0,
                       command=self._on_autostart).pack(side="left", padx=(10, 0))

        tk.Button(tools, text="退出", command=self.quit_app, bg=BG,
                  fg=MUTED, activebackground=CARD, activeforeground=RED,
                  bd=0, font=(FONT, 8), cursor="hand2",
                  highlightthickness=0).pack(side="right", padx=(0, 10))

        tk.Button(tools, text="调试信息", command=self.show_debug, bg=BG,
                  fg=MUTED, activebackground=CARD, activeforeground=TEXT,
                  bd=0, font=(FONT, 8), cursor="hand2",
                  highlightthickness=0).pack(side="right")

    # -- 设置项 -------------------------------------------------------
    def _on_auto_toggle(self) -> None:
        if self.auto.get():
            self._schedule_next()
        elif self._timer is not None:
            try:
                self.root.after_cancel(self._timer)
            except Exception:
                pass
            self._timer = None

    def _on_topmost(self) -> None:
        self.root.attributes("-topmost", bool(self.topmost.get()))

    def _on_autostart(self) -> None:
        wanted = bool(self.autostart.get())
        if not S.set_enabled(wanted):
            self.autostart.set(not wanted)
            self.status.configure(text="开机自启设置失败（可能被安全软件拦截）")
            return
        self.status.configure(text="已开启开机自启，下次登录自动运行" if wanted
                              else "已关闭开机自启")

    # -- 调试信息 -----------------------------------------------------
    def show_debug(self) -> None:
        tk = self.tk
        win = tk.Toplevel(self.root, bg=BG)
        win.title("HID 接口调试信息")
        win.geometry("640x520")
        text = tk.Text(win, bg="#0b0d12", fg="#c9d3e6", insertbackground=TEXT,
                       font=(FONT_MONO, 9), bd=0, padx=12, pady=10, wrap="none")
        scroll = tk.Scrollbar(win, command=text.yview, width=10, bd=0,
                              troughcolor=BG, bg=CARD_BORDER)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        text.insert("1.0", "正在枚举…")
        text.configure(state="disabled")
        win.after(50, lambda: self._fill_debug(text))

    def _fill_debug(self, text) -> None:
        content = build_dump()
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("1.0", content)
        text.configure(state="disabled")

    # -- 刷新 ---------------------------------------------------------
    def refresh(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.refresh_btn.configure(text="读取中…", state="disabled")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        started = time.time()
        try:
            devices = H.list_devices()
            # 2.4G 探测和蓝牙子进程互不依赖，并行跑
            with ThreadPoolExecutor(max_workers=2) as pool:
                hid_future = pool.submit(B.collect, devices, self.calibrations)
                bt_future = pool.submit(_collect_bluetooth)
                hid = hid_future.result()
                bt = bt_future.result()
            if bt is None:
                bt = list(self._last_bt)
            else:
                self._last_bt = bt
            self.queue.put(("ok", (hid, bt), time.time() - started))
        except Exception as exc:  # 采集失败也要让界面恢复
            self.queue.put(("error", exc, time.time() - started))

    def _pump(self) -> None:
        try:
            while True:
                kind, payload, elapsed = self.queue.get_nowait()
                self._apply(kind, payload, elapsed)
        except queue.Empty:
            pass
        try:
            while True:
                event = self.tray.events.get_nowait()
                if event == "refresh":
                    self.refresh()
                elif event == "show":
                    self.show_panel()
        except queue.Empty:
            pass
        self.root.after(120, self._pump)

    def _apply(self, kind: str, payload, elapsed: float) -> None:
        self.busy = False
        self.refresh_btn.configure(text="刷新", state="normal")
        if kind == "error":
            self.status.configure(text=f"读取失败：{payload}")
            self._ensure_visible_if_no_tray()
            return
        hid, bt = payload
        self._render(hid, bt)
        self.status.configure(
            text=f"{time.strftime('%H:%M:%S')} · 2.4G {len(hid)} · "
                 f"蓝牙 {len(bt)} · {elapsed:.1f}s")
        self._ensure_visible_if_no_tray()
        self._schedule_next()

    def _ensure_visible_if_no_tray(self) -> None:
        if self.tray.available or not self._start_hidden:
            return
        self._start_hidden = False
        self.show_panel()
        self.status.configure(text="托盘不可用：请从资源管理器启动")

    def _schedule_next(self) -> None:
        if self._timer is not None:
            try:
                self.root.after_cancel(self._timer)
            except Exception:
                pass
            self._timer = None
        if not self.auto.get():
            return
        seconds = int(self.interval.get().split(" ")[0])
        self._timer = self.root.after(seconds * 1000, self.refresh)

    def _render(self, hid: list, bt: list[dict]) -> None:
        models_24g = []
        for dev, reading in hid:
            models_24g.append(CardModel(
                key=f"2g:{dev.vid_pid}:{dev.name}",
                title=_display_name(dev),
                subtitle=f"{dev.vid_pid} · {_device_kind(dev)}",
                percent=reading.percent,
                charging=reading.charging,
                detail=reading.detail(),
            ))
        models_bt = []
        for item in bt:
            address = str(item.get("address", ""))
            name = str(item.get("name") or "蓝牙设备")
            battery = item.get("battery")
            percent = int(battery) if isinstance(battery, (int, float)) else None
            pretty = ":".join(address[i:i + 2]
                              for i in range(0, len(address), 2))
            models_bt.append(CardModel(
                key=f"bt:{address}",
                title=name,
                subtitle=f"{pretty} · 蓝牙",
                percent=percent,
                charging=None,
                detail="蓝牙 GATT 电量（Battery Service）" if percent is not None
                       else "读不到电量，可能未连接",
            ))

        self.tab_24g.render(models_24g)
        self.tab_bt.render(models_bt)
        self.notebook.tab(0, text=f"2.4G 设备  {len(models_24g)}")
        self.notebook.tab(1, text=f"蓝牙设备  {len(models_bt)}")
        if self._first_render:
            self._first_render = False
            if not models_24g and models_bt:
                self.notebook.select(1)
        self._update_tray(hid, models_bt)

    def _update_tray(self, hid: list, models_bt: list[CardModel]) -> None:
        items = [
            T.TrayItem(key=model.key, label=model.title,
                       percent=model.percent,
                       charging=model.charging is True)
            for model in models_bt
        ]
        for dev, reading in hid:
            items.append(T.TrayItem(
                key=f"2g:{dev.vid_pid}:{dev.name}",
                label=_display_name(dev),
                percent=reading.percent,
                charging=reading.charging is True))
        self.tray.update(items[:4])

    # -- 托盘与窗口 ---------------------------------------------------
    def hide_window(self) -> None:
        if not self.tray.available:
            self.root.iconify()
            return
        self.root.withdraw()
        if self._hidden_hint:
            self._hidden_hint = False
            self.status.configure(text="已缩到托盘：左键刷新、右键打开面板")

    def show_panel(self) -> None:
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()
        self.root.attributes("-topmost", bool(self.topmost.get()))

    def quit_app(self) -> None:
        try:
            self.tray.stop()
        finally:
            self.root.destroy()


# ------------------------------------------------------------------ 命令行


def build_dump() -> str:
    lines: list[str] = []
    devices = H.list_devices()
    lines.append(f"共 {len(devices)} 个 HID 接口\n")
    for d in devices:
        lines.append(f"{d.vid_pid}  usage={d.usage_page:04X}/{d.usage:04X}  "
                     f"in/out/feature={d.input_len}/{d.output_len}/{d.feature_len}  "
                     f"{d.name}")
        if d.error:
            lines.append(f"    打开失败：{d.error}")
        for cap in d.value_caps:
            lines.append(f"    {cap.report_type_name:7s} id={cap.report_id:#04x} "
                         f"usage={cap.usage_page:04X}/{cap.usage:04X} "
                         f"logical={cap.logical_min}..{cap.logical_max}")
        if d.feature_len and d.feature_len >= 2:
            ids = sorted({c.report_id for c in d.value_caps}) or [0]
            for rid in ids[:4]:
                try:
                    with H._hid_device_open(d) as h:
                        data = h.get_feature(rid, d.feature_len)
                    lines.append(f"    feature {rid:#04x} -> {data.hex(' ')}")
                except H.HidError:
                    pass
        lines.append("")
    return "\n".join(lines)


def _dump() -> int:
    print(build_dump())
    return 0


def _collect_all() -> tuple[list, list[dict]]:
    hid = B.collect(H.list_devices(), B.load_calibrations(_calibration_path()))
    return hid, (_collect_bluetooth() or [])


def _print_once(as_json: bool) -> int:
    hid, bt = _collect_all()
    if as_json:
        payload = {
            "2.4g": [{"name": d.name, "vid_pid": d.vid_pid,
                      "percent": r.percent, "charging": r.charging,
                      "voltage_mv": r.voltage_mv, "source": r.source}
                     for d, r in hid],
            "bluetooth": bt,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if (hid or bt) else 1
    if not hid and not bt:
        print("没有找到可读电量的设备。")
        return 1
    print("[2.4G]")
    for dev, reading in hid:
        print(f"  {_display_name(dev)}  ({dev.vid_pid})  "
              f"{reading.percent}%  {reading.detail()}")
    if not hid:
        print("  （无）")
    print("[蓝牙]")
    for item in bt:
        if item.get("battery") is None:
            print(f"  {item.get('name')}  ({item.get('address')})  读不到电量")
        else:
            print(f"  {item.get('name')}  ({item.get('address')})  "
                  f"{item.get('battery')}%")
    if not bt:
        print("  （无）")
    return 0


def _tray_selftest() -> int:
    tray = T.Tray()
    tray.start()
    tray.update([T.TrayItem("a", "测试鼠标", 75),
                 T.TrayItem("b", "测试键盘", 20, charging=True)])
    time.sleep(1.2)
    log = list(tray.debug)
    tray.update([T.TrayItem("a", "测试鼠标", 60)])
    time.sleep(0.8)
    result = {
        "hwnd": int(tray._hwnd or 0),
        "available": tray.available,
        "slots": dict(tray._slots),
        "debug": log,
    }
    tray.stop()
    time.sleep(0.6)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "tray_selftest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="2.4G / 蓝牙 外设电量")
    parser.add_argument("--once", action="store_true", help="命令行读一次后退出")
    parser.add_argument("--json", action="store_true", help="配合 --once 输出 JSON")
    parser.add_argument("--dump", action="store_true", help="列出所有 HID 接口")
    parser.add_argument("--show", action="store_true", help="启动时直接显示面板")
    parser.add_argument("--tray-test", action="store_true",
                        help="只测试托盘注册并写 tray_selftest.json")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("只支持 Windows。")
        return 2

    if args.dump:
        return _dump()
    if args.tray_test:
        return _tray_selftest()
    if args.once or args.json:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        return _print_once(args.json)

    import tkinter as tk
    set_dpi_aware()
    root = tk.Tk()
    App(root, start_hidden=not args.show)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
