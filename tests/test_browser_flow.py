"""Прогон бота в настоящем Chromium на имитации страниц Meet и Classroom.

Это не проверяет настоящий интерфейс Google (он доступен только с аккаунтом), но
проверяет всю цепочку: поиск ссылки → вход → чат → выход.
"""
import datetime as dt
import os
import time

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from classbot import errors, meet, mentions, runner, zoom  # noqa: E402
from classbot.classroom import fetch_meet_links  # noqa: E402
from classbot.links import normalize_link  # noqa: E402
from classbot.config import ClassEntry, Occurrence, Settings, TelegramSettings, Config  # noqa: E402

FAKE_MEET = """<!doctype html><html><body>
<div id="app"></div>
<script>
const q = new URLSearchParams(location.search);
const app = document.getElementById('app');
let mic = true, cam = true;
function btn(label, text, onclick, extra) {
  const b = document.createElement('button');
  b.setAttribute('aria-label', label); b.textContent = text; b.onclick = onclick;
  if (extra) for (const k in extra) b.setAttribute(k, extra[k]);
  app.appendChild(b); return b;
}
function prejoin() {
  app.innerHTML = '<div>Ready to join? hl=' + q.get('hl') + '</div>';
  if (q.get('fatal')) { app.innerHTML = "<h1>You can't join this video call</h1>"; return; }
  if (!window.dialogShown) {
    window.dialogShown = true;
    const d = document.createElement('div'); d.setAttribute('role', 'dialog');
    d.innerHTML = '<p>Do you want people to see and hear you?</p>';
    const c = document.createElement('button'); c.textContent = 'Continue without microphone and camera';
    c.onclick = () => d.remove(); d.appendChild(c); document.body.appendChild(d);
  }
  btn(mic ? 'Turn off microphone (ctrl + d)' : 'Turn on microphone', 'mic', () => { mic = !mic; prejoin(); });
  btn(cam ? 'Turn off camera (ctrl + e)' : 'Turn on camera', 'cam', () => { cam = !cam; prejoin(); });
  const join = document.createElement('button');
  join.textContent = q.get('ask') ? 'Ask to join' : 'Join now';
  join.onclick = () => {
    window.joinedWith = {mic, cam, dialogOpen: !!document.querySelector('[role=dialog]')};
    if (q.get('ask')) { app.innerHTML = 'Asking to be let in...'; setTimeout(incall, 2500); } else incall();
  };
  app.appendChild(join);
}
function incall() {
  app.innerHTML = '<div class="tile">Иван Иванов</div>';
  btn('Turn on captions (c)', 'cc', () => { window.captions = true; incall(); });
  btn('Chat with everyone', 'chat', () => {
    const p = document.createElement('div'); p.id = 'chat';
    p.innerHTML = '<textarea aria-label="Send a message"></textarea>'; app.appendChild(p);
  }, {'aria-pressed': 'false'});
  btn('Leave call', 'leave', () => { app.innerHTML = 'You left the meeting <button>Rejoin</button>'; });
  if (!window.msgTimer) window.msgTimer = setTimeout(() => {
    const m = document.createElement('div'); m.setAttribute('data-message-id', 'x1');
    m.textContent = 'Иванов, вы здесь?'; document.body.appendChild(m);
  }, 4000);
}
setTimeout(prejoin, 500);
</script></body></html>"""

A = "https://meet.google.com/aaa-aaaa-aaa"
B = "https://meet.google.com/bbb-bbbb-bbb"


def classroom_html(links):
    body = "".join(f'<div class="post"><a href="{link}">{link}</a></div>' for link in links)
    return f"<html><body><h1>Stream</h1><a href='https://www.google.com/url?q={links[0]}&sa=D'>x</a>{body}</body></html>" \
        if links else "<html><body>empty</body></html>"


ZOOM_OUTER = """<!doctype html><html><body><h3>Zoom web client</h3>
<iframe id="webclient" style="width:900px;height:700px" src="/wc/frame/inner"></iframe>
<script>document.getElementById('webclient').src = '/wc/frame/inner' + location.search;</script>
</body></html>"""

