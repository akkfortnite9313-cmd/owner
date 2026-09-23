"""Общие действия со страницей: найти видимую кнопку, нажать, прочитать текст (в т. ч. во фреймах)."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_TEXT_JS = "() => document.body ? document.body.innerText : ''"


def frames(page, all_frames: bool = True) -> list:
    if not all_frames:
        return [page.main_frame]
    try:
        return list(page.frames)
    except Exception:
        return [page.main_frame]


def page_text(page, all_frames: bool = False) -> str:
    parts = []
    for frame in frames(page, all_frames):
        try:
            parts.append(frame.evaluate(_TEXT_JS) or "")
        except Exception:
            pass
    return "\n".join(parts)


def first_visible(locator):
    try:
        for el in locator.all():
            if el.is_visible():
                return el
    except Exception:
        pass
    return None


def first_visible_in(page, make_locator, all_frames: bool = True):
    """make_locator(frame) -> Locator; ищет первый видимый элемент во всех фреймах страницы."""
    for frame in frames(page, all_frames):
        try:
            el = first_visible(make_locator(frame))
        except Exception:
            el = None
        if el:
            return el
    return None


def click(el, what: str) -> bool:
    if not el:
        return False
    try:
        el.click(timeout=5000)
        log.info("Нажал: %s", what)
        return True
    except Exception as ex:
        log.debug("Не удалось нажать %s: %s", what, ex)
        return False


def click_first_visible(locator, what: str) -> bool:
    return click(first_visible(locator), what)


def count_in(page, selector: str, all_frames: bool = True) -> int:
    total = 0
    for frame in frames(page, all_frames):
        try:
            total += frame.locator(selector).count()
        except Exception:
            pass
    return total
