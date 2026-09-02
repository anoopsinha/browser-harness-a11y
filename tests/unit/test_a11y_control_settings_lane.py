"""Settings requests the Controller's grammar did not catch.

"turn off live captioning" misses the grammar's `captions?` and falls through to
the agent lane — which is a model, and a model asked to turn something off will
report that it did. It has: Live Caption stayed on while the person was told it
was off, and they may have no way to look.

The settings we can read back are answered here instead, where the claim is
checked before it is made.
"""
import asyncio
import json

import pytest

from browser_harness.a11y import control
from browser_harness.a11y.control import browser_setting_request as recognise

# Captured before any fixture stubs it out, for the two tests that are about
# _ensure itself rather than about a method that happens to call it.
REAL_ENSURE = control.Receiver._ensure


@pytest.mark.parametrize("utterance,expected", [
    ("turn off live captioning", ("liveCaptions", False)),   # the reported miss
    ("turn off live captions", ("liveCaptions", False)),
    ("live captions off", ("liveCaptions", False)),
    ("no live captions", ("liveCaptions", False)),
    ("please turn off live captioning", ("liveCaptions", False)),
    ("turn on live captioning", ("liveCaptions", True)),
    ("turn off image descriptions", ("autoDescribe", False)),
    ("stop live translation", ("liveTranslate", False)),
])
def test_an_instruction_is_recognised(utterance, expected):
    assert recognise(utterance) == expected


@pytest.mark.parametrize("utterance", [
    "what do live captions do",
    "how do captions work",
    "describe the images",          # ambiguous: could be asking the agent to do it now
    "read me the page",
])
def test_a_question_is_left_to_the_agent(utterance):
    assert recognise(utterance) is None


@pytest.fixture
def receiver(monkeypatch):
    r = control.Receiver.__new__(control.Receiver)
    r._target, r._sid, r._persist_scope = "driven", "s1", None
    monkeypatch.setattr(control, "_log", lambda *a, **k: None)
    monkeypatch.setattr(control.Receiver, "_ensure", lambda self: None)
    return r


def test_it_is_answered_here_rather_than_by_the_agent(receiver, monkeypatch):
    monkeypatch.setattr(control, "a11y_chrome_apply",
                        lambda **kw: {"liveCaptions": {"state": "off", "changed": True}})
    monkeypatch.setattr(control.Receiver, "_task",
                        lambda self, u: pytest.fail("should not have reached the agent"))

    r = receiver.performAction("task", None, "turn off live captioning", {})

    assert r["ok"] is True
    assert r["detail"] == "Live Caption is off"


def test_a_change_that_did_not_take_is_reported_as_failure(receiver, monkeypatch):
    """The bug this replaces was a confident success while nothing had changed."""
    monkeypatch.setattr(control, "a11y_chrome_apply",
                        lambda **kw: {"liveCaptions": {"state": "on", "changed": False}})
    monkeypatch.setattr(control.Receiver, "_task", lambda self, u: pytest.fail("no agent"))

    r = receiver.performAction("task", None, "turn off live captioning", {})

    assert r["ok"] is False
    assert "Live Caption" in r["detail"]


def test_a_missing_control_explains_itself(receiver, monkeypatch):
    monkeypatch.setattr(control, "a11y_chrome_apply",
                        lambda **kw: {"liveCaptions": {"state": "not-found",
                                                       "detail": "Chrome moved it"}})
    monkeypatch.setattr(control.Receiver, "_task", lambda self, u: pytest.fail("no agent"))

    r = receiver.performAction("task", None, "turn off live captioning", {})

    assert r["ok"] is False
    assert r["detail"] == "Chrome moved it"


def test_anything_else_still_goes_to_the_agent(receiver, monkeypatch):
    seen = {}
    monkeypatch.setattr(control.Receiver, "_task",
                        lambda self, u: seen.setdefault("utterance", u) or {"ok": True})
    monkeypatch.setattr(control, "a11y_chrome_apply",
                        lambda **kw: pytest.fail("should not have touched Chrome"))

    receiver.performAction("task", None, "open the news", {})

    assert seen["utterance"] == "open the news"


# ---- iframe mode --------------------------------------------------------
# The page under test is in an iframe served from localhost, and the person
# reading it is on another machine watching their own copy through a tunnel.
# Navigating a tab here moves nothing on their screen.

def test_navigate_asks_the_iframe_host_rather_than_driving_a_tab(receiver, monkeypatch):
    monkeypatch.setattr(control, "IFRAME_HOST", "http://127.0.0.1:8124")
    monkeypatch.setattr(control.Receiver, "_eval",
                        lambda self, e: pytest.fail("must not navigate a tab in iframe mode"))
    sent = {}

    class Resp:
        def read(self): return b'{"url":"https://example.com/","rev":4}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(req, timeout=None):
        sent["url"] = req.full_url
        sent["body"] = req.data
        return Resp()

    monkeypatch.setattr(control.urllib.request, "urlopen", fake_open)

    r = receiver.performAction("navigate", "example.com", None, {})

    assert sent["url"] == "http://127.0.0.1:8124/state"
    assert b"https://example.com" in sent["body"]
    assert r == {"ok": True, "detail": "opening https://example.com"}


