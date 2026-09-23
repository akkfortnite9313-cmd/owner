"""Действия, которые запускаются из окна программы и из командной строки: проверка, вход, тест."""
from __future__ import annotations

import datetime as dt
import logging
import subprocess
import time

from . import control
from .browser import find_browser, launch, open_plain_browser
from .classroom import fetch_meet_links, is_logged_in, list_courses
from .config import Config, describe_source, upcoming
from .errors import CallError, NotAdmitted, NotLoggedIn
from .mentions import MentionWatcher
from .notify import Notifier
from .paths import Paths
from .runner import platform_for
from .state import State, save_courses

log = logging.getLogger(__name__)

LOGIN_URLS = ["https://classroom.google.com/", "https://zoom.us/signin"]


def _playwright():
    from playwright.sync_api import sync_playwright
    return sync_playwright()


def open_login_window(cfg: Config, paths: Paths) -> subprocess.Popen:
    """Обычное окно браузера с профилем бота: вкладки Classroom и Zoom — войти в аккаунты."""
    return open_plain_browser(cfg.settings, paths.profile, *LOGIN_URLS)


def finish_login(proc: subprocess.Popen) -> None:
    """Ждёт, пока окно входа закроют (браузер сохраняет вход на диск при закрытии)."""
    if proc.poll() is None:
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.terminate()
            time.sleep(3)


def verify_login(cfg: Config, paths: Paths) -> bool:
    """Проверяет, что в профиле бота есть вход в Google Classroom."""
    with _playwright() as pw:
        ctx = launch(pw, cfg.settings, paths.profile)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://classroom.google.com/", wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            ok = is_logged_in(page)
            if ok:
                _remember_courses(page, paths)
        finally:
            ctx.close()
    log.info("Вход в Google: %s", "есть" if ok else "НЕТ")
    return ok


def _remember_courses(page, paths: Paths) -> list[dict]:
    """Запоминает список курсов, чтобы в окне пары курс можно было выбрать, а не вставлять ссылку."""
    try:
        courses = list_courses(page)
    except Exception as ex:
        log.warning("Не удалось прочитать список курсов: %s", ex)
        return []
    if courses:
        save_courses(paths.courses, courses)
        log.info("Курсов в Classroom: %d (%s)", len(courses), ", ".join(c["name"] for c in courses))
    else:
        log.warning("На главной странице Classroom не нашлось ни одного курса")
    return courses


def fetch_courses(cfg: Config, paths: Paths) -> list[dict]:
    """Открывает Classroom и возвращает список курсов (и запоминает его)."""
    with _playwright() as pw:
        ctx = launch(pw, cfg.settings, paths.profile)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://classroom.google.com/", wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            if not is_logged_in(page):
                raise NotLoggedIn("бот не вошёл в Google — сначала нажмите «Войти в аккаунты»")
            return _remember_courses(page, paths)
        finally:
            ctx.close()


def login_in_bot_window(cfg: Config, paths: Paths) -> bool:
    """Запасной способ: войти прямо в окне, которым управляет бот. Ждёт, пока окно закроют."""
    with _playwright() as pw:
        ctx = launch(pw, cfg.settings, paths.profile)
        closed = []
        ctx.on("close", lambda _: closed.append(True))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://accounts.google.com/ServiceLogin?continue=https://classroom.google.com/")
        log.info("Войдите в аккаунт в открывшемся окне и закройте его")
        deadline = time.monotonic() + 20 * 60
        while not closed and time.monotonic() < deadline:
            try:
                page.wait_for_timeout(1000)
            except Exception:
                break
            control.check()
        try:
            ctx.close()
        except Exception:
            pass
    return verify_login(cfg, paths)


