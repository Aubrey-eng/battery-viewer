# -*- coding: utf-8 -*-
"""开机自启：写 HKCU\\...\\Run，任务管理器「启动」里也能看到并启用/禁用。"""

from __future__ import annotations

import os
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "2.4GBatteryViewer"


def _command() -> str:
    if getattr(sys, "frozen", False):  # PyInstaller 打包后直接指向 exe
        return f'"{sys.executable}"'
    script = os.path.abspath(sys.argv[0] if sys.argv and sys.argv[0].endswith(".py")
                             else __file__)
    python = sys.executable
    pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
    if os.path.exists(pythonw):
        python = pythonw
    return f'"{python}" "{script}"'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except OSError:
        return False


def set_enabled(enabled: bool) -> bool:
    """返回是否操作成功；不抛异常，避免界面卡在错误状态。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False
