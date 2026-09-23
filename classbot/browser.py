"""Запуск установленного Chrome/Edge с отдельным профилем, в котором сохранён вход в Google."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)


class BrowserNotFound(Exception):
    pass


def _candidates(kind: str) -> list[str]:
    if sys.platform == "win32":
        roots = [os.environ.get(v) for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        rel = {"chrome": r"Google\Chrome\Application\chrome.exe",
               "msedge": r"Microsoft\Edge\Application\msedge.exe"}[kind]
        return [str(Path(root) / rel) for root in roots if root]
    if sys.platform == "darwin":
        return {"chrome": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
                "msedge": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]}[kind]
    names = {"chrome": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
             "msedge": ["microsoft-edge", "microsoft-edge-stable"]}[kind]
    return [p for p in (shutil.which(n) for n in names) if p]


def find_browser(settings: Settings) -> str:
    if settings.browser_path:
        if not Path(settings.browser_path).exists():
            raise BrowserNotFound(f"browser_path указывает на несуществующий файл: {settings.browser_path}")
        return settings.browser_path
    kinds = ["chrome", "msedge"] if settings.browser in ("auto", "", None) else [settings.browser]
    for kind in kinds:
        for path in _candidates(kind):
            if Path(path).exists():
                return path
    raise BrowserNotFound("Не нашёл Google Chrome или Microsoft Edge. Установите Chrome "
                          "(https://www.google.com/chrome/) или укажите settings.browser_path")


def _sandbox_supported() -> bool:
    # Playwright по умолчанию выключает «песочницу» Chrome (--no-sandbox), и Chrome пишет об этом
    # предупреждение. На обычном компьютере она работает; не работает только под root в Linux.
    return not (sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0)


def launch(pw, settings: Settings, profile_dir: Path, executable: str | None = None):
    """Открывает браузер с профилем бота. Камера и микрофон запрещены на уровне браузера."""
    args = [
        # Любой запрос сайта на камеру/микрофон автоматически отклоняется —
        # даже если что-то пойдёт не так, бот физически не сможет их включить.
        "--deny-permission-prompts",
        "--disable-blink-features=AutomationControlled",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--no-first-run",
        "--no-default-browser-check",
        "--lang=en-US",
    ]
    if settings.mute_audio:
        args.append("--mute-audio")
    profile_dir.mkdir(parents=True, exist_ok=True)
    return pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        executable_path=executable or find_browser(settings),
        headless=False,
        args=args,
        # Без этих флагов Google хуже пускает в аккаунт, а куки из обычного окна не читаются.
        ignore_default_args=["--enable-automation", "--use-mock-keychain", "--password-store=basic"],
        no_viewport=True,
        chromium_sandbox=_sandbox_supported(),
    )


def open_plain_browser(settings: Settings, profile_dir: Path, *urls: str) -> subprocess.Popen:
    """Обычное окно браузера (без автоматизации) с профилем бота — для входа в аккаунты."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    exe = find_browser(settings)
    return subprocess.Popen([exe, f"--user-data-dir={profile_dir}", "--no-first-run",
                             "--no-default-browser-check", *urls])
