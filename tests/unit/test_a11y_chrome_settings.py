"""Settings that live in the browser, not the page.

chrome://settings/accessibility carries things no page-level adapter can do —
Chrome will describe unlabelled images with a real model, where the toolkit can
only report autoDescribe as needs-ai. These tests are about which of the two
places a given request should land in.
"""
import json

import pytest

from browser_harness import a11y
from browser_harness.a11y import control


@pytest.fixture
def prefs_file(tmp_path, monkeypatch):
    f = tmp_path / "prefs.json"
    monkeypatch.setattr(a11y, "_BROWSER_PREFS_FILE", f)
    return f


@pytest.fixture
def receiver(monkeypatch):
    r = control.Receiver.__new__(control.Receiver)
    r._target, r._sid, r._undo, r._persist_scope = "driven", "s1", [], None
    monkeypatch.setattr(control, "_log", lambda *a, **k: None)
    monkeypatch.setattr(control.Receiver, "_ensure", lambda self: None)
    monkeypatch.setattr(control, "_follow_captions", lambda s, explicit=False: {})
    return r


def page_does(monkeypatch, applied_keys, skipped_keys=()):
    def eval_(self, expr):
        if "activeSettings" in expr:
            return {}
        return {"applied": [{"from": list(applied_keys)}] if applied_keys else [],
                "disabled": [],
                "skipped": [{"setting": k, "reason": "needs-ai"} for k in skipped_keys],
                "errors": []}
    monkeypatch.setattr(control.Receiver, "_eval", eval_)


def chrome_answers(monkeypatch, states):
    seen = {}

    def fake(patience=10.0, **settings):
        seen.update(settings)
        return {k: {"state": "on" if states.get(k, settings[k]) else "off", "changed": True}
                for k in settings}

    monkeypatch.setattr(control, "a11y_chrome_apply", fake)
    return seen


def test_a_setting_the_page_cannot_do_is_asked_of_the_browser(receiver, monkeypatch):
    """autoDescribe comes back needs-ai from a toolkit holding no model; Chrome
    has one."""
    page_does(monkeypatch, [], skipped_keys=["autoDescribe"])
    seen = chrome_answers(monkeypatch, {})

    r = receiver.applySettings({"autoDescribe": True})

    assert seen == {"autoDescribe": True}
    assert r["applied"] == {"autoDescribe": True}
    assert "autoDescribe" not in r["rejected"]
    assert r["chrome"]["autoDescribe"]["state"] == "on"


def test_turning_one_off_reaches_the_browser_too(receiver, monkeypatch):
    """An off value never lands in the reject list — the dispatcher looks for an
    adapter to stop, finds none for a browser setting, and moves on silently."""
    page_does(monkeypatch, [])  # nothing applied, nothing rejected
    seen = chrome_answers(monkeypatch, {})

    r = receiver.applySettings({"caretBrowsing": False})

    assert seen == {"caretBrowsing": False}
    assert r["applied"] == {"caretBrowsing": False}


def test_what_the_page_already_did_is_not_also_done_to_the_browser(receiver, monkeypatch):
    """These settings are browser-wide and persist. Reaching for one where a
    page adapter already did the job changes more than was asked for."""
    page_does(monkeypatch, ["focusHighlight"])
    seen = chrome_answers(monkeypatch, {})

    r = receiver.applySettings({"focusHighlight": True})

    assert seen == {}
    assert "chrome" not in r


def test_a_page_only_setting_is_never_diverted(receiver, monkeypatch):
    page_does(monkeypatch, [], skipped_keys=["somethingElse"])
    seen = chrome_answers(monkeypatch, {})

    receiver.applySettings({"somethingElse": True})

    assert seen == {}


def test_a_browser_that_will_not_answer_does_not_sink_the_page_change(receiver, monkeypatch):
    page_does(monkeypatch, ["fontScale"], skipped_keys=["autoDescribe"])

    def boom(**kw):
        raise RuntimeError("settings page would not open")

    monkeypatch.setattr(control, "a11y_chrome_apply", boom)

    r = receiver.applySettings({"fontScale": 130, "autoDescribe": True})

    assert r["applied"] == {"fontScale": 130}


def test_a_toggle_chrome_no_longer_has_is_not_counted_as_applied(receiver, monkeypatch):
    page_does(monkeypatch, [], skipped_keys=["autoDescribe"])
    monkeypatch.setattr(control, "a11y_chrome_apply",
                        lambda **kw: {"autoDescribe": {"state": "not-found", "detail": "gone"}})

    r = receiver.applySettings({"autoDescribe": True})

    assert "autoDescribe" not in r.get("applied", {})


