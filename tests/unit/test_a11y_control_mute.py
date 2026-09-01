"""muteAudio: the Controller fires this the moment voice input starts.

A web Controller can only silence its own tab, so everything still making noise
is in a tab that only this receiver can reach. The person is mid-sentence while
this runs, which is why the walk is bounded and why one unresponsive tab must
not hold up the rest.
"""
import pytest

from browser_harness.a11y import control


@pytest.fixture
def receiver(monkeypatch):
    r = control.Receiver.__new__(control.Receiver)
    r._target = "driven"
    monkeypatch.setattr(control, "_log", lambda *a, **k: None)
    return r


def fake_browser(monkeypatch, tabs, per_tab):
    """`per_tab` maps a target id to a pause count, or to an exception to raise."""
    monkeypatch.setattr(control, "list_tabs",
                        lambda include_chrome=True: [{"targetId": t} for t in tabs])
    seen = []

    def fake_js(expression, target_id=None):
        seen.append(target_id)
        outcome = per_tab.get(target_id, 0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(control, "js", fake_js)
    return seen


def test_muteAudio_is_advertised_so_the_controller_will_send_it(receiver, monkeypatch):
    monkeypatch.setattr(control.Receiver, "_agent_token", lambda self: None)

    assert "muteAudio" in receiver._actions()


def test_pauses_media_in_every_tab_not_just_the_driven_one(receiver, monkeypatch):
    seen = fake_browser(monkeypatch, ["driven", "other", "chat"],
                        {"driven": 1, "other": 2, "chat": 0})

    result = receiver.performAction("muteAudio", None, None, {})

    assert result["ok"] is True
    assert seen == ["driven", "other", "chat"]
    assert "paused 3 in 3 tabs" == result["detail"]


def test_a_tab_that_will_not_answer_does_not_block_the_others(receiver, monkeypatch):
    seen = fake_browser(monkeypatch, ["wedged", "playing"],
                        {"wedged": RuntimeError("target closed"), "playing": 1})

    result = receiver.performAction("muteAudio", None, None, {})

    assert result["ok"] is True
    assert seen == ["wedged", "playing"]  # it moved on rather than giving up
    assert result["detail"] == "paused 1 in 1 tabs"


def test_the_walk_is_bounded_so_a_crowded_browser_still_answers_in_time(receiver, monkeypatch):
    monkeypatch.setattr(control, "MUTE_TAB_LIMIT", 3)
    seen = fake_browser(monkeypatch, [f"t{i}" for i in range(50)], {})

    receiver.performAction("muteAudio", None, None, {})

    assert seen == ["t0", "t1", "t2"]


def test_tabs_without_a_target_id_are_skipped(receiver, monkeypatch):
    monkeypatch.setattr(control, "list_tabs",
                        lambda include_chrome=True: [{"targetId": None}, {"targetId": "real"}])
    seen = []
    monkeypatch.setattr(control, "js",
                        lambda expression, target_id=None: (seen.append(target_id), 1)[1])

    result = receiver.performAction("muteAudio", None, None, {})

    assert seen == ["real"]
    assert result["detail"] == "paused 1 in 1 tabs"


def test_it_pauses_media_and_cancels_speech(receiver, monkeypatch):
    """The Controller reading a result aloud is often the loudest thing playing."""
    sent = {}
    monkeypatch.setattr(control, "list_tabs", lambda include_chrome=True: [{"targetId": "t"}])
    monkeypatch.setattr(control, "js",
                        lambda expression, target_id=None: (sent.setdefault("js", expression), 0)[1])

    receiver.performAction("muteAudio", None, None, {})

    assert "querySelectorAll('audio, video')" in sent["js"]
    assert ".pause()" in sent["js"]
    assert "speechSynthesis.cancel()" in sent["js"]


def test_the_control_surface_is_left_alone(receiver, monkeypatch):
    """It silences its own audio, and it is where the person is dictating."""
    fake_browser(monkeypatch, ["chat", "video"], {"chat": -1, "video": 2})

    result = receiver.performAction("muteAudio", None, None, {})

    # the skipped tab counts as neither reached nor paused
    assert result["detail"] == "paused 2 in 1 tabs"


def test_the_sweep_recognises_the_control_surface_itself(receiver, monkeypatch):
    """Skipping happens inside the one round trip, not via a second probe."""
    assert control._IS_CONTROLLER_JS in control._MUTE_JS
    assert "return -1" in control._MUTE_JS
