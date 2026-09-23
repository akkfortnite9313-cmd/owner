"""Уведомления в Telegram через Bot API (без сторонних библиотек)."""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from urllib import error, request

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class Notifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def _call(self, method: str, data: bytes, content_type: str, timeout: float = 20) -> dict:
        req = request.Request(API.format(token=self.bot_token, method=method), data=data,
                              headers={"Content-Type": content_type})
        with request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def api(self, method: str, **params) -> dict:
        return self._call(method, json.dumps(params).encode("utf-8"), "application/json")

    def send(self, text: str) -> bool:
        log.info("Telegram: %s", text.replace("\n", " | "))
        if not self.enabled:
            return False
        try:
            self.api("sendMessage", chat_id=self.chat_id, text=text[:4000], disable_web_page_preview=True)
            return True
        except (error.URLError, OSError, ValueError) as ex:
            log.warning("Не удалось отправить сообщение в Telegram: %s", ex)
            return False

    def send_photo(self, path: Path, caption: str) -> bool:
        if not self.enabled or not path or not Path(path).exists():
            return self.send(caption)
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in (("chat_id", self.chat_id), ("caption", caption[:1000])):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
                         .encode("utf-8"))
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="screen.png"\r\n'
                     f"Content-Type: image/png\r\n\r\n".encode("utf-8"))
        parts.append(Path(path).read_bytes())
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        log.info("Telegram (скриншот): %s", caption.replace("\n", " | "))
        try:
            self._call("sendPhoto", b"".join(parts), f"multipart/form-data; boundary={boundary}", timeout=60)
            return True
        except (error.URLError, OSError, ValueError) as ex:
            log.warning("Не удалось отправить скриншот в Telegram: %s", ex)
            return self.send(caption)
