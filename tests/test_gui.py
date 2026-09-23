"""Окно программы: запуск/остановка бота, редактирование расписания, сохранение настроек.

Нужны tkinter и экран (на сервере — xvfb-run), иначе тесты пропускаются.
"""
import datetime as dt
import os
import time
from types import SimpleNamespace

import pytest

tk = pytest.importorskip("tkinter")
if not os.environ.get("DISPLAY") and os.name != "nt":
    pytest.skip("нет экрана для окна (запустите через xvfb-run)", allow_module_level=True)

from classbot import control, gui  # noqa: E402
from classbot.config import load_config, parse_config, save_config  # noqa: E402
from classbot.paths import Paths  # noqa: E402


@pytest.fixture
def app(tmp_path, monkeypatch):
    shown = []
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gui.messagebox, name, lambda title, msg, _n=name, **kw: shown.append((_n, msg)))
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **kw: True)
    paths = Paths(tmp_path)
    save_config(parse_config({
        "settings": {"browser_path": os.__file__},
        "classes": [{"name": "Матан", "days": ["пн", "вт", "ср", "чт", "пт", "сб", "вс"],
                     "start": "23:58", "end": "23:59", "course": "https://classroom.google.com/c/abc"}],
    }), paths.config)
    root = tk.Tk()
    a = gui.App(root, paths)
    a.shown = shown
    yield a
    control.stop_event.set()
    try:
        root.destroy()
    except tk.TclError:
        pass
    control.stop_event.clear()


def pump(app, cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        app.root.update()
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_start_and_stop_bot(app):
    app.start_bot()
    assert pump(app, app.bot_running, 5)
    assert pump(app, lambda: "Жду пару" in app.status_var.get(), 5), app.status_var.get()
    assert app.state_label.cget("text") == "Бот работает"
    app.stop_bot()
    assert pump(app, lambda: not app.bot_running(), 10)
    pump(app, lambda: False, 0.3)
    assert app.state_label.cget("text") == "Бот выключен"
    assert not control.stop_event.is_set()


def test_class_dialog_and_save(app):
    dlg = gui.ClassDialog(app.root, None, {"Матан"})
    dlg.name.set("Английский")
    for i in (0, 2):
        dlg.days[i].set(True)
    dlg.start.set("13:00")
    dlg.end.set("14:30")
    dlg.source.set("link")
    dlg.link.set("https://us05web.zoom.us/j/81234567890?pwd=abc")
    dlg.passcode.set("777")
    dlg._ok()
    entry = dlg.result
    assert entry and entry.days == frozenset({0, 2}) and entry.start == dt.time(13, 0)
    app.cfg.classes.append(entry)
    app.name_var.set("Иван Иванов")
    app.num_vars["join_before_min"].set("2")
    app.keywords_text.insert("1.0", "Иванов\n")
    app._schedule_changed(entry)
    saved = load_config(app.paths.config)
    assert [c.name for c in saved.classes] == ["Матан", "Английский"]
    assert saved.classes[1].passcode == "777" and saved.settings.display_name == "Иван Иванов"
    assert saved.settings.join_before_min == 2 and "Иванов" in saved.telegram.mention_keywords
    assert len(app.tree.get_children()) == 2


def test_invalid_input_is_reported_not_saved(app):
    dlg = gui.ClassDialog(app.root, None, set())
    dlg.name.set("Х")
    dlg.days[0].set(True)
    dlg.start.set("25:99")
    dlg.course.set("https://classroom.google.com/c/abc")
    dlg._ok()
    assert dlg.result is None and app.shown and "время" in app.shown[-1][1]
    dlg.destroy()
    app.num_vars["admit_wait_min"].set("много")
    assert app.save() is None and "нужно число" in app.shown[-1][1]


def test_clipboard_action_for_russian_layout():
    # Windows: код клавиши V/C/X/A не зависит от раскладки
    assert gui.clipboard_action(86, "Cyrillic_em", windows=True) == "<<Paste>>"
    assert gui.clipboard_action(67, "Cyrillic_es", windows=True) == "<<Copy>>"
    assert gui.clipboard_action(88, "Cyrillic_che", windows=True) == "<<Cut>>"
    assert gui.clipboard_action(65, "Cyrillic_ef", windows=True) == "<<SelectAll>>"
    # латинская раскладка — стандартные привязки Tk, ничего не делаем
    assert gui.clipboard_action(86, "v", windows=True) is None
    assert gui.clipboard_action(55, "Cyrillic_em", windows=False) == "<<Paste>>"
    assert gui.clipboard_action(10, "Cyrillic_a", windows=True) is None


def test_paste_with_russian_layout_menu_and_button(app):
    dlg = gui.ClassDialog(app.root, None, set())
    app.root.clipboard_clear()
    app.root.clipboard_append("https://meet.google.com/abc-defg-hij")
    dlg.source.set("link")
    dlg._toggle()
    # Ctrl+V при русской раскладке
    dlg.link_entry.focus_set()
    fake = SimpleNamespace(widget=dlg.link_entry, keycode=86 if os.name == "nt" else 55, keysym="Cyrillic_em")
    assert gui._on_control_key(fake) == "break"
    app.root.update()
    assert dlg.link.get() == "https://meet.google.com/abc-defg-hij"
    # кнопка «Вставить»
    dlg.link.set("")
    app.root.clipboard_clear()
    app.root.clipboard_append("  https://zoom.us/j/81234567890?pwd=x \n")
    dlg.link_paste.invoke()
    assert dlg.link.get() == "https://zoom.us/j/81234567890?pwd=x"
    dlg.destroy()


def test_course_picked_from_list(app):
    courses = [{"name": "Матанализ 2 курс", "url": "https://classroom.google.com/c/MTIz"},
               {"name": "Английский", "url": "https://classroom.google.com/c/NDU2"}]
    dlg = gui.ClassDialog(app.root, None, set(), courses)
    assert list(dlg.course_box.cget("values")) == ["Матанализ 2 курс", "Английский"]
    dlg.course.set("Английский")
    dlg._course_picked()
    assert dlg.name.get() == "Английский"
    dlg.days[2].set(True)
    dlg._ok()
    assert dlg.result.course == "https://classroom.google.com/c/NDU2"
    # при изменении пары курс показывается названием, а не ссылкой
    dlg2 = gui.ClassDialog(app.root, dlg.result, set(), courses)
    assert dlg2.course.get() == "Английский"
    dlg2.destroy()
    # без списка курсов можно вставить ссылку, но не что попало
    dlg3 = gui.ClassDialog(app.root, None, set(), [])
    dlg3.name.set("X")
    dlg3.days[0].set(True)
    dlg3.course.set("просто текст")
    dlg3._ok()
    assert dlg3.result is None and "Не понимаю, какой это курс" in app.shown[-1][1]
    dlg3.course.set("https://classroom.google.com/u/1/c/Nzg5/t/all")
    dlg3._ok()
    assert dlg3.result.course == "https://classroom.google.com/u/1/c/Nzg5/t/all"


def test_schedule_shows_course_names(app):
    app.courses = [{"name": "Матанализ 2 курс", "url": "https://classroom.google.com/c/abc"}]
    app.refresh_schedule()
    row = app.tree.item(app.tree.get_children()[0])["values"]
    assert "Матанализ 2 курс" in row[3]
