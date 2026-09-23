import datetime as dt

import pytest

from classbot.config import (ConfigError, Settings, config_to_dict, load_config, next_occurrence, parse_config,
                             parse_date, save_config, upcoming, week_parity)
from classbot.links import (count_links, links_from_text, normalize_course_url, normalize_link,
                            normalize_meet_link, pick_new,
                            platform_of, with_lang, zoom_app_url, zoom_web_url)
from classbot.mentions import MentionWatcher


def cfg(**overrides):
    data = {
        "classes": [
            {"name": "Матан", "days": ["пн", "Чт"], "start": "09:00", "end": "10:30",
             "course": "https://classroom.google.com/c/abc"},
            {"name": "Англ", "days": "ср", "start": 780, "end": "14:30",  # 780 = 13:00, как YAML читает 13:00
             "meet": "https://meet.google.com/abc-defg-hij"},
        ],
    }
    data.update(overrides)
    return parse_config(data)


def test_parse_classes():
    c = cfg()
    matan, eng = c.classes
    assert matan.days == frozenset({0, 3})
    assert matan.start == dt.time(9, 0) and matan.end == dt.time(10, 30)
    assert eng.start == dt.time(13, 0)
    assert eng.days == frozenset({2})


@pytest.mark.parametrize("bad, message", [
    ({"days": "пн", "start": "10:00", "end": "09:00", "course": "https://classroom.google.com/c/x"}, "позже"),
    ({"days": "понед", "start": "09:00", "end": "10:00", "course": "https://classroom.google.com/c/x"}, "день"),
    ({"days": "пн", "start": "9:75", "end": "10:00", "course": "https://classroom.google.com/c/x"}, "время"),
    ({"days": "пн", "start": "09:00", "end": "10:00"}, "ссылку на курс"),
    ({"days": "пн", "start": "09:00", "end": "10:00", "link": "https://example.com/x"}, "не понимаю ссылку"),
    ({"days": "пн", "start": "09:00", "end": "10:00", "course": "https://example.com"}, "classroom"),
    ({"days": "пн", "start": "09:00", "end": "10:00", "meet": "https://meet.google.com/x", "week": "odd"},
     "semester_start"),
])
def test_config_errors(bad, message):
    with pytest.raises(ConfigError, match=message):
        parse_config({"classes": [dict(name="X", **bad)]})


def test_yaml_sexagesimal_time():
    import yaml
    data = yaml.safe_load("classes:\n- {name: A, days: пн, start: 9:00, end: 10:30, "
                          "course: 'https://classroom.google.com/c/x'}")
    entry = parse_config(data).classes[0]
    assert (entry.start, entry.end) == (dt.time(9, 0), dt.time(10, 30))


def test_next_occurrence_order_and_skip():
    c = cfg()
    tuesday = dt.datetime(2026, 9, 22, 12, 0)  # вторник
    occ = next_occurrence(c.classes, tuesday, c.settings)
    assert occ.entry.name == "Англ" and occ.start == dt.datetime(2026, 9, 23, 13, 0)
    nxt = next_occurrence(c.classes, tuesday, c.settings, {occ.key})
    assert nxt.entry.name == "Матан" and nxt.start == dt.datetime(2026, 9, 24, 9, 0)


def test_class_in_progress_is_returned_but_not_when_almost_over():
    c = cfg()
    thursday = dt.datetime(2026, 9, 24, 9, 45)
    assert next_occurrence(c.classes, thursday, c.settings).start == dt.datetime(2026, 9, 24, 9, 0)
    almost_over = dt.datetime(2026, 9, 24, 10, 29)
    assert next_occurrence(c.classes, almost_over, c.settings).start == dt.datetime(2026, 9, 28, 9, 0)


