"""Запуск установленного Chrome/Edge с отдельным профилем, в котором сохранён вход в Google."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
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


# Флаги, которые Playwright добавляет сам, но боту они мешают: с ними Google хуже пускает в аккаунт,
# куки из обычного окна не читаются, а Chrome показывает жёлтую плашку «unsupported command-line flag».
IGNORED_DEFAULT_ARGS = ["--enable-automation", "--use-mock-keychain", "--password-store=basic",
                        "--enable-unsafe-swiftshader", "--unsafely-disable-devtools-self-xss-warnings"]
# Флаги, из-за которых Chrome показывает предупреждение (проверяются в самопроверке сборки).
WARNING_FLAGS = ["--no-sandbox", "--enable-unsafe-swiftshader", "--unsafely-disable-devtools-self-xss-warnings",
                 "--disable-web-security", "--ignore-certificate-errors", "--single-process"]


def _sandbox_supported() -> bool:
    # Playwright по умолчанию выключает «песочницу» Chrome (--no-sandbox), и Chrome пишет об этом
    # предупреждение. На обычном компьютере она работает; не работает только под root в Linux.
    return not (sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0)


# --- профиль уже занят другим окном ---------------------------------------------------

def profile_in_use(profile_dir: Path) -> bool:
    """Открыт ли сейчас какой-нибудь браузер с профилем бота."""
    if sys.platform == "win32":
        # Chrome держит этот файл открытым, пока работает, и Windows удаляет его, когда Chrome закрывается.
        return (profile_dir / "lockfile").exists()
    lock = profile_dir / "SingletonLock"
    if not lock.is_symlink():
        return False
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[1])
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, IndexError):
        return False


def _run_quiet(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=30,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _profile_pids(profile_dir: Path) -> list[int]:
    """Главные процессы браузера (не вкладки), запущенные с профилем бота."""
    path = str(profile_dir)
    if sys.platform == "win32":
        quoted = path.replace("'", "''")
        script = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='msedge.exe'\" | "
                  f"Where-Object {{ $_.CommandLine -and $_.CommandLine.Contains('{quoted}') -and "
                  "-not $_.CommandLine.Contains('--type=') } | ForEach-Object { $_.ProcessId }")
        out = _run_quiet(["powershell", "-NoProfile", "-NonInteractive", "-Command", script]).stdout
    else:
        out = _run_quiet(["pgrep", "-f", "--", f"--user-data-dir={path}"]).stdout
    return [int(x) for x in out.split() if x.strip().isdigit()]


def close_profile_browser(profile_dir: Path, timeout: float = 15) -> bool:
    """Закрывает окно браузера, которое держит профиль бота: сначала вежливо (чтобы Chrome
    успел сохранить вход), потом принудительно. True — профиль свободен."""
    if not profile_in_use(profile_dir):
        return True
    log.info("Браузер бота ещё открыт — закрываю его")
    try:
        pids = _profile_pids(profile_dir)
        for force in (False, True):
            for pid in pids:
                if sys.platform == "win32":
                    _run_quiet(["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else []))
                else:
                    _run_quiet(["kill", "-9" if force else "-15", str(pid)])
            deadline = time.monotonic() + (timeout if not force else 5)
            while time.monotonic() < deadline:
                if not profile_in_use(profile_dir):
                    return True
                time.sleep(0.5)
    except (OSError, subprocess.SubprocessError) as ex:
        log.warning("Не получилось закрыть браузер бота: %s", ex)
    return not profile_in_use(profile_dir)


def friendly_error(ex: BaseException) -> str:
    """Короткое понятное описание ошибки (длинный технический текст остаётся в журнале)."""
    text = str(ex).strip()
    if "existing browser session" in text or "already in use" in text:
        return ("Браузер бота уже открыт (например, окно для входа в аккаунты). "
                "Закройте его и попробуйте ещё раз.")
    first = text.splitlines()[0] if text else type(ex).__name__
    return first[:300]


def launch(pw, settings: Settings, profile_dir: Path, executable: str | None = None, headless: bool = False):
    """Открывает браузер с профилем бота. Камера и микрофон запрещены на уровне браузера."""
    # Если осталось открытым окно для входа (или другое окно с профилем бота), браузер
    # бота не запустится: Chrome просто передаст ему задание и закроется.
    close_profile_browser(profile_dir)
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
        headless=headless,
        args=args,
        ignore_default_args=IGNORED_DEFAULT_ARGS,
        no_viewport=True,
        chromium_sandbox=_sandbox_supported(),
    )


def open_plain_browser(settings: Settings, profile_dir: Path, *urls: str) -> subprocess.Popen:
    """Обычное окно браузера (без автоматизации) с профилем бота — для входа в аккаунты."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    exe = find_browser(settings)
    extra = [] if _sandbox_supported() else ["--no-sandbox"]
    return subprocess.Popen([exe, f"--user-data-dir={profile_dir}", "--no-first-run",
                             "--no-default-browser-check", *extra, *urls])