FAKE_ZOOM_INNER = """<!doctype html><html><body>
<div id="app"></div>
<script>
const q = new URLSearchParams('FLAGS');
const app = document.getElementById('app');
let mic = true, cam = true;
function el(tag, attrs, text, onclick) {
  const e = document.createElement(tag);
  for (const k in (attrs || {})) e.setAttribute(k, attrs[k]);
  if (text) e.textContent = text; if (onclick) e.onclick = onclick;
  app.appendChild(e); return e;
}
function preview() {
  const nameVal = document.getElementById('input-for-name') ? document.getElementById('input-for-name').value : '';
  app.innerHTML = '';
  if (q.get('captcha')) { el('p', {}, 'Please verify you are human (reCAPTCHA)'); return; }
  if (!window.cookiesOk) el('button', {}, 'Accept Cookies', () => { window.cookiesOk = true; preview(); });
  const name = el('input', {id: 'input-for-name', placeholder: 'Your Name'}); name.value = nameVal;
  if (q.get('needpwd')) el('input', {id: 'input-for-pwd', type: 'password', placeholder: 'Meeting Passcode'});
  el('button', {'aria-label': mic ? 'Mute' : 'Unmute'}, 'mic', () => { mic = !mic; preview(); });
  el('button', {'aria-label': cam ? 'Stop Video' : 'Start Video'}, 'cam', () => { cam = !cam; preview(); });
  el('button', {}, 'Join', () => {
    const pwd = document.getElementById('input-for-pwd');
    window.joinedWith = {name: document.getElementById('input-for-name').value, mic, cam,
                         cookies: !!window.cookiesOk, pwd: pwd ? pwd.value : null};
    if (q.get('wait')) {
      app.innerHTML = '';
      el('p', {}, 'Please wait, the meeting host will let you in soon.');
      el('button', {'aria-label': 'Leave'}, 'Leave');
      setTimeout(meeting, 2500);
    } else meeting();
  });
}
function meeting() {
  app.innerHTML = '<div class="tile">Иван Иванов</div>';
  const d = el('div', {role: 'dialog'}); d.innerHTML = '<p>Join Audio by Computer</p>';
  const c = document.createElement('button'); c.setAttribute('aria-label', 'close'); c.textContent = 'x';
  c.onclick = () => { d.remove(); window.audioClosed = true; }; d.appendChild(c);
  const chat = el('button', {'aria-label': 'open the chat pane'}, 'Chat', () => {
    chat.setAttribute('aria-label', 'close the chat pane'); el('div', {class: 'chat-container'}, '');
  });
  el('button', {'aria-label': 'Leave', class: 'footer__leave-btn'}, 'Leave', () => {
    el('button', {}, 'Leave Meeting', () => { app.innerHTML = 'You have left the meeting'; });
  });
  setTimeout(() => el('div', {class: 'chat-message'}, 'Иванов, вы тут?'), 4000);
}
setTimeout(preview, 300);
</script></body></html>"""

ZOOM_NEW = "https://us05web.zoom.us/j/81234567890?pwd=secret"
# Режим поддельного Zoom для теста: wait=1 (зал ожидания), captcha=1, needpwd=1.
ZOOM_FLAGS = {"value": ""}


def zoom_route(route):
    inner = "/wc/frame/inner" in route.request.url
    body = FAKE_ZOOM_INNER.replace("FLAGS", ZOOM_FLAGS["value"]) if inner else ZOOM_OUTER
    route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)


def install_routes(ctx, classroom_links):
    ctx.route("https://meet.google.com/**", lambda route: route.fulfill(
        status=200, content_type="text/html; charset=utf-8", body=FAKE_MEET))
    ctx.route("https://zoom.us/**", zoom_route)
    ctx.route("https://classroom.google.com/**", lambda route: route.fulfill(
        status=200, content_type="text/html; charset=utf-8", body=classroom_html(classroom_links)))


@pytest.fixture
def pw():
    with sync_playwright() as p:
        yield p


@pytest.fixture
def page(pw):
    ZOOM_FLAGS["value"] = ""
    browser = pw.chromium.launch()
    ctx = browser.new_context()
    install_routes(ctx, [B, A])
    yield ctx.new_page()
    browser.close()


def test_join_mutes_and_dismisses_dialog(page):
    meet.join(page, A, timeout_s=20)
    assert meet.in_call(page)
    assert page.evaluate("window.joinedWith") == {"mic": False, "cam": False, "dialogOpen": False}
    assert "hl=en" in page.url
    meet.prepare_in_call(page, captions=True)
    assert page.evaluate("window.captions") is True
    assert meet.open_chat(page) == "clicked"
    assert meet.open_chat(page) == "open"
    meet.leave(page)
    assert not meet.in_call(page)


def test_ask_to_join_waits_for_admission(page):
    waited = []
    meet.join(page, A + "?ask=1", timeout_s=30, on_waiting=lambda: waited.append(1))
    assert waited == [1] and meet.in_call(page)


def test_fatal_error(page):
    with pytest.raises(errors.CallError, match="can't join"):
        meet.join(page, A + "?fatal=1", timeout_s=20)