def test_week_parity():
    start = dt.date(2026, 9, 1)  # вторник
    assert week_parity(dt.date(2026, 8, 31), start) == "odd"
    assert week_parity(dt.date(2026, 9, 6), start) == "odd"
    assert week_parity(dt.date(2026, 9, 7), start) == "even"
    c = parse_config({
        "settings": {"semester_start": dt.date(2026, 9, 1)},
        "classes": [{"name": "Физ", "days": "пт", "start": "10:40", "end": "12:10",
                     "meet": "https://meet.google.com/abc-defg-hij", "week": "чётная"}],
    })
    occs = upcoming(c.classes, dt.datetime(2026, 9, 1, 8, 0), c.settings)
    assert [o.start.date() for o in occs] == [dt.date(2026, 9, 11)]


def test_unknown_setting():
    with pytest.raises(ConfigError, match="неизвестный"):
        parse_config({"settings": {"join_before": 1}, "classes": []})
    assert isinstance(parse_config({"classes": []}).settings, Settings)


@pytest.mark.parametrize("url, expected", [
    ("https://meet.google.com/abc-defg-hij", "https://meet.google.com/abc-defg-hij"),
    ("https://meet.google.com/ABC-DEFG-HIJ?authuser=1&hs=179", "https://meet.google.com/abc-defg-hij"),
    ("meet.google.com/abcdefghij", "https://meet.google.com/abc-defg-hij"),
    ("https://www.google.com/url?q=https://meet.google.com/abc-defg-hij&sa=D", "https://meet.google.com/abc-defg-hij"),
    ("https://meet.google.com/lookup/fqs6xyz?authuser=0", "https://meet.google.com/lookup/fqs6xyz"),
    ("https://meet.google.com/", None),
    ("https://meet.google.com/new", None),
    ("https://classroom.google.com/c/abc", None),
    ("", None),
])
def test_normalize(url, expected):
    assert normalize_meet_link(url) == expected


def test_new_link_detection():
    a, b = "https://meet.google.com/aaa-aaaa-aaa", "https://meet.google.com/bbb-bbbb-bbb"
    order, counts = count_links([a, a + "?authuser=0", "https://example.com"])
    baseline = counts
    assert order == [a] and counts == {a: 2}
    # Выложили новую ссылку — сверху ленты.
    order, counts = count_links([b, a, a])
    assert pick_new(order, counts, baseline) == [b]
    # Преподаватель заново выложил ту же самую ссылку.
    order, counts = count_links([a, a, a])
    assert pick_new(order, counts, baseline) == [a]
    # Ничего не изменилось.
    assert pick_new(*count_links([a, a]), baseline) == []
    assert pick_new(*count_links([a]), None) == [a]


def test_with_lang():
    assert with_lang("https://meet.google.com/abc-defg-hij") == "https://meet.google.com/abc-defg-hij?hl=en"
    assert with_lang("https://meet.google.com/abc-defg-hij?authuser=1&hl=ru") == \
        "https://meet.google.com/abc-defg-hij?authuser=1&hl=en"
    assert with_lang("http://localhost:8000/meet.html") == "http://localhost:8000/meet.html"


def test_mention_watcher_ignores_existing_name_and_catches_new():
    w = MentionWatcher(["Иванов", "перекличк"])
    base = "Иван Иванов\nПётр Петров\nYou"
    assert w.feed([], base) == []
    # Интерфейс перестроился, строка с вашим именем та же — не уведомляем.
    assert w.feed([], "Пётр Петров\nИван Иванов\nYou") == []
    # Преподаватель пишет в чат (без data-message-id).
    hits = w.feed([], base + "\nПреподаватель\nИВАНОВ, вы тут?")
    assert len(hits) == 1 and "ИВАНОВ, вы тут?" in hits[0]
    # Сообщение с id: уведомляем один раз, без дублирования по тексту страницы.
    msgs = [["m1", "Начинаем перекличку"]]
    hits = w.feed(msgs, base + "\nНачинаем перекличку")
    assert hits == ["Начинаем перекличку"]
    assert w.feed(msgs, base + "\nНачинаем перекличку") == []
    # Сообщения без ключевых слов молча пропускаем.
    assert w.feed(msgs + [["m2", "Всем привет"]], base + "\nВсем привет") == []


