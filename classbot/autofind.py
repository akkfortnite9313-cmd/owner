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
_MON = r"(?P<mon>[A-Za-zА-Яа-яЁёІіЇїЄєҐґʼ']{3,})\.?"
_T = r"(?P<h>\d{1,2})[:.](?P<m>\d{2})(?:[ \t]*(?P<ap>am|pm|дп|пп)\b)?"
# Время окончания: «10:40дп – 12:00пп», «10:40–12:00», «10:40 – 11:30am».
_END = r"(?:[ \t]*[–—-][ \t]*(?P<eh>\d{1,2})[:.](?P<em>\d{2})(?:[ \t]*(?P<eap>am|pm|дп|пп)\b)?)?"
# Между датой и временем: пробелы, запятые, точки из Google Календаря (·, ⋅), «р.», «о», «в», «at».
_SEP = r"[ \t,·⋅•]*"
_GAP = _SEP + r"(?:р\.|г\.|року|года)?" + _SEP + r"(?:о|об|в|во|at|у)?[ \t]*"

_DAY_MONTH_RE = re.compile(r"(?<!\d)(?P<d>\d{1,2})[ \t]+" + _MON + r"[ \t,]*(?P<y>\d{4})?" + _GAP + _T + _END, re.I)
_MONTH_DAY_RE = re.compile(r"\b" + _MON + r"[ \t]+(?P<d>\d{1,2}),?[ \t]*(?P<y>\d{4})?" + _GAP + _T + _END, re.I)
_NUMERIC_RE = re.compile(r"(?<![\d.:])(?P<d>\d{1,2})\.(?P<mo>\d{1,2})(?:\.(?P<y>\d{2,4}))?" + _GAP + _T + _END, re.I)
_RELATIVE_RE = re.compile(r"\b(?P<rel>сьогодні|сегодня|today|завтра|tomorrow)" + _GAP + _T + _END, re.I)
_LABELED_TIME_RE = re.compile(r"(?:^|\n)[ \t]*(?:час|время|time|початок|начало|start)[ \t]*:?[ \t]*" + _T + _END, re.I)
_PREP_TIME_RE = re.compile(r"(?:^|[\s(])(?:о|об|в|во|at|з|с)[ \t]+" + _T + _END, re.I)

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


def _times(m: re.Match) -> tuple[dt.time | None, dt.time | None]:
    """(начало, конец) из совпадения; «10:40 – 11:30am»: am/pm конца относится и к началу."""
    end = _hour(m.group("eh"), m.group("em"), m.group("eap")) if m.group("eh") else None
    ap = m.group("ap")
    if not ap and m.group("eap") and end:
        start = _hour(m.group("h"), m.group("m"), m.group("eap"))
        if start and start > end:
            start = _hour(m.group("h"), m.group("m"), "am")
    else:
        start = _hour(m.group("h"), m.group("m"), ap)
    return start, end


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


def parse_when_range(text: str, today: dt.date) -> tuple[dt.datetime | None, dt.time | None, dt.time | None]:
    """(точные дата и время начала | None, время без даты | None, время окончания | None)."""
    for regex in (_DAY_MONTH_RE, _MONTH_DAY_RE, _NUMERIC_RE):
        for m in regex.finditer(text):
            if "mon" in m.groupdict():
                month = _MONTHS.get(m.group("mon").replace("ʼ", "").replace("'", "")[:3].lower())
            else:
                month = int(m.group("mo"))
                month = month if 1 <= month <= 12 else None
            start, end = _times(m)
            d = month and _date(m.group("y"), month, int(m.group("d")), today)
            if d and start:
                return dt.datetime.combine(d, start), None, end
    for m in _RELATIVE_RE.finditer(text):
        start, end = _times(m)
        if start:
            shift = 1 if m.group("rel").lower() in ("завтра", "tomorrow") else 0
            return dt.datetime.combine(today + dt.timedelta(days=shift), start), None, end
    for regex in (_LABELED_TIME_RE, _PREP_TIME_RE):
        for m in regex.finditer(text):
            start, end = _times(m)
            if start:
                return None, start, end
    return None, None, None


def parse_when(text: str, today: dt.date) -> tuple[dt.datetime | None, dt.time | None]:
    """(точная дата и время) или (только время без даты) из текста поста; (None, None) — не нашлось."""
    when, time_only, _ = parse_when_range(text, today)
    return when, time_only


LINK_MARK = "\u2063ССЫЛКА\u2063"  # где в тексте поста стоит ссылка (ставит classroom.POSTS_JS)


def split_at_link(text: str) -> tuple[list[str], list[str]]:
    """Строки текста до ссылки и после неё."""
    before, _, after = text.partition(LINK_MARK) if LINK_MARK in text else (text, "", "")
    return before.splitlines(), after.splitlines()


def clean(text: str) -> str:
    return text.replace(LINK_MARK, "")


@dataclass
class PostInfo:
    when: dt.datetime | None
    time_only: dt.time | None
    title: str
    passcode: str | None
    end_time: dt.time | None = None


