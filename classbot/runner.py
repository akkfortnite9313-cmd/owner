"""Главный цикл: ждёт пары по расписанию, находит ссылку, заходит, сидит до конца и выходит."""
from __future__ import annotations

import datetime as dt
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import classroom, meet
from .browser import find_browser, launch
from .classroom import NotLoggedIn
from .config import Config, Occurrence, next_occurrence
from .links import pick_new
from .notify import Notifier
from .state import State

log = logging.getLogger(__name__)

MAX_REJOINS = 3
KEEP_SCREENSHOTS = 40


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


def now() -> dt.datetime:
    return dt.datetime.now()


def minutes(value: float) -> dt.timedelta:
    return dt.timedelta(minutes=value)


def sleep_until(target: dt.datetime) -> None:
    # Короткими шагами, чтобы правильно пережить перевод часов и сон компьютера.
    while True:
        left = (target - now()).total_seconds()
        if left <= 0:
            return
        time.sleep(min(left, 30))


def keep_awake() -> None:
    """Не даёт Windows уснуть, пока бот запущен (экран при этом гаснуть может)."""
    if sys.platform != "win32":
        return
    import ctypes
    es_continuous, es_system_required = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)


def screenshot(page, logs_dir: Path, tag: str) -> Path | None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{now():%Y%m%d-%H%M%S}-{tag}"
    path = logs_dir / f"{stamp}.png"
    try:
        page.screenshot(path=str(path))
        (logs_dir / f"{stamp}.html").write_text(page.content(), encoding="utf-8")
    except Exception as ex:
        log.debug("Скриншот не получился: %s", ex)
        return None
    for extra in sorted(logs_dir.glob("*.png"))[:-KEEP_SCREENSHOTS]:
        extra.unlink(missing_ok=True)
        extra.with_suffix(".html").unlink(missing_ok=True)
    return path


class Runner:
    def __init__(self, cfg: Config, paths: Paths, notifier: Notifier):
        self.cfg = cfg
        self.s = cfg.settings
        self.paths = paths
        self.notifier = notifier
        self.state = State(paths.state)
        self.executable = find_browser(self.s)

    def launch(self, pw):
        for attempt in range(3):
            try:
                return launch(pw, self.s, self.paths.profile, self.executable)
            except Exception as ex:
                if attempt == 2:
                    raise
                log.warning("Браузер не запустился (%s). Возможно, профиль бота открыт в другом окне — "
                            "пробую ещё раз через 20 с", ex)
                time.sleep(20)

    def notify_login(self) -> None:
        self.notifier.send("🔑 Бот не вошёл в Google (или вход слетел). "
                           "Запустите на компьютере 2_login.bat и войдите заново.")

    def run_forever(self) -> None:
        keep_awake()
        done: set[str] = set()
        baselined: set[str] = set()
        announced = None
        first = next_occurrence(self.cfg.classes, now(), self.s)
        self.notifier.send("🤖 Бот запущен. Ближайшая пара: " + (first.describe() if first else "нет в расписании"))
        while True:
            occ = next_occurrence(self.cfg.classes, now(), self.s, done)
            if occ is None:
                log.info("В расписании нет пар — проверю снова через час")
                time.sleep(3600)
                continue
            if occ.key != announced:
                log.info("Следующая пара: %s", occ.describe())
                announced = occ.key

            course = None if occ.entry.meet else occ.entry.course
            wake_at = occ.start - minutes(self.s.look_for_link_before_min if course else self.s.join_before_min)
            if course and occ.key not in baselined:
                snap_at = occ.start - minutes(self.s.baseline_before_min)
                if now() < snap_at:
                    sleep_until(snap_at)
                    continue
                baselined.add(occ.key)
                if now() < wake_at:
                    self.take_baseline(course)
                    continue
            if now() < wake_at:
                sleep_until(wake_at)
                continue
            self.run_session(occ)
            done.add(occ.key)

    def take_baseline(self, course: str) -> None:
        """Запоминает, какие ссылки уже есть в ленте, чтобы потом узнать новую."""
        log.info("Запоминаю текущие ссылки в ленте курса")
        try:
            with sync_playwright() as pw:
                ctx = self.launch(pw)
                try:
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    _, counts = classroom.fetch_meet_links(page, course)
                    self.state.set_baseline(course, counts)
                finally:
                    ctx.close()
        except NotLoggedIn:
            self.notify_login()
        except Exception as ex:
            log.warning("Не удалось посмотреть ленту курса заранее: %s", ex)

    def run_session(self, occ: Occurrence) -> None:
        log.info("===== %s =====", occ.describe())
        try:
            with sync_playwright() as pw:
                ctx = self.launch(pw)
                try:
                    Session(self, ctx, occ).run()
                finally:
                    try:
                        ctx.close()
                    except Exception:
                        pass
        except NotLoggedIn as ex:
            log.error("Нет входа в Google: %s", ex)
            self.notify_login()
        except Exception as ex:
            log.exception("Ошибка на паре %s", occ.entry.name)
            self.notifier.send(f"⚠️ Ошибка на паре «{occ.entry.name}»: {ex}")