def test_mention_watcher_captions_growing_line_notifies_once():
    w = MentionWatcher(["иванов"])
    w.feed([], "Иван Иванов")
    assert len(w.feed([], "Иван Иванов\nтак Иванов")) == 1
    assert w.feed([], "Иван Иванов\nтак Иванов здесь") == []
    assert w.feed([], "Иван Иванов\nтак Иванов здесь или нет") == []


def test_mention_watcher_without_keywords():
    assert MentionWatcher([]).feed([["m", "x"]], "anything") == []


def test_mention_watcher_warmup_absorbs_late_ui():
    w = MentionWatcher(["иванов"])
    w.feed([], "You")
    # Подпись с вашим именем прогрузилась во время разогрева — это не упоминание.
    assert w.feed([], "You\nИван Иванов", absorb=True) == []
    assert w.feed([], "You\nИван Иванов") == []
    assert len(w.feed([], "You\nИван Иванов\nИванов, ответьте")) == 1


def test_stop_after_drop_regex():
    from classbot.meet import STOP_AFTER_DROP_RE
    assert STOP_AFTER_DROP_RE.search("The host ended the call for everyone")
    assert STOP_AFTER_DROP_RE.search("You've been removed from the meeting")
    assert not STOP_AFTER_DROP_RE.search("You lost your network connection. Recommended: check wifi. Rejoin")


@pytest.mark.parametrize("url, expected", [
    ("https://us05web.zoom.us/j/81234567890?pwd=AbC.1", "https://zoom.us/j/81234567890?pwd=AbC.1"),
    ("https://zoom.us/j/81234567890", "https://zoom.us/j/81234567890"),
    ("https://university.zoom.us/w/81234567890?tk=x&pwd=p", "https://zoom.us/j/81234567890?pwd=p"),
    ("https://app.zoom.us/wc/81234567890/join?pwd=p", "https://zoom.us/j/81234567890?pwd=p"),
    ("https://zoom.us/my/Ivanova.Teacher", "https://zoom.us/my/ivanova.teacher"),
    ("https://www.google.com/url?q=https://us02web.zoom.us/j/81234567890%3Fpwd%3Dxyz&sa=D",
     "https://zoom.us/j/81234567890?pwd=xyz"),
    ("https://zoom.us/signin", None),
    ("https://zoom.us.evil.com/j/81234567890", None),
])
def test_normalize_zoom(url, expected):
    assert normalize_link(url) == expected


def test_zoom_urls_and_platform():
    link = "https://zoom.us/j/81234567890?pwd=AbC"
    assert platform_of(link) == "zoom" and platform_of("https://meet.google.com/abc-defg-hij") == "meet"
    assert zoom_web_url(link) == "https://zoom.us/wc/join/81234567890?pwd=AbC"
    assert zoom_app_url(link, "Иван Иванов") == \
        "zoommtg://zoom.us/join?action=join&confno=81234567890&pwd=AbC&uname=%D0%98%D0%B2%D0%B0%D0%BD+%D0%98%D0%B2%D0%B0%D0%BD%D0%BE%D0%B2"
    assert zoom_app_url("https://zoom.us/my/teacher") is None


def test_links_from_text_finds_zoom_and_meet():
    text = "Пара тут: https://us06web.zoom.us/j/81234567890?pwd=abc. И запасная meet.google.com/abc-defg-hij"
    found = [normalize_link(u) for u in links_from_text(text)]
    assert found == ["https://zoom.us/j/81234567890?pwd=abc", "https://meet.google.com/abc-defg-hij"]


def test_parse_date_formats():
    assert parse_date("01.09.2026", "x") == dt.date(2026, 9, 1)
    assert parse_date("2026-09-01", "x") == dt.date(2026, 9, 1)
    with pytest.raises(ConfigError):
        parse_date("31.02.2026", "x")


