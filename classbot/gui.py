"""Окно программы ClassBot: расписание, уведомления, настройки и кнопка «Запустить»."""
from __future__ import annotations

import datetime as dt
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from urllib.error import HTTPError

from . import autostart, control, tasks
from .browser import BrowserNotFound, friendly_error, profile_in_use
from .classroom import course_id
from .errors import NotLoggedIn
from .config import (DAY_SHORT, ClassEntry, Config, ConfigError, config_to_dict, default_config, describe_source,
                     load_config, parse_class, parse_config, save_config, upcoming)
from .links import normalize_link
from .notify import Notifier
from .paths import Paths
from .state import State, load_courses
from .autofind import AutoCalls
from .links import platform_of

log = logging.getLogger(__name__)

DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WEEK_CHOICES = [("", "каждую неделю"), ("odd", "только нечётные"), ("even", "только чётные")]
BROWSERS = [("auto", "Автоматически (Chrome, если есть)"), ("chrome", "Google Chrome"), ("msedge", "Microsoft Edge")]
NUMBER_FIELDS = [
    ("join_before_min", "Заходить в звонок за (мин до начала)", 0, 30),
    ("look_for_link_before_min", "Начинать искать ссылку в ленте за (мин до начала)", 1, 120),
    ("fallback_after_min", "Если новой ссылки нет — зайти по последней через (мин после начала)", 0, 60),
    ("admit_wait_min", "Сколько ждать, пока впустят (мин)", 1, 90),
    ("stay_after_end_min", "Посидеть после конца пары (мин)", 0, 60),
]
DEFAULT_KEYWORDS = ["перекличк", "отметьтесь", "присутству"]


def ui_font(size: int, bold: bool = False) -> tkfont.Font:
    """Шрифт интерфейса нужного размера (работает с любой темой и системой)."""
    font = tkfont.nametofont("TkDefaultFont").copy()
    font.configure(size=size, weight="bold" if bold else "normal")
    return font


def asset(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "assets" / name


class QueueLogHandler(logging.Handler):
    """Передаёт строки журнала в окно (окно забирает их из очереди в своём потоке)."""

    def __init__(self, q: queue.Queue):
        super().__init__(logging.INFO)
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.q.put_nowait(self.format(record))
        except Exception:
            pass


# --- копирование и вставка -------------------------------------------------------
# В Tk сочетания Ctrl+V/C/X/A привязаны к латинским буквам, поэтому при русской раскладке
# они не работают. Ловим их по коду клавиши и добавляем меню по правой кнопке мыши.

_EDIT_CLASSES = ("Entry", "TEntry", "TCombobox", "TSpinbox", "Spinbox", "Text")
_WIN_KEYCODES = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}
_CYRILLIC_KEYSYMS = {"cyrillic_em": "<<Paste>>", "cyrillic_es": "<<Copy>>", "cyrillic_che": "<<Cut>>",
                     "cyrillic_ef": "<<SelectAll>>"}


def clipboard_action(keycode: int, keysym: str, windows: bool) -> str | None:
    """Какое действие выполнить для Ctrl+клавиша при нелатинской раскладке (None — Tk справится сам)."""
    sym = (keysym or "").lower()
    if sym in ("v", "c", "x", "a"):
        return None
    if windows:
        return _WIN_KEYCODES.get(keycode)
    return _CYRILLIC_KEYSYMS.get(sym)


def _on_control_key(event):
    action = clipboard_action(event.keycode, event.keysym, sys.platform == "win32")
    if action:
        event.widget.event_generate(action)
        return "break"
    return None


def _show_edit_menu(event):
    widget = event.widget
    try:
        widget.focus_set()
    except tk.TclError:
        pass
    menu = tk.Menu(widget, tearoff=0)
    for label, action in (("Вырезать", "<<Cut>>"), ("Копировать", "<<Copy>>"), ("Вставить", "<<Paste>>")):
        menu.add_command(label=label, command=lambda a=action: widget.event_generate(a))
    menu.add_separator()
    menu.add_command(label="Выделить всё", command=lambda: widget.event_generate("<<SelectAll>>"))
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        menu.grab_release()


def install_edit_bindings(root: tk.Misc) -> None:
    button = "<Button-2>" if sys.platform == "darwin" else "<Button-3>"
    for cls in _EDIT_CLASSES:
        root.bind_class(cls, "<Control-KeyPress>", _on_control_key, add="+")
        root.bind_class(cls, button, _show_edit_menu, add="+")


def paste_into(var: tk.StringVar, parent) -> None:
    """Кнопка «Вставить»: кладёт в поле то, что сейчас в буфере обмена."""
    try:
        text = parent.clipboard_get()
    except tk.TclError:
        messagebox.showinfo("Вставить", "Буфер обмена пуст — сначала скопируйте ссылку.", parent=parent)
        return
    var.set(text.strip())