def test_mentions_in_chat(page, monkeypatch):
    monkeypatch.setattr(mentions.MentionWatcher, "WARMUP_S", 0)
    meet.join(page, A, timeout_s=20)
    w = mentions.MentionWatcher(["иванов"])
    assert w.check(*meet.chat_snapshot(page)) == []
    time.sleep(4.5)
    assert w.check(*meet.chat_snapshot(page)) == ["Иванов, вы здесь?"]
    assert w.check(*meet.chat_snapshot(page)) == []


def test_fetch_links(page):
    order, counts = fetch_meet_links(page, "https://classroom.google.com/c/course1")
    assert order == [B, A] and counts == {B: 2, A: 1}


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)
        return True

    def send_photo(self, path, caption):
        return self.send(caption)


@pytest.mark.parametrize("new_link, platform_name", [(B, "Google Meet"), (ZOOM_NEW, "Zoom")])
def test_full_session(tmp_path, monkeypatch, new_link, platform_name):
    """Пара целиком: в ленте появилась новая ссылка → зашёл → упомянули в чате → вышел."""
    def fake_launch(pw, settings, profile_dir, executable=None, background=False):
        # Как настоящий launch(), но без окна и с поддельными страницами Google и Zoom.
        browser = pw.chromium.launch()
        ctx = browser.new_context()
        install_routes(ctx, [new_link, A])
        ctx.new_page()
        orig_close = ctx.close
        ctx.close = lambda: (orig_close(), browser.close())
        return ctx

    monkeypatch.setattr(runner, "launch", fake_launch)
    monkeypatch.setattr(mentions.MentionWatcher, "WARMUP_S", 1)
    course = "https://classroom.google.com/c/course1"
    now = dt.datetime.now()
    entry = ClassEntry(name="Тест", days=frozenset(range(7)), start=now.time(), end=now.time(),
                       course=course)
    occ = Occurrence(entry, now, now + dt.timedelta(seconds=25))
    settings = Settings(link_poll_sec=1, browser_path=os.__file__, watch_captions=True, display_name="Иван Иванов")
    cfg = Config(settings=settings, telegram=TelegramSettings(mention_keywords=["Иванов"]), classes=[entry])
    notifier = FakeNotifier()
    r = runner.Runner(cfg, runner.Paths(tmp_path), notifier)
    r.state.set_baseline(course, {A: 1})

    r.run_session(occ)

    text = "\n".join(notifier.messages)
    key = normalize_link(new_link)
    assert "Зашёл на пару «Тест»" in text and platform_name in text and key in text, text
    assert "вас упомянули" in text and "Иванов, вы" in text, text
    assert "закончилась, вышел из звонка" in text, text
    assert "Ошибка" not in text and "⚠️" not in text, text
    assert r.state.baseline(course) == {key: 2, A: 1}


ZOOM_LINK = "https://zoom.us/j/81234567890?pwd=secret"


def test_zoom_join_inside_iframe(page, monkeypatch):
    monkeypatch.setattr(mentions.MentionWatcher, "WARMUP_S", 0)
    zoom.join(page, ZOOM_LINK, timeout_s=30, name="Иван Иванов")
    assert "/wc/join/81234567890?pwd=secret" in page.url
    frame = page.frame_locator("#webclient")
    inner = next(f for f in page.frames if "inner" in f.url)
    assert inner.evaluate("window.joinedWith") == {
        "name": "Иван Иванов", "mic": False, "cam": False, "cookies": True, "pwd": None}
    assert zoom.in_call(page)
    assert inner.evaluate("window.audioClosed") is True
    assert zoom.open_chat(page) == "clicked" and zoom.open_chat(page) == "open"
    w = mentions.MentionWatcher(["иванов"])
    assert w.check(*zoom.chat_snapshot(page)) == []
    time.sleep(4.5)
    hits = w.check(*zoom.chat_snapshot(page))
    assert len(hits) == 1 and "Иванов, вы тут?" in hits[0]
    zoom.leave(page)
    assert not zoom.in_call(page)
    assert "You have left the meeting" in frame.locator("#app").inner_text()


def test_zoom_waiting_room_is_not_in_call(page):
    waited = []
    ZOOM_FLAGS["value"] = "wait=1"
    zoom.join(page, ZOOM_LINK, timeout_s=30, on_waiting=lambda: waited.append(1))
    assert waited == [1] and zoom.in_call(page)


