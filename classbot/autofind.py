"""Автоматический режим: бот сам находит в ленте курса ссылки на звонки и время пар.

Каждые несколько минут бот читает ленту и для каждой ссылки на Meet/Zoom берёт текст её поста.
- Если в посте есть дата и время (как в приглашении Zoom: «Час: 23 вересня 2026 9:00 AM Київ»),
  бот зайдёт к этому времени.
- Если в ленте появилась новая ссылка без даты: с временем («о 10:40») — зайдёт к нему,
  без времени — зайдёт сразу (в дневные часы).
Как долго сидеть, если время конца неизвестно, задаётся в настройках.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass

from .config import ClassEntry, Occurrence
from .links import count_links, normalize_link, pick_new, platform_of

log = logging.getLogger(__name__)

# --- время из текста поста --------------------------------------------------------

_MONTHS = {
    "січ": 1, "янв": 1, "jan": 1,
    "лют": 2, "фев": 2, "feb": 2,
    "бер": 3, "мар": 3, "mar": 3,
    "кві": 4, "апр": 4, "apr": 4,
    "тра": 5, "мая": 5, "май": 5, "may": 5,
    "чер": 6, "июн": 6, "jun": 6,
    "лип": 7, "июл": 7, "jul": 7,
    "сер": 8, "авг": 8, "aug": 8,
    "вер": 9, "сен": 9, "sep": 9,
    "жов": 10, "окт": 10, "oct": 10,
    "лис": 11, "ноя": 11, "nov": 11,
    "гру": 12, "дек": 12, "dec": 12,
}
_WORD = r"([A-Za-zА-Яа-яЁёІіЇїЄєҐґ]{3,})\.?"
_TIME = r"(\d{1,2})[:.](\d{2})(?:[ \t]*(am|pm|дп|пп)\b)?"
_GAP = r"[ \t,]*(?:р\.|г\.|року|года)?[ \t,]*(?:о|об|в|во|at|у)?[ \t]*"

_DAY_MONTH_RE = re.compile(r"(?<!\d)(\d{1,2})[ \t]+" + _WORD + r"[ \t,]*(\d{4})?" + _GAP + _TIME, re.I)
_MONTH_DAY_RE = re.compile(r"\b" + _WORD + r"[ \t]+(\d{1,2}),?[ \t]*(\d{4})?" + _GAP + _TIME, re.I)
_NUMERIC_RE = re.compile(r"(?<![\d.:])(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?" + _GAP + _TIME, re.I)
_RELATIVE_RE = re.compile(r"\b(сьогодні|сегодня|today|завтра|tomorrow)" + _GAP + _TIME, re.I)
_LABELED_TIME_RE = re.compile(r"(?:^|\n)[ \t]*(?:час|время|time|початок|начало|start)[ \t]*:?[ \t]*" + _TIME, re.I)
_PREP_TIME_RE = re.compile(r"(?:^|[\s(])(?:о|об|в|во|at|з|с)[ \t]+" + _TIME, re.I)

_TITLE_RE = re.compile(r"(?:^|\n)[ \t]*(?:тема|topic)[ \t]*:[ \t]*(.+)", re.I)
_PASSCODE_RE = re.compile(r"(?:код доступу|код доступа|пароль|passcode|password)[ \t]*:[ \t]*(\S+)", re.I)


def _hour(h: str, m: str, ampm: str | None) -> dt.time | None:
    hour, minute = int(h), int(m)
    if ampm:
        ampm = ampm.lower()
        if hour == 12:
            hour = 0
        if ampm in ("pm", "пп"):
            hour += 12
    if hour > 23 or minute > 59:
        return None
    return dt.time(hour, minute)


def _date(year: str | None, month: int, day: int, today: dt.date) -> dt.date | None:
    try:
        if year:
            y = int(year)
            return dt.date(y + 2000 if y < 100 else y, month, day)
        candidate = dt.date(today.year, month, day)
    except ValueError:
        return None
    # Без года: дата, которая была почти год назад (например, «12 января» в декабре), — это следующий год.
    if (today - candidate).days > 300:
        try:
            candidate = dt.date(today.year + 1, month, day)
        except ValueError:
            return None
    return candidate


def parse_when(text: str, today: dt.date) -> tuple[dt.datetime | None, dt.time | None]:
    """(точная дата и время) или (только время без даты) из текста поста; (None, None) — не нашлось."""
    for m in _DAY_MONTH_RE.finditer(text):
        month = _MONTHS.get(m.group(2)[:3].lower())
        t = _hour(m.group(4), m.group(5), m.group(6))
        d = month and _date(m.group(3), month, int(m.group(1)), today)
        if d and t:
            return dt.datetime.combine(d, t), None
    for m in _MONTH_DAY_RE.finditer(text):
        month = _MONTHS.get(m.group(1)[:3].lower())
        t = _hour(m.group(4), m.group(5), m.group(6))
        d = month and _date(m.group(3), month, int(m.group(2)), today)
        if d and t:
            return dt.datetime.combine(d, t), None
    for m in _NUMERIC_RE.finditer(text):
        month, day = int(m.group(2)), int(m.group(1))
        t = _hour(m.group(4), m.group(5), m.group(6))
        d = 1 <= month <= 12 and _date(m.group(3), month, day, today)
        if d and t:
            return dt.datetime.combine(d, t), None
    for m in _RELATIVE_RE.finditer(text):
        t = _hour(m.group(2), m.group(3), m.group(4))
        if t:
            shift = 1 if m.group(1).lower() in ("завтра", "tomorrow") else 0
            return dt.datetime.combine(today + dt.timedelta(days=shift), t), None
    for regex in (_LABELED_TIME_RE, _PREP_TIME_RE):
        for m in regex.finditer(text):
            t = _hour(m.group(1), m.group(2), m.group(3))
            if t:
                return None, t
    return None, None


def post_title(text: str, link: str) -> str:
    m = _TITLE_RE.search(text)
    if m:
        title = m.group(1).strip()
    else:
        # Первые содержательные строки поста (обычно имя преподавателя и начало сообщения).
        lines = [ln.strip() for ln in text.splitlines()
                 if len(ln.strip()) >= 4 and "http" not in ln and not re.fullmatch(r"[\d:. ]+", ln.strip())]
        title = " — ".join(lines[:2])
    title = re.sub(r"\s+", " ", title) or {"zoom": "Звонок Zoom", "meet": "Звонок Meet"}.get(platform_of(link), "Звонок")
    return title[:70] + ("…" if len(title) > 70 else "")


def post_passcode(text: str) -> str | None:
    m = _PASSCODE_RE.search(text)
    return m.group(1).strip(".,;") if m else None


@dataclass
class Found:
    link: str
    title: str
    start: dt.datetime
    passcode: str | None
    kind: str  # "exact" — время из поста, "time" — время без даты, "now" — без времени, зайти сразу


def _in_hours(moment: dt.datetime, hours: tuple[dt.time, dt.time]) -> bool:
    return hours[0] <= moment.time() <= hours[1]


class AutoCalls:
    """Найденные в лентах звонки. Хранятся в state.json, чтобы пережить перезапуск."""

    def __init__(self, state):
        self.state = state
        self.data = state.data.setdefault("auto", {"baseline": {}, "calls": {}})
        self.data.setdefault("baseline", {})
        self.data.setdefault("calls", {})

    def update(self, course: str, posts: list[tuple[str, str]], now: dt.datetime,
               duration: dt.timedelta, hours: tuple[dt.time, dt.time]) -> list[Found]:
        """Разбирает посты ленты. Возвращает звонки, найденные впервые и ещё не прошедшие."""
        found: list[Found] = []
        texts: dict[str, str] = {}  # ссылка -> текст самого свежего (верхнего) поста с ней
        for href, text in posts:
            link = normalize_link(href)
            if not link:
                continue
            texts.setdefault(link, text)
            when, _ = parse_when(text, now.date())
            if when:
                found.append(Found(link, post_title(text, link), when, post_passcode(text), "exact"))

        # Новые ссылки без даты — по тому, что их в ленте стало больше, чем в прошлый раз.
        order, counts = count_links(href for href, _ in posts)
        baseline = self.data["baseline"].get(course)
        self.data["baseline"][course] = counts
        exact_links = {f.link for f in found}
        if baseline is not None:
            for link in pick_new(order, counts, baseline):
                if link in exact_links:
                    continue
                text = texts.get(link, "")
                _, time_only = parse_when(text, now.date())
                if time_only:
                    start = dt.datetime.combine(now.date(), time_only)
                    if start + duration < now:
                        start += dt.timedelta(days=1)
                    kind = "time"
                else:
                    if not _in_hours(now, hours):
                        log.info("Новая ссылка в ленте ночью, без времени — не захожу: %s", link)
                        continue
                    start, kind = now, "now"
                found.append(Found(link, post_title(text, link), start, post_passcode(text), kind))

        new = []
        calls = self.data["calls"]
        for f in found:
            key = f"{f.link}|{f.start:%Y-%m-%dT%H:%M}"
            if key in calls:
                continue
            calls[key] = {"link": f.link, "title": f.title, "start": f.start.isoformat(timespec="minutes"),
                          "passcode": f.passcode, "kind": f.kind, "course": course,
                          "found": now.isoformat(timespec="minutes")}
            if f.start + duration > now:
                new.append(f)
        # Старьё (больше недели) не храним.
        for key in [k for k, v in calls.items() if dt.datetime.fromisoformat(v["start"]) < now - dt.timedelta(days=7)]:
            del calls[key]
        self.state.save()
        return new

    def occurrences(self, now: dt.datetime, duration: dt.timedelta) -> list[Occurrence]:
        result = []
        for rec in self.data["calls"].values():
            start = dt.datetime.fromisoformat(rec["start"])
            end = start + duration
            if end - dt.timedelta(minutes=2) <= now:
                continue
            entry = ClassEntry(name=rec["title"], days=frozenset(), start=start.time(), end=end.time(),
                               link=rec["link"], passcode=rec.get("passcode"))
            result.append(Occurrence(entry, start, end))
        result.sort(key=lambda o: o.start)
        return result


def describe_found(f: Found) -> str:
    platform = {"zoom": "Zoom", "meet": "Meet"}.get(platform_of(f.link), "")
    when = "прямо сейчас" if f.kind == "now" else f"{f.start:%d.%m} в {f.start:%H:%M}"
    return f"«{f.title}» — {when} ({platform})"
