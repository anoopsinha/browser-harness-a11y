"""Chrome's Live Caption, followed from the profile.

Different thing from the showCaptions adapter: that switches on a video's own
caption track, this has Chrome caption any audio on-device. It is a browser
preference, so unlike everything else the toolkit applies it outlives the tab
and the session — which is what these tests are mostly about.
"""
import json

import pytest

from browser_harness import a11y


@pytest.fixture
def prefs_file(tmp_path, monkeypatch):
    f = tmp_path / "a11y-browser-prefs.json"
    monkeypatch.setattr(a11y, "_BROWSER_PREFS_FILE", f)
    return f


@pytest.fixture
def settings_page(monkeypatch):
    """A settings tab already open, so nothing is opened or closed."""
    monkeypatch.setattr(a11y, "list_tabs",
                        lambda include_chrome=True: [
                            {"targetId": "settings", "url": "chrome://settings/accessibility"}])
    monkeypatch.setattr(a11y, "current_tab", lambda: {"targetId": "driven"})
    monkeypatch.setattr(a11y, "switch_tab", lambda *a, **k: None)
    monkeypatch.setattr(a11y, "activate_tab", lambda *a, **k: None)
    monkeypatch.setattr(a11y, "cdp", lambda *a, **k: {})
    monkeypatch.setattr(a11y, "_driven_target", None)


def scripted(monkeypatch, states):
    """Feed reads from `states`; each click advances to the next one."""
    seq = list(states)
    seen = {"clicks": 0}

    def fake_js(expression, target_id=None):
        if "el.click()" in expression:
            seen["clicks"] += 1
            if len(seq) > 1:
                seq.pop(0)
            return "clicked"
        return seq[0]

    monkeypatch.setattr(a11y, "js", fake_js)
    return seen


# ---- the toggle ---------------------------------------------------------

def test_turning_it_on_clicks_once_and_records_that_it_was_ours(
        settings_page, prefs_file, monkeypatch):
    seen = scripted(monkeypatch, ["off", "on"])

    r = a11y.a11y_live_captions(True)

    assert r == {"live_captions": "on", "changed": True}
    assert seen["clicks"] == 1
    assert json.loads(prefs_file.read_text())["live_captions_ours"] is True


def test_already_on_is_left_alone_and_not_claimed_as_ours(
        settings_page, prefs_file, monkeypatch):
    """Not ours to switch off later, so it must not be recorded as ours."""
    seen = scripted(monkeypatch, ["on"])

    r = a11y.a11y_live_captions(True)

    assert r == {"live_captions": "on", "changed": False}
    assert seen["clicks"] == 0
    assert "live_captions_ours" not in json.loads(prefs_file.read_text())


def test_a_click_that_does_not_take_is_retried(settings_page, prefs_file, monkeypatch):
    """The toggle exists before it is bound to the pref; early clicks are dropped."""
    seen = scripted(monkeypatch, ["off", "off", "off", "on"])

    r = a11y.a11y_live_captions(True, patience=5.0)

    assert r["live_captions"] == "on"
    assert seen["clicks"] == 3


def test_it_gives_up_with_an_explanation_rather_than_hanging(
        settings_page, prefs_file, monkeypatch):
    scripted(monkeypatch, ["off"])  # never moves

    r = a11y.a11y_live_captions(True, patience=1.0)

    assert r["live_captions"] == "off"
    assert "stayed off" in r["detail"]


def test_a_renamed_toggle_is_an_answer_not_an_exception(
        settings_page, prefs_file, monkeypatch):
    """The element id is Chrome's, and a release may rename it."""
    scripted(monkeypatch, ["not-found"])

    r = a11y.a11y_live_captions(True, patience=1.0)

    assert r["live_captions"] == "not-found"
    assert "chrome://settings/accessibility" in r["detail"]


def test_switching_it_off_drops_our_claim(settings_page, prefs_file, monkeypatch):
    prefs_file.write_text(json.dumps({"live_captions_ours": True}))
    scripted(monkeypatch, ["on", "off"])

    a11y.a11y_live_captions(False)

    assert "live_captions_ours" not in json.loads(prefs_file.read_text())


def test_the_tab_it_opened_is_closed_and_the_session_comes_home_first(
        prefs_file, monkeypatch):
    """Closing the tab while the daemon is still attached to it disconnects CDP."""
    monkeypatch.setattr(a11y, "list_tabs", lambda include_chrome=True: [])
    monkeypatch.setattr(a11y, "current_tab", lambda: {"targetId": "driven"})
    monkeypatch.setattr(a11y, "new_tab", lambda url: "scratch")
    monkeypatch.setattr(a11y, "_driven_target", None)
    order = []
    monkeypatch.setattr(a11y, "switch_tab", lambda t, **k: order.append(("switch", t)))
    monkeypatch.setattr(a11y, "activate_tab", lambda t: order.append(("activate", t)))
    monkeypatch.setattr(a11y, "cdp",
                        lambda m, **k: order.append((m, k.get("targetId"))) or {})
    scripted(monkeypatch, ["off", "on"])

    a11y.a11y_live_captions(True)

    assert order == [("switch", "driven"),
                     ("Target.closeTarget", "scratch"),
                     ("activate", "driven")]


def test_a_reused_blank_driven_tab_is_never_closed(prefs_file, monkeypatch):
    """new_tab reuses the attached tab when it is blank — closing it would take
    the page the person is on with it."""
    monkeypatch.setattr(a11y, "list_tabs", lambda include_chrome=True: [])
    monkeypatch.setattr(a11y, "current_tab", lambda: {"targetId": "driven"})
    monkeypatch.setattr(a11y, "new_tab", lambda url: "driven")  # reused, not new
    monkeypatch.setattr(a11y, "_driven_target", None)
    monkeypatch.setattr(a11y, "switch_tab", lambda *a, **k: None)
    monkeypatch.setattr(a11y, "activate_tab", lambda *a, **k: None)
    closed = []
    monkeypatch.setattr(a11y, "cdp", lambda m, **k: closed.append(m) or {})
    scripted(monkeypatch, ["off", "on"])

    a11y.a11y_live_captions(True)

    assert "Target.closeTarget" not in closed


# ---- following the profile ---------------------------------------------

@pytest.fixture
def spy(monkeypatch):
    calls = []
    monkeypatch.setattr(a11y, "a11y_live_captions",
                        lambda on=True, **k: calls.append(on) or {"live_captions": on})
    return calls


@pytest.mark.parametrize("settings", [{"showCaptions": True},
                                      {"autoCaptions": True},
                                      {"showCaptions": True, "autoCaptions": True}])
def test_a_profile_that_reads_rather_than_hears_turns_it_on(settings, spy, prefs_file):
    a11y._follow_captions(settings)

    assert spy == [True]


def test_it_is_turned_off_again_when_the_profile_stops_asking(spy, prefs_file):
    prefs_file.write_text(json.dumps({"live_captions_ours": True}))

    a11y._follow_captions({"largeText": True})

    assert spy == [False]


def test_a_setting_they_made_themselves_is_not_ours_to_undo(spy, prefs_file):
    prefs_file.write_text(json.dumps({}))  # we never turned it on

    r = a11y._follow_captions({"largeText": True})

    assert spy == []
    assert r == {"live_captions": "left alone"}


def test_captions_switched_off_in_the_profile_do_not_count_as_wanted(spy, prefs_file):
    r = a11y._follow_captions({"showCaptions": False, "autoCaptions": False})

    assert spy == []
    assert r == {"live_captions": "left alone"}
