"""Чтение ленты курса Google Classroom."""
from __future__ import annotations

import logging
import re
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


_COURSE_PATH_RE = re.compile(r"^/(?:u/\d+/)?c/([A-Za-z0-9_-]+)")

# Ссылки на курсы на главной странице Classroom (карточки курсов и боковое меню).
_COURSES_JS = r"""() => {
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    let path;
    try { path = new URL(a.href, location.href).pathname; } catch (e) { continue; }
    const m = path.match(/^\/(?:u\/\d+\/)?c\/([A-Za-z0-9_-]+)\/?$/);
    if (!m) continue;
    let name = (a.innerText || '').trim().split('\n')[0] || a.getAttribute('aria-label') || '';
    if (!name) {
      const card = a.closest('li');
      if (card) name = (card.innerText || '').trim().split('\n')[0];
    }
    out.push([m[1], name.trim()]);
  }
  return out;
}"""


def course_id(url: str) -> str | None:
    """Номер курса из ссылки https://classroom.google.com/c/<номер>/... (или None)."""
    parsed = urlparse((url or "").strip())
    if (parsed.hostname or "") != CLASSROOM_HOST:
        return None
    m = _COURSE_PATH_RE.match(parsed.path)
    return m.group(1) if m else None


def list_courses(page) -> list[dict]:
    """Курсы с главной страницы Classroom: [{"name": ..., "url": ...}]. Страница уже должна быть открыта."""
    found: dict[str, str] = {}
    previous = None
    for attempt in range(8):
        control.sleep(1.5)
        found = {}
        for cid, name in page.evaluate(_COURSES_JS):
            if cid not in found or (name and not found[cid]):
                found[cid] = name
        if found and list(found) == previous:
            break
        previous = list(found)
    courses, seen = [], set()
    for cid, name in found.items():
        label = name or f"Курс {cid}"
        # Два курса с одинаковым названием различаем номером.
        base, n = label, 2
        while label in seen:
            label, n = f"{base} ({n})", n + 1
        seen.add(label)
        courses.append({"name": label, "url": f"https://{CLASSROOM_HOST}/c/{cid}"})
    return courses


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
