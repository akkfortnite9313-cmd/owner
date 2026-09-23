"""Разбор ссылок на Google Meet и Zoom и поиск новой ссылки в ленте курса.

Как понять, что преподаватель выложил ссылку на сегодняшнюю пару: заранее (за час
до пары и после каждой пары) бот запоминает, сколько раз каждая ссылка на звонок
встречается в ленте. Ссылка, которой стало больше, чем было, — новая. Так
работает и когда ссылка каждый раз новая, и когда преподаватель заново постит
одну и ту же.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

MEET_HOST = "meet.google.com"
_CODE_RE = re.compile(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$")
_CODE_NODASH_RE = re.compile(r"^[a-z]{10}$")
# Служебные страницы Meet, которые не являются встречами.
_NOT_MEETINGS = {"", "new", "landing", "_meet", "about", "linkredirect", "calling", "whoops", "unsupported"}
_ZOOM_ID_RE = re.compile(r"^/(?:j|w|s|wc/join|wc)/(\d{9,12})(?:/|$)")
_ZOOM_VANITY_RE = re.compile(r"^/my/([A-Za-z0-9._-]+)")
_TEXT_LINK_RE = re.compile(
    r"(?:https?://)?meet\.google\.com/[A-Za-z0-9_\-/]+"
    r"|https?://(?:[A-Za-z0-9-]+\.)*zoom\.us/(?:j|w|my)/[^\s<>\"')]+", re.I)


def _unwrap_redirect(url: str) -> str:
    """Classroom иногда оборачивает ссылки в https://www.google.com/url?q=..."""
    parsed = urlparse(url)
    if parsed.hostname in ("www.google.com", "google.com") and parsed.path == "/url":
        qs = parse_qs(parsed.query)
        for key in ("q", "url"):
            if qs.get(key):
                return qs[key][0]
    return url


def _parse(url: str):
    if not url:
        return None
    url = _unwrap_redirect(url.strip())
    if "://" not in url:
        url = "https://" + url
    return urlparse(url)


def _is_zoom_host(host: str) -> bool:
    return host == "zoom.us" or host.endswith(".zoom.us")


def normalize_meet_link(url: str) -> str | None:
    """Приводит ссылку на встречу к виду https://meet.google.com/abc-defg-hij; не-Meet -> None."""
    parsed = _parse(url)
    if not parsed or (parsed.hostname or "").lower() != MEET_HOST:
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


def normalize_zoom_link(url: str) -> str | None:
    """Приводит ссылку на Zoom к виду https://zoom.us/j/<номер>?pwd=<пароль>; не-Zoom -> None."""
    parsed = _parse(url)
    if not parsed:
        return None
    host = (parsed.hostname or "").lower()
    if not _is_zoom_host(host):
        return None
    pwd = parse_qs(parsed.query).get("pwd", [""])[0]
    suffix = f"?pwd={quote(pwd, safe='.-_')}" if pwd else ""
    m = _ZOOM_ID_RE.match(parsed.path)
    if m:
        return f"https://zoom.us/j/{m.group(1)}{suffix}"
    m = _ZOOM_VANITY_RE.match(parsed.path)
    if m:
        return f"https://{host}/my/{m.group(1).lower()}{suffix}"
    return None


def normalize_link(url: str) -> str | None:
    return normalize_meet_link(url) or normalize_zoom_link(url)


def platform_of(link: str) -> str | None:
    if normalize_meet_link(link):
        return "meet"
    if normalize_zoom_link(link):
        return "zoom"
    return None


def links_from_text(text: str) -> list[str]:
    # Точку или запятую в конце предложения к ссылке не относим.
    return [m.group(0).rstrip(".,;:!?") for m in _TEXT_LINK_RE.finditer(text or "")]


def count_links(urls) -> tuple[list[str], dict[str, int]]:
    """Возвращает (уникальные ссылки в порядке появления на странице, сколько раз каждая встречается)."""
    order: list[str] = []
    counts: dict[str, int] = {}
    for url in urls:
        key = normalize_link(url)
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


def zoom_parts(link: str) -> tuple[str | None, str]:
    """(номер встречи или None для /my/-ссылок, пароль из ссылки)."""
    parsed = _parse(link)
    if not parsed:
        return None, ""
    pwd = parse_qs(parsed.query).get("pwd", [""])[0]
    m = _ZOOM_ID_RE.match(parsed.path)
    return (m.group(1) if m else None), pwd


def zoom_web_url(link: str) -> str:
    """Ссылка на веб-клиент Zoom (вход прямо в браузере, без приложения)."""
    meeting_id, pwd = zoom_parts(link)
    if not meeting_id:
        return link
    url = f"https://zoom.us/wc/join/{meeting_id}"
    return url + (f"?pwd={quote(pwd, safe='.-_')}" if pwd else "")


def zoom_app_url(link: str, name: str = "") -> str | None:
    """zoommtg://-ссылка, которая открывает встречу в приложении Zoom."""
    meeting_id, pwd = zoom_parts(link)
    if not meeting_id:
        return None
    params = {"action": "join", "confno": meeting_id}
    if pwd:
        params["pwd"] = pwd
    if name:
        params["uname"] = name
    return "zoommtg://zoom.us/join?" + urlencode(params)
