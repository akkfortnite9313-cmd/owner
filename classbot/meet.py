"""Google Meet: вход, контроль, что камера и микрофон выключены, чат, выход.

Селекторы собраны в начале файла. Если Google поменяет интерфейс Meet, чинить нужно здесь.
Ссылки открываются с ?hl=en, поэтому основные надписи английские; русские варианты — запасные.
"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlparse

from . import control
from .errors import CallError, NotAdmitted, NotLoggedIn
from .links import with_lang
from .web import click, click_first_visible, first_visible, page_text

log = logging.getLogger(__name__)

NAME = "Google Meet"

JOIN_BUTTON_RE = re.compile(
    r"^\s*(join now|ask to join|join here too|switch here|присоединиться|попросить[^\n]*)\s*$", re.I)
ASK_TO_JOIN_RE = re.compile(r"ask|попросить", re.I)
LEAVE_SEL = ('[aria-label^="Leave call" i], [aria-label^="Покинуть" i], '
             '[aria-label^="Выйти из видеовстречи" i], [aria-label^="Выйти из звонка" i]')
MIC_ON_SEL = '[aria-label^="Turn off microphone" i], [aria-label^="Выключить микрофон" i]'
CAM_ON_SEL = '[aria-label^="Turn off camera" i], [aria-label^="Выключить камеру" i]'
CAPTIONS_OFF_SEL = '[aria-label^="Turn on captions" i], [aria-label^="Включить субтитры" i]'
CHAT_BUTTON_SEL = ('[aria-label^="Chat with everyone" i], [aria-label^="Open chat" i], '
                   '[aria-label="Chat" i], [aria-label^="Чат" i]')
CHAT_INPUT_SEL = ('textarea[aria-label*="message" i], textarea[placeholder*="message" i], '
                  'textarea[aria-label*="сообщени" i], textarea[placeholder*="сообщени" i]')
NAME_INPUT_SEL = 'input[aria-label="Your name" i], input[placeholder="Your name" i]'

# Всплывашки, которые можно закрыть где угодно на странице.
DISMISS_ANYWHERE_RE = re.compile(
    r"^\s*(continue without microphone and camera|продолжить без микрофона и камеры|got it|понятно)\s*$",
    re.I)
# Кнопки внутри диалогов «вы ещё здесь?» и подобных.
DISMISS_IN_DIALOG_RE = re.compile(
    r"^\s*(dismiss|ok|ок|stay in (the )?call|i.m still here|keep waiting|остаться[^\n]*|я здесь|"
    r"продолжить ожидание)\s*$", re.I)
JUST_LEAVE_RE = re.compile(r"^\s*(just leave the call|leave call|просто выйти[^\n]*)\s*$", re.I)

# Текст страницы, после которого пытаться дальше бессмысленно.
FATAL_RE = re.compile(
    r"(can.t join this (video )?call|check your meeting code|invalid video call name|"
    r"denied your request|you.ve been removed|can.t create a meeting|"
    r"не удается присоединиться|не удалось присоединиться|вы не можете присоединиться|"
    r"проверьте код|отклонил|вас удалили)", re.I)
LOGIN_TEXT_RE = re.compile(r"(what.s your name|как вас зовут)", re.I)
# Если выкинуло из звонка с таким текстом — перезаходить не надо.
STOP_AFTER_DROP_RE = re.compile(
    r"\b(removed|ended|only one|no one else)\b|удалил|заверш|никого|единственн", re.I)


def in_call(page) -> bool:
    try:
        return page.locator(LEAVE_SEL).count() > 0
    except Exception:
        return False


def ensure_muted(page) -> None:
    """Если камера или микрофон почему-то включены — выключает (кнопка «Turn off …» есть только у включённых)."""
    for sel, what in ((MIC_ON_SEL, "выключить микрофон"), (CAM_ON_SEL, "выключить камеру")):
        # Перезапрашиваем после каждого клика: одна и та же кнопка может встречаться дважды,
        # и второй клик включил бы обратно.
        for _ in range(3):
            if not click_first_visible(page.locator(sel), what):
                break
            time.sleep(0.7)


def dismiss_popups(page) -> None:
    click_first_visible(page.get_by_role("button", name=DISMISS_ANYWHERE_RE), "закрыть подсказку")
    dialogs = page.locator('[role="dialog"], [role="alertdialog"]')
    click_first_visible(dialogs.get_by_role("button", name=DISMISS_IN_DIALOG_RE), "ответить на диалог")


def _check_not_logged_in(page, text: str) -> None:
    host = urlparse(page.url).hostname or ""
    if host.startswith("accounts.google.") or LOGIN_TEXT_RE.search(text):
        raise NotLoggedIn("Meet просит войти в аккаунт")
    try:
        asks_for_name = page.locator(NAME_INPUT_SEL).count() > 0
    except Exception:
        asks_for_name = False
    if asks_for_name:
        raise NotLoggedIn("Meet просит ввести имя — бот не вошёл в Google")


def _fatal_line(text: str) -> str | None:
    for line in text.splitlines():
        if FATAL_RE.search(line):
            return line.strip()
    return None


def join(page, url: str, timeout_s: float, on_waiting=None, name: str = "", passcode: str = "") -> None:
    """Открывает встречу и заходит. Возвращается, когда бот уже в звонке."""
    target = with_lang(url)
    log.info("Открываю %s", target)
    page.goto(target, wait_until="domcontentloaded", timeout=60_000)
    deadline = time.monotonic() + timeout_s
    last_click = None
    while time.monotonic() < deadline:
        if in_call(page):
            ensure_muted(page)
            return
        text = page_text(page)
        _check_not_logged_in(page, text)
        fatal = _fatal_line(text)
        if fatal:
            raise CallError(fatal)
        dismiss_popups(page)
        button = first_visible(page.get_by_role("button", name=JOIN_BUTTON_RE))
        # Повторно жмём, только если кнопка снова появилась (например, запрос на вход истёк).
        if button and (last_click is None or time.monotonic() - last_click > 30):
            ensure_muted(page)
            try:
                label = (button.inner_text() or "").strip()
            except Exception:
                label = ""
            if click(button, f"«{label}»"):
                last_click = time.monotonic()
                if ASK_TO_JOIN_RE.search(label) and on_waiting:
                    on_waiting()
                    on_waiting = None
        control.sleep(2)
    raise NotAdmitted(f"не удалось зайти за {timeout_s / 60:.0f} мин")


def prepare_in_call(page, captions: bool) -> None:
    """Проверки внутри звонка: камера/микрофон выключены, всплывашки закрыты, субтитры включены."""
    dismiss_popups(page)
    ensure_muted(page)
    if captions:
        click_first_visible(page.locator(CAPTIONS_OFF_SEL), "включить субтитры")


def open_chat(page) -> str:
    """Открывает панель чата, чтобы видеть сообщения. Возвращает "open", "clicked" или "none"."""
    try:
        if page.locator(CHAT_INPUT_SEL).count() > 0:
            return "open"
        button = first_visible(page.locator(CHAT_BUTTON_SEL))
        if not button:
            return "none"
        if button.get_attribute("aria-pressed") == "true":
            return "open"
        return "clicked" if click(button, "открыть чат") else "none"
    except Exception as ex:
        log.debug("Не удалось открыть чат: %s", ex)
        return "none"


def leave(page) -> None:
    if not in_call(page):
        return
    if click_first_visible(page.locator(LEAVE_SEL), "покинуть звонок"):
        time.sleep(2)
        click_first_visible(page.get_by_role("button", name=JUST_LEAVE_RE), "просто выйти")
        time.sleep(1)


_CHAT_JS = """() => ({
  msgs: Array.from(document.querySelectorAll('[data-message-id]'),
                   n => [n.getAttribute('data-message-id'), n.innerText || '']),
  text: document.body ? document.body.innerText : '',
})"""


def chat_snapshot(page) -> tuple[list, str]:
    """(сообщения чата с id, весь текст страницы) — для поиска упоминаний."""
    try:
        data = page.evaluate(_CHAT_JS)
        return data["msgs"], data["text"]
    except Exception:
        return [], ""


def drop_reason(page) -> tuple[str, bool]:
    """(текст на экране после вылета, нужно ли перестать перезаходить)."""
    text = page_text(page)
    reason = " ".join(line.strip() for line in text.splitlines() if line.strip())[:200]
    return reason, bool(STOP_AFTER_DROP_RE.search(text))
