"""Запуск ClassBot вместе с Windows (запись в реестре текущего пользователя, без прав администратора)."""
from __future__ import annotations

import sys
from pathlib import Path

from .paths import SOURCE_DIR

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE = "ClassBot"


def supported() -> bool:
    return sys.platform == "win32"


def command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --autostart'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{exe}" "{SOURCE_DIR / "main.py"}" --autostart'


def is_enabled() -> bool:
    if not supported():
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _VALUE)
            return True
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    if not supported():
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, _VALUE, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(key, _VALUE)
            except FileNotFoundError:
                pass
