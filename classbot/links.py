"""Разбор ссылок на Google Meet и поиск новой ссылки в ленте курса.

Как понять, что преподаватель выложил ссылку на сегодняшнюю пару: заранее (за час
до пары и после каждой пары) бот запоминает, сколько раз каждая Meet-ссылка
встречается в ленте. Ссылка, которой стало больше, чем было, — новая. Так
работает и когда ссылка каждый раз новая, и когда преподаватель заново постит
одну и ту же.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

MEET_HOST = "meet.google.com"
_CODE_RE = re.compile(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$")
_CODE_NODASH_RE = re.compile(r"^[a-z]{10}$")
# Служебные страницы Meet, которые не являются встречами.
_NOT_MEETINGS = {"", "new", "landing", "_meet", "about", "linkredirect", "calling", "whoops", "unsupported"}
_TEXT_LINK_RE = re.compile(r"(?:https?://)?meet\.google\.com/[A-Za-z0-9_\-/]+")


def _unwrap_redirect(url: str) -> str:
    """Classroom иногда оборачивает ссылки в https://www.google.com/url?q=..."""
    parsed = urlparse(url)
    if parsed.hostname in ("www.google.com", "google.com") and parsed.path == "/url":
        qs = parse_qs(parsed.query)
        for key in ("q", "url"):
            if qs.get(key):
                return qs[key][0]
    return url


def normalize_meet_link(url: str) -> str | None:
    """Приводит ссылку на встречу к виду https://meet.google.com/abc-defg-hij; не-Meet -> None."""
    if not url:
        return None
    url = _unwrap_redirect(url.strip())
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() != MEET_HOST:
        return None
    parts = [p for p in parsed.path.lower().split("/") if p]
    if not parts or parts[0] in _NOT_MEETINGS:
        return None
    if parts[0] == "lookup":
        if len(parts) < 2:
            return None
        return f"https://{MEET_HOST}/lookup/{parts[1]}"
    code = parts[0]
    if _CODE_NODASH_RE.match(code):
        code = f"{code[:3]}-{code[3:7]}-{code[7:]}"
    return f"https://{MEET_HOST}/{code}"


def links_from_text(text: str) -> list[str]:
    return [m.group(0) for m in _TEXT_LINK_RE.finditer(text or "")]


def count_links(urls) -> tuple[list[str], dict[str, int]]:
    """Возвращает (уникальные ссылки в порядке появления на странице, сколько раз каждая встречается)."""
    order: list[str] = []
    counts: dict[str, int] = {}
    for url in urls:
        key = normalize_meet_link(url)
        if not key:
            continue
        if key not in counts:
            order.append(key)
            counts[key] = 0
        counts[key] += 1
    return order, counts


def pick_new(order: list[str], counts: dict[str, int], baseline: dict[str, int] | None) -> list[str]:
    """Ссылки, которых на странице стало больше, чем в запомненном снимке (в порядке на странице)."""
    baseline = baseline or {}
    return [key for key in order if counts.get(key, 0) > baseline.get(key, 0)]


def with_lang(url: str, lang: str = "en") -> str:
    """Добавляет ?hl=en к ссылке Meet, чтобы интерфейс был на предсказуемом языке."""
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() != MEET_HOST:
        return url
    qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    qs["hl"] = lang
    return urlunparse(parsed._replace(query=urlencode(qs)))