def test_config_save_roundtrip(tmp_path):
    c = parse_config({
        "telegram": {"bot_token": "1:A", "chat_id": "42", "mention_keywords": ["Иванов"]},
        "settings": {"display_name": "Иван", "zoom_mode": "app", "semester_start": "01.09.2026",
                     "join_before_min": 2},
        "classes": [
            {"name": "Матан", "days": ["пн", "чт"], "start": "10:30", "end": "12:00",
             "course": "https://classroom.google.com/c/abc"},
            {"name": "Физра", "days": "пт", "start": "09:00", "end": "10:30",
             "link": "https://zoom.us/j/81234567890?pwd=x", "passcode": "123", "week": "чётная"},
        ],
    })
    path = tmp_path / "config.yaml"
    save_config(c, path)
    again = load_config(path)
    assert config_to_dict(again) == config_to_dict(c)
    assert again.classes[1].week == "even" and again.settings.semester_start == dt.date(2026, 9, 1)
    assert again.settings.zoom_mode == "app" and again.classes[0].start == dt.time(10, 30)


def test_bad_settings_values():
    with pytest.raises(ConfigError, match="zoom_mode"):
        parse_config({"settings": {"zoom_mode": "desktop"}, "classes": []})
    with pytest.raises(ConfigError, match="число"):
        parse_config({"settings": {"join_before_min": "две"}, "classes": []})


@pytest.mark.parametrize("url, expected", [
    ("https://classroom.google.com/c/NjQ3ODk1", "https://classroom.google.com/c/NjQ3ODk1"),
    ("https://classroom.google.com/w/ODc2MzY0NzU3Nzlz/t/all", "https://classroom.google.com/c/ODc2MzY0NzU3Nzlz"),
    ("classroom.google.com/u/1/c/NjQ3/a/Nzg5/details", "https://classroom.google.com/u/1/c/NjQ3"),
    ("https://classroom.google.com/r/NjQ3/sort-last-name", "https://classroom.google.com/c/NjQ3"),
    ("https://classroom.google.com/h", None),
    ("https://example.com/c/NjQ3", None),
])
def test_normalize_course_url(url, expected):
    assert normalize_course_url(url) == expected


def test_config_turns_any_course_link_into_stream():
    c = parse_config({"classes": [{"name": "X", "days": "пн", "start": "09:00", "end": "10:00",
                                   "course": "https://classroom.google.com/w/ODc2MzY0NzU3Nzlz/t/all"}]})
    assert c.classes[0].course == "https://classroom.google.com/c/ODc2MzY0NzU3Nzlz"


# --- автоматический режим ---

from classbot.autofind import AutoCalls, parse_when, post_passcode, post_title  # noqa: E402
from classbot.state import State  # noqa: E402

ZOOM_POST = """Олександр Чорний
Учора
Олександр Чорний запрошує на заплановану конференцію Zoom студентів, які відсутні на очних заняттях

Тема: Конференції - Лекція №09 з Відеоінформаційних технологій
Час: 23 вересня 2026 9:00 AM Київ
Приєднатися до конференції Zoom
https://us04web.zoom.us/j/3637539970?pwd=3Sms84pcRJCbIPGjOEo4oblgpXi76d.1&omn=76509568269

Посилання на чат конференції
https://us04web.zoom.us/launch/jc/76509568269

Ідентифікатор конференції: 363 753 9970
Код доступу: 111
Додати коментар"""