def test_a_host_that_is_not_running_is_reported_not_swallowed(receiver, monkeypatch):
    monkeypatch.setattr(control, "IFRAME_HOST", "http://127.0.0.1:8124")
    monkeypatch.setattr(control.Receiver, "_eval", lambda self, e: None)

    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(control.urllib.request, "urlopen", boom)

    r = receiver.performAction("navigate", "example.com", None, {})

    assert r["ok"] is False
    assert "iframe host" in r["detail"]


def test_without_the_env_it_still_drives_the_tab(receiver, monkeypatch):
    monkeypatch.setattr(control, "IFRAME_HOST", "")
    seen = {}
    monkeypatch.setattr(control.Receiver, "_eval",
                        lambda self, e: seen.setdefault("eval", e))

    r = receiver.performAction("navigate", "example.com", None, {})

    assert "location.assign" in seen["eval"]
    assert r["ok"] is True


def test_the_viewer_is_found_by_what_it_is_not_where_it_is_served(receiver, monkeypatch):
    """localhost:8124 and 127.0.0.1:8124 are the same server and different
    strings. Matching the configured URL found nothing while the page sat open
    under the other name."""
    monkeypatch.setattr(control, "IFRAME_HOST", "http://127.0.0.1:8124")
    monkeypatch.setattr(control, "list_tabs", lambda include_chrome=True: [
        {"targetId": "other", "url": "https://example.com/"},
        {"targetId": "viewer", "url": "http://localhost:8124/"},
    ])
    asked = []

    def fake_js(expression, target_id=None):
        asked.append(target_id)
        return target_id == "viewer"

    monkeypatch.setattr(control, "js", fake_js)

    assert receiver._iframe_viewer() == "viewer"
    assert asked == ["viewer"]  # the port narrowed it before the page was asked


def test_a_tunnelled_viewer_on_an_unexpected_url_is_still_found(receiver, monkeypatch):
    monkeypatch.setattr(control, "IFRAME_HOST", "http://127.0.0.1:8124")
    monkeypatch.setattr(control, "list_tabs", lambda include_chrome=True: [
        {"targetId": "a", "url": "https://example.com/"},
        {"targetId": "b", "url": "https://tunnel.example/session/xyz"},
    ])
    monkeypatch.setattr(control, "js",
                        lambda expression, target_id=None: target_id == "b")

    assert receiver._iframe_viewer() == "b"


def test_no_viewer_says_what_to_open(receiver, monkeypatch):
    monkeypatch.setattr(control, "IFRAME_HOST", "http://127.0.0.1:8124")
    monkeypatch.setattr(control, "list_tabs", lambda include_chrome=True: [])
    monkeypatch.setattr(control, "js", lambda *a, **k: False)

    r = receiver._iframe_call("getContent", "outline")

    assert "Framed page" in r["error"]


def test_evaluation_goes_into_the_frame_never_the_pinned_tab(receiver, monkeypatch):
    """The pinned tab in this mode is whatever was open — often the hosting
    service's own page. Everything the receiver evaluates belongs to the page
    under test, which is the frame."""
    monkeypatch.setattr(control, "IFRAME_HOST", "http://127.0.0.1:8124")
    receiver._target = "assistivlabs-tab"
    monkeypatch.setattr(control.Receiver, "_iframe_viewer", lambda self: "framed-page")
    seen = {}

    def fake_js(expression, target_id=None):
        seen["target"] = target_id
        seen["expr"] = expression
        return "ok"

    monkeypatch.setattr(control, "js", fake_js)

    assert receiver._eval("return document.title") == "ok"
    assert seen["target"] == "framed-page"          # not the pinned tab
    assert "contentWindow.eval" in seen["expr"]     # and inside its frame


def test_the_catalog_is_never_injected_into_a_tab_in_iframe_mode(receiver, monkeypatch):
    """The proxy already put it in the framed page; injecting from here would
    land in whatever tab was pinned."""
    monkeypatch.setattr(control, "IFRAME_HOST", "http://127.0.0.1:8124")
    monkeypatch.setattr(control.Receiver, "_eval", lambda self, e: True)
    monkeypatch.setattr(control, "cdp",
                        lambda *a, **k: pytest.fail("must not inject into a tab"))

    REAL_ENSURE(receiver)  # returns without injecting


def test_a_framed_page_without_adapters_says_so(receiver, monkeypatch):
    monkeypatch.setattr(control, "IFRAME_HOST", "http://127.0.0.1:8124")
    monkeypatch.setattr(control.Receiver, "_eval", lambda self, e: False)

    with pytest.raises(RuntimeError, match="no adapters"):
        REAL_ENSURE(receiver)


