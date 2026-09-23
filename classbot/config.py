"""Загрузка config.yaml и расчёт ближайших пар по расписанию."""
from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

from .links import normalize_course_url, normalize_link, platform_of


class ConfigError(Exception):
    pass


_DAY_NAMES = {
    0: ["пн", "пон", "понедельник", "mon", "monday"],
    1: ["вт", "вто", "вторник", "tue", "tues", "tuesday"],
    2: ["ср", "сре", "среда", "wed", "wednesday"],
    3: ["чт", "чет", "четверг", "thu", "thurs", "thursday"],
    4: ["пт", "пят", "пятница", "fri", "friday"],
    5: ["сб", "суб", "суббота", "sat", "saturday"],
    6: ["вс", "вос", "воскресенье", "sun", "sunday"],
}
DAY_ALIASES = {name: num for num, names in _DAY_NAMES.items() for name in names}
DAY_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

_WEEK_ALIASES = {
    "odd": "odd", "нечетная": "odd", "нечет": "odd", "нечетные": "odd",
    "even": "even", "четная": "even", "чет": "even", "четные": "even",
}


@dataclass(frozen=True)
class ClassEntry:
    name: str
    days: frozenset[int]  # 0 = понедельник
    start: dt.time
    end: dt.time
    course: str | None = None   # ссылка на курс Classroom — ссылку на звонок брать из ленты
    link: str | None = None     # или постоянная ссылка на Meet / Zoom
    passcode: str | None = None  # код доступа Zoom, если его нет в ссылке
    week: str | None = None     # "odd" / "even" / None


@dataclass
class Settings:
    join_before_min: float = 1
    look_for_link_before_min: float = 15
    baseline_before_min: float = 60
    fallback_after_min: float = 3
    admit_wait_min: float = 15
    stay_after_end_min: float = 0
    link_poll_sec: float = 30
    mute_audio: bool = True
    watch_captions: bool = True
    browser: str = "auto"
    browser_path: str | None = None
    semester_start: dt.date | None = None
    display_name: str = ""       # имя для Zoom, если бот не вошёл в аккаунт Zoom
    zoom_mode: str = "browser"   # browser — веб-версия Zoom, app — приложение Zoom
    # Автоматический режим: сам находить звонки в лентах этих курсов и заходить на них.
    auto_enabled: bool = True
    auto_courses: list = field(default_factory=list)
    auto_duration_min: float = 80   # сколько сидеть, если время конца неизвестно
    auto_scan_min: float = 5        # как часто смотреть ленту
    auto_hours: str = "07:00-21:00"  # когда заходить по ссылке без времени сразу


@dataclass
class TelegramSettings:
    bot_token: str = ""
    chat_id: str = ""
    mention_keywords: list[str] = field(default_factory=list)


@dataclass
class Config:
    settings: Settings
    telegram: TelegramSettings
    classes: list[ClassEntry]
    path: Path | None = None


@dataclass(frozen=True)
class Occurrence:
    entry: ClassEntry
    start: dt.datetime
    end: dt.datetime

    @property
    def key(self) -> str:
        return f"{self.entry.name}|{self.start.isoformat()}"

    def describe(self) -> str:
        return (f"«{self.entry.name}» {DAY_SHORT[self.start.weekday()]} "
                f"{self.start:%d.%m} {self.start:%H:%M}–{self.end:%H:%M}")


def _norm(text: str) -> str:
    return str(text).strip().lower().replace("ё", "е")