@pytest.mark.parametrize("text, expected", [
    (ZOOM_POST, (dt.datetime(2026, 9, 23, 9, 0), None)),
    ("Topic: Math\nTime: Sep 24, 2026 01:30 PM Kyiv", (dt.datetime(2026, 9, 24, 13, 30), None)),
    ("Тема: Физика\nВремя: 24 сент. 2026 09:00 AM Москва", (dt.datetime(2026, 9, 24, 9, 0), None)),
    ("Пара 24.09 о 10:40, заходьте", (dt.datetime(2026, 9, 24, 10, 40), None)),
    ("Завтра в 9:00 лекция", (dt.datetime(2026, 9, 24, 9, 0), None)),
    ("Сьогодні о 12:20 практична", (dt.datetime(2026, 9, 23, 12, 20), None)),
    ("Пара о 10:40\nhttps://meet.google.com/abc-defg-hij", (None, dt.time(10, 40))),
    ("Заходьте на пару\nhttps://meet.google.com/abc-defg-hij", (None, None)),
    ("Юрій Бурліков публікує нове завдання: \"Лабораторна робота 1\"\n09:12", (None, None)),
    ("Олександр Чорний публікує новий матеріал: \"Лекція №09 BIT-123 Тема 2.3. Робот...\"\n08:02", (None, None)),
])
def test_parse_when(text, expected):
    assert parse_when(text, dt.date(2026, 9, 23)) == expected


def test_parse_when_next_year_and_titles():
    assert parse_when("12 січня о 9:00", dt.date(2026, 12, 20)) == (dt.datetime(2027, 1, 12, 9, 0), None)
    assert parse_when("5 березня о 9:00", dt.date(2026, 9, 23)) == (dt.datetime(2026, 3, 5, 9, 0), None)
    assert post_title(ZOOM_POST, "z") == "Конференції - Лекція №09 з Відеоінформаційних технологій"
    assert post_passcode(ZOOM_POST) == "111"
    assert post_title("Автор допису: Олександр Чорний\nОлександр Чорний\nСтворено 09:12\n09:12\nmore_vert\n"
                      "Заходьте на пару\nhttps://meet.google.com/x", "x") == "Заходьте на пару (Олександр Чорний)"


def test_auto_calls_flow(tmp_path):
    auto = AutoCalls(State(tmp_path / "state.json"))
    course = "https://classroom.google.com/c/abc"
    hours = (dt.time(7, 0), dt.time(21, 0))
    dur = dt.timedelta(minutes=80)
    header = ("https://meet.google.com/aaa-bbbb-ccc", "Meet\nПриєднатися")
    invite = ("https://us04web.zoom.us/j/3637539970?pwd=X&omn=1", ZOOM_POST.replace("23 вересня", "24 вересня"))
    old_link = ("https://meet.google.com/old-link-xyz", "Вчорашня пара\nhttps://meet.google.com/old-link-xyz")

    # Первый осмотр: приглашение на завтра найдено, старые ссылки без времени — нет.
    t0 = dt.datetime(2026, 9, 23, 20, 0)
    new = auto.update(course, [header, invite, old_link], t0, dur, hours)
    assert [(f.link, f.start, f.kind, f.passcode) for f in new] == [
        ("https://zoom.us/j/3637539970?pwd=X", dt.datetime(2026, 9, 24, 9, 0), "exact", "111")]
    assert auto.update(course, [header, invite, old_link], t0, dur, hours) == []  # второй раз не сообщаем

    # Утром появилась новая ссылка без времени — зайти сразу.
    t1 = dt.datetime(2026, 9, 24, 10, 38)
    fresh = ("https://meet.google.com/new-link-abc", "Антон Правда\n10:37\nЗаходьте")
    new = auto.update(course, [fresh, header, invite, old_link], t1, dur, hours)
    assert [(f.link, f.start, f.kind) for f in new] == [("https://meet.google.com/new-link-abc", t1, "now")]
    # Ещё одна — со временем без даты.
    later = ("https://meet.google.com/lat-erli-nkx", "Практика о 12:20")
    new = auto.update(course, [later, fresh, header, invite, old_link], t1, dur, hours)
    assert [(f.start, f.kind) for f in new] == [(dt.datetime(2026, 9, 24, 12, 20), "time")]
    # Ночью ссылка без времени — не заходим.
    night = ("https://meet.google.com/nig-htli-nkx", "Заходьте")
    assert auto.update(course, [night, later, fresh, header, invite, old_link],
                       dt.datetime(2026, 9, 24, 23, 30), dur, hours) == []

    occ = auto.occurrences(dt.datetime(2026, 9, 24, 8, 0), dur)
    assert [(o.start, o.entry.link, o.entry.passcode) for o in occ][:1] == [
        (dt.datetime(2026, 9, 24, 9, 0), "https://zoom.us/j/3637539970?pwd=X", "111")]
    # Всё сохранилось в state.json и переживает перезапуск.
    again = AutoCalls(State(tmp_path / "state.json"))
    assert len(again.occurrences(dt.datetime(2026, 9, 24, 8, 0), dur)) == len(occ)


