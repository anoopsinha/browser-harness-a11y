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