class Session:
    """Одна пара: найти ссылку → зайти → сидеть до конца → выйти."""

    def __init__(self, runner: Runner, ctx, occ: Occurrence):
        self.r = runner
        self.s = runner.s
        self.ctx = ctx
        self.occ = occ
        self.name = occ.entry.name
        self.course = None if occ.entry.meet else occ.entry.course
        self.baseline = runner.state.baseline(self.course) if self.course else None
        self.leave_at = occ.end + minutes(self.s.stay_after_end_min)
        self.meet_page = ctx.pages[0] if ctx.pages else ctx.new_page()
        self.cls_page = None

    def notify(self, text: str) -> None:
        self.r.notifier.send(text)

    def fail(self, reason: str, page=None) -> None:
        shot = screenshot(page, self.r.paths.logs, "fail") if page else None
        text = f"⚠️ «{self.name}»: {reason}"
        if shot:
            self.r.notifier.send_photo(shot, text)
        else:
            self.notify(text)

    def run(self) -> None:
        try:
            self._run()
        finally:
            self.update_baseline()

    def _run(self) -> None:
        if self.occ.entry.meet:
            link, fallback = self.occ.entry.meet, False
        else:
            link, fallback = self.wait_for_link()
            if not link:
                return
        sleep_until(self.occ.start - minutes(self.s.join_before_min))

        rejoins = 0
        while now() < self.leave_at:
            if not self.join(link, first=rejoins == 0, fallback=fallback):
                return
            outcome, info = self.stay(link, fallback)
            if outcome == "time_up":
                break
            if outcome == "new_link":
                meet.leave(self.meet_page)
                self.notify(f"🔁 «{self.name}»: в ленте появилась новая ссылка, перехожу\n{info}")
                link, fallback = info, False
                continue
            if outcome == "dropped_stop":
                self.notify(f"🚪 «{self.name}»: звонок закончился или меня убрали из него.\n{info}")
                return
            rejoins += 1
            if rejoins > MAX_REJOINS:
                self.notify(f"⚠️ «{self.name}»: вылетаю из звонка уже {MAX_REJOINS} раз, больше не захожу.")
                return
            self.notify(f"⚠️ «{self.name}»: вылетел из звонка ({info}). Захожу снова…")
            time.sleep(15)

        meet.leave(self.meet_page)
        self.notify(f"👋 Пара «{self.name}» закончилась, вышел из звонка")

    # --- ссылка из ленты -------------------------------------------------

    def fetch_links(self) -> tuple[list[str], dict[str, int]]:
        if self.cls_page is None or self.cls_page.is_closed():
            self.cls_page = self.ctx.new_page()
        try:
            return classroom.fetch_meet_links(self.cls_page, self.course)
        finally:
            try:
                self.meet_page.bring_to_front()
            except Exception:
                pass

    def wait_for_link(self) -> tuple[str | None, bool]:
        """Ждёт новую ссылку в ленте. Возвращает (ссылка, это_запасной_вариант)."""
        log.info("Ищу ссылку на Meet в ленте курса %s", self.course)
        fallback_at = self.occ.start + minutes(self.s.fallback_after_min)
        alerted = False
        while now() < self.occ.end:
            try:
                order, counts = self.fetch_links()
            except NotLoggedIn:
                raise
            except Exception as ex:
                log.warning("Не удалось открыть ленту курса: %s", ex)
                time.sleep(self.s.link_poll_sec)
                continue
            if self.baseline is None:
                # Бот впервые видит этот курс: всё, что есть сейчас, считаем старым.
                log.info("Первый запуск для курса — запоминаю %d ссылок как старые", len(order))
                self.baseline = counts
                self.r.state.set_baseline(self.course, counts)
            new = pick_new(order, counts, self.baseline)
            if new:
                log.info("Новая ссылка в ленте: %s", new[0])
                return new[0], False
            if now() >= fallback_at:
                if order:
                    self.notify(f"ℹ️ «{self.name}»: новой ссылки в ленте нет, захожу по самой свежей из "
                                f"имеющихся. Если появится новая — перейду на неё.\n{order[0]}")
                    return order[0], True
                if not alerted:
                    self.notify(f"⏳ «{self.name}»: в ленте курса пока нет ссылки на Meet, продолжаю проверять")
                    alerted = True
            time.sleep(self.s.link_poll_sec)
        self.fail("за всю пару ссылка на Meet в ленте так и не появилась")
        return None, False

    def find_new_link(self, current: str) -> str | None:
        try:
            order, counts = self.fetch_links()
        except NotLoggedIn:
            raise
        except Exception as ex:
            log.warning("Не удалось проверить ленту: %s", ex)
            return None
        new = [link for link in pick_new(order, counts, self.baseline) if link != current]
        return new[0] if new else None

    def update_baseline(self) -> None:
        if not self.course:
            return
        try:
            _, counts = self.fetch_links()
            self.r.state.set_baseline(self.course, counts)
        except Exception as ex:
            log.warning("Не удалось обновить список ссылок курса: %s", ex)

    # --- звонок ---------------------------------------------------------------

    def join(self, link: str, first: bool, fallback: bool) -> bool:
        timeout = min(self.s.admit_wait_min * 60, max(60.0, (self.leave_at - now()).total_seconds()))
        waiting = lambda: self.notify(f"⏳ «{self.name}»: попросил разрешения войти, жду, пока впустят")  # noqa: E731
        try:
            meet.join(self.meet_page, link, timeout, on_waiting=waiting)
        except meet.MeetError as ex:
            self.fail(f"не получилось зайти: {ex}", self.meet_page)
            return False
        except meet.NotAdmitted as ex:
            self.fail(f"не получилось зайти: {ex}", self.meet_page)
            return False
        start, end = f"{self.occ.start:%H:%M}", f"{self.occ.end:%H:%M}"
        if first:
            self.notify(f"✅ Зашёл на пару «{self.name}» ({start}–{end}), камера и микрофон выключены\n{link}")
        else:
            self.notify(f"✅ «{self.name}»: снова в звонке")
        return True

    def stay(self, link: str, fallback: bool) -> tuple[str, str]:
        page = self.meet_page
        watcher = meet.MentionWatcher(self.r.cfg.telegram.mention_keywords)
        missing = 0
        chat_clicks = 0
        next_check = 0.0
        next_link_check = time.monotonic() + 60
        while now() < self.leave_at:
            if meet.in_call(page):
                missing = 0
            else:
                missing += 1
                if missing >= 3:
                    text = meet.page_text(page)
                    reason = " ".join(line.strip() for line in text.splitlines() if line.strip())[:200]
                    screenshot(page, self.r.paths.logs, "dropped")
                    kind = "dropped_stop" if meet.STOP_AFTER_DROP_RE.search(text) else "dropped_retry"
                    return kind, reason
                time.sleep(3)
                continue

            if time.monotonic() >= next_check:
                meet.prepare_in_call(page, self.s.watch_captions)
                if watcher.keywords and chat_clicks < 3 and meet.open_chat(page) == "clicked":
                    chat_clicks += 1
                next_check = time.monotonic() + 30

            for hit in watcher.poll(page):
                self.notify(f"🔔 «{self.name}»: вас упомянули!\n{hit[:1500]}")

            if fallback and self.course and time.monotonic() >= next_link_check:
                next_link_check = time.monotonic() + 60
                new = self.find_new_link(link)
                if new:
                    return "new_link", new
            time.sleep(5)
        return "time_up", ""
