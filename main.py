"""Бот, который сам заходит на пары в Google Meet по ссылкам из Google Classroom.

Команды:
  python main.py run                 — основной режим: ждать пары по расписанию и заходить
  python main.py login               — войти в Google-аккаунт в браузере бота (один раз)
  python main.py telegram            — узнать chat_id для уведомлений и записать в config.yaml
  python main.py check               — проверить настройки, Telegram, вход и ленты курсов
  python main.py join <ссылка> [-m N] — тест: зайти на встречу прямо сейчас на N минут
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from classbot.browser import BrowserNotFound, find_browser, launch, open_plain_browser  # noqa: E402
from classbot.classroom import NotLoggedIn, fetch_meet_links, is_logged_in  # noqa: E402
from classbot.config import ConfigError, load_config, upcoming  # noqa: E402
from classbot.notify import Notifier  # noqa: E402
from classbot.runner import Paths, Runner  # noqa: E402

log = logging.getLogger("classbot")


def setup_logging(paths: Paths) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    paths.logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = RotatingFileHandler(paths.logs / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file)


def cmd_run(cfg, paths: Paths, notifier: Notifier, args) -> int:
    if not cfg.classes:
        log.error("В config.yaml нет ни одной пары (раздел classes)")
        return 1
    if not notifier.enabled:
        log.warning("Telegram не настроен — уведомлений не будет (запустите 3_telegram.bat)")
    log.info("Бот запущен. Не закрывайте это окно. Остановить — Ctrl+C.")
    try:
        Runner(cfg, paths, notifier).run_forever()
    except KeyboardInterrupt:
        log.info("Остановлено")
    return 0


def _check_login(pw, cfg, paths: Paths) -> bool:
    ctx = launch(pw, cfg.settings, paths.profile)
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://classroom.google.com/", wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        return is_logged_in(page)
    finally:
        ctx.close()


def cmd_login(cfg, paths: Paths, notifier: Notifier, args) -> int:
    from playwright.sync_api import sync_playwright

    print("\nСейчас откроется отдельное окно браузера (это профиль бота, ваш обычный браузер не затронут).")
    print("1. Войдите в Google-аккаунт, через который заходите в Classroom.")
    print("2. Убедитесь, что открылся Classroom и видны ваши курсы.")
    print("3. Закройте окно браузера и вернитесь сюда.\n")
    proc = open_plain_browser(cfg.settings, paths.profile, "https://classroom.google.com/")
    input("Когда войдёте и закроете браузер — нажмите Enter...")
    if proc.poll() is None:
        # Браузер сохраняет вход на диск при закрытии, поэтому ждём, а не убиваем его сразу.
        print("Закройте окно браузера бота (жду до минуты)...")
        try:
            proc.wait(timeout=60)
        except Exception:
            proc.terminate()
            time.sleep(3)

    print("Проверяю вход...")
    with sync_playwright() as pw:
        if _check_login(pw, cfg, paths):
            print("\nГотово: бот вошёл в Google Classroom.")
            return 0

        print("\nНе вижу входа. Попробуем войти прямо в окне бота.")
        ctx = launch(pw, cfg.settings, paths.profile)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://accounts.google.com/ServiceLogin?continue=https://classroom.google.com/")
        input("Войдите в аккаунт в открывшемся окне и нажмите здесь Enter (окно не закрывайте)...")
        ctx.close()
        if _check_login(pw, cfg, paths):
            print("\nГотово: бот вошёл в Google Classroom.")
            return 0
    print("\nВойти не получилось. Если Google пишет «браузер небезопасен» — установите Google Chrome "
          "и запустите вход ещё раз.")
    return 1


def cmd_telegram(cfg, paths: Paths, notifier: Notifier, args) -> int:
    token = cfg.telegram.bot_token
    if not token:
        print("Сначала впишите bot_token в config.yaml (раздел telegram). Как получить — в README.")
        return 1
    bot = Notifier(token, "")
    try:
        me = bot.api("getMe")["result"]
    except Exception as ex:
        print(f"Telegram не принял токен: {ex}")
        return 1
    print(f"Бот: @{me['username']}. Откройте его в Telegram и отправьте ему любое сообщение (например /start).")
    chat_id = None
    for _ in range(60):
        updates = bot.api("getUpdates", timeout=0).get("result", [])
        chats = [u["message"]["chat"]["id"] for u in updates if "message" in u]
        if chats:
            chat_id = str(chats[-1])
            break
        time.sleep(3)
    if not chat_id:
        print("Сообщение так и не пришло. Запустите команду ещё раз.")
        return 1
    text = paths.config.read_text(encoding="utf-8-sig")
    new_text, n = re.subn(r'(?m)^(\s*chat_id:).*$', lambda m: f'{m.group(1)} "{chat_id}"', text, count=1)
    if n:
        paths.config.write_text(new_text, encoding="utf-8")
        print(f"chat_id = {chat_id} записан в config.yaml")
    else:
        print(f"Впишите в config.yaml в раздел telegram строку:  chat_id: \"{chat_id}\"")
    Notifier(token, chat_id).send("✅ Уведомления от бота пар настроены")
    return 0


def cmd_check(cfg, paths: Paths, notifier: Notifier, args) -> int:
    from playwright.sync_api import sync_playwright

    ok = True
    print(f"\nconfig.yaml: OK, пар в расписании: {len(cfg.classes)}")
    print("Ближайшие пары:")
    for occ in upcoming(cfg.classes, dt.datetime.now(), cfg.settings)[:10]:
        src = "постоянная ссылка" if occ.entry.meet else "ссылка из ленты курса"
        print(f"  {occ.describe()}  ({src})")

    if notifier.enabled:
        sent = notifier.send("🧪 Проверка: уведомления работают")
        print("Telegram: " + ("тестовое сообщение отправлено" if sent else "НЕ удалось отправить"))
        ok &= sent
    else:
        print("Telegram: не настроен (уведомлений не будет)")

    try:
        print(f"Браузер: {find_browser(cfg.settings)}")
    except BrowserNotFound as ex:
        print(f"Браузер: {ex}")
        return 1

    from classbot.state import State
    state = State(paths.state)
    courses = sorted({c.course for c in cfg.classes if c.course and not c.meet})
    with sync_playwright() as pw:
        ctx = launch(pw, cfg.settings, paths.profile)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            for course in courses:
                names = ", ".join(c.name for c in cfg.classes if c.course == course)
                try:
                    order, counts = fetch_meet_links(page, course)
                except NotLoggedIn:
                    print("Вход в Google: НЕТ — запустите 2_login.bat")
                    return 1
                print(f"\n{names}: Meet-ссылок в ленте — {len(order)}")
                for link in order[:3]:
                    print(f"    {link}")
                # Всё, что уже лежит в ленте, считаем старым.
                state.set_baseline(course, counts)
            if not courses:
                page.goto("https://classroom.google.com/", wait_until="domcontentloaded", timeout=60_000)
                time.sleep(3)
                if not is_logged_in(page):
                    print("Вход в Google: НЕТ — запустите 2_login.bat")
                    return 1
            print("\nВход в Google: OK")
        finally:
            ctx.close()
    print("\nВсё готово." if ok else "\nЕсть проблемы, см. выше.")
    return 0 if ok else 1


def cmd_join(cfg, paths: Paths, notifier: Notifier, args) -> int:
    from playwright.sync_api import sync_playwright

    from classbot import meet

    watcher = meet.MentionWatcher(cfg.telegram.mention_keywords)
    with sync_playwright() as pw:
        ctx = launch(pw, cfg.settings, paths.profile)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                meet.join(page, args.url, cfg.settings.admit_wait_min * 60,
                          on_waiting=lambda: log.info("Жду, пока впустят..."))
            except (meet.MeetError, meet.NotAdmitted, NotLoggedIn) as ex:
                log.error("Не получилось зайти: %s", ex)
                return 1
            log.info("В звонке. Сижу %s мин. Подождите полминуты и напишите в чат встречи слово из "
                     "mention_keywords (например, фамилию) — должно прийти уведомление.", args.minutes)
            end = time.monotonic() + args.minutes * 60
            next_check = 0.0
            while time.monotonic() < end:
                if not meet.in_call(page):
                    log.warning("Выкинуло из звонка: %s", meet.page_text(page)[:200])
                    return 1
                if time.monotonic() >= next_check:
                    meet.prepare_in_call(page, cfg.settings.watch_captions)
                    meet.open_chat(page)
                    next_check = time.monotonic() + 30
                for hit in watcher.poll(page):
                    notifier.send(f"🔔 Тест: вас упомянули!\n{hit}")
                time.sleep(5)
            meet.leave(page)
            log.info("Вышел из звонка. Тест пройден.")
        finally:
            ctx.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Автозаход на пары в Google Meet из Classroom")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="ждать пары по расписанию и заходить")
    sub.add_parser("login", help="войти в Google в браузере бота")
    sub.add_parser("telegram", help="настроить уведомления в Telegram")
    sub.add_parser("check", help="проверить настройки")
    join = sub.add_parser("join", help="тест: зайти на встречу прямо сейчас")
    join.add_argument("url")
    join.add_argument("-m", "--minutes", type=float, default=2)
    args = parser.parse_args(argv)

    paths = Paths(BASE)
    setup_logging(paths)
    try:
        cfg = load_config(paths.config)
    except ConfigError as ex:
        log.error("Ошибка в настройках: %s", ex)
        return 2
    notifier = Notifier(cfg.telegram.bot_token, cfg.telegram.chat_id)
    handlers = {"run": cmd_run, "login": cmd_login, "telegram": cmd_telegram, "check": cmd_check,
                "join": cmd_join, None: cmd_run}
    try:
        return handlers[args.command](cfg, paths, notifier, args)
    except BrowserNotFound as ex:
        log.error("%s", ex)
        return 1


if __name__ == "__main__":
    sys.exit(main())
