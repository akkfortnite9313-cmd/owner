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


# Для каждой ссылки на звонок — текст её поста: поднимаемся от ссылки вверх по странице,
# пока не упрёмся в соседний пост с другой ссылкой на звонок или в слишком большой блок.
POSTS_JS = r"""() => {
  const real = h => {
    try {
      const u = new URL(h);
      if (/(^|\.)google\.com$/.test(u.hostname) && u.pathname === '/url')
        return u.searchParams.get('q') || u.searchParams.get('url') || h;
    } catch (e) {}
    return h;
  };
  const keyOf = h => {
    h = real(h);
    let m = h.match(/zoom\.us\/(?:j|w|s|wc\/join|wc)\/(\d{9,12})/i);
    if (m) return 'z' + m[1];
    m = h.match(/zoom\.us\/my\/([\w.-]+)/i);
    if (m) return 'zm' + m[1].toLowerCase();
    m = h.match(/meet\.google\.com\/(lookup\/[^\/?#]+|[a-z]{3}-?[a-z]{4}-?[a-z]{3})(?:[\/?#]|$)/i);
    if (m) return 'm' + m[1].toLowerCase().replace(/-/g, '');
    return null;
  };
  const keysIn = el => {
    const s = new Set();
    for (const x of el.querySelectorAll('a[href]')) { const k = keyOf(x.href); if (k) s.add(k); }
    return s;
  };
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    if (!keyOf(a.href)) continue;
    let node = a;
    for (let i = 0; i < 15 && node.parentElement; i++) {
      const p = node.parentElement;
      if ((p.innerText || '').length > 4000 || keysIn(p).size > 1) break;
      node = p;
    }
    // Помечаем место ссылки в тексте поста, чтобы время искать рядом с ней, а не в соседних постах.
    const mark = document.createElement('span');
    mark.textContent = '\u2063ССЫЛКА\u2063';
    a.before(mark);
    const text = node.innerText || '';
    mark.remove();
    out.push([real(a.href), text]);
  }
  return out;
}"""


def fetch_posts(page, course_url: str) -> list[tuple[str, str]]:
    """Открывает ленту курса и возвращает [(ссылка на звонок, текст её поста), …] сверху вниз."""
    _open_feed(page, course_url)
    previous, posts = None, []
    for attempt in range(6):
        control.sleep(2)
        posts = [(href, text) for href, text in page.evaluate(POSTS_JS)]
        if [h for h, _ in posts] == previous and (posts or attempt >= 3):
            break
        previous = [h for h, _ in posts]
    log.info("В ленте %s постов со ссылками на звонки: %d", course_url, len(posts))
    return posts


def is_logged_in(page) -> bool:
    return (urlparse(page.url).hostname or "") == CLASSROOM_HOST


def fetch_meet_links(page, course_url: str) -> tuple[list[str], dict[str, int]]:
    """Открывает ленту курса и возвращает найденные ссылки на Meet/Zoom (см. links.count_links)."""
    _open_feed(page, course_url)
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


def _open_feed(page, course_url: str) -> None:
    page.goto(course_url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    if not is_logged_in(page):
        raise NotLoggedIn(f"Classroom открылся как {page.url}")
