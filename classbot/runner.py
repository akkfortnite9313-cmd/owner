"""Главный цикл: ждёт пары по расписанию, находит ссылку, заходит, сидит до конца и выходит."""
from __future__ import annotations

import datetime as dt
import logging
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import classroom, control, meet, zoom
from .autofind import AutoCalls, _in_hours, describe_found
from .browser import find_browser, launch
from .config import Config, Occurrence, parse_hours, upcoming
from .errors import CallError, NotAdmitted, NotLoggedIn
from .links import normalize_link, pick_new, platform_of
from .mentions import MentionWatcher
from .notify import Notifier
from .paths import Paths  # noqa: F401 — импортируют отсюда
from .state import State

log = logging.getLogger(__name__)

MAX_REJOINS = 3
KEEP_SCREENSHOTS = 40


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
        control.sleep(min(left, 30))


def keep_awake() -> None:
    """Не даёт Windows уснуть, пока работает поток бота (экран при этом гаснуть может)."""
    if sys.platform != "win32":
        return
    import ctypes
    es_continuous, es_system_required = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)


def platform_for(link: str):
    return zoom if platform_of(link) == "zoom" else meet


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
    def __init__(self, cfg: Config, paths: Paths, notifier: Notifier, on_status=None):
        self.cfg = cfg
        self.s = cfg.settings
        self.paths = paths
        self.notifier = notifier
        self.on_status = on_status
        self.state = State(paths.state)
        self.executable = find_browser(self.s)
        self.auto = AutoCalls(self.state) if self.s.auto_enabled and self.s.auto_courses else None
        self.next_scan = now()
        self.last_login_notice: dt.datetime | None = None

    def status(self, text: str) -> None:
        log.info("%s", text)
        if self.on_status:
            self.on_status(text)

    def launch(self, pw, headless: bool = False):
        for attempt in range(3):
            try:
                return launch(pw, self.s, self.paths.profile, self.executable, headless=headless)
            except Exception as ex:
                if attempt == 2:
                    raise
                log.warning("Браузер не запустился (%s). Возможно, окно бота уже открыто — "
                            "пробую ещё раз через 20 с", ex)
                control.sleep(20)

    def notify_login(self, detail: str = "") -> None:
        # Не чаще раза в 3 часа: лента проверяется часто, а вход сам не появится.
        if self.last_login_notice and now() - self.last_login_notice < dt.timedelta(hours=3):
            return
        self.last_login_notice = now()
        self.notifier.send("🔑 Бот не вошёл в аккаунт" + (f" ({detail})" if detail else "") +
                           ". Откройте ClassBot на компьютере и нажмите «Войти в аккаунты».")

    @property
    def auto_duration(self) -> dt.timedelta:
        return minutes(self.s.auto_duration_min)

    def pending(self, done: set[str]) -> list[Occurrence]:
        """Все предстоящие пары: из расписания и найденные в лентах, по времени начала."""
        items = [o for o in upcoming(self.cfg.classes, now(), self.s) if o.key not in done]
        if self.auto:
            scheduled = list(items)
            for o in self.auto.occurrences(now(), self.auto_duration):
                # Пара из расписания в это же время важнее: два звонка сразу не бывает.
                if o.key in done or any(s.start < o.end and o.start < s.end for s in scheduled):
                    continue
                items.append(o)
        items.sort(key=lambda o: (o.start, o.entry.name))
        return items

    def wait_until(self, target: dt.datetime) -> None:
        """Ждать до target, но проснуться раньше, если пора смотреть ленту."""
        sleep_until(min(target, self.next_scan) if self.auto else target)

    def scan_feeds(self) -> None:
        """Автоматический режим: посмотреть ленты и запомнить найденные звонки."""
        hours = parse_hours(self.s.auto_hours)
        self.next_scan = now() + minutes(self.s.auto_scan_min if _in_hours(now(), hours) else 60)
        try:
            with sync_playwright() as pw:
                ctx = self.launch(pw, headless=True)
                try:
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    for course in self.s.auto_courses:
                        posts = classroom.fetch_posts(page, course)
                        for found in self.auto.update(course, posts, now(), self.auto_duration, hours):
                            action = "Захожу." if found.kind == "now" else "Зайду сам."
                            self.notifier.send(f"📅 Нашёл в ленте звонок: {describe_found(found)}. {action}\n{found.link}")
                finally:
                    ctx.close()
        except NotLoggedIn:
            self.notify_login("Google")
        except Exception as ex:
            log.warning("Не удалось посмотреть ленту: %s", ex)

    def run_forever(self) -> None:
        keep_awake()
        done: set[str] = set()
        baselined: set[str] = set()
        announced = None
        if self.auto:
            self.scan_feeds()
        items = self.pending(done)
        self.notifier.send("🤖 Бот запущен. Ближайшая пара: " + (items[0].describe() if items else "пока нет") +
                           ("\nСлежу за лентой и захожу на найденные звонки сам." if self.auto else ""))
        while True:
            if self.auto and now() >= self.next_scan:
                self.scan_feeds()
            items = self.pending(done)
            if not items:
                if announced != "none":
                    self.status("Жду: в ленте пока нет новых звонков" if self.auto else "В расписании нет пар")
                    announced = "none"
                self.wait_until(now() + dt.timedelta(hours=1))
                continue
            occ = items[0]
            if occ.key != announced:
                self.status(f"Жду пару {occ.describe()}")
                announced = occ.key

            course = None if occ.entry.link else occ.entry.course
            wake_at = occ.start - minutes(self.s.look_for_link_before_min if course else self.s.join_before_min)
            if course and occ.key not in baselined:
                snap_at = occ.start - minutes(self.s.baseline_before_min)
                if now() < snap_at:
                    self.wait_until(snap_at)
                    continue
                baselined.add(occ.key)
                if now() < wake_at:
                    self.take_baseline(course)
                    continue
            if now() < wake_at:
                self.wait_until(wake_at)
                continue
            self.run_session(occ)
            done.add(occ.key)
            self.mark_joined(occ, done)
            announced = None

    def mark_joined(self, occ: Occurrence, done: set[str]) -> None:
        """На найденный в ленте звонок с той же ссылкой в это же время второй раз не заходим."""
        if not self.auto:
            return
        link = normalize_link(occ.entry.link or "")
        for other in self.auto.occurrences(now() - dt.timedelta(days=1), self.auto_duration):
            same_link = link and normalize_link(other.entry.link or "") == link
            if other.start < occ.end and (same_link or other.start >= occ.start):
                done.add(other.key)

    def take_baseline(self, course: str) -> None:
        """Запоминает, какие ссылки уже есть в ленте, чтобы потом узнать новую."""
        log.info("Запоминаю текущие ссылки в ленте курса")
        try:
            with sync_playwright() as pw:
                ctx = self.launch(pw, headless=True)
                try:
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    _, counts = classroom.fetch_meet_links(page, course)
                    self.state.set_baseline(course, counts)
                finally:
                    ctx.close()
        except NotLoggedIn:
            self.notify_login("Google")
        except Exception as ex:
            log.warning("Не удалось посмотреть ленту курса заранее: %s", ex)

    def run_session(self, occ: Occurrence) -> None:
        self.status(f"Пара {occ.describe()}")
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
            log.error("Нет входа в аккаунт: %s", ex)
            self.notify_login(str(ex))
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
        self.course = None if occ.entry.link else occ.entry.course
        self.baseline = runner.state.baseline(self.course) if self.course else None
        self.leave_at = occ.end + minutes(self.s.stay_after_end_min)
        self.page = ctx.pages[0] if ctx.pages else ctx.new_page()
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
            if not control.stop_event.is_set():
                self.update_baseline()

    def _run(self) -> None:
        if self.occ.entry.link:
            link, fallback = self.occ.entry.link, False
        else:
            self.r.status(f"Ищу ссылку в ленте курса: «{self.name}»")
            link, fallback = self.wait_for_link()
            if not link:
                return
        sleep_until(self.occ.start - minutes(self.s.join_before_min))

        if platform_of(link) == "zoom" and self.s.zoom_mode == "app":
            self.run_zoom_app(link)
            return

        rejoins = 0
        while now() < self.leave_at:
            if not self.join(link, first=rejoins == 0):
                return
            outcome, info = self.stay(link, fallback)
            if outcome == "time_up":
                break
            if outcome == "new_link":
                platform_for(link).leave(self.page)
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
            control.sleep(15)

        platform_for(link).leave(self.page)
        self.notify(f"👋 Пара «{self.name}» закончилась, вышел из звонка")

    def run_zoom_app(self, link: str) -> None:
        try:
            zoom.open_app(link, self.s.display_name)
        except (CallError, OSError) as ex:
            self.fail(f"не получилось открыть приложение Zoom: {ex}")
            return
        self.r.status(f"На паре «{self.name}» (приложение Zoom)")
        self.notify(f"✅ «{self.name}»: открыл встречу в приложении Zoom ({self.occ.start:%H:%M}–"
                    f"{self.occ.end:%H:%M}). В этом режиме бот не видит, пустили ли его, и не следит за чатом.\n{link}")
        sleep_until(self.leave_at)
        zoom.close_app()
        self.notify(f"👋 Пара «{self.name}» закончилась, закрыл Zoom")

    # --- ссылка из ленты -------------------------------------------------

    def fetch_links(self) -> tuple[list[str], dict[str, int]]:
        if self.cls_page is None or self.cls_page.is_closed():
            self.cls_page = self.ctx.new_page()
        try:
            return classroom.fetch_meet_links(self.cls_page, self.course)
        finally:
            try:
                self.page.bring_to_front()
            except Exception:
                pass

    def wait_for_link(self) -> tuple[str | None, bool]:
        """Ждёт новую ссылку в ленте. Возвращает (ссылка, это_запасной_вариант)."""
        log.info("Ищу ссылку на звонок в ленте курса %s", self.course)
        fallback_at = self.occ.start + minutes(self.s.fallback_after_min)
        alerted = False
        while now() < self.occ.end:
            try:
                order, counts = self.fetch_links()
            except NotLoggedIn:
                raise
            except Exception as ex:
                log.warning("Не удалось открыть ленту курса: %s", ex)
                control.sleep(self.s.link_poll_sec)
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
                    self.notify(f"⏳ «{self.name}»: в ленте курса пока нет ссылки на звонок, продолжаю проверять")
                    alerted = True
            control.sleep(self.s.link_poll_sec)
        self.fail("за всю пару ссылка на звонок в ленте так и не появилась")
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

    def join(self, link: str, first: bool) -> bool:
        platform = platform_for(link)
        timeout = min(self.s.admit_wait_min * 60, max(60.0, (self.leave_at - now()).total_seconds()))

        def waiting():
            self.r.status(f"«{self.name}»: жду, пока впустят")
            self.notify(f"⏳ «{self.name}»: жду, пока преподаватель впустит (или начнёт встречу)")

        try:
            platform.join(self.page, link, timeout, on_waiting=waiting,
                          name=self.s.display_name, passcode=self.occ.entry.passcode or "")
        except (CallError, NotAdmitted) as ex:
            self.fail(f"не получилось зайти в {platform.NAME}: {ex}", self.page)
            return False
        self.r.status(f"На паре «{self.name}» ({platform.NAME})")
        if first:
            self.notify(f"✅ Зашёл на пару «{self.name}» ({self.occ.start:%H:%M}–{self.occ.end:%H:%M}, "
                        f"{platform.NAME}), камера и микрофон выключены\n{link}")
        else:
            self.notify(f"✅ «{self.name}»: снова в звонке")
        return True

    def stay(self, link: str, fallback: bool) -> tuple[str, str]:
        platform = platform_for(link)
        page = self.page
        watcher = MentionWatcher(self.r.cfg.telegram.mention_keywords)
        missing = 0
        chat_clicks = 0
        next_check = 0.0
        next_link_check = time.monotonic() + 60
        while now() < self.leave_at:
            if platform.in_call(page):
                missing = 0
            else:
                missing += 1
                if missing >= 3:
                    reason, stop = platform.drop_reason(page)
                    screenshot(page, self.r.paths.logs, "dropped")
                    return ("dropped_stop" if stop else "dropped_retry"), reason
                control.sleep(3)
                continue

            if time.monotonic() >= next_check:
                platform.prepare_in_call(page, self.s.watch_captions)
                if watcher.keywords and chat_clicks < 3 and platform.open_chat(page) == "clicked":
                    chat_clicks += 1
                next_check = time.monotonic() + 30

            for hit in watcher.check(*platform.chat_snapshot(page)):
                self.notify(f"🔔 «{self.name}»: вас упомянули!\n{hit[:1500]}")

            if fallback and self.course and time.monotonic() >= next_link_check:
                next_link_check = time.monotonic() + 60
                new = self.find_new_link(link)
                if new:
                    return "new_link", new
            control.sleep(5)
        return "time_up", ""