def parse_post(text: str, link: str, today: dt.date) -> PostInfo:
    """Время, тема и код доступа — из строк рядом со ссылкой (сначала ближайшие), чтобы не
    перепутать с датами из соседних постов (например, сроками сдачи заданий)."""
    before, after = split_at_link(text)
    when, time_only, end_time, date_line = None, None, None, None
    for n in range(0, min(len(before), 15) + 1):
        window = before[len(before) - n:] + after[:3]
        when, time_only, end_time = parse_when_range("\n".join(window), today)
        if when or time_only:
            date_line = window[0] if n else None
            break
    near = "\n".join(before[-15:] + after[:10])
    return PostInfo(when, time_only, post_title(near, link, date_line), post_passcode(near), end_time)


# Служебные строки ленты Classroom, из которых не надо делать название.
_JUNK_PREFIX_RE = re.compile(
    r"^(автор допису|автор публикации|post author|створено|создано|created|часовий пояс|часовой пояс|time ?zone|"
    r"інформація для приєднання|информация для подключения|joining info|посилання на відеодзвінок|"
    r"ссылка на видеовстречу|video call link)", re.I)
_JUNK_LINES = {"more_vert", "більше варіантів", "больше вариантов", "more options", "додати коментар",
               "добавить комментарий", "add class comment", "учора", "вчера", "yesterday", "сьогодні", "сегодня",
               "today", "meet", "приєднатися", "присоединиться", "join"}
_TIMESTAMP_RE = re.compile(r"\d{1,2}[:.]\d{2}|\d{1,2} \S{3,}\.?", re.I)
_AUTHOR_RE = re.compile(r"(?:автор допису|автор публикации|post author)[ \t]*:[ \t]*(.+)", re.I)


def _content_lines(text: str, author: str | None) -> list[str]:
    lines = []
    for ln in clean(text).splitlines():
        s = ln.strip()
        if (len(s) < 4 or "http" in s or s.lower() in _JUNK_LINES or _JUNK_PREFIX_RE.match(s)
                or _TIMESTAMP_RE.fullmatch(s) or (author and s == author)):
            continue
        lines.append(s)
    return lines


def post_title(text: str, link: str, date_line: str | None = None) -> str:
    author_match = _AUTHOR_RE.search(clean(text))
    author = author_match.group(1).strip() if author_match else None
    m = _TITLE_RE.search(clean(text))
    if m:
        title = m.group(1).strip()
    else:
        lines = _content_lines(text, author)
        title = ""
        # Как в приглашении Google Календаря: название — строка прямо над датой и временем.
        if date_line and date_line.strip() in lines:
            i = lines.index(date_line.strip())
            title = lines[i - 1] if i > 0 else ""
        title = title or (lines[0] if lines else "")
    title = re.sub(r"\s+", " ", title)
    if len(title) > 70:
        title = title[:70] + "…"
    if author and author not in title:
        title = f"{title} ({author})" if title else author
    return title or {"zoom": "Звонок Zoom", "meet": "Звонок Meet"}.get(platform_of(link), "Звонок")


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
    end: dt.datetime | None = None  # если в посте указано время окончания


def _end_at(start: dt.datetime, end_time: dt.time | None) -> dt.datetime | None:
    if not end_time:
        return None
    end = dt.datetime.combine(start.date(), end_time)
    return end if end > start else end + dt.timedelta(days=1)


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
        infos: dict[str, PostInfo] = {}
        for href, text in posts:
            link = normalize_link(href)
            if not link:
                continue
            info = parse_post(text, link, now.date())
            infos.setdefault(link, info)
            if info.when:
                found.append(Found(link, info.title, info.when, info.passcode, "exact",
                                   _end_at(info.when, info.end_time)))

        # Новые ссылки без даты — по тому, что их в ленте стало больше, чем в прошлый раз.
        order, counts = count_links(href for href, _ in posts)
        baseline = self.data["baseline"].get(course)
        self.data["baseline"][course] = counts
        exact_links = {f.link for f in found}
        if baseline is not None:
            for link in pick_new(order, counts, baseline):
                if link in exact_links:
                    continue
                info = infos.get(link) or PostInfo(None, None, post_title("", link), None)
                if info.time_only:
                    start = dt.datetime.combine(now.date(), info.time_only)
                    if start + duration < now:
                        start += dt.timedelta(days=1)
                    kind = "time"
                else:
                    if not _in_hours(now, hours):
                        log.info("Новая ссылка в ленте ночью, без времени — не захожу: %s", link)
                        continue
                    start, kind = now, "now"
                found.append(Found(link, info.title, start, info.passcode, kind,
                                   _end_at(start, info.end_time) if kind == "time" else None))

        new = []
        calls = self.data["calls"]
        for f in found:
            key = f"{f.link}|{f.start:%Y-%m-%dT%H:%M}"
            if key in calls:
                continue
            calls[key] = {"link": f.link, "title": f.title, "start": f.start.isoformat(timespec="minutes"),
                          "end": f.end.isoformat(timespec="minutes") if f.end else None,
                          "passcode": f.passcode, "kind": f.kind, "course": course,
                          "found": now.isoformat(timespec="minutes")}
            if (f.end or f.start + duration) > now:
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
            end = dt.datetime.fromisoformat(rec["end"]) if rec.get("end") else start + duration
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
    if f.end and f.kind != "now":
        when += f"–{f.end:%H:%M}"
    return f"«{f.title}» — {when} ({platform})"
