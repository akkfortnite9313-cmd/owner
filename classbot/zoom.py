"""Zoom: вход через веб-версию в браузере бота (или, запасной вариант, через приложение Zoom).

Веб-версия Zoom часто работает внутри фрейма, поэтому все поиски идут по всем фреймам страницы.
Селекторы собраны в начале файла — если Zoom поменяет интерфейс, чинить нужно здесь.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time

from . import control
from .errors import CallError, NotAdmitted, NotLoggedIn
from .links import zoom_app_url, zoom_parts, zoom_web_url
from .web import click, count_in, first_visible, first_visible_in, frames, page_text

log = logging.getLogger(__name__)

NAME = "Zoom"

NAME_INPUT_SEL = ('#input-for-name, input[placeholder*="your name" i], input[aria-label*="your name" i], '
                  'input[placeholder*="ваше имя" i], input[aria-label*="ваше имя" i]')
PASSCODE_INPUT_SEL = ('#input-for-pwd, input[type="password"], input[placeholder*="passcode" i], '
                      'input[placeholder*="код доступа" i]')
JOIN_BUTTON_RE = re.compile(r"^\s*(join|join meeting|войти|присоединиться|войти в конференцию)\s*$", re.I)
AGREE_RE = re.compile(
    r"^\s*(i agree|agree|accept cookies|accept all cookies|accept all|got it|"
    r"принимаю|согласен|согласна|принять|принять все|понятно)\s*$", re.I)
JOIN_FROM_BROWSER_RE = re.compile(r"join from (your )?browser|войти из браузера|присоединиться из браузера", re.I)
LEAVE_SEL = ('button[aria-label="Leave" i], button[aria-label^="Leave meeting" i], .footer__leave-btn, '
             'button[aria-label="Выйти" i], button[aria-label^="Покинуть" i]')
LEAVE_CONFIRM_RE = re.compile(r"^\s*(leave meeting|выйти из конференции|покинуть конференцию)\s*$", re.I)
# Кнопки, которые есть только когда микрофон/камера включены.
MIC_ON_SEL = ('button[aria-label="Mute" i], button[aria-label^="mute my microphone" i], '
              'button[aria-label^="Выключить звук" i]')
CAM_ON_SEL = ('button[aria-label="Stop Video" i], button[aria-label^="stop my video" i], '
              'button[aria-label^="Остановить видео" i]')
CHAT_BUTTON_SEL = 'button[aria-label*="chat" i], button[aria-label*="чат" i]'
AUDIO_DIALOG_RE = re.compile(r"join audio|computer audio|звук компьютера|подключить звук", re.I)
CLOSE_BUTTON_SEL = 'button[aria-label*="close" i], button[aria-label*="закрыть" i]'

WAITING_RE = re.compile(
    r"(host will let you in|waiting room|waiting for the host|meeting has not started|host has not joined|"
    r"организатор скоро впустит|зал ожидания|ожидание организатора|конференция ещ[её] не началась)", re.I)
FATAL_RE = re.compile(
    r"(invalid meeting id|meeting id is not valid|this meeting link is invalid|passcode wrong|incorrect passcode|"
    r"unable to join this meeting|removed you from|ended by (the )?host|meeting has ended|has been locked|"
    r"неверный идентификатор|неверный код доступа|вас удалили|завершена организатором|конференция заблокирована)",
    re.I)
CAPTCHA_RE = re.compile(r"(captcha|not a robot|verify you are human|я не робот|подтвердите, что вы не)", re.I)
SIGNIN_RE = re.compile(
    r"(sign in to join|authorized attendees only|only authenticated users|"
    r"войдите, чтобы присоединиться|только для авторизованных)", re.I)
STOP_AFTER_DROP_RE = re.compile(r"\b(ended|removed)\b|заверш|удалил", re.I)


def _wake(page) -> None:
    """Панель кнопок Zoom прячется, когда мышь не двигается, — шевелим мышью."""
    try:
        page.mouse.move(300 + int(time.time()) % 50, 300)
    except Exception:
        pass


def in_call(page, text: str | None = None) -> bool:
    _wake(page)
    if count_in(page, LEAVE_SEL) == 0:
        return False
    # В зале ожидания кнопка «Выйти» тоже есть, но это ещё не звонок.
    text = page_text(page, all_frames=True) if text is None else text
    return not WAITING_RE.search(text)


def _click_in_frames(page, make_locator, what: str) -> bool:
    return click(first_visible_in(page, make_locator), what)


def ensure_muted(page) -> None:
    for sel, what in ((MIC_ON_SEL, "выключить микрофон"), (CAM_ON_SEL, "выключить камеру")):
        for _ in range(3):
            if not _click_in_frames(page, lambda f, s=sel: f.locator(s), what):
                break
            time.sleep(0.7)


def _close_audio_dialog(page) -> None:
    # Звук в звонок не подключаем: микрофона у бота всё равно нет.
    for frame in frames(page):
        try:
            dialogs = frame.locator('[role="dialog"], .join-dialog').filter(has_text=AUDIO_DIALOG_RE)
            if click(first_visible(dialogs.locator(CLOSE_BUTTON_SEL)), "закрыть окно подключения звука"):
                return
        except Exception:
            pass


def _dismiss_popups(page) -> None:
    _click_in_frames(page, lambda f: f.get_by_role("button", name=AGREE_RE), "принять условия")


def _fill_if_empty(el, value: str, what: str) -> None:
    try:
        if not el.input_value():
            el.fill(value)
            log.info("Ввёл %s", what)
    except Exception as ex:
        log.debug("Не удалось ввести %s: %s", what, ex)


def _fatal_line(text: str) -> str | None:
    for line in text.splitlines():
        if FATAL_RE.search(line):
            return line.strip()
    return None


def join(page, url: str, timeout_s: float, on_waiting=None, name: str = "", passcode: str = "") -> None:
    """Открывает встречу в веб-версии Zoom и заходит. Возвращается, когда бот уже в звонке."""
    target = zoom_web_url(url)
    log.info("Открываю %s", target)
    page.goto(target, wait_until="domcontentloaded", timeout=60_000)
    deadline = time.monotonic() + timeout_s
    last_click = None
    while time.monotonic() < deadline:
        text = page_text(page, all_frames=True)
        if in_call(page, text):
            ensure_muted(page)
            _close_audio_dialog(page)
            return
        if CAPTCHA_RE.search(text):
            raise CallError("Zoom просит пройти проверку «я не робот». Войдите в аккаунт Zoom в браузере бота "
                            "(кнопка «Войти в аккаунты») или включите в настройках «Zoom через приложение».")
        if SIGNIN_RE.search(text):
            raise NotLoggedIn("Zoom пускает только вошедших в аккаунт Zoom — войдите в Zoom в браузере бота")
        fatal = _fatal_line(text)
        if fatal:
            raise CallError(fatal)
        if WAITING_RE.search(text) and on_waiting:
            on_waiting()
            on_waiting = None

        # Ссылка вида zoom.us/my/имя: после перехода в адресе появляется номер встречи.
        meeting_id, _ = zoom_parts(page.url)
        if meeting_id and "/wc/" not in page.url:
            page.goto(zoom_web_url(page.url), wait_until="domcontentloaded", timeout=60_000)
            continue

        _dismiss_popups(page)
        _click_in_frames(page, lambda f: f.get_by_text(JOIN_FROM_BROWSER_RE), "войти из браузера")
        # Сначала камера и микрофон: переключение может перерисовать форму и стереть введённое.
        ensure_muted(page)
        name_input = first_visible_in(page, lambda f: f.locator(NAME_INPUT_SEL))
        if name_input:
            _fill_if_empty(name_input, name or "Student", "имя")
        pwd_input = first_visible_in(page, lambda f: f.locator(PASSCODE_INPUT_SEL))
        if pwd_input:
            if not passcode:
                raise CallError("Zoom просит код доступа — впишите его в настройках этой пары (поле «Код Zoom»)")
            _fill_if_empty(pwd_input, passcode, "код доступа")

        button = first_visible_in(page, lambda f: f.get_by_role("button", name=JOIN_BUTTON_RE))
        if button and (last_click is None or time.monotonic() - last_click > 20):
            if click(button, "«Join»"):
                last_click = time.monotonic()
        control.sleep(2)
    raise NotAdmitted(f"не удалось зайти за {timeout_s / 60:.0f} мин")


def prepare_in_call(page, captions: bool) -> None:
    _wake(page)
    _close_audio_dialog(page)
    _dismiss_popups(page)
    ensure_muted(page)


def open_chat(page) -> str:
    _wake(page)
    button = first_visible_in(page, lambda f: f.locator(CHAT_BUTTON_SEL))
    if not button:
        return "none"
    try:
        label = button.get_attribute("aria-label") or ""
    except Exception:
        label = ""
    if re.search(r"close|закрыть", label, re.I):
        return "open"
    return "clicked" if click(button, "открыть чат") else "none"


def leave(page) -> None:
    if not in_call(page):
        return
    if _click_in_frames(page, lambda f: f.locator(LEAVE_SEL), "выйти из конференции"):
        time.sleep(1.5)
        _click_in_frames(page, lambda f: f.get_by_role("button", name=LEAVE_CONFIRM_RE), "подтвердить выход")
        time.sleep(1)


def chat_snapshot(page) -> tuple[list, str]:
    return [], page_text(page, all_frames=True)


def drop_reason(page) -> tuple[str, bool]:
    text = page_text(page, all_frames=True)
    reason = " ".join(line.strip() for line in text.splitlines() if line.strip())[:200]
    return reason, bool(STOP_AFTER_DROP_RE.search(text))


# --- запасной режим: приложение Zoom ------------------------------------------

def open_app(link: str, name: str = "") -> None:
    uri = zoom_app_url(link, name)
    if not uri:
        raise CallError("для приложения Zoom нужна ссылка вида https://zoom.us/j/номер")
    log.info("Открываю приложение Zoom: %s", uri)
    if sys.platform == "win32":
        os.startfile(uri)  # noqa: S606 — так Windows открывает zoommtg:// в приложении Zoom
    elif sys.platform == "darwin":
        subprocess.Popen(["open", uri])
    else:
        subprocess.Popen(["xdg-open", uri])


def close_app() -> None:
    """Закрывает приложение Zoom целиком — так бот выходит со встречи."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/IM", "Zoom.exe", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    elif sys.platform == "darwin":
        subprocess.run(["pkill", "-x", "zoom.us"], capture_output=True)
    else:
        subprocess.run(["pkill", "-x", "zoom"], capture_output=True)
