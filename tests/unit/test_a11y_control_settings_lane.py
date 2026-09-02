"""Settings requests the Controller's grammar did not catch.

"turn off live captioning" misses the grammar's `captions?` and falls through to
the agent lane — which is a model, and a model asked to turn something off will
report that it did. It has: Live Caption stayed on while the person was told it
was off, and they may have no way to look.

The settings we can read back are answered here instead, where the claim is
checked before it is made.
"""
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


def test_no_agent_task_is_offered_in_iframe_mode(receiver, monkeypatch):
    """The agent drives a browser over CDP, and that browser is not the one
    being read. Offering it sends unmatched sentences somewhere that cannot
    help, and leaves stray tabs on this machine."""
    monkeypatch.setattr(control, "IFRAME_HOST", "http://127.0.0.1:8124")
    monkeypatch.setattr(control.Receiver, "_agent_token", lambda self: "a-token")

    actions = receiver._actions()

    assert "task" not in actions and "stop" not in actions
    assert "navigate" in actions and "activate" in actions


def test_a_task_that_arrives_anyway_is_declined_not_driven(receiver, monkeypatch):
    monkeypatch.setattr(control, "IFRAME_HOST", "http://127.0.0.1:8124")
    monkeypatch.setattr(control.Receiver, "_task",
                        lambda self, u: pytest.fail("must not drive a browser"))

    r = receiver.performAction("task", None, "find me a flight", {})

    assert r["ok"] is False
    assert "cannot run open-ended tasks" in r["detail"]


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
