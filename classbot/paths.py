"""Где бот хранит настройки, профиль браузера, журнал."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "ClassBot"
SOURCE_DIR = Path(__file__).resolve().parent.parent


def default_home() -> Path:
    """Для ClassBot.exe — %LOCALAPPDATA%\\ClassBot, при запуске из исходников — папка проекта."""
    env = os.environ.get("CLASSBOT_HOME")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(base) if base else Path.home()) / APP_NAME
    return SOURCE_DIR


@dataclass
class Paths:
    base: Path

    @property
    def profile(self) -> Path:
        return self.base / "chrome-profile"

    @property
    def logs(self) -> Path:
        return self.base / "logs"

    @property
    def state(self) -> Path:
        return self.base / "state.json"

    @property
    def config(self) -> Path:
        return self.base / "config.yaml"