# ---- ownership, now per setting ----------------------------------------

def test_each_setting_is_claimed_separately(prefs_file):
    a11y._claim("liveCaptions", True)
    a11y._claim("autoDescribe", True)
    a11y._claim("autoDescribe", False)

    assert a11y._is_ours("liveCaptions")
    assert not a11y._is_ours("autoDescribe")


def test_the_single_flag_this_replaced_is_still_honoured(prefs_file):
    """Written by the version that only ever touched Live Caption."""
    prefs_file.write_text(json.dumps({"live_captions_ours": True}))

    assert a11y._is_ours("liveCaptions")
    assert not a11y._is_ours("autoDescribe")


# ---- autoDescribe from a profile ---------------------------------------
# The only implementation of autoDescribe on this platform. Applying it through
# the toolkit reports needs-ai and changes nothing, which is what a blind
# person's profile used to do here.

@pytest.fixture
def follow_spy(monkeypatch):
    calls = []

    def fake(patience=10.0, **settings):
        calls.append(settings)
        return {k: {"state": "on" if v else "off", "changed": True}
                for k, v in settings.items()}

    monkeypatch.setattr(a11y, "a11y_chrome_apply", fake)
    return calls


def test_a_profile_asking_for_descriptions_reaches_chrome(follow_spy, prefs_file):
    r = a11y._follow_browser({"autoDescribe": True})

    assert {"autoDescribe": True} in follow_spy
    assert r["autoDescribe"]["state"] == "on"


def test_a_profile_that_does_not_ask_leaves_a_setting_we_do_not_own(follow_spy, prefs_file):
    r = a11y._follow_browser({"fontScale": 150})

    assert follow_spy == []
    assert r["autoDescribe"] == {"state": "left alone"}


def test_what_we_switched_on_is_switched_back_off(follow_spy, prefs_file):
    a11y._claim("autoDescribe", True)

    a11y._follow_browser({"fontScale": 150})

    assert {"autoDescribe": False} in follow_spy


def test_being_told_overrides_ownership(follow_spy, prefs_file):
    """Not ours, but they asked — that is an instruction, not an inference."""
    a11y._follow_browser({}, explicit=("autoDescribe",))

    assert {"autoDescribe": False} in follow_spy


def test_captions_and_descriptions_are_followed_independently(follow_spy, prefs_file):
    r = a11y._follow_browser({"liveCaptions": True, "autoDescribe": False})

    assert {"liveCaptions": True} in follow_spy
    assert {"autoDescribe": False} in follow_spy
    assert r["autoDescribe"]["state"] == "off"


# ---- the profile is the only record ------------------------------------
# These used to be remembered in a file here, because the profile could not
# express liveCaptions and so could not carry an explicit "off". It names the
# setting now, so the decision lives at user-explicit in the profile — the tier
# resetToProfile forgets — and this reads it rather than keeping a copy.

def test_an_explicit_off_in_the_profile_is_honoured(follow_spy, prefs_file):
    """How "turn off live captions" survives the next sync."""
    r = a11y._follow_browser({"showCaptions": True, "liveCaptions": False})

    assert {"liveCaptions": False} in follow_spy
    assert r["liveCaptions"]["state"] == "off"


def test_the_profile_asking_for_it_turns_it_on(follow_spy, prefs_file):
    """And how resetToProfile gives it back: the record goes, this returns True."""
    a11y._follow_browser({"showCaptions": True, "liveCaptions": True})

    assert {"liveCaptions": True} in follow_spy


def test_a_setting_the_profile_never_mentions_is_left_alone(follow_spy, prefs_file):
    r = a11y._follow_browser({"fontScale": 150})

    assert follow_spy == []
    assert r["liveCaptions"] == {"state": "left alone"}


def test_captions_are_no_longer_inferred_from_the_page_caption_keys(follow_spy, prefs_file):
    """Inferring it meant owning state the profile never expressed, so an
    explicit off could not be recorded against it."""
    a11y._follow_browser({"showCaptions": True, "autoCaptions": True})

    assert follow_spy == []


def test_what_we_switched_on_is_still_switched_back_off(follow_spy, prefs_file):
    """Unrelated to preference: this is about the browser's prior state."""
    a11y._claim("autoDescribe", True)

    a11y._follow_browser({"fontScale": 150})

    assert {"autoDescribe": False} in follow_spy
