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
    assert a11y._is_ours("liveCaptions")


def test_already_on_is_left_alone_and_not_claimed_as_ours(
        settings_page, prefs_file, monkeypatch):
    """Not ours to switch off later, so it must not be recorded as ours."""
    seen = scripted(monkeypatch, ["on"])

    r = a11y.a11y_live_captions(True)

    assert r == {"live_captions": "on", "changed": False}
    assert seen["clicks"] == 0
    assert not a11y._is_ours("liveCaptions")


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
    prefs_file.write_text(json.dumps({"ours": {"liveCaptions": True}}))
    scripted(monkeypatch, ["on", "off"])

    a11y.a11y_live_captions(False)

    assert not a11y._is_ours("liveCaptions")


def test_the_tab_it_opened_is_closed_and_the_session_comes_home_first(
        prefs_file, monkeypatch):
    """Closing the tab while the daemon is still attached to it disconnects CDP."""
    monkeypatch.setattr(a11y, "list_tabs", lambda include_chrome=True: [])
    monkeypatch.setattr(a11y, "current_tab", lambda: {"targetId": "driven"})
    monkeypatch.setattr(a11y, "goto_url", lambda url: None)
    monkeypatch.setattr(a11y, "_driven_target", None)
    order = []
    monkeypatch.setattr(a11y, "switch_tab", lambda t, **k: order.append(("switch", t)))
    monkeypatch.setattr(a11y, "activate_tab", lambda t: order.append(("activate", t)))

    def fake_cdp(method, **kw):
        if method == "Target.createTarget":
            return {"targetId": "scratch"}
        order.append((method, kw.get("targetId")))
        return {}

    monkeypatch.setattr(a11y, "cdp", fake_cdp)
    scripted(monkeypatch, ["off", "on"])

    a11y.a11y_live_captions(True)

    assert order == [("switch", "scratch"),          # to drive the new tab
                     ("switch", "driven"),           # home before closing it
                     ("Target.closeTarget", "scratch"),
                     ("activate", "driven")]


def test_the_settings_page_never_lands_in_the_driven_tab(prefs_file, monkeypatch):
    """new_tab reuses the attached tab when it is blank, and at startup the
    driven tab is blank — so the settings page took it over and the session was
    left pinned to chrome://settings."""
    monkeypatch.setattr(a11y, "list_tabs", lambda include_chrome=True: [])
    monkeypatch.setattr(a11y, "current_tab", lambda: {"targetId": "driven"})
    monkeypatch.setattr(a11y, "goto_url", lambda url: None)
    monkeypatch.setattr(a11y, "_driven_target", None)
    monkeypatch.setattr(a11y, "switch_tab", lambda *a, **k: None)
    monkeypatch.setattr(a11y, "activate_tab", lambda *a, **k: None)
    monkeypatch.setattr(a11y, "new_tab",
                        lambda *a, **k: pytest.fail("must not reuse a tab it did not make"))
    monkeypatch.setattr(a11y, "cdp",
                        lambda m, **kw: {"targetId": "scratch"} if m == "Target.createTarget" else {})
    scripted(monkeypatch, ["off", "on"])

    a11y.a11y_live_captions(True)


# ---- following a settings change ----------------------------------------
# _follow_captions is no longer the profile's path — that is _follow_browser,
# which reads liveCaptions from the resolved settings by name. What is left here
# runs after a settings change, and may only act when the change was about
# captions.

@pytest.fixture
def spy(monkeypatch):
    calls = []
    monkeypatch.setattr(a11y, "a11y_live_captions",
                        lambda on=True, **k: calls.append(on) or {"live_captions": on})
    return calls


@pytest.mark.parametrize("settings", [{"showCaptions": True},
                                      {"autoCaptions": True},
                                      {"showCaptions": True, "autoCaptions": True}])
def test_switching_captions_on_turns_it_on(settings, spy, prefs_file):
    a11y._follow_captions(settings, explicit=True)

    assert spy == [True]


def test_what_we_switched_on_comes_off_with_the_captions(spy, prefs_file):
    prefs_file.write_text(json.dumps({"ours": {"liveCaptions": True}}))

    a11y._follow_captions({"showCaptions": False}, explicit=True)

    assert spy == [False]


def test_a_setting_they_made_themselves_is_not_ours_to_undo(spy, prefs_file):
    prefs_file.write_text(json.dumps({}))  # we never turned it on

    r = a11y._follow_captions({"showCaptions": False})

    assert spy == []
    assert r == {"live_captions": "left alone"}


def test_captions_switched_off_in_the_profile_do_not_count_as_wanted(spy, prefs_file):
    r = a11y._follow_captions({"showCaptions": False, "autoCaptions": False})

    assert spy == []
    assert r == {"live_captions": "left alone"}


def test_an_explicit_request_switches_off_what_we_do_not_own(spy, prefs_file):
    """"Hide live captions" said aloud, with Live Caption already on before we
    ever ran: declining it because we did not switch it on is the bug, not the
    safeguard. Ownership governs the inferred case, not the instruction."""
    prefs_file.write_text(json.dumps({}))  # not ours

    a11y._follow_captions({"showCaptions": False}, explicit=True)

    assert spy == [False]


def test_following_a_profile_still_leaves_what_we_do_not_own(spy, prefs_file):
    prefs_file.write_text(json.dumps({}))

    r = a11y._follow_captions({"showCaptions": False})

    assert spy == []
    assert r == {"live_captions": "left alone"}


# ---- only a change about captions may move Chrome's ---------------------

def test_an_unrelated_setting_leaves_live_caption_alone(spy, prefs_file):
    """Reported: "smaller text" switched Live Caption on. Both callers pass the
    settings active *after* the change, and on a hearing profile those always
    mention captions — so intent read from them was never the person's."""
    r = a11y._follow_captions({"showCaptions": True, "fontScale": 90}, explicit=False)

    assert spy == []
    assert r == {"live_captions": "left alone"}


def test_a_change_about_captions_still_turns_it_on(spy, prefs_file):
    a11y._follow_captions({"showCaptions": True}, explicit=True)

    assert spy == [True]


def test_a_change_about_captions_still_turns_it_off(spy, prefs_file):
    a11y._follow_captions({"showCaptions": False}, explicit=True)

    assert spy == [False]
