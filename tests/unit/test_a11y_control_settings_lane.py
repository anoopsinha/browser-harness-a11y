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
    r._target, r._sid = "driven", "s1"
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