def test_search_goes_through_the_host_not_the_frames_own_location(receiver, monkeypatch):
    """_eval runs inside the frame now, so location.assign there would steer
    this viewer around the proxy and leave the others behind."""
    monkeypatch.setattr(control, "IFRAME_HOST", "http://127.0.0.1:8124")
    monkeypatch.setattr(control.Receiver, "_eval",
                        lambda self, e: pytest.fail("must not steer the frame directly"))
    seen = {}
    monkeypatch.setattr(control.Receiver, "_navigate_iframe",
                        lambda self, url: seen.setdefault("url", url) and None or {"ok": True})

    r = receiver.performAction("search", "apples", None, {})

    assert "google.com" in seen["url"] and "apples" in seen["url"]
    assert "searching google for apples" in r["detail"]


def test_no_stray_tab_is_opened_when_there_is_nothing_to_reacquire(receiver, monkeypatch):
    monkeypatch.setattr(control, "IFRAME_HOST", "http://127.0.0.1:8124")
    monkeypatch.setattr(control, "list_tabs", lambda include_chrome=True: [])
    monkeypatch.setattr(control, "new_tab",
                        lambda *a, **k: pytest.fail("must not open a tab in iframe mode"))

    assert receiver._reacquire() is False


# ---- an answer must outlive the connection that asked for it ------------
# A task brings the driven tab to the front, and the chat closes its socket
# while it is in the background. The reply is then ready at the exact moment
# there is nobody to hand it to, and the person sits watching "working on it".

class FakeSocket:
    def __init__(self, fail_after=None):
        self.sent, self.fail_after = [], fail_after

    async def send(self, payload):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise RuntimeError("socket closed")
        self.sent.append(json.loads(payload))


@pytest.fixture(autouse=True)
def empty_queue(monkeypatch):
    monkeypatch.setattr(control, "_log", lambda *a, **k: None)
    control._PENDING_NOTES.clear()
    yield
    control._PENDING_NOTES.clear()


def test_a_held_answer_reaches_the_next_connection():
    control.hold_note("I searched Google for apples.")

    ws = FakeSocket()
    assert asyncio.run(control.flush_notes(ws)) == 1
    assert ws.sent[0]["text"] == "I searched Google for apples."
    assert control._PENDING_NOTES == []


def test_answers_are_delivered_oldest_first():
    control.hold_note("first")
    control.hold_note("second")

    ws = FakeSocket()
    asyncio.run(control.flush_notes(ws))

    assert [m["text"] for m in ws.sent] == ["first", "second"]


def test_a_second_failure_keeps_the_answer_rather_than_dropping_it():
    """Losing it here is the same silence as before, one connection later."""
    control.hold_note("first")
    control.hold_note("second")

    ws = FakeSocket(fail_after=1)
    assert asyncio.run(control.flush_notes(ws)) == 1

    assert control._PENDING_NOTES == ["second"]


def test_nothing_held_sends_nothing():
    ws = FakeSocket()
    assert asyncio.run(control.flush_notes(ws)) == 0
    assert ws.sent == []


# ---- the agent works here too -------------------------------------------
# Withholding it was wrong: an open-ended sentence should reach the model and
# come back summarised, exactly as it does when the page is a tab. What differs
# is only where the page is, and that belongs in the agent's instructions.

def test_the_agent_is_offered_in_iframe_mode(receiver, monkeypatch):
    monkeypatch.setattr(control, "IFRAME_HOST", "http://127.0.0.1:8124")
    monkeypatch.setattr(control.Receiver, "_agent_token", lambda self: "a-token")

    assert "task" in receiver._actions()
    assert "stop" in receiver._actions()


def test_an_open_ended_sentence_reaches_the_agent(receiver, monkeypatch):
    monkeypatch.setattr(control, "IFRAME_HOST", "http://127.0.0.1:8124")
    seen = {}
    monkeypatch.setattr(control.Receiver, "_task",
                        lambda self, u: seen.setdefault("utterance", u) or {"ok": True})

    receiver.performAction("task", None, "summarise this article", {})

    assert seen["utterance"] == "summarise this article"


def test_the_agent_is_told_to_navigate_through_the_host(receiver, monkeypatch):
    """A navigation done here moves this screen alone, around the proxy, while
    the person's copy stays where it was."""
    monkeypatch.setattr(control, "IFRAME_HOST", "http://127.0.0.1:8124")
    monkeypatch.setattr(control.Receiver, "_iframe_viewer", lambda self: "viewer-tab")
    monkeypatch.setattr(control.Receiver, "_agent_token", lambda self: "a-token")
    sent = {}
    monkeypatch.setattr(control.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no agent service")))
    monkeypatch.setattr(control, "AGENT_URL", "http://127.0.0.1:8787/stream")

    # the prompt is built before the request goes out; capture it from the body
    class Boom(Exception):
        pass

    def capture(req, timeout=None):
        sent["body"] = req.data.decode()
        raise Boom()

    monkeypatch.setattr(control.urllib.request, "urlopen", capture)
    try:
        receiver._task("summarise this")
    except Exception:
        pass

    body = sent.get("body", "")
    assert "iframe on http://127.0.0.1:8124/" in body
    assert "/state" in body and "do NOT call" in body
    assert "switch_tab('viewer-tab')" in body