class ClassDialog(tk.Toplevel):
    """Добавление / изменение пары."""

    def __init__(self, parent, entry: ClassEntry | None, other_names: set[str],
                 courses: list[dict] | None = None, on_load_courses=None):
        super().__init__(parent)
        self.title("Пара" if entry else "Новая пара")
        self.transient(parent)
        self.resizable(False, False)
        self.result: ClassEntry | None = None
        self.other_names = other_names
        self.courses = list(courses or [])
        self.on_load_courses = on_load_courses

        frm = ttk.Frame(self, padding=16)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(0, minsize=150)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Название").grid(row=0, column=0, sticky="w", pady=4)
        self.name = tk.StringVar(value=entry.name if entry else "")
        name_entry = ttk.Entry(frm, textvariable=self.name, width=40)
        name_entry.grid(row=0, column=1, columnspan=2, sticky="we", pady=4)

        ttk.Label(frm, text="Дни").grid(row=1, column=0, sticky="w", pady=4)
        days = ttk.Frame(frm)
        days.grid(row=1, column=1, columnspan=2, sticky="w", pady=4)
        self.days = []
        for i, label in enumerate(DAY_NAMES):
            var = tk.BooleanVar(value=bool(entry and i in entry.days))
            ttk.Checkbutton(days, text=label, variable=var).pack(side="left", padx=(0, 8))
            self.days.append(var)

        ttk.Label(frm, text="Время").grid(row=2, column=0, sticky="w", pady=4)
        times = ttk.Frame(frm)
        times.grid(row=2, column=1, columnspan=2, sticky="w", pady=4)
        self.start = tk.StringVar(value=f"{entry.start:%H:%M}" if entry else "09:00")
        self.end = tk.StringVar(value=f"{entry.end:%H:%M}" if entry else "10:30")
        ttk.Label(times, text="с").pack(side="left")
        ttk.Entry(times, textvariable=self.start, width=7).pack(side="left", padx=6)
        ttk.Label(times, text="до").pack(side="left")
        ttk.Entry(times, textvariable=self.end, width=7).pack(side="left", padx=6)
        ttk.Label(times, text="(например 09:00)", foreground="gray").pack(side="left", padx=6)

        ttk.Separator(frm).grid(row=3, column=0, columnspan=3, sticky="we", pady=10)
        ttk.Label(frm, text="Где брать ссылку на звонок").grid(row=4, column=0, columnspan=3, sticky="w")
        self.source = tk.StringVar(value="link" if entry and entry.link else "course")
        ttk.Radiobutton(frm, text="Из ленты курса в Google Classroom — преподаватель выкладывает ссылку перед парой",
                        variable=self.source, value="course", command=self._toggle).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(6, 2))
        ttk.Label(frm, text="Курс").grid(row=6, column=0, sticky="w", padx=(22, 0))
        self.course = tk.StringVar(value=self._course_label(entry.course) if entry and entry.course else "")
        self.course_box = ttk.Combobox(frm, textvariable=self.course, width=56,
                                       values=[c["name"] for c in self.courses])
        self.course_box.grid(row=6, column=1, sticky="we", pady=2)
        self.course_box.bind("<<ComboboxSelected>>", self._course_picked)
        course_buttons = ttk.Frame(frm)
        course_buttons.grid(row=6, column=2, sticky="w", padx=(6, 0))
        self.course_paste = ttk.Button(course_buttons, text="Вставить",
                                       command=lambda: paste_into(self.course, self))
        self.course_paste.pack(side="left")
        self.load_btn = ttk.Button(course_buttons, text="Загрузить курсы", command=self._load_courses)
        self.load_btn.pack(side="left", padx=(6, 0))
        self.course_hint = tk.StringVar()
        ttk.Label(frm, textvariable=self.course_hint, foreground="gray", wraplength=560, justify="left").grid(
            row=7, column=1, columnspan=2, sticky="w")
        self._update_course_hint()

        ttk.Radiobutton(frm, text="Постоянная ссылка на Google Meet или Zoom", variable=self.source, value="link",
                        command=self._toggle).grid(row=8, column=0, columnspan=3, sticky="w", pady=(10, 2))
        ttk.Label(frm, text="Ссылка").grid(row=9, column=0, sticky="w", padx=(22, 0))
        self.link = tk.StringVar(value=(entry.link or "") if entry else "")
        self.link_entry = ttk.Entry(frm, textvariable=self.link, width=58)
        self.link_entry.grid(row=9, column=1, sticky="we", pady=2)
        self.link_paste = ttk.Button(frm, text="Вставить", command=lambda: paste_into(self.link, self))
        self.link_paste.grid(row=9, column=2, sticky="w", padx=(6, 0))
        ttk.Label(frm, text="Код Zoom").grid(row=10, column=0, sticky="w", padx=(22, 0))
        code_row = ttk.Frame(frm)
        code_row.grid(row=10, column=1, columnspan=2, sticky="w", pady=2)
        self.passcode = tk.StringVar(value=(entry.passcode or "") if entry else "")
        self.passcode_entry = ttk.Entry(code_row, textvariable=self.passcode, width=16)
        self.passcode_entry.pack(side="left")
        ttk.Label(code_row, text="нужен, только если Zoom спрашивает код и его нет в ссылке",
                  foreground="gray").pack(side="left", padx=8)

        ttk.Separator(frm).grid(row=11, column=0, columnspan=3, sticky="we", pady=10)
        ttk.Label(frm, text="Недели").grid(row=12, column=0, sticky="w")
        weeks = ttk.Frame(frm)
        weeks.grid(row=12, column=1, columnspan=2, sticky="w")
        self.week = tk.StringVar(value=(entry.week or "") if entry else "")
        for value, label in WEEK_CHOICES:
            ttk.Radiobutton(weeks, text=label, variable=self.week, value=value).pack(side="left", padx=(0, 12))

        buttons = ttk.Frame(frm)
        buttons.grid(row=13, column=0, columnspan=3, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Отмена", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="Сохранить", style="Accent.TButton", command=self._ok).pack(side="right")

        self._toggle()
        self.bind("<Return>", lambda _: self._ok())
        self.bind("<Escape>", lambda _: self.destroy())
        self.wait_visibility()
        self.grab_set()
        name_entry.focus_set()

    # --- курс ---

    def _course_label(self, url: str) -> str:
        cid = course_id(url)
        for c in self.courses:
            if cid and course_id(c["url"]) == cid:
                return c["name"]
        return url

    def _course_url(self) -> str:
        text = self.course.get().strip()
        for c in self.courses:
            if c["name"] == text:
                return c["url"]
        return text

    def _course_picked(self, _event=None):
        if not self.name.get().strip():
            self.name.set(self.course.get())

    def _update_course_hint(self):
        if self.courses:
            self.course_hint.set("Выберите курс из списка — ссылку на пару бот найдёт в его ленте сам. "
                                 "Можно и вставить ссылку на курс из адресной строки браузера.")
        else:
            self.course_hint.set("Список курсов появится после «Войти в аккаунты» на главной вкладке "
                                 "(или нажмите «Загрузить курсы»). Можно и вставить ссылку на курс "
                                 "из адресной строки браузера: https://classroom.google.com/c/…")

    def _load_courses(self):
        if self.on_load_courses:
            self.on_load_courses(self._courses_loaded)

    def _courses_loaded(self, courses: list[dict]):
        if not self.winfo_exists():
            return
        self.courses = list(courses or [])
        self.course_box.configure(values=[c["name"] for c in self.courses])
        self._update_course_hint()
        if self.courses:
            self.course_box.focus_set()

    def _toggle(self):
        course = self.source.get() == "course"
        for widget in (self.course_box, self.course_paste, self.load_btn):
            widget.configure(state="normal" if course else "disabled")
        for widget in (self.link_entry, self.link_paste, self.passcode_entry):
            widget.configure(state="disabled" if course else "normal")

    def _ok(self):
        days = [DAY_SHORT[i] for i, var in enumerate(self.days) if var.get()]
        if not self.name.get().strip():
            messagebox.showerror("Пара", "Впишите название пары", parent=self)
            return
        if not days:
            messagebox.showerror("Пара", "Отметьте хотя бы один день недели", parent=self)
            return
        raw = {"name": self.name.get().strip(), "days": days, "start": self.start.get().strip(),
               "end": self.end.get().strip(), "week": self.week.get() or None}
        if self.source.get() == "course":
            course = self._course_url()
            if not course:
                messagebox.showerror("Пара", "Выберите курс из списка или вставьте ссылку на курс", parent=self)
                return
            if not course_id(course):
                messagebox.showerror("Пара", "Не понимаю, какой это курс. Выберите курс из списка или вставьте "
                                             "ссылку на курс из браузера (https://classroom.google.com/…)",
                                     parent=self)
                return
            raw["course"] = course  # любая ссылка на курс превратится в ссылку на его ленту
        else:
            if not self.link.get().strip():
                messagebox.showerror("Пара", "Вставьте ссылку на Meet или Zoom", parent=self)
                return
            raw["link"] = self.link.get().strip()
            raw["passcode"] = self.passcode.get().strip()
        try:
            entry = parse_class(raw)
        except ConfigError as ex:
            messagebox.showerror("Пара", str(ex), parent=self)
            return
        if entry.name in self.other_names:
            messagebox.showerror("Пара", "Пара с таким названием уже есть — назовите иначе", parent=self)
            return
        self.result = entry
        self.destroy()


class LoginDialog(tk.Toplevel):
    """Пока открыт браузер для входа: инструкция и кнопка «Готово»."""

    def __init__(self, parent, proc: subprocess.Popen, profile: Path, on_done):
        super().__init__(parent)
        self.title("Вход в аккаунты")
        self.transient(parent)
        self.resizable(False, False)
        self.proc = proc
        self.profile = profile
        self.on_done = on_done
        self.started = time.monotonic()
        frm = ttk.Frame(self, padding=18)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Открылось окно браузера бота", font=ui_font(13, True)).pack(anchor="w")
        ttk.Label(frm, justify="left", wraplength=460, text=(
            "Это отдельный профиль: ваш обычный браузер не затронут.\n\n"
            "1. На вкладке Classroom войдите в Google-аккаунт, через который учитесь, "
            "и проверьте, что видны ваши курсы.\n"
            "2. Если преподаватели требуют вход в Zoom — войдите и на вкладке Zoom. Иначе её можно просто закрыть.\n"
            "3. Закройте окно браузера — проверка начнётся сама (или нажмите кнопку ниже)."
        )).pack(anchor="w", pady=(8, 14))
        ttk.Button(frm, text="Я вошёл — закрыть браузер и проверить", style="Accent.TButton",
                   command=self._finish).pack(anchor="e")
        self.protocol("WM_DELETE_WINDOW", self._finish)
        self.wait_visibility()
        self.grab_set()
        self.after(500, self._watch)

    def _watch(self):
        if not self.winfo_exists():
            return
        # Ждём, пока закроется само окно браузера (запущенный процесс мог сразу передать его другому).
        if self.proc.poll() is not None and not profile_in_use(self.profile) and time.monotonic() - self.started > 3:
            self._finish()
        else:
            self.after(500, self._watch)

    def _finish(self):
        if self.on_done:
            on_done, self.on_done = self.on_done, None
            self.destroy()
            on_done()


class App:
    def __init__(self, root: tk.Tk, paths: Paths, autostart_mode: bool = False):
        self.root = root
        self.paths = paths
        self.ui_queue: queue.Queue = queue.Queue()
        self.log_queue: queue.Queue = queue.Queue()
        self.log_handler = QueueLogHandler(self.log_queue)
        logging.getLogger().addHandler(self.log_handler)
        self.bot_thread: threading.Thread | None = None
        self.task_thread: threading.Thread | None = None
        self.closing = False

        self.cfg, load_error = self._load()
        self.courses = load_courses(paths.courses)
        install_edit_bindings(root)
        self._build()
        self._fill_from_config()
        self._update_buttons()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(150, self._poll)
        root.after(60_000, self._tick)
        if load_error:
            messagebox.showwarning("Настройки", f"Не получилось прочитать настройки: {load_error}\n\n"
                                                "Открыты настройки по умолчанию.")
        if autostart_mode:
            root.iconify()
            root.after(1500, self.start_bot)

    # --- настройки ------------------------------------------------------------

    def _load(self) -> tuple[Config, str | None]:
        if self.paths.config.exists():
            try:
                return load_config(self.paths.config), None
            except ConfigError as ex:
                return default_config(), str(ex)
        cfg = default_config()
        cfg.telegram.mention_keywords = list(DEFAULT_KEYWORDS)
        return cfg, None

    def collect(self) -> Config:
        """Собирает настройки из полей окна и проверяет их."""
        raw = config_to_dict(self.cfg)
        raw["telegram"] = {
            "bot_token": self.token_var.get().strip(),
            "chat_id": self.chat_var.get().strip(),
            "mention_keywords": [k.strip() for k in self.keywords_text.get("1.0", "end").splitlines() if k.strip()],
        }
        st = raw["settings"]
        for key, label, _, _ in NUMBER_FIELDS:
            text = self.num_vars[key].get().strip().replace(",", ".")
            try:
                value = float(text)
            except ValueError:
                raise ConfigError(f"«{label}»: нужно число, а не {text!r}") from None
            if value < 0:
                raise ConfigError(f"«{label}»: число не может быть отрицательным")
            st[key] = value
        st["display_name"] = self.name_var.get().strip()
        st["zoom_mode"] = self.zoom_mode_var.get()
        st["mute_audio"] = self.mute_var.get()
        st["watch_captions"] = self.captions_var.get()
        st["browser"] = BROWSERS[self.browser_box.current()][0] if self.browser_box.current() >= 0 else "auto"
        st["semester_start"] = self.semester_var.get().strip() or None
        st["auto_enabled"] = self.auto_var.get()
        st["auto_courses"] = [url for url, var in self.auto_course_vars.items() if var.get()]
        text = self.auto_duration_var.get().strip().replace(",", ".")
        try:
            st["auto_duration_min"] = float(text)
        except ValueError:
            raise ConfigError(f"«Сколько сидеть на звонке»: нужно число минут, а не {text!r}") from None
        try:
            return parse_config(raw, self.paths.config)
        except ConfigError as ex:
            text = str(ex).replace("settings.semester_start", "«Первый день семестра» во вкладке «Настройки»")
            raise ConfigError(text) from None

    def save(self, quiet: bool = False) -> Config | None:
        try:
            cfg = self.collect()
        except ConfigError as ex:
            if not quiet:
                messagebox.showerror("Настройки не сохранены", str(ex))
            return None
        save_config(cfg, self.paths.config)
        self.cfg = cfg
        note = " Перезапустите бота, чтобы изменения применились." if self.bot_running() else ""
        self.saved_var.set(f"Сохранено в {dt.datetime.now():%H:%M}.{note}")
        self.refresh_upcoming()
        return cfg

    # --- окно -----------------------------------------------------------------

    def _build(self):
        root = self.root
        root.title("ClassBot — автозаход на пары")
        root.geometry("940x700")
        root.minsize(800, 580)
        outer = ttk.Frame(root, padding=10)
        outer.pack(fill="both", expand=True)
        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True)
        self.tab_main = ttk.Frame(self.nb, padding=14)
        self.tab_schedule = ttk.Frame(self.nb, padding=14)
        self.tab_telegram = ttk.Frame(self.nb, padding=14)
        self.tab_settings = ttk.Frame(self.nb, padding=14)
        self.nb.add(self.tab_main, text="  Главная  ")
        self.nb.add(self.tab_schedule, text="  Расписание  ")
        self.nb.add(self.tab_telegram, text="  Уведомления  ")
        self.nb.add(self.tab_settings, text="  Настройки  ")
        self._build_main()
        self._build_schedule()
        self._build_telegram()
        self._build_settings()

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(8, 0))
        self.saved_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.saved_var, foreground="gray").pack(side="left")
        ttk.Button(bottom, text="Сохранить настройки", command=self.save).pack(side="right")

    def _build_main(self):
        f = self.tab_main
        top = ttk.Frame(f)
        top.pack(fill="x")
        left = ttk.Frame(top)
        left.pack(side="left", fill="x", expand=True)
        self.state_label = ttk.Label(left, text="Бот выключен", font=ui_font(17, True))
        self.state_label.pack(anchor="w")
        self.status_var = tk.StringVar(value="Настройте расписание и нажмите «Запустить»")
        ttk.Label(left, textvariable=self.status_var, wraplength=560).pack(anchor="w", pady=(2, 0))
        buttons = ttk.Frame(top)
        buttons.pack(side="right")
        self.start_btn = ttk.Button(buttons, text="Запустить", style="Accent.TButton", command=self.start_bot)
        self.start_btn.pack(side="left", padx=4, ipadx=14)
        self.stop_btn = ttk.Button(buttons, text="Остановить", command=self.stop_bot)
        self.stop_btn.pack(side="left", padx=4, ipadx=8)

        actions = ttk.Frame(f)
        actions.pack(fill="x", pady=(14, 4))
        self.login_btn = ttk.Button(actions, text="Войти в аккаунты", command=self.login)
        self.check_btn = ttk.Button(actions, text="Проверить всё", command=self.check)
        self.test_btn = ttk.Button(actions, text="Тестовый заход…", command=self.test_join)
        self.logs_btn = ttk.Button(actions, text="Папка с журналом", command=self.open_logs)
        self.diag_btn = ttk.Button(actions, text="Диагностика ленты", command=self.diagnose)
        for b in (self.login_btn, self.check_btn, self.test_btn, self.diag_btn, self.logs_btn):
            b.pack(side="left", padx=(0, 8))
        ttk.Label(f, foreground="gray", wraplength=880, justify="left", text=(
            "Как начать: 1) «Войти в аккаунты»  2) во вкладке «Расписание» включите автоматический режим и отметьте "
            "курс  3) подключите Telegram во вкладке «Уведомления»  4) «Проверить всё» — бот покажет, какие звонки "
            "видит в ленте  5) «Запустить». Компьютер должен оставаться включённым.")).pack(anchor="w", pady=(4, 10))

        ttk.Label(f, text="Ближайшие пары", font=ui_font(11, True)).pack(anchor="w")
        self.upcoming_tree = ttk.Treeview(f, columns=("when", "name", "source"), show="headings", height=4)
        for col, title, width in (("when", "Когда", 200), ("name", "Пара", 280), ("source", "Ссылка", 260)):
            self.upcoming_tree.heading(col, text=title, anchor="w")
            self.upcoming_tree.column(col, width=width, anchor="w")
        self.upcoming_tree.pack(fill="x", pady=(4, 10))

        ttk.Label(f, text="Журнал", font=ui_font(11, True)).pack(anchor="w")
        self.log_text = ScrolledText(f, height=10, wrap="word", state="disabled", relief="solid", borderwidth=1,
                                     background="white", highlightthickness=0,
                                     font=("Consolas" if sys.platform == "win32" else "TkFixedFont", 9))
        self.log_text.pack(fill="both", expand=True, pady=(4, 0))

    def _build_schedule(self):
        f = self.tab_schedule
        auto = ttk.LabelFrame(f, text=" Автоматический режим ", padding=12)
        auto.pack(fill="x", pady=(0, 14))
        self.auto_var = tk.BooleanVar()
        ttk.Checkbutton(auto, text="Сам находить звонки в ленте курса и заходить на них",
                        variable=self.auto_var, command=self._auto_changed).pack(anchor="w")
        ttk.Label(auto, text="Следить за курсами:").pack(anchor="w", padx=(28, 0), pady=(8, 0))
        self.auto_courses_frame = ttk.Frame(auto)
        self.auto_courses_frame.pack(anchor="w", padx=(28, 0))
        self.auto_course_vars: dict[str, tk.BooleanVar] = {}
        dur = ttk.Frame(auto)
        dur.pack(anchor="w", padx=(28, 0), pady=(8, 0))
        ttk.Label(dur, text="Сколько сидеть на звонке (время конца в постах обычно не пишут):").pack(side="left")
        self.auto_duration_var = tk.StringVar()
        ttk.Spinbox(dur, from_=20, to=240, textvariable=self.auto_duration_var, width=6).pack(side="left", padx=8)
        ttk.Label(dur, text="мин").pack(side="left")
        ttk.Label(auto, foreground="gray", wraplength=860, justify="left", text=(
            "Бот каждые 5 минут смотрит ленту. Если в посте со ссылкой есть дата и время (как в приглашении Zoom: "
            "«Час: 23 вересня 2026 9:00») — зайдёт к этому времени. Если появилась новая ссылка без даты — "
            "зайдёт к указанному в посте времени («о 10:40») или сразу (с 7:00 до 21:00). О каждом найденном "
            "звонке придёт сообщение в Telegram. С этим режимом расписание ниже можно не заполнять.")).pack(
            anchor="w", pady=(8, 0))
        ttk.Label(f, text="Постоянное расписание (необязательно)", font=ui_font(11, True)).pack(anchor="w", pady=(0, 4))
        cols = (("name", "Пара", 190), ("days", "Дни", 130), ("time", "Время", 110),
                ("source", "Ссылка", 250), ("week", "Недели", 130))
        self.tree = ttk.Treeview(f, columns=[c[0] for c in cols], show="headings", selectmode="browse", height=5)
        for col, title, width in cols:
            self.tree.heading(col, text=title, anchor="w")
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _: self.edit_class())
        buttons = ttk.Frame(f)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="+ Добавить пару", style="Accent.TButton", command=self.add_class).pack(side="left")
        ttk.Button(buttons, text="Изменить", command=self.edit_class).pack(side="left", padx=8)
        ttk.Button(buttons, text="Удалить", command=self.delete_class).pack(side="left")
        ttk.Button(buttons, text="Загрузить мои курсы из Classroom", command=self.load_courses).pack(side="right")
        ttk.Label(f, foreground="gray", wraplength=880, justify="left", text=(
            "Сюда можно добавить пары с постоянным временем: бот за 15 минут до начала найдёт в ленте курса "
            "свежую ссылку (или возьмёт постоянную ссылку на Meet/Zoom).")).pack(anchor="w", pady=(10, 0))

    def _build_telegram(self):
        f = self.tab_telegram
        f.columnconfigure(1, weight=1)
        ttk.Label(f, justify="left", wraplength=860, text=(
            "В Telegram приходит: зашёл на пару, вышел, не смог зайти (со скриншотом) и — главное — "
            "если в чате или субтитрах пары прозвучала ваша фамилия или слова вроде «перекличка». "
            "Тогда можно быстро зайти с телефона и ответить самому.\n\n"
            "1. В Telegram откройте @BotFather, отправьте /newbot, придумайте имя и логин бота "
            "(логин должен заканчиваться на bot).\n"
            "2. Скопируйте токен, который пришлёт BotFather, и вставьте ниже.\n"
            "3. Нажмите «Подключить» и отправьте своему новому боту любое сообщение.")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 14))
        ttk.Label(f, text="Токен бота").grid(row=1, column=0, sticky="w", pady=4)
        self.token_var = tk.StringVar()
        token_row = ttk.Frame(f)
        token_row.grid(row=1, column=1, columnspan=2, sticky="we", pady=4)
        ttk.Entry(token_row, textvariable=self.token_var).pack(side="left", fill="x", expand=True)
        ttk.Button(token_row, text="Вставить", command=lambda: paste_into(self.token_var, self.root)).pack(
            side="left", padx=(8, 0))
        self.tg_btn = ttk.Button(token_row, text="Подключить", style="Accent.TButton", command=self.connect_telegram)
        self.tg_btn.pack(side="left", padx=(8, 0))
        self.tg_status = tk.StringVar()
        ttk.Label(f, textvariable=self.tg_status, foreground="#1a7f37", wraplength=700).grid(
            row=2, column=1, columnspan=2, sticky="w")
        ttk.Label(f, text="Chat ID").grid(row=3, column=0, sticky="w", pady=4)
        self.chat_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.chat_var, width=20).grid(row=3, column=1, sticky="w", pady=4)
        ttk.Label(f, text="Слова для уведомлений\n(по одному в строке)").grid(row=4, column=0, sticky="nw", pady=(12, 4))
        self.keywords_text = tk.Text(f, height=7, width=40, relief="solid", borderwidth=1, background="white",
                                     highlightthickness=0, padx=6, pady=4, font=ui_font(10))
        self.keywords_text.grid(row=4, column=1, sticky="w", pady=(12, 4))
        ttk.Label(f, foreground="gray", wraplength=300, justify="left", text=(
            "Обязательно впишите свою фамилию. Можно писать начало слова: «перекличк» найдёт и «перекличка», "
            "и «перекличку».")).grid(row=4, column=2, sticky="nw", padx=(8, 0), pady=(12, 0))
        self.tg_test_btn = ttk.Button(f, text="Отправить тестовое сообщение", command=self.test_telegram)
        self.tg_test_btn.grid(row=5, column=1, sticky="w", pady=(10, 0))

    def _build_settings(self):
        f = self.tab_settings
        f.columnconfigure(1, weight=1)
        row = 0
        ttk.Label(f, text="Ваше имя для Zoom").grid(row=row, column=0, sticky="w", pady=4)
        self.name_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.name_var, width=32).grid(row=row, column=1, sticky="w", pady=4)
        row += 1
        ttk.Label(f, text="Если вы не вошли в аккаунт Zoom, бот представится этим именем",
                  foreground="gray").grid(row=row, column=1, sticky="w")
        row += 1
        ttk.Label(f, text="Как заходить в Zoom").grid(row=row, column=0, sticky="nw", pady=(10, 4))
        zoom_box = ttk.Frame(f)
        zoom_box.grid(row=row, column=1, sticky="w", pady=(10, 4))
        self.zoom_mode_var = tk.StringVar(value="browser")
        ttk.Radiobutton(zoom_box, text="В браузере (рекомендуется)",
                        variable=self.zoom_mode_var, value="browser").pack(anchor="w")
        ttk.Label(zoom_box, text="бот видит, что зашёл, и следит за чатом", foreground="gray").pack(
            anchor="w", padx=(28, 0))
        ttk.Radiobutton(zoom_box, text="В приложении Zoom",
                        variable=self.zoom_mode_var, value="app").pack(anchor="w", pady=(6, 0))
        ttk.Label(zoom_box, text="запасной вариант, если в браузере Zoom не пускает: бот не видит чат\n"
                                 "и в конце пары закрывает приложение Zoom", foreground="gray",
                  justify="left").pack(anchor="w", padx=(28, 0))
        row += 1
        ttk.Separator(f).grid(row=row, column=0, columnspan=2, sticky="we", pady=10)
        row += 1
        self.num_vars = {}
        for key, label, low, high in NUMBER_FIELDS:
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=3)
            var = tk.StringVar()
            ttk.Spinbox(f, from_=low, to=high, textvariable=var, width=6).grid(row=row, column=1, sticky="w", pady=3)
            self.num_vars[key] = var
            row += 1
        ttk.Separator(f).grid(row=row, column=0, columnspan=2, sticky="we", pady=10)
        row += 1
        self.mute_var = tk.BooleanVar()
        ttk.Checkbutton(f, text="Не выводить звук пары на колонки", variable=self.mute_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1
        self.captions_var = tk.BooleanVar()
        ttk.Checkbutton(f, text="Включать субтитры в Meet, чтобы бот слышал фамилию, сказанную вслух",
                        variable=self.captions_var).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1
        ttk.Label(f, text="Браузер").grid(row=row, column=0, sticky="w", pady=(10, 4))
        self.browser_box = ttk.Combobox(f, state="readonly", width=34, values=[b[1] for b in BROWSERS])
        self.browser_box.grid(row=row, column=1, sticky="w", pady=(10, 4))
        row += 1
        ttk.Label(f, text="Первый день семестра").grid(row=row, column=0, sticky="w", pady=4)
        self.semester_var = tk.StringVar()
        sem = ttk.Frame(f)
        sem.grid(row=row, column=1, sticky="w", pady=4)
        ttk.Entry(sem, textvariable=self.semester_var, width=12).pack(side="left")
        ttk.Label(sem, text="ДД.ММ.ГГГГ — нужен только для пар по чётным/нечётным неделям",
                  foreground="gray").pack(side="left", padx=8)
        row += 1
        self.autostart_var = tk.BooleanVar(value=autostart.is_enabled())
        cb = ttk.Checkbutton(f, text="Запускать ClassBot вместе с Windows и сразу включать бота",
                             variable=self.autostart_var, command=self.toggle_autostart)
        cb.grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 2))
        if not autostart.supported():
            cb.state(["disabled"])
        row += 1
        ttk.Label(f, text=f"Настройки и журнал хранятся в {self.paths.base}", foreground="gray").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(14, 0))

    def _fill_from_config(self):
        c = self.cfg
        self.token_var.set(c.telegram.bot_token)
        self.chat_var.set(c.telegram.chat_id)
        self.keywords_text.delete("1.0", "end")
        self.keywords_text.insert("1.0", "\n".join(c.telegram.mention_keywords))
        s = c.settings
        self.name_var.set(s.display_name)
        self.zoom_mode_var.set(s.zoom_mode)
        for key, *_ in NUMBER_FIELDS:
            value = getattr(s, key)
            self.num_vars[key].set(str(int(value)) if float(value).is_integer() else str(value))
        self.mute_var.set(s.mute_audio)
        self.captions_var.set(s.watch_captions)
        self.browser_box.current([b[0] for b in BROWSERS].index(s.browser) if s.browser in dict(BROWSERS) else 0)
        self.semester_var.set(f"{s.semester_start:%d.%m.%Y}" if s.semester_start else "")
        self.auto_var.set(s.auto_enabled)
        self.auto_duration_var.set(str(int(s.auto_duration_min)) if float(s.auto_duration_min).is_integer()
                                   else str(s.auto_duration_min))
        self.refresh_auto_courses(s.auto_courses)
        self.refresh_schedule()
        self.refresh_upcoming()

    def refresh_schedule(self):
        self.tree.delete(*self.tree.get_children())
        for i, c in enumerate(self.cfg.classes):
            days = ", ".join(DAY_NAMES[d] for d in sorted(c.days))
            week = dict(WEEK_CHOICES).get(c.week or "", "")
            self.tree.insert("", "end", iid=str(i), values=(
                c.name, days, f"{c.start:%H:%M}–{c.end:%H:%M}", self.source_label(c), week))

    def refresh_upcoming(self):
        self.upcoming_tree.delete(*self.upcoming_tree.get_children())
        now = dt.datetime.now()
        s = self.cfg.settings
        items = [(occ, self.source_label(occ.entry)) for occ in upcoming(self.cfg.classes, now, s)[:6]]
        if s.auto_enabled and s.auto_courses:
            try:
                found = AutoCalls(State(self.paths.state)).occurrences(now, dt.timedelta(minutes=s.auto_duration_min))
            except Exception:
                found = []
            scheduled = [occ for occ, _ in items]
            for occ in found:
                if not any(o.start < occ.end and occ.start < o.end for o in scheduled):
                    platform = {"zoom": "Zoom", "meet": "Meet"}.get(platform_of(occ.entry.link or ""), "")
                    items.append((occ, f"найдено в ленте ({platform})"))
        items.sort(key=lambda item: item[0].start)
        for occ, source in items[:6]:
            when = f"{DAY_NAMES[occ.start.weekday()]} {occ.start:%d.%m}  {occ.start:%H:%M}–{occ.end:%H:%M}"
            self.upcoming_tree.insert("", "end", values=(when, occ.entry.name, source))
        if not items and s.auto_enabled and s.auto_courses:
            # Показать, что бот следит за лентой, даже когда впереди ничего нет.
            try:
                calls = State(self.paths.state).data.get("auto", {}).get("calls", {}).values()
                last = max(calls, key=lambda rec: rec["start"], default=None)
            except Exception:
                last = None
            note = (f"последний найденный: {last['title']} — {dt.datetime.fromisoformat(last['start']):%d.%m %H:%M}"
                    if last else "бот смотрит ленту каждые 5 минут, пока бот запущен")
            self.upcoming_tree.insert("", "end", values=("—", "Новых звонков в ленте пока нет", note))

    # --- автоматический режим ---

    def refresh_auto_courses(self, selected: list[str] | None = None):
        if selected is None:
            selected = [url for url, var in self.auto_course_vars.items() if var.get()]
        selected_ids = {course_id(u) for u in selected}
        for child in self.auto_courses_frame.winfo_children():
            child.destroy()
        self.auto_course_vars = {}
        courses = list(self.courses)
        known = {course_id(c["url"]) for c in courses}
        courses += [{"name": url, "url": url} for url in selected if course_id(url) not in known]
        for c in courses:
            var = tk.BooleanVar(value=course_id(c["url"]) in selected_ids)
            ttk.Checkbutton(self.auto_courses_frame, text=c["name"], variable=var,
                            command=lambda: self.save(quiet=True)).pack(anchor="w")
            self.auto_course_vars[c["url"]] = var
        if not courses:
            ttk.Label(self.auto_courses_frame, foreground="gray", text=(
                "Список курсов появится после «Войти в аккаунты» на главной вкладке "
                "(или нажмите «Загрузить мои курсы из Classroom» ниже).")).pack(anchor="w")

    def _select_all_courses_if_none(self):
        if self.auto_var.get() and self.auto_course_vars and not any(v.get() for v in self.auto_course_vars.values()):
            for var in self.auto_course_vars.values():
                var.set(True)

    def _auto_changed(self):
        self._select_all_courses_if_none()
        self.save(quiet=True)

    def _courses_updated(self):
        self.refresh_auto_courses()
        self._select_all_courses_if_none()
        self.save(quiet=True)
        self.refresh_schedule()
        self.refresh_upcoming()

    def source_label(self, entry: ClassEntry) -> str:
        if entry.course and not entry.link:
            cid = course_id(entry.course)
            for c in self.courses:
                if cid and course_id(c["url"]) == cid:
                    return f"лента курса «{c['name']}»"
        return describe_source(entry)

    # --- расписание -------------------------------------------------------------

    def _selected(self) -> int | None:
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def add_class(self):
        dlg = ClassDialog(self.root, None, {c.name for c in self.cfg.classes}, self.courses, self.load_courses)
        self.root.wait_window(dlg)
        if dlg.result:
            self.cfg.classes.append(dlg.result)
            self._schedule_changed(dlg.result)

    def edit_class(self):
        idx = self._selected()
        if idx is None:
            messagebox.showinfo("Расписание", "Выберите пару в списке")
            return
        entry = self.cfg.classes[idx]
        dlg = ClassDialog(self.root, entry, {c.name for c in self.cfg.classes} - {entry.name}, self.courses,
                          self.load_courses)
        self.root.wait_window(dlg)
        if dlg.result:
            self.cfg.classes[idx] = dlg.result
            self._schedule_changed(dlg.result)

    def delete_class(self):
        idx = self._selected()
        if idx is None:
            return
        name = self.cfg.classes[idx].name
        if messagebox.askyesno("Удалить пару", f"Удалить пару «{name}» из расписания?"):
            del self.cfg.classes[idx]
            self._schedule_changed(None)

    def _schedule_changed(self, entry: ClassEntry | None):
        self.refresh_schedule()
        if entry and entry.week and not self.semester_var.get().strip():
            messagebox.showinfo("Чётные и нечётные недели",
                                "Чтобы бот знал, какая неделя чётная, укажите первый день семестра "
                                "во вкладке «Настройки».")
            self.nb.select(self.tab_settings)
            return
        self.save()

    # --- бот -----------------------------------------------------------------------

    def bot_running(self) -> bool:
        return bool(self.bot_thread and self.bot_thread.is_alive())

    def task_running(self) -> bool:
        return bool(self.task_thread and self.task_thread.is_alive())

    def _update_buttons(self):
        bot, task = self.bot_running(), self.task_running()
        idle = not bot and not task
        for b in (self.start_btn, self.login_btn, self.check_btn, self.test_btn, self.diag_btn):
            b.state(["!disabled"] if idle else ["disabled"])
        self.stop_btn.state(["!disabled"] if (bot or task) and not control.stop_event.is_set() else ["disabled"])
        self.tg_btn.state(["!disabled"] if not task else ["disabled"])
        if bot:
            self.state_label.configure(text="Бот работает", foreground="#1a7f37")
        else:
            self.state_label.configure(text="Бот выключен", foreground="#6e7781")

    def start_bot(self):
        from .runner import Runner

        if self.bot_running() or self.task_running():
            return
        cfg = self.save()
        if not cfg:
            return
        if not cfg.classes and not (cfg.settings.auto_enabled and cfg.settings.auto_courses):
            messagebox.showwarning("Нет пар", "Во вкладке «Расписание» включите автоматический режим и отметьте "
                                              "курс (или добавьте пары вручную).")
            self.nb.select(self.tab_schedule)
            return
        notifier = Notifier(cfg.telegram.bot_token, cfg.telegram.chat_id)
        try:
            runner = Runner(cfg, self.paths, notifier, on_status=lambda text: self.post(self.status_var.set, text))
        except BrowserNotFound as ex:
            messagebox.showerror("Нет браузера", str(ex))
            return
        if not notifier.enabled:
            log.warning("Telegram не подключён — уведомлений не будет")
        control.stop_event.clear()
        self.bot_thread = threading.Thread(target=self._bot_main, args=(runner,), daemon=True, name="bot")
        self.bot_thread.start()
        self._update_buttons()

    def _bot_main(self, runner):
        try:
            runner.run_forever()
        except control.Stopped:
            pass
        except Exception:
            log.exception("Бот остановился из-за ошибки")
        finally:
            self.post(self._bot_finished)

    def _bot_finished(self):
        self.bot_thread = None
        control.stop_event.clear()
        self.status_var.set("Бот остановлен")
        log.info("Бот остановлен")
        self._update_buttons()

    def stop_bot(self):
        if self.bot_running() or self.task_running():
            control.stop_event.set()
            self.status_var.set("Останавливаю… (закрываю браузер)")
            self._update_buttons()

    # --- фоновые задачи ------------------------------------------------------------

    def post(self, fn, *args):
        """Выполнить fn в потоке окна (из любого потока)."""
        self.ui_queue.put((fn, args))

    def run_task(self, title: str, fn, on_done=None):
        if self.bot_running() or self.task_running():
            messagebox.showinfo(title, "Сначала остановите бота — браузер бота может быть открыт только один раз.")
            return
        control.stop_event.clear()
        self.task_thread = threading.Thread(target=self._task_main, args=(title, fn, on_done), daemon=True)
        self.task_thread.start()
        self._update_buttons()

    def _task_main(self, title, fn, on_done):
        result, error = None, None
        stopped = False
        try:
            result = fn()
        except control.Stopped:
            stopped = True
            log.info("%s: остановлено", title)
        except (BrowserNotFound, NotLoggedIn) as ex:
            error = str(ex)[:1].upper() + str(ex)[1:]
        except HTTPError as ex:
            error = "Telegram не принял токен — проверьте, что скопировали его целиком" if ex.code == 401 else str(ex)
        except Exception as ex:
            log.exception("%s: ошибка", title)
            error = friendly_error(ex)
            if not error.startswith("Браузер бота уже открыт"):
                error += "\n\nПодробности — в журнале (кнопка «Папка с журналом»)."
        self.post(self._task_finished, title, result, error, None if stopped else on_done)

    def _task_finished(self, title, result, error, on_done):
        self.task_thread = None
        control.stop_event.clear()
        self._update_buttons()
        if error:
            messagebox.showerror(title, error)
        elif on_done:
            on_done(result)

    def check(self):
        cfg = self.save()
        if not cfg:
            return
        self.nb.select(self.tab_main)
        notifier = Notifier(cfg.telegram.bot_token, cfg.telegram.chat_id)
        self.run_task("Проверка", lambda: tasks.check(cfg, self.paths, notifier), lambda problems: (
            messagebox.showwarning("Проверка", "Что поправить:\n\n" + "\n\n".join(f"• {p}" for p in problems))
            if problems else messagebox.showinfo("Проверка", "Всё в порядке.")))

    def diagnose(self):
        cfg = self.save()
        if not cfg:
            return
        self.nb.select(self.tab_main)

        def done(report):
            self.root.clipboard_clear()
            self.root.clipboard_append(report)
            messagebox.showinfo("Диагностика ленты", (
                "Готово. Отчёт скопирован в буфер обмена — вставьте его в чат с разработчиком (Ctrl+V).\n\n"
                "Он же сохранён в папке с журналом: diagnostics.txt, а снимок ленты — diagnostics-feed.png.\n"
                "В отчёте есть текст вашей ленты (посты, ссылки и коды доступа)."))

        self.run_task("Диагностика ленты", lambda: tasks.diagnose_feed(cfg, self.paths), done)

    def test_join(self):
        cfg = self.save()
        if not cfg:
            return
        url = simpledialog.askstring(
            "Тестовый заход",
            "Создайте свою встречу в Meet или Zoom (лучше с другого аккаунта или попросите друга)\n"
            "и вставьте ссылку. Бот зайдёт на 3 минуты — напишите в чат встречи свою фамилию,\n"
            "чтобы проверить уведомление.\n\nСсылка:", parent=self.root)
        if not url:
            return
        if not normalize_link(url.strip()):
            messagebox.showerror("Тестовый заход", "Это не похоже на ссылку Google Meet или Zoom")
            return
        self.nb.select(self.tab_main)
        notifier = Notifier(cfg.telegram.bot_token, cfg.telegram.chat_id)
        self.run_task("Тестовый заход", lambda: tasks.test_join(cfg, self.paths, notifier, url.strip(), 3),
                      lambda ok: messagebox.showinfo("Тестовый заход", "Готово: бот зашёл и вышел." if ok else
                                                     "Не получилось — подробности в журнале на главной вкладке."))

    def login(self):
        cfg = self.save()
        if not cfg:
            return
        if self.bot_running() or self.task_running():
            messagebox.showinfo("Вход", "Сначала остановите бота.")
            return
        try:
            proc = tasks.open_login_window(cfg, self.paths)
        except BrowserNotFound as ex:
            messagebox.showerror("Нет браузера", str(ex))
            return

        def verify():
            tasks.finish_login(proc, self.paths)
            return tasks.verify_login(cfg, self.paths)

        LoginDialog(self.root, proc, self.paths.profile,
                    lambda: self.run_task("Проверка входа", verify, self._login_checked))

    def _login_checked(self, ok):
        if ok:
            self.courses = load_courses(self.paths.courses)
            self._courses_updated()
            found = (f"\n\nНашёл ваших курсов: {len(self.courses)}. Теперь во вкладке «Расписание» при добавлении "
                     "пары курс можно просто выбрать из списка.") if self.courses else ""
            messagebox.showinfo("Вход", "Готово: бот вошёл в Google Classroom." + found)
            return
        if messagebox.askyesno("Вход", "Бот не видит входа в Google.\n\nПопробовать войти прямо в окне бота? "
                                       "Войдите в открывшемся окне и закройте его."):
            cfg = self.cfg
            self.run_task("Вход", lambda: tasks.login_in_bot_window(cfg, self.paths), lambda ok2: (
                messagebox.showinfo("Вход", "Готово: бот вошёл в Google Classroom.") if ok2 else
                messagebox.showerror("Вход", "Войти не получилось. Если Google пишет, что браузер небезопасен, "
                                             "установите Google Chrome и попробуйте снова.")))

    def load_courses(self, callback=None):
        """Загрузить список курсов из Classroom (для выбора курса в окне пары)."""
        cfg = self.save(quiet=True) or self.cfg

        def done(courses):
            self.courses = courses or []
            self._courses_updated()
            if not self.courses:
                messagebox.showwarning("Курсы", "На главной странице Classroom не нашлось ни одного курса. "
                                                "Проверьте, что бот вошёл в тот аккаунт, через который вы учитесь.")
            elif callback is None:
                messagebox.showinfo("Курсы", f"Нашёл ваших курсов: {len(self.courses)}. Теперь при добавлении пары "
                                             "курс можно выбрать из списка.")
            if callback:
                callback(self.courses)

        self.run_task("Курсы", lambda: tasks.fetch_courses(cfg, self.paths), done)

    def connect_telegram(self):
        token = self.token_var.get().strip()
        if not token:
            messagebox.showinfo("Telegram", "Сначала вставьте токен бота от @BotFather (инструкция выше).")
            return
        self.tg_status.set("Проверяю токен…")

        def work():
            name = tasks.telegram_bot_name(token)
            self.post(self.tg_status.set, f"Откройте в Telegram бота @{name} и отправьте ему любое сообщение "
                                          f"(например /start). Жду до 3 минут…")
            return tasks.telegram_wait_for_chat(token)

        def done(chat_id):
            if not chat_id:
                self.tg_status.set("Сообщение так и не пришло. Нажмите «Подключить» ещё раз.")
                return
            self.chat_var.set(chat_id)
            if self.save():
                self.tg_status.set("Подключено. В Telegram пришло сообщение от бота.")
                threading.Thread(target=Notifier(token, chat_id).send, daemon=True,
                                 args=("✅ ClassBot подключён. Сюда будут приходить уведомления о парах.",)).start()

        self.run_task("Telegram", work, done)

    def test_telegram(self):
        cfg = self.save()
        if not cfg:
            return
        notifier = Notifier(cfg.telegram.bot_token, cfg.telegram.chat_id)
        if not notifier.enabled:
            messagebox.showinfo("Telegram", "Сначала подключите Telegram кнопкой «Подключить».")
            return
        self.run_task("Telegram", lambda: notifier.send("🧪 Проверка: уведомления ClassBot работают"),
                      lambda ok: messagebox.showinfo("Telegram", "Сообщение отправлено.") if ok else
                      messagebox.showerror("Telegram", "Не получилось отправить — проверьте токен и Chat ID."))

    def toggle_autostart(self):
        try:
            autostart.set_enabled(self.autostart_var.get())
        except OSError as ex:
            self.autostart_var.set(autostart.is_enabled())
            messagebox.showerror("Автозапуск", f"Не получилось: {ex}")

    def open_logs(self):
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(self.paths.logs)  # noqa: S606
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(self.paths.logs)])

    # --- цикл окна -----------------------------------------------------------------

    def _poll(self):
        try:
            while True:
                fn, args = self.ui_queue.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        lines = []
        try:
            while True:
                lines.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", "\n".join(lines) + "\n")
            extra = int(self.log_text.index("end-1c").split(".")[0]) - 1500
            if extra > 0:
                self.log_text.delete("1.0", f"{extra}.0")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        if not self.closing:
            self.root.after(150, self._poll)

    def _tick(self):
        self.refresh_upcoming()
        self.root.after(60_000, self._tick)

    def on_close(self):
        if self.bot_running() and not messagebox.askyesno(
                "Закрыть ClassBot?", "Бот сейчас работает. Если закрыть окно, он перестанет заходить на пары.\n\n"
                                     "Закрыть?"):
            return
        try:
            save_config(self.collect(), self.paths.config)
        except (ConfigError, OSError):
            pass
        if self.bot_running() or self.task_running():
            control.stop_event.set()
            self.status_var.set("Закрываю…")
            self._wait_and_destroy(time_left=15.0)
        else:
            self._destroy()

    def _wait_and_destroy(self, time_left: float):
        if (self.bot_running() or self.task_running()) and time_left > 0:
            self.root.after(200, self._wait_and_destroy, time_left - 0.2)
        else:
            self._destroy()

    def _destroy(self):
        self.closing = True
        logging.getLogger().removeHandler(self.log_handler)
        self.root.destroy()


def _apply_theme(root: tk.Tk) -> None:
    try:
        import sv_ttk
        sv_ttk.set_theme("light")
    except Exception:
        style = ttk.Style(root)
        for theme in ("vista", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
    try:
        icon = tk.PhotoImage(file=str(asset("icon.png")))
        root.iconphoto(True, icon)
        root._icon = icon  # не дать сборщику мусора удалить картинку
    except Exception:
        pass


def run_gui(paths: Paths, autostart: bool = False) -> int:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # чёткий текст на экранах с масштабом
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ClassBot")
        except Exception:
            pass
    root = tk.Tk()
    _apply_theme(root)
    App(root, paths, autostart_mode=autostart)
    root.mainloop()
    return 0
