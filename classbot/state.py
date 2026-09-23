"""state.json: какие ссылки уже были в ленте каждого курса; courses.json: список курсов Classroom."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"courses": {}}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
                self.data.setdefault("courses", {})
            except (OSError, ValueError) as ex:
                log.warning("Не удалось прочитать %s (%s), начинаю с чистого листа", path.name, ex)

    def baseline(self, course: str) -> dict[str, int] | None:
        entry = self.data["courses"].get(course)
        return dict(entry["links"]) if entry else None

    def set_baseline(self, course: str, counts: dict[str, int]) -> None:
        self.data["courses"][course] = {
            "links": dict(counts),
            "updated": dt.datetime.now().isoformat(timespec="seconds"),
        }
        self._save()

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def load_courses(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [c for c in data if isinstance(c, dict) and c.get("name") and c.get("url")]
    except (OSError, ValueError):
        return []


def save_courses(path: Path, courses: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(courses, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