def parse_time(value, where: str) -> dt.time:
    # YAML 1.1 читает 10:30 без кавычек как число 630 (10*60+30) — это и есть минуты от полуночи.
    if isinstance(value, int) and not isinstance(value, bool):
        minutes = value
    else:
        m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", str(value).strip())
        if not m or int(m.group(2)) >= 60:
            raise ConfigError(f"{where}: не понимаю время {value!r}, нужно вида \"09:00\"")
        minutes = int(m.group(1)) * 60 + int(m.group(2))
    if not 0 <= minutes < 24 * 60:
        raise ConfigError(f"{where}: неверное время {value!r}")
    return dt.time(minutes // 60, minutes % 60)


def parse_days(value, where: str) -> frozenset[int]:
    if value is None:
        raise ConfigError(f"{where}: не указаны дни (days: [пн, ср])")
    items = value if isinstance(value, list) else re.split(r"[,\s]+", str(value))
    days = set()
    for item in items:
        token = _norm(item)
        if not token:
            continue
        if token not in DAY_ALIASES:
            raise ConfigError(f"{where}: не понимаю день недели {item!r} (пишите пн, вт, ср, чт, пт, сб, вс)")
        days.add(DAY_ALIASES[token])
    if not days:
        raise ConfigError(f"{where}: не указаны дни (days: [пн, ср])")
    return frozenset(days)


def parse_week(value, where: str) -> str | None:
    if value in (None, "", "any", "все", "любая"):
        return None
    token = _norm(value)
    if token not in _WEEK_ALIASES:
        raise ConfigError(f"{where}: week может быть odd/even (нечётная/чётная), а не {value!r}")
    return _WEEK_ALIASES[token]


def parse_date(value, where: str) -> dt.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    try:
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
        if m:
            return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        return dt.date.fromisoformat(text)
    except ValueError:
        raise ConfigError(f"{where}: дата должна быть вида 01.09.2026, а не {value!r}") from None


def _parse_auto_courses(value) -> list[str]:
    items = value if isinstance(value, list) else [value] if value else []
    result = []
    for item in items:
        url = normalize_course_url(str(item))
        if not url:
            raise ConfigError(f"settings.auto_courses: не понимаю ссылку на курс {item!r}")
        if url not in result:
            result.append(url)
    return result


def parse_hours(value) -> tuple[dt.time, dt.time]:
    m = re.fullmatch(r"\s*(\d{1,2}[:.]\d{2})\s*-\s*(\d{1,2}[:.]\d{2})\s*", str(value or ""))
    if not m:
        raise ConfigError(f"settings.auto_hours: нужно вида 07:00-21:00, а не {value!r}")
    return parse_time(m.group(1), "settings.auto_hours"), parse_time(m.group(2), "settings.auto_hours")


def _parse_class(raw: dict, idx: int) -> ClassEntry:
    if not isinstance(raw, dict):
        raise ConfigError(f"classes[{idx}]: ожидается описание пары (name, days, start, end, course)")
    name = str(raw.get("name") or f"Пара {idx + 1}").strip()
    where = f"пара «{name}»"
    days = parse_days(raw.get("days", raw.get("day")), where)
    start = parse_time(raw.get("start"), where + ", start")
    end = parse_time(raw.get("end"), where + ", end")
    if end <= start:
        raise ConfigError(f"{where}: конец ({end:%H:%M}) должен быть позже начала ({start:%H:%M})")
    course = str(raw.get("course") or "").strip() or None
    # Раньше постоянная ссылка называлась meet — читаем и так.
    link = str(raw.get("link") or raw.get("meet") or "").strip() or None
    if not course and not link:
        raise ConfigError(f"{where}: укажите ссылку на курс в Classroom или постоянную ссылку на Meet/Zoom")
    if course:
        normalized = normalize_course_url(course)
        if not normalized:
            raise ConfigError(f"{where}: ссылка на курс должна быть вида https://classroom.google.com/c/...")
        course = normalized
    if link and not normalize_link(link):
        raise ConfigError(f"{where}: не понимаю ссылку {link!r} — нужна ссылка на Google Meet "
                          "(https://meet.google.com/abc-defg-hij) или Zoom (https://zoom.us/j/123456789)")
    passcode = str(raw.get("passcode") or "").strip() or None
    return ClassEntry(name=name, days=days, start=start, end=end, course=course, link=link,
                      passcode=passcode, week=parse_week(raw.get("week"), where))


def parse_config(data: dict, path: Path | None = None) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config.yaml пустой или повреждён")

    raw_settings = data.get("settings") or {}
    settings = Settings()
    for key, value in raw_settings.items():
        if not hasattr(settings, key):
            raise ConfigError(f"settings: неизвестный параметр {key!r}")
        if key == "semester_start":
            value = parse_date(value, "settings.semester_start")
        elif key == "auto_courses":
            value = _parse_auto_courses(value)
        elif key == "auto_hours":
            parse_hours(value)
        elif isinstance(getattr(Settings, key, None), bool):
            value = bool(value)
        elif isinstance(getattr(Settings, key, None), (int, float)):
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ConfigError(f"settings.{key}: нужно число, а не {value!r}") from None
        setattr(settings, key, value)
    settings.display_name = str(settings.display_name or "").strip()
    if settings.zoom_mode not in ("browser", "app"):
        raise ConfigError("settings.zoom_mode: browser (в браузере) или app (в приложении Zoom)")
    if settings.browser not in ("auto", "chrome", "msedge"):
        raise ConfigError("settings.browser: auto, chrome или msedge")

    raw_tg = data.get("telegram") or {}
    keywords = raw_tg.get("mention_keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    telegram = TelegramSettings(
        bot_token=str(raw_tg.get("bot_token") or "").strip(),
        chat_id=str(raw_tg.get("chat_id") or "").strip(),
        mention_keywords=[str(k).strip() for k in keywords if str(k).strip()],
    )

    raw_classes = data.get("classes") or []
    if not isinstance(raw_classes, list):
        raise ConfigError("classes: ожидается список пар")
    classes = [_parse_class(raw, i) for i, raw in enumerate(raw_classes)]
    names = [c.name for c in classes]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"названия пар должны различаться: {', '.join(sorted(dupes))}")
    if any(c.week for c in classes) and not settings.semester_start:
        raise ConfigError("для чётных/нечётных недель укажите settings.semester_start (первый день семестра)")

    return Config(settings=settings, telegram=telegram, classes=classes, path=path)


