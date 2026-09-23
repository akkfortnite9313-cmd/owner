"""Чтение ленты курса Google Classroom."""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from . import control
from .errors import NotLoggedIn  # noqa: F401 — импортируют отсюда
from .links import count_links, links_from_text, normalize_course_url

log = logging.getLogger(__name__)

CLASSROOM_HOST = "classroom.google.com"


def _collect(page) -> list[str]:
    hrefs = page.evaluate("() => Array.from(document.querySelectorAll('a[href]'), a => a.href)")
    urls = [h for h in hrefs if "meet.google.com" in h or "zoom.us" in h or "google.com/url" in h]
    if not count_links(urls)[0]:
        # Если ссылка вставлена просто текстом и не превратилась в <a>.
        urls = links_from_text(page.evaluate("() => document.body ? document.body.innerText : ''"))
    return urls



# Ссылки на курсы на главной странице Classroom (карточки курсов и боковое меню).
_COURSES_JS = r"""() => {
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    let path;
    try { path = new URL(a.href, location.href).pathname; } catch (e) { continue; }
    const m = path.match(/^\/((?:u\/\d+\/)?)c\/([A-Za-z0-9_-]+)\/?$/);
    if (!m) continue;
    let name = (a.innerText || '').trim().split('\n')[0] || a.getAttribute('aria-label') || '';
    if (!name) {
      const card = a.closest('li');
      if (card) name = (card.innerText || '').trim().split('\n')[0];
    }
    out.push([m[1] + 'c/' + m[2], name.trim()]);
  }
  return out;
}"""


def course_id(url: str) -> str | None:
    """Номер курса из любой ссылки на курс Classroom (или None)."""
    normalized = normalize_course_url(url)
    return normalized.rsplit("/", 1)[1] if normalized else None


def list_courses(page) -> list[dict]:
    """Курсы с главной страницы Classroom: [{"name": ..., "url": ...}]. Страница уже должна быть открыта."""
    found: dict[str, str] = {}
    previous = None
    for attempt in range(8):
        control.sleep(1.5)
        found = {}
        paths: dict[str, str] = {}
        for path, name in page.evaluate(_COURSES_JS):
            if path.startswith("u/0/"):
                path = path[4:]  # первый аккаунт — адрес без /u/0/
            cid = path.rsplit("/", 1)[1]
            paths.setdefault(cid, path)
            if paths[cid] not in found or (name and not found[paths[cid]]):
                found[paths[cid]] = name
        if found and list(found) == previous:
            break
        previous = list(found)
    courses, seen = [], set()
    for path, name in found.items():
        label = name or f"Курс {path.rsplit('/', 1)[1]}"
        # Два курса с одинаковым названием различаем номером.
        base, n = label, 2
        while label in seen:
            label, n = f"{base} ({n})", n + 1
        seen.add(label)
        courses.append({"name": label, "url": f"https://{CLASSROOM_HOST}/{path}"})
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