def test_friendly_error_and_profile_lock(tmp_path):
    import os
    import sys
    from classbot.browser import friendly_error, profile_in_use
    msg = friendly_error(Exception("BrowserType.launch_persistent_context: Opening in existing browser session.\nCall log: ..."))
    assert msg.startswith("Браузер бота уже открыт")
    assert friendly_error(Exception("Page.goto: net::ERR_INTERNET_DISCONNECTED\nCall log:\n - navigating")) == \
        "Page.goto: net::ERR_INTERNET_DISCONNECTED"
    if sys.platform == "win32":
        (tmp_path / "lockfile").write_text("")
        assert profile_in_use(tmp_path)
    else:
        assert not profile_in_use(tmp_path)
        os.symlink(f"host-{os.getpid()}", tmp_path / "SingletonLock")
        assert profile_in_use(tmp_path)
        (tmp_path / "SingletonLock").unlink()
        os.symlink("host-999999999", tmp_path / "SingletonLock")
        assert not profile_in_use(tmp_path)



def test_parse_post_prefers_time_next_to_link():
    from classbot.autofind import LINK_MARK, parse_post
    text = ("Юрій Бурліков\n09:12\nЛабораторна робота 1. Термін здачі: 25.09 о 23:59\n"
            "Олександр Чорний\nУчора\nТема: Лекція №09\nЧас: 24 вересня 2026 9:00 AM Київ\n"
            "Приєднатися до конференції Zoom\n" + LINK_MARK + "https://us04web.zoom.us/j/3637539970?pwd=X\n"
            "Код доступу: 111\nДодати коментар")
    info = parse_post(text, "https://zoom.us/j/3637539970?pwd=X", dt.date(2026, 9, 23))
    assert info.when == dt.datetime(2026, 9, 24, 9, 0) and info.title == "Лекція №09" and info.passcode == "111"
    # Ссылка в той же строке, что и время.
    info = parse_post("Антон Правда\n10:37\nПара о 10:40 " + LINK_MARK + "meet", "m", dt.date(2026, 9, 23))
    assert info.when is None and info.time_only == dt.time(10, 40)
    # Без времени рядом со ссылкой — время из далёкого соседнего поста не берём.
    far = "Контрольна 26.09 о 12:00\n" + "\n".join(f"рядок {i}" for i in range(20)) + "\n" + LINK_MARK + "meet"
    assert parse_post(far, "m", dt.date(2026, 9, 23)).when is None


@pytest.mark.parametrize("text, expected", [
    ("Пʼятниця, 18 вересня · 10:40дп – 12:00пп", (dt.datetime(2026, 9, 18, 10, 40), None, dt.time(12, 0))),
    ("пятница, 18 сентября · 10:40–12:00", (dt.datetime(2026, 9, 18, 10, 40), None, dt.time(12, 0))),
    ("Friday, September 18⋅10:40 – 11:30am", (dt.datetime(2026, 9, 18, 10, 40), None, dt.time(11, 30))),
    ("Friday, September 18⋅11:00 – 1:00pm", (dt.datetime(2026, 9, 18, 11, 0), None, dt.time(13, 0))),
    ("Час: 23 вересня 2026 9:00 AM Київ", (dt.datetime(2026, 9, 23, 9, 0), None, None)),
    ("Пара о 10:40–12:00", (None, dt.time(10, 40), dt.time(12, 0))),
])
def test_calendar_invite_times(text, expected):
    from classbot.autofind import parse_when_range
    assert parse_when_range(text, dt.date(2026, 9, 23)) == expected
