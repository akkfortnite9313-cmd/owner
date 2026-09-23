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

from classbot import meet, runner  # noqa: E402
from classbot.classroom import fetch_meet_links  # noqa: E402
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


def install_routes(ctx, classroom_links):
    ctx.route("https://meet.google.com/**", lambda route: route.fulfill(
        status=200, content_type="text/html; charset=utf-8", body=FAKE_MEET))
    ctx.route("https://classroom.google.com/**", lambda route: route.fulfill(
        status=200, content_type="text/html; charset=utf-8", body=classroom_html(classroom_links)))


@pytest.fixture
def pw():
    with sync_playwright() as p:
        yield p


@pytest.fixture
def page(pw):
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
    with pytest.raises(meet.MeetError, match="can't join"):
        meet.join(page, A + "?fatal=1", timeout_s=20)


def test_mentions_in_chat(page, monkeypatch):
    monkeypatch.setattr(meet.MentionWatcher, "WARMUP_S", 0)
    meet.join(page, A, timeout_s=20)
    w = meet.MentionWatcher(["иванов"])
    assert w.poll(page) == []
    time.sleep(4.5)
    assert w.poll(page) == ["Иванов, вы здесь?"]
    assert w.poll(page) == []


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


def test_full_session(tmp_path, monkeypatch):
    """Пара целиком: в ленте появилась новая ссылка B → зашёл → упомянули в чате → вышел."""
    def fake_launch(pw, settings, profile_dir, executable=None):
        # Как настоящий launch(), но без окна и с поддельными страницами Google.
        browser = pw.chromium.launch()
        ctx = browser.new_context()
        install_routes(ctx, [B, A])
        ctx.new_page()
        orig_close = ctx.close
        ctx.close = lambda: (orig_close(), browser.close())
        return ctx

    monkeypatch.setattr(runner, "launch", fake_launch)
    monkeypatch.setattr(meet.MentionWatcher, "WARMUP_S", 1)
    course = "https://classroom.google.com/c/course1"
    now = dt.datetime.now()
    entry = ClassEntry(name="Тест", days=frozenset(range(7)), start=now.time(), end=now.time(),
                       course=course)
    occ = Occurrence(entry, now, now + dt.timedelta(seconds=25))
    settings = Settings(link_poll_sec=1, browser_path=os.__file__, watch_captions=True)
    cfg = Config(settings=settings, telegram=TelegramSettings(mention_keywords=["Иванов"]), classes=[entry])
    notifier = FakeNotifier()
    r = runner.Runner(cfg, runner.Paths(tmp_path), notifier)
    r.state.set_baseline(course, {A: 1})

    r.run_session(occ)

    text = "\n".join(notifier.messages)
    assert "Зашёл на пару «Тест»" in text and B in text, text
    assert "вас упомянули" in text and "Иванов, вы здесь?" in text, text
    assert "закончилась, вышел из звонка" in text, text
    assert "Ошибка" not in text, text
    assert r.state.baseline(course) == {B: 2, A: 1}