def check(cfg: Config, paths: Paths, notifier: Notifier) -> bool:
    """Проверяет всё по очереди и пишет результат в журнал. True — всё хорошо."""
    ok = True
    log.info("===== Проверка =====")
    log.info("Пар в расписании: %d", len(cfg.classes))
    for occ in upcoming(cfg.classes, dt.datetime.now(), cfg.settings)[:5]:
        log.info("  %s — %s", occ.describe(), describe_source(occ.entry))
    if not cfg.classes:
        log.warning("Добавьте пары во вкладке «Расписание»")
        ok = False

    if notifier.enabled:
        sent = notifier.send("🧪 Проверка: уведомления работают")
        log.info("Telegram: %s", "тестовое сообщение отправлено" if sent else "НЕ удалось отправить")
        ok &= sent
    else:
        log.warning("Telegram не подключён — уведомлений не будет")

    log.info("Браузер: %s", find_browser(cfg.settings))
    state = State(paths.state)
    courses = sorted({c.course for c in cfg.classes if c.course and not c.link})
    with _playwright() as pw:
        ctx = launch(pw, cfg.settings, paths.profile)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            opened = 0
            for course in courses:
                control.check()
                names = ", ".join(c.name for c in cfg.classes if c.course == course)
                try:
                    order, counts = fetch_meet_links(page, course)
                except NotLoggedIn:
                    log.error("Вход в Google: НЕТ — нажмите «Войти в аккаунты»")
                    return False
                except Exception as ex:
                    log.error("%s: не открылась лента курса (%s). Проверьте интернет и ссылку на курс.",
                              names, str(ex).splitlines()[0])
                    ok = False
                    continue
                opened += 1
                log.info("%s: ссылок на звонки в ленте — %d%s", names, len(order),
                         (", самая свежая: " + order[0]) if order else "")
                # Всё, что уже лежит в ленте, считаем старым.
                state.set_baseline(course, counts)
            if not courses:
                page.goto("https://classroom.google.com/", wait_until="domcontentloaded", timeout=60_000)
                control.sleep(3)
                if not is_logged_in(page):
                    log.error("Вход в Google: НЕТ — нажмите «Войти в аккаунты»")
                    return False
            if opened or not courses:
                log.info("Вход в Google: есть")
        finally:
            ctx.close()
    log.info("Проверка закончена: %s", "всё в порядке" if ok else "есть проблемы, см. выше")
    return ok


def test_join(cfg: Config, paths: Paths, notifier: Notifier, url: str, minutes: float = 3) -> bool:
    """Зайти на встречу прямо сейчас на несколько минут: проверить вход, выключенные камеру и микрофон, чат."""
    platform = platform_for(url)
    watcher = MentionWatcher(cfg.telegram.mention_keywords)
    with _playwright() as pw:
        ctx = launch(pw, cfg.settings, paths.profile)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                platform.join(page, url, cfg.settings.admit_wait_min * 60,
                              on_waiting=lambda: log.info("Жду, пока впустят..."),
                              name=cfg.settings.display_name)
            except (CallError, NotAdmitted, NotLoggedIn) as ex:
                log.error("Не получилось зайти: %s", ex)
                return False
            log.info("В звонке (%s). Сижу %s мин. Подождите полминуты и напишите в чат встречи слово из "
                     "списка для уведомлений (например, фамилию) — должно прийти уведомление.", platform.NAME, minutes)
            end = time.monotonic() + minutes * 60
            next_check = 0.0
            while time.monotonic() < end:
                if not platform.in_call(page):
                    log.warning("Выкинуло из звонка: %s", platform.drop_reason(page)[0])
                    return False
                if time.monotonic() >= next_check:
                    platform.prepare_in_call(page, cfg.settings.watch_captions)
                    platform.open_chat(page)
                    next_check = time.monotonic() + 30
                for hit in watcher.check(*platform.chat_snapshot(page)):
                    log.info("Упоминание: %s", hit)
                    notifier.send(f"🔔 Тест: вас упомянули!\n{hit}")
                control.sleep(5)
            platform.leave(page)
            log.info("Вышел из звонка. Тестовый заход прошёл успешно.")
            return True
        finally:
            ctx.close()


def telegram_bot_name(token: str) -> str:
    return Notifier(token, "").api("getMe")["result"]["username"]


def telegram_wait_for_chat(token: str, timeout_s: float = 180) -> str | None:
    """Ждёт, пока пользователь напишет боту, и возвращает chat_id."""
    bot = Notifier(token, "")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        updates = bot.api("getUpdates", timeout=0).get("result", [])
        chats = [u["message"]["chat"]["id"] for u in updates if "message" in u]
        if chats:
            return str(chats[-1])
        control.sleep(3)
    return None
