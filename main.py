"""ClassBot — сам заходит на пары в Google Meet и Zoom по расписанию и ссылкам из Google Classroom.

Без аргументов открывается окно программы. Команды для командной строки:
  python main.py run                  — работать без окна: ждать пары по расписанию и заходить
  python main.py login                — войти в аккаунты в браузере бота
  python main.py telegram             — подключить уведомления в Telegram
  python main.py check                — проверить настройки, Telegram, вход и ленты курсов
  python main.py join <ссылка> [-m N] — тест: зайти на встречу прямо сейчас на N минут
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from classbot import tasks  # noqa: E402
from classbot.browser import BrowserNotFound  # noqa: E402
from classbot.config import ConfigError, load_config  # noqa: E402
from classbot.notify import Notifier  # noqa: E402
from classbot.paths import Paths, default_home  # noqa: E402

log = logging.getLogger("classbot")


def setup_logging(paths: Paths, console: bool = True) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if console and sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(fmt)
        root.addHandler(handler)
    paths.logs.mkdir(parents=True, exist_ok=True)
    file = RotatingFileHandler(paths.logs / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file.setFormatter(fmt)
    root.addHandler(file)


def cmd_run(cfg, paths: Paths, notifier: Notifier, args) -> int:
    from classbot.runner import Runner

    if not cfg.classes and not (cfg.settings.auto_enabled and cfg.settings.auto_courses):
        log.error("Нет ни одной пары и не выбран курс для автоматического режима")
        return 1
    if not notifier.enabled:
        log.warning("Telegram не подключён — уведомлений не будет")
    log.info("Бот запущен. Не закрывайте это окно. Остановить — Ctrl+C.")
    try:
        Runner(cfg, paths, notifier).run_forever()
    except KeyboardInterrupt:
        log.info("Остановлено")
    return 0


def cmd_login(cfg, paths: Paths, notifier: Notifier, args) -> int:
    print("\nСейчас откроется отдельное окно браузера (это профиль бота, ваш обычный браузер не затронут).")
    print("Войдите в Google-аккаунт для Classroom (и в Zoom, если преподаватели требуют вход), затем закройте окно.\n")
    proc = tasks.open_login_window(cfg, paths)
    input("Когда войдёте и закроете браузер — нажмите Enter...")
    tasks.finish_login(proc)
    if tasks.verify_login(cfg, paths):
        print("\nГотово: бот вошёл в Google Classroom.")
        return 0
    print("\nНе вижу входа. Попробуем войти прямо в окне бота — войдите и закройте окно.")
    if tasks.login_in_bot_window(cfg, paths):
        print("\nГотово: бот вошёл в Google Classroom.")
        return 0
    print("\nВойти не получилось. Если Google пишет «браузер небезопасен» — установите Google Chrome.")
    return 1


def cmd_telegram(cfg, paths: Paths, notifier: Notifier, args) -> int:
    token = cfg.telegram.bot_token
    if not token:
        print("Сначала впишите bot_token в config.yaml (раздел telegram).")
        return 1
    print(f"Откройте в Telegram бота @{tasks.telegram_bot_name(token)} и отправьте ему любое сообщение.")
    chat_id = tasks.telegram_wait_for_chat(token)
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
    Notifier(token, chat_id).send("✅ Уведомления от ClassBot настроены")
    return 0


def cmd_check(cfg, paths: Paths, notifier: Notifier, args) -> int:
    return 1 if tasks.check(cfg, paths, notifier) else 0


def cmd_join(cfg, paths: Paths, notifier: Notifier, args) -> int:
    return 0 if tasks.test_join(cfg, paths, notifier, args.url, args.minutes) else 1


def cmd_selftest(args) -> int:
    """Проверка собранного exe: Playwright и браузер запускаются. Результат пишется в файл."""
    out = Path(args.out)
    try:
        from playwright.sync_api import sync_playwright

        from classbot.browser import find_browser
        from classbot.config import Settings

        import tempfile

        from classbot.browser import launch

        settings = Settings(browser_path=args.browser)
        exe = find_browser(settings)
        # Браузер запускается ровно так же, как на паре (профиль, флаги, песочница).
        with sync_playwright() as pw, tempfile.TemporaryDirectory() as tmp:
            ctx = launch(pw, settings, Path(tmp) / "profile")
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_content("<p id=x>ok</p>")
            text = page.inner_text("#x")
            page.goto("chrome://version")
            command_line = page.inner_text("#command_line")
            ctx.close()
        if "--no-sandbox" in command_line and sys.platform == "win32":
            raise RuntimeError("Chrome запущен с --no-sandbox")
        # Окно программы: tkinter, тема и иконка должны были попасть в сборку.
        import tkinter as tk
        from tkinter import ttk

        from classbot.gui import App, _apply_theme, asset
        root = tk.Tk()
        _apply_theme(root)
        theme = ttk.Style(root).theme_use()
        with tempfile.TemporaryDirectory() as tmp:
            app = App(root, Paths(Path(tmp)))
            root.update()
            app._destroy()
        if not asset("icon.png").exists():
            raise RuntimeError("нет assets/icon.png")
        out.write_text(f"OK page={text} theme={theme} browser={exe}\ncommand line: {command_line}", encoding="utf-8")
        return 0
    except Exception as ex:  # noqa: BLE001
        out.write_text(f"FAIL {type(ex).__name__}: {ex}", encoding="utf-8")
        return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ClassBot — автозаход на пары в Google Meet и Zoom")
    parser.add_argument("--autostart", action="store_true", help="запуск вместе с Windows: окно свёрнуто, бот включён")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("gui", help="окно программы (по умолчанию)")
    sub.add_parser("run", help="работать без окна")
    sub.add_parser("login", help="войти в аккаунты в браузере бота")
    sub.add_parser("telegram", help="подключить уведомления в Telegram")
    sub.add_parser("check", help="проверить настройки")
    join = sub.add_parser("join", help="тест: зайти на встречу прямо сейчас")
    join.add_argument("url")
    join.add_argument("-m", "--minutes", type=float, default=3)
    selftest = sub.add_parser("selftest", help="проверка сборки")
    selftest.add_argument("out")
    selftest.add_argument("--browser", help="путь к браузеру (по умолчанию ищется Chrome/Edge)")
    args = parser.parse_args(argv)

    if args.command == "selftest":
        return cmd_selftest(args)

    paths = Paths(default_home())
    paths.base.mkdir(parents=True, exist_ok=True)
    if args.command in (None, "gui"):
        setup_logging(paths, console=False)
        from classbot.gui import run_gui
        return run_gui(paths, autostart=args.autostart)

    setup_logging(paths)
    try:
        cfg = load_config(paths.config)
    except ConfigError as ex:
        log.error("Ошибка в настройках: %s", ex)
        return 2
    notifier = Notifier(cfg.telegram.bot_token, cfg.telegram.chat_id)
    handlers = {"run": cmd_run, "login": cmd_login, "telegram": cmd_telegram, "check": cmd_check, "join": cmd_join}
    try:
        return handlers[args.command](cfg, paths, notifier, args)
    except BrowserNotFound as ex:
        log.error("%s", ex)
        return 1


if __name__ == "__main__":
    sys.exit(main())
