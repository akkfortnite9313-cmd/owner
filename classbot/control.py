"""Остановка бота кнопкой: все ожидания в боте идут через sleep() отсюда."""
from __future__ import annotations

import threading

stop_event = threading.Event()


class Stopped(BaseException):
    """Бота остановили. BaseException — чтобы не проглатывался в `except Exception`."""


def sleep(seconds: float) -> None:
    if stop_event.wait(max(0.0, seconds)):
        raise Stopped()


def check() -> None:
    if stop_event.is_set():
        raise Stopped()