def test_zoom_captcha_and_passcode(page):
    ZOOM_FLAGS["value"] = "captcha=1"
    with pytest.raises(errors.CallError, match="не робот"):
        zoom.join(page, ZOOM_LINK, timeout_s=20)
    ZOOM_FLAGS["value"] = "needpwd=1"
    with pytest.raises(errors.CallError, match="код доступа"):
        zoom.join(page, "https://zoom.us/j/81234567890", timeout_s=20)
    zoom.join(page, "https://zoom.us/j/81234567890", timeout_s=20, passcode="777")
    inner = next(f for f in page.frames if "inner" in f.url)
    assert inner.evaluate("window.joinedWith.pwd") == "777"


CLASSROOM_HOME = """<html><body>
<nav><a href="/u/0/c/MTIz">Матанализ 2 курс</a><a href="/c/MTIz/a/Nzg5/details">Задание 1</a></nav>
<ol>
  <li><div>Матанализ 2 курс</div><a href="https://classroom.google.com/c/MTIz" aria-label=""></a></li>
  <li><a href="https://classroom.google.com/c/NDU2"><div>Английский</div><div>Группа Б</div></a></li>
  <li><a href="https://classroom.google.com/c/Nzg5" aria-label="Английский"></a></li>
  <li><a href="https://classroom.google.com/h">Главная</a></li>
</ol></body></html>"""


def test_list_courses_from_classroom_home(page):
    from classbot.classroom import list_courses
    page.route("https://classroom.google.com/**", lambda route: route.fulfill(
        status=200, content_type="text/html; charset=utf-8", body=CLASSROOM_HOME))
    page.goto("https://classroom.google.com/")
    assert list_courses(page) == [
        {"name": "Матанализ 2 курс", "url": "https://classroom.google.com/c/MTIz"},
        {"name": "Английский", "url": "https://classroom.google.com/c/NDU2"},
        {"name": "Английский (2)", "url": "https://classroom.google.com/c/Nzg5"},
    ]


def test_check_lists_concrete_problems(tmp_path, monkeypatch):
    from classbot import tasks
    from classbot.notify import Notifier

    def fake_launch(pw, settings, profile_dir, executable=None, background=False):
        browser = pw.chromium.launch()
        ctx = browser.new_context()
        install_routes(ctx, [A])
        ctx.new_page()
        orig_close = ctx.close
        ctx.close = lambda: (orig_close(), browser.close())
        return ctx

    monkeypatch.setattr(tasks, "launch", fake_launch)
    cfg = Config(settings=Settings(browser_path=os.__file__), telegram=TelegramSettings(), classes=[])
    problems = tasks.check(cfg, runner.Paths(tmp_path), Notifier("", ""))
    assert any("ни одной пары" in p for p in problems), problems
    assert any("не отмечен курс" in p for p in problems), problems
    assert any("Telegram не подключён" in p for p in problems), problems
    assert not any("не вошёл" in p for p in problems), problems


UK_MONTHS = ["", "січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня",
             "жовтня", "листопада", "грудня"]


def stream_html(extra_posts="", day=None):
    day = day or (dt.date.today() + dt.timedelta(days=1))
    return f"""<html><body>
<div class="left"><div>Meet</div><a href="https://meet.google.com/xyz-abcd-efg">Приєднатися</a></div>
<div class="stream">
 {extra_posts}
 <div class="post"><div>Юрій Бурліков</div><div>09:12</div><div>Лабораторна робота 1. Термін здачі: 25.09 о 23:59</div></div>
 <div class="post"><div>Олександр Чорний</div><div>Учора</div>
   <div class="body"><b>Олександр Чорний запрошує на заплановану конференцію Zoom студентів</b><br>
   Тема: Конференції - Лекція №09 з Відеоінформаційних технологій<br>
   Час: {day.day} {UK_MONTHS[day.month]} {day.year} 9:00 AM Київ<br>Приєднатися до конференції Zoom<br>
   <a href="https://us04web.zoom.us/j/3637539970?pwd=3Sms84&amp;omn=765">https://us04web.zoom.us/j/3637539970?pwd=3Sms84&amp;omn=765</a><br><br>
   Посилання на чат конференції<br><a href="https://us04web.zoom.us/launch/jc/76509568269">чат</a><br>
   Ідентифікатор конференції: 363 753 9970<br>Код доступу: 111</div>
   <div>Додати коментар</div></div>
 <div class="post"><div>Антон Правда публікує новий матеріал: "Лекція №8"</div><div>Учора</div></div>
</div></body></html>"""


