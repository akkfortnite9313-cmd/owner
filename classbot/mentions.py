"""Поиск упоминаний (фамилия, «перекличка»…) в чате и субтитрах звонка."""
from __future__ import annotations

import time


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")


class MentionWatcher:
    """Сообщения чата с id (Meet: data-message-id) проверяются по одному. Дополнительно весь
    текст страницы: упоминание засчитывается, если ключевых слов на странице стало больше
    и появилась новая, ни разу не виденная строка с ними (так не срабатывает на ваше
    собственное имя под плиткой видео).

    Первые WARMUP_S секунд бот только запоминает, что есть на странице: интерфейс
    звонка ещё прогружается, и ваше имя может появиться не сразу.
    """

    WARMUP_S = 20

    def __init__(self, keywords: list[str]):
        self.keywords = [_norm(k) for k in keywords if k.strip()]
        self.seen_ids: set[str] = set()
        self.seen_lines: set[str] = set()
        self.prev_count = 0
        self.primed = False
        self.started = None

    def _count(self, text: str) -> int:
        text = _norm(text)
        return sum(text.count(k) for k in self.keywords)

    def check(self, msgs: list, text: str) -> list[str]:
        """Новые упоминания с прошлого вызова. msgs — [[id, текст], …], text — весь текст страницы."""
        if not self.keywords:
            return []
        if self.started is None:
            self.started = time.monotonic()
        warming_up = time.monotonic() - self.started < self.WARMUP_S
        return self.feed(msgs, text, absorb=warming_up)

    def feed(self, msgs: list, text: str, absorb: bool = False) -> list[str]:
        if not self.keywords:
            return []
        lines = [line.strip() for line in text.splitlines()]
        count = self._count(text)
        if not self.primed or absorb:
            self.seen_ids.update(mid for mid, _ in msgs)
            self.seen_lines.update(lines)
            self.prev_count = count
            self.primed = True
            return []

        hits = []
        for mid, body in msgs:
            if mid in self.seen_ids:
                continue
            self.seen_ids.add(mid)
            if self._count(body):
                hits.append(body.strip())

        new_lines = []
        for i, line in enumerate(lines):
            if line and line not in self.seen_lines:
                self.seen_lines.add(line)
                if self._count(line):
                    new_lines.append(" / ".join(x for x in lines[max(0, i - 2):i + 1] if x))
        if not hits and count > self.prev_count and new_lines:
            hits.extend(new_lines)
        self.prev_count = count
        return hits