def parse_class(raw: dict) -> ClassEntry:
    return _parse_class(raw, 0)


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"не найден {path.name} — скопируйте config.example.yaml в config.yaml и заполните")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except yaml.YAMLError as ex:
        raise ConfigError(f"ошибка в {path.name}: {ex}") from None
    return parse_config(data, path)


def week_parity(day: dt.date, semester_start: dt.date) -> str:
    first_monday = semester_start - dt.timedelta(days=semester_start.weekday())
    week_no = (day - first_monday).days // 7 + 1
    return "odd" if week_no % 2 else "even"


def upcoming(classes: Iterable[ClassEntry], now: dt.datetime, settings: Settings,
             days_ahead: int = 15) -> list[Occurrence]:
    """Все пары, которые ещё не закончились, в порядке начала."""
    result = []
    classes = list(classes)
    for offset in range(days_ahead):
        day = now.date() + dt.timedelta(days=offset)
        for c in classes:
            if day.weekday() not in c.days:
                continue
            if c.week and week_parity(day, settings.semester_start) != c.week:
                continue
            start = dt.datetime.combine(day, c.start)
            end = dt.datetime.combine(day, c.end)
            # Заходить на пару, до конца которой меньше пары минут, смысла нет.
            if end - dt.timedelta(minutes=2) <= now:
                continue
            result.append(Occurrence(c, start, end))
    result.sort(key=lambda o: (o.start, o.entry.name))
    return result


def next_occurrence(classes: Iterable[ClassEntry], now: dt.datetime, settings: Settings,
                    skip: set[str] = frozenset()) -> Occurrence | None:
    for occ in upcoming(classes, now, settings):
        if occ.key not in skip:
            return occ
    return None


def default_config() -> Config:
    return Config(settings=Settings(), telegram=TelegramSettings(), classes=[])


def class_to_dict(c: ClassEntry) -> dict:
    data = {"name": c.name, "days": [DAY_SHORT[d] for d in sorted(c.days)],
            "start": f"{c.start:%H:%M}", "end": f"{c.end:%H:%M}"}
    for key in ("course", "link", "passcode", "week"):
        if getattr(c, key):
            data[key] = getattr(c, key)
    return data


def _plain(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def config_to_dict(cfg: Config) -> dict:
    return {
        "telegram": {"bot_token": cfg.telegram.bot_token, "chat_id": cfg.telegram.chat_id,
                     "mention_keywords": list(cfg.telegram.mention_keywords)},
        "settings": {k: _plain(v) for k, v in asdict(cfg.settings).items()},
        "classes": [class_to_dict(c) for c in cfg.classes],
    }


def save_config(cfg: Config, path: Path) -> None:
    text = ("# Настройки бота для пар. Удобнее менять их в окне программы ClassBot.\n"
            + yaml.safe_dump(config_to_dict(cfg), allow_unicode=True, sort_keys=False))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    cfg.path = path


def describe_source(c: ClassEntry) -> str:
    if c.link:
        return {"meet": "Meet", "zoom": "Zoom"}.get(platform_of(c.link), "") + " (постоянная ссылка)"
    return "из ленты Classroom"