def test_fetch_posts_gives_each_link_its_own_post(page):
    from classbot.classroom import fetch_posts
    page.route("https://classroom.google.com/**", lambda route: route.fulfill(
        status=200, content_type="text/html; charset=utf-8", body=stream_html(
            '<div class="post"><div>Антон Правда</div><div>10:37</div>'
            '<div>Заходьте на пару <a href="https://meet.google.com/new-link-abc">meet</a></div></div>')))
    posts = dict((normalize_link(h), t) for h, t in fetch_posts(page, "https://classroom.google.com/c/abc")
                 if normalize_link(h))
    assert set(posts) == {"https://meet.google.com/xyz-abcd-efg", "https://zoom.us/j/3637539970?pwd=3Sms84",
                          "https://meet.google.com/new-link-abc"}
    assert "Час:" in posts["https://zoom.us/j/3637539970?pwd=3Sms84"]
    from classbot.autofind import parse_post
    info = parse_post(posts["https://zoom.us/j/3637539970?pwd=3Sms84"], "z", dt.date.today())
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    # Время — из приглашения рядом со ссылкой, а не срок сдачи задания из соседнего поста.
    assert info.when == dt.datetime.combine(tomorrow, dt.time(9, 0)), info
    assert info.passcode == "111" and "Лекція №09" in info.title
    assert "Код доступу: 111" in posts["https://zoom.us/j/3637539970?pwd=3Sms84"]
    assert "Заходьте" in posts["https://meet.google.com/new-link-abc"]
    assert "Час:" not in posts["https://meet.google.com/new-link-abc"]
    assert "Час:" not in posts["https://meet.google.com/xyz-abcd-efg"]


def test_runner_finds_calls_in_feed(tmp_path, monkeypatch):
    extra = {"html": ""}

    def fake_launch(pw, settings, profile_dir, executable=None, background=False):
        browser = pw.chromium.launch()
        ctx = browser.new_context()
        ctx.route("https://classroom.google.com/**", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=stream_html(extra["html"])))
        ctx.new_page()
        orig_close = ctx.close
        ctx.close = lambda: (orig_close(), browser.close())
        return ctx

    monkeypatch.setattr(runner, "launch", fake_launch)
    course = "https://classroom.google.com/c/abc"
    settings = Settings(browser_path=os.__file__, auto_enabled=True, auto_courses=[course], auto_hours="00:00-23:59")
    cfg = Config(settings=settings, telegram=TelegramSettings(), classes=[])
    notifier = FakeNotifier()
    r = runner.Runner(cfg, runner.Paths(tmp_path), notifier)

    r.scan_feeds()
    tomorrow9 = dt.datetime.combine(dt.date.today() + dt.timedelta(days=1), dt.time(9, 0))
    assert any("Лекція №09" in m and "Зайду сам" in m for m in notifier.messages), notifier.messages
    first = r.pending(set())[0]
    assert (first.start, first.entry.link, first.entry.passcode) == (
        tomorrow9, "https://zoom.us/j/3637539970?pwd=3Sms84", "111")

    extra["html"] = ('<div class="post"><div>Антон Правда</div><div>10:37</div>'
                     '<div>Заходьте <a href="https://meet.google.com/new-link-abc">meet</a></div></div>')
    r.scan_feeds()
    assert any("прямо сейчас" in m and "Захожу" in m for m in notifier.messages), notifier.messages
    now_call = r.pending(set())[0]
    assert now_call.entry.link == "https://meet.google.com/new-link-abc" and now_call.start <= dt.datetime.now()
    assert len([m for m in notifier.messages if "Нашёл" in m]) == 2


def test_diagnostics_report(tmp_path, monkeypatch):
    from classbot import tasks

    def fake_launch(pw, settings, profile_dir, executable=None, background=False):
        browser = pw.chromium.launch()
        ctx = browser.new_context()
        ctx.route("https://classroom.google.com/**", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=stream_html()))
        ctx.new_page()
        orig_close = ctx.close
        ctx.close = lambda: (orig_close(), browser.close())
        return ctx

    monkeypatch.setattr(tasks, "launch", fake_launch)
    course = "https://classroom.google.com/c/abc"
    cfg = Config(settings=Settings(browser_path=os.__file__, auto_courses=[course]), telegram=TelegramSettings(),
                 classes=[])
    paths = runner.Paths(tmp_path)
    report = tasks.diagnose_feed(cfg, paths)
    print(report)
    assert "постов со ссылками на звонки (как видит бот): 2" in report
    assert "⟦ССЫЛКА⟧" in report and "код: 111" in report
    assert "https://zoom.us/j/3637539970?pwd=3Sms84" in report and "время понял: " in report
    assert "блоки вокруг них" in report and "div.post" in report
    assert (paths.logs / "diagnostics.txt").read_text(encoding="utf-8") == report
    assert len(report) < 15001
