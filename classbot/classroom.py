"""Чтение ленты курса Google Classroom."""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from . import control
from .errors import NotLoggedIn  # noqa: F401 — импортируют отсюда
from .links import count_links, links_from_text

log = logging.getLogger(__name__)

CLASSROOM_HOST = "classroom.google.com"


def _collect(page) -> list[str]:
    hrefs = page.evaluate("() => Array.from(document.querySelectorAll('a[href]'), a => a.href)")
    urls = [h for h in hrefs if "meet.google.com" in h or "zoom.us" in h or "google.com/url" in h]
    if not count_links(urls)[0]:
        # Если ссылка вставлена просто текстом и не превратилась в <a>.
        urls = links_from_text(page.evaluate("() => document.body ? document.body.innerText : ''"))
    return urls


def is_logged_in(page) -> bool:
    return (urlparse(page.url).hostname or "") == CLASSROOM_HOST


def fetch_meet_links(page, course_url: str) -> tuple[list[str], dict[str, int]]:
    """Открывает ленту курса и возвращает найденные ссылки на Meet/Zoom (см. links.count_links)."""
    page.goto(course_url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    if not is_logged_in(page):
        raise NotLoggedIn(f"Classroom открылся как {page.url}")
    # Лента подгружается скриптами — ждём, пока число ссылок перестанет меняться.
    previous = None
    for attempt in range(6):
        control.sleep(2)
        order, counts = count_links(_collect(page))
        if counts == previous and (order or attempt >= 3):
            break
        previous = counts
    log.info("В ленте %s найдено ссылок на звонки: %d", course_url, len(order))
    return order, counts
