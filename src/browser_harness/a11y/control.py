"""Control-protocol receiver — the Controller drives this browser.

Implements toolkit/controller/PROTOCOL.md: a WebSocket endpoint speaking
`aa-control-req` / `aa-control-res`, with the seven ControlPort methods mapped
onto CDP. The Controller UI is the client; it connects out to us.

    browser-harness control --port 9333

A person types or speaks into the Controller; the Controller resolves that to a
settings change or an action against the vocabulary we declare, and it lands on
the page through the same adapter catalog a11y_apply uses. Their profile comes
from the Librarian service, so the Controller adapts *this person's* browser
rather than applying anonymous defaults.

The harness helpers are blocking (they do synchronous IPC to the daemon), so
every one of them is called through asyncio.to_thread — otherwise a single slow
relayout would stall the socket and the Controller would time out at 10s.
"""
import asyncio
import json
import re
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import (SERVICE_URL, _CHROME_CONTROLS, _WANTS_CAPTIONS, _build_id,
               _bundle_source, _follow_captions, _guarded, _js,
               a11y_chrome_apply, a11y_live_captions, a11y_service,
               a11y_sync, a11y_target)
from ..admin import ensure_daemon, restart_daemon
from ..helpers import cdp, current_tab, js, list_tabs, new_tab

# The agent that executes anything the Controller's grammar could not resolve.
# browser-harness supplies capability and deliberately holds no model, so the
# deciding is done by an external agent — here the Gemini CLI service in the
# sibling browser-harness checkout, which drives this same browser through the
# harness skill. Unset AGENT_URL to leave `task` undeclared.
# /stream rather than /run: the service tracks streaming subprocesses so POST
# /cancel can kill them, and it starts them in their own session so the whole
# group goes — gemini and the browser-harness child it spawned. /run is a
# blocking subprocess.run that nothing can interrupt, so a task started there
# cannot be stopped once it is moving.
AGENT_URL = os.environ.get("BH_AGENT_URL", "http://127.0.0.1:8787/stream")

# When the page under test lives in an iframe served by scripts/iframe-host,
# navigation is server state rather than a tab. Two browsers render that page —
# this machine's and the one on the hosted VM reached through the tunnel — and
# they are separate documents, so driving a tab here moves nothing on the
# tester's screen. Setting the shared URL moves both.
IFRAME_HOST = (os.environ.get("BH_IFRAME_HOST") or "").rstrip("/")

# Preferences are scoped by the page they are for, and at connect time the
# session may not have opened one yet. The host's own address stands in: it asks
# the Librarian for the general, un-scoped settings rather than none at all.
STATE_URL_FALLBACK = IFRAME_HOST or "http://127.0.0.1"
AGENT_CANCEL_URL = os.environ.get("BH_AGENT_CANCEL_URL", "http://127.0.0.1:8787/cancel")
AGENT_TOKEN_FILE = os.environ.get(
    "BH_AGENT_TOKEN_FILE",
    str(Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/projects"
                      "/browser-harness/extension-service/.token"))
# The agent may browse for minutes. The Controller gives up at 10s, so a task is
# acknowledged immediately and left running rather than held open.
AGENT_TIMEOUT = float(os.environ.get("BH_AGENT_TIMEOUT", "600"))

ENGINES = {
    "google": "https://www.google.com/search?q=",
    "duckduckgo": "https://duckduckgo.com/?q=",
    "ddg": "https://duckduckgo.com/?q=",
    "bing": "https://www.bing.com/search?q=",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search=",
}

# Engines that will not render inside a frame. Stripping X-Frame-Options gets
# the document delivered, but these blank themselves once they see they are
# framed — measured: title present, body empty. Bing renders normally, so it is
# the default while the page under test is a frame, and the substitution is said
# out loud rather than leaving someone reading an empty result page.
_FRAME_HOSTILE = {"google", "duckduckgo", "ddg"}
_FRAME_ENGINE = "bing"

# Typed "stop" arrives as a task, because the Controller sends everything
# unparsed straight through. Spawning a second agent to reason about the word
# while the first keeps driving the browser is the opposite of what was asked.
#
# Deliberately narrow: the WHOLE utterance must be a stop word. "stop the video"
# and "stop autoplay" are real instructions for the agent, and only an utterance
# that is nothing but the intent to halt is treated as one.
# Browser-level settings, recognised straight from the utterance. These reach
# here only when the Controller's grammar did not match — "turn off live
# captioning" misses its `captions?` — and the lane behind it is a model, which
# when asked to turn something off will report that it did. It has: Live Caption
# stayed on while the person was told it was off.
#
# So the settings we can read back are answered here instead, where the claim is
# checked before it is made. Everything else still goes to the agent.
_BROWSER_PHRASES = (
    (re.compile(r"\blive\s+caption(s|ing)?\b", re.I), "liveCaptions"),
    (re.compile(r"\bimage\s+description(s)?\b|\bdescribe\s+(the\s+)?images?\b", re.I),
     "autoDescribe"),
    (re.compile(r"\b(caret|text.cursor)\s*brows(e|ing)\b"
                r"|\bnavigate\b.*\btext\s+cursor\b", re.I), "caretBrowsing"),
    (re.compile(r"\bhide\s+profanit(y|ies)\b|\bprofanit(y|ies)\b", re.I), "hideProfanity"),
    (re.compile(r"\blive\s+translat(e|ion|ing)\b", re.I), "liveTranslate"),
)
# An imperative, not a mention: "turn off live captions" is an instruction,
# "what do live captions do" is a question and belongs to the agent.
_TURN_OFF_RE = re.compile(
    r"^\s*(please\s+)?(can you\s+)?(turn|switch|shut|put)?\s*"
    r"(off|no|stop|disable|hide|remove|kill)\b|\boff\s*[.!]?\s*$", re.I)
_TURN_ON_RE = re.compile(
    r"^\s*(please\s+)?(can you\s+)?(turn|switch|put)?\s*"
    r"(on|enable|start|show|activate|give me)\b|\bon\s*[.!]?\s*$", re.I)


def browser_setting_request(utterance):
    """(setting, wanted) when this plainly asks for one, else None."""
    for pattern, name in _BROWSER_PHRASES:
        if not pattern.search(utterance):
            continue
        if _TURN_OFF_RE.search(utterance):
            return name, False
        if _TURN_ON_RE.search(utterance):
            return name, True
        return None  # named without asking for a change: a question
    return None


# Questions about the page itself, which the receiver can answer from the page
# rather than by asking a model. The Controller's grammar has no rule for these,
# so they arrive as open-ended tasks — and where there is no agent to take them,
# "what is in the page" was refused by something that had the answer in hand.
_ASK_OUTLINE_RE = re.compile(
    r"\b(what(?:'s| is| are)?\s+(?:in|on)\s+(?:this|the)\s+page"
    r"|what(?:'s| is)\s+(?:this|the)\s+page\s+about"
    r"|what does (?:this|the) page say"
    r"|describe (?:this|the) page"
    r"|what(?:'s| is) here"
    r"|(?:the )?(?:headings?|sections?|outline)\b"
    r"|summar(?:y|ise|ize)\b)", re.I)
_ASK_TEXT_RE = re.compile(
    r"\bread\s+(?:me\s+)?(?:this|the|it)\b|\bread it (?:to me|out|aloud)\b"
    r"|\bwhat does it say\b", re.I)
_ASK_LINKS_RE = re.compile(
    r"\b(what|which)\s+(links?|buttons?)\b|\bwhat can i (click|press|activate)\b"
    r"|\bwhere can i go\b", re.I)


def page_question(utterance):
    """'outline', 'text', 'links' — or None when it is not about this page."""
    if _ASK_LINKS_RE.search(utterance):
        return "links"
    if _ASK_TEXT_RE.search(utterance):
        return "text"
    if _ASK_OUTLINE_RE.search(utterance):
        return "outline"
    return None


_STOP_RE = re.compile(
    r"^\s*(stop|stop it|stop that|stop please|please stop|cancel|abort|halt|"
    r"quit|never ?mind|forget it|enough)\s*[.!]*\s*$", re.I)

REQ = "aa-control-req"
RES = "aa-control-res"
# Unsolicited: what the receiver says when something finishes long after the
# request that started it. The protocol tells a client to ignore any kind it does
# not recognise, so emitting this is safe against a Controller that has not
# learned it yet — it simply lights up when one does.
NOTE = "aa-control-note"

# The Controller times out a request after 10s and shows the person an error, so
# anything slower than this should fail loudly rather than hang the UI.
CALL_TIMEOUT = 9.0

# muteAudio walks every tab, and the person is mid-sentence while it does. A
# browser with dozens of tabs must not push the round trip past the Controller's
# ten-second timeout, so the walk is bounded rather than exhaustive.
MUTE_TAB_LIMIT = int(os.environ.get("BH_MUTE_TAB_LIMIT", "25"))


def _target_id_of(tab):
    """new_tab() returns a target id; other helpers hand back a dict."""
    return tab.get("targetId") if isinstance(tab, dict) else tab


# Two shapes over the same core: /controller mounts the widget, while /chat
# builds its own window with createController and never calls mountController —
# so there is no .aa-controller on it, and looking only for that would let the
# agent navigate away the very chat the person is typing into.
#
# Kept as an expression rather than a statement so muteAudio can fold it into
# its own sweep and skip the control surface without a second round trip.
_IS_CONTROLLER_JS = (
    "!!(document.querySelector('.aa-controller')"
    " || (document.getElementById('composer-input')"
    "     && document.getElementById('transcript')))"
)


# One round trip per tab: js() answers a top-level `return` by letting Chrome
# reject it and retrying wrapped, and two round trips per tab is a poor trade
# while someone waits to speak. Returns -1 for the control surface, which
# silences its own audio and is where the person is dictating.
_MUTE_JS = """(() => {
  if (""" + _IS_CONTROLLER_JS + """) return -1;
  let n = 0;
  for (const m of document.querySelectorAll('audio, video')) {
    if (!m.paused) { try { m.pause(); n++; } catch (e) {} }
  }
  try { if (window.speechSynthesis) window.speechSynthesis.cancel(); } catch (e) {}
  return n;
})()"""


def is_controller_tab(tid):
    """True when this tab is running the Controller widget.

    Checked by looking for the mounted widget, not by URL: the Controller is
    served from both localhost:4000 and 127.0.0.1:4000, and a host may mount it
    on any page. Driving it would have the agent act on the control surface
    instead of the content — and navigate away the very page the person is
    typing into.
    """
    try:
        return bool(js("return " + _IS_CONTROLLER_JS, target_id=tid))
    except Exception:
        return False  # a tab that will not answer is not a control surface


def _lost_browser(msg):
    """The daemon's CDP link to Chrome went away, as opposed to a page error."""
    return ("no close frame" in msg or "ConnectionClosed" in msg
            or "not connected" in msg or "connection is closed" in msg.lower())


def _log(msg):
    print(f"[control] {msg}", flush=True)


# One agent, one browser, one process — so the task is process-wide, not per
# connection. A Receiver is built per WebSocket, and the chat reconnects (on
# refresh, and after a dropout), so per-instance state meant a task started on
# one connection was invisible to the Stop that arrived on the next: it answered
# "nothing running" while the agent was still driving.
#
# _notify is deliberately the LATEST connection rather than the one that started
# the task — a result posted to a socket that has since closed reaches nobody.
# Answers that had nowhere to go. A task brings the driven tab to the front, and
# the chat closes its socket while it is in the background — so the reply is
# ready at the exact moment there is nobody to hand it to, and the person is
# left watching "working on it". Held here and given to whoever connects next,
# which is the same chat a second later.
_PENDING_NOTES = []


def hold_note(text):
    """Keep an answer that could not be delivered."""
    _PENDING_NOTES.append(text)


async def flush_notes(ws):
    """Hand over anything held, oldest first. Returns how many landed.

    A failure puts the note back at the front rather than dropping it: losing it
    here is the same silence as before, one connection later.
    """
    sent = 0
    while _PENDING_NOTES:
        held = _PENDING_NOTES.pop(0)
        try:
            await ws.send(json.dumps({"kind": NOTE, "text": held}))
            sent += 1
            _log(f"delivered a held note: {held[:70]!r}")
        except Exception as e:
            _PENDING_NOTES.insert(0, held)
            _log(f"could not deliver the held note ({e})")
            break
    return sent


_TASK = {
    "running": False,
    "cancelled": threading.Event(),
    "notify": None,
}


class Receiver:
    """One ControlPort over one WebSocket connection."""

    def __init__(self, persist_scope=None, target=None):
        # The tab this Controller drives. The Controller UI runs in a tab of its
        # own, and opening it makes *it* current — so a receiver that followed
        # "current" would adapt the Controller instead of the page the person is
        # reading. Pinned once, switched to before every call.
        self._target = target
        # LIFO journal of {key: previous_value} maps, one per applySettings.
        self._undo = []
        # When set, an applySettings also records the change against the person's
        # profile at this scope, so a preference stated once outlives the tab.
        self._persist_scope = persist_scope
        self._sid = None
        # Set by the connection handler: deliver an unsolicited note.
        self._notify = None
        # The tab the Controller itself is in, so we can hand focus back when a
        # task finishes. Only browser-harness can do this — it owns the CDP
        # connection and it was the thing that moved focus away in the first
        # place. Resolved lazily: a WebSocket does not say which tab it came
        # from, so the Controller is found by its own widget in the DOM.
        self._controller_tab = None
        self._return_to_controller = True
        # Task state is shared — see _TASK.

    # ---- helpers ---------------------------------------------------------

    def _session(self):
        """A CDP session on the driven tab.

        Deliberately NOT switch_tab(): the daemon has one notion of "current
        tab", so a receiver that switched would yank the tab out from under any
        other harness client — including the script driving the Controller. A
        per-target session touches no shared state.
        """
        if not self._target:
            return None
        if self._sid is None:
            self._sid = cdp("Target.attachToTarget",
                            targetId=self._target, flatten=True)["sessionId"]
        return self._sid

    def _reacquire(self):
        """The driven tab is gone. Adopt another — never the Controller's own.

        A person closing the tab an agent was working in is ordinary, and until
        this the receiver stayed pinned to the dead target and failed every
        call forever with "No target with given id found".
        """
        self._sid = None
        # Prefer wherever the harness already is — that is the page the person
        # is most likely looking at — before falling back to any other tab.
        try:
            cur = current_tab()
            tid = cur.get("targetId")
            if tid and tid != self._controller_tab and not is_controller_tab(tid):
                self._target = tid
                _log(f"driven tab closed; now driving {(cur.get('title') or '')[:40]!r}")
                return True
        except Exception:
            pass
        for t in list_tabs(include_chrome=False):
            tid = t.get("targetId")
            if not tid or tid == self._controller_tab or is_controller_tab(tid):
                continue
            self._target = tid
            _log(f"driven tab closed; now driving {(t.get('title') or '')[:40]!r}")
            return True
        if IFRAME_HOST:
            # No tab is being driven in this mode, so there is nothing to
            # reacquire and a fresh one would just be a stray window nobody
            # asked for — which is exactly what kept appearing.
            _log("no tab to reacquire in iframe mode; the frame is the surface")
            return False
        # Nothing left to drive: open a tab rather than fail every later call.
        try:
            self._target = _target_id_of(new_tab("about:blank"))
            _log("driven tab closed; opened a fresh one")
            return True
        except Exception as e:
            _log(f"driven tab closed and could not open another: {e}")
            self._target = None
            return False

    def _in_frame(self, expression):
        """Run an expression inside the Framed page's iframe.

        Everything the receiver evaluates belongs to the page under test, which
        here is the frame — not the page holding it, and certainly not whichever
        tab happened to be pinned. Same-origin, because the proxy serves both,
        so the frame's own realm is reachable through its window; and `eval`
        works there because the proxy strips the page's CSP on the way past.
        """
        tid = self._iframe_viewer()
        if not tid:
            raise RuntimeError("no viewer open on this machine — open the "
                               f"Framed page ({IFRAME_HOST}/) in a tab here")
        wrapped = "(function(){%s})()" % expression
        return js("""(() => {
            const f = document.getElementById('frame');
            if (!f || !f.contentWindow) throw new Error('the Framed page has no frame');
            return f.contentWindow.eval(%s);
        })()""" % json.dumps(wrapped), target_id=tid)

    def _eval(self, expression, tries=4):
        """Evaluate in the driven tab, waiting out a renderer mid-relayout."""
        reacquired = False
        for attempt in range(tries):
            try:
                if IFRAME_HOST:
                    return self._in_frame(expression)
                if self._target:
                    return js(expression, target_id=self._target)
                return _js(expression)
            except Exception as e:
                msg = str(e)
                # The daemon's own CDP socket to Chrome can drop — Chrome
                # restarting, or the per-connection "Allow remote debugging"
                # prompt. It reconnects on the next call, so retry once rather
                # than surfacing a raw websocket error the person cannot act on.
                if _lost_browser(msg) and attempt < tries - 1:
                    # The daemon can be alive holding a dead socket to Chrome,
                    # and it does not reconnect on its own. A fresh CLI call
                    # would heal it — ensure_daemon() checks and respawns at
                    # process start — but this process is long-lived and ran
                    # that once, at startup. So do here what a new invocation
                    # would: stop the daemon and let the next call respawn it.
                    self._sid = None
                    if attempt == 0:
                        _log("lost the connection to Chrome; retrying")
                        time.sleep(1.0)
                    else:
                        _log("still lost; restarting the harness daemon")
                        try:
                            restart_daemon()
                            ensure_daemon()
                        except Exception as e:
                            _log(f"could not restart the daemon: {e}")
                        time.sleep(1.0)
                    continue
                if "No target with given id" in msg and not reacquired:
                    reacquired = True
                    if self._reacquire():
                        continue
                    raise RuntimeError("the tab being driven was closed") from None
                if "timed out" in msg and attempt < tries - 1:
                    time.sleep(1.0)
                    continue
                raise

    def _ensure(self):
        """Attach the catalog to the driven tab when it is missing or stale."""
        if IFRAME_HOST:
            # The proxy already put the catalog in the framed page. Injecting
            # from here would land in the tab, which in this mode is the page
            # holding the frame — or worse, whatever else was pinned.
            # `!!`, not `typeof`: typeof answers with the string "undefined",
            # which is perfectly truthy on this side of the wire, so the guard
            # never fired and a page without adapters looked fine.
            if not self._eval("return !!globalThis.__BH_A11Y"):
                raise RuntimeError("the framed page has no adapters — is it being "
                                   "served through the iframe host?")
            return
        if self._eval("return globalThis.__BH_A11Y_BUILD || null") == _build_id(_bundle_source()):
            return
        sid = self._session()
        source = _guarded(_bundle_source())
        cdp("Runtime.evaluate", session_id=sid, expression=source)
        _log(f"injected catalog into the driven tab ({len(source)} bytes)")

    def _agent_token(self):
        try:
            return Path(AGENT_TOKEN_FILE).read_text().strip()
        except OSError:
            return None

    def _actions(self):
        """What we can do. `task` is only offered when an agent is reachable —
        the Controller routes every unparsed utterance to it, so declaring it
        without a backend would swallow commands into a dead end."""
        base = ["scroll", "activate", "back", "forward", "navigate", "search",
                "muteAudio"]
        # `stop` is only meaningful where there is an agent to stop.
        return base + (["task", "stop"] if self._agent_token() else [])

    def _find_controller_tab(self):
        """The tab running the Controller widget — not the one we are driving."""
        for t in list_tabs(include_chrome=False):
            tid = t.get("targetId")
            if not tid or tid == self._target:
                continue
            if is_controller_tab(tid):
                return tid
        return None

    def _capabilities(self):
        self._ensure()
        return {
            "platform": "browser-harness-iframe" if IFRAME_HOST else "browser-harness",
            # liveCaptions is ours, not the toolkit's: Chrome captions any audio
            # on-device, which no page-level adapter can do. Advertised so the
            # Controller can offer it by name.
            # Chrome's own accessibility settings are ours to offer too: the
            # page-level catalog has no adapter for several of them, and for
            # autoDescribe no adapter that can actually run.
            "settingKeys": sorted(set(
                (self._eval("return globalThis.__BH_A11Y.supportedKeys()") or [])
                + list(_CHROME_CONTROLS))),
            "actions": self._actions(),
            "canReadContent": True,
            "targets": self._eval("return globalThis.__BH_A11Y.targets(40)"),
        }

    # ---- the seven methods ----------------------------------------------

    def describeCapabilities(self):
        return self._capabilities()

    def getContext(self):
        self._ensure()
        title = (self._eval("return document.title") or "").lstrip("\U0001F434 ").strip()
        return {
            "focus": title or None,
            "activeSettings": self._eval("return globalThis.__BH_A11Y.activeSettings()"),
            "capabilities": self._capabilities(),
        }

    def _record(self, settings, scope=None):
        """Write a stated preference into the profile, at user-explicit.

        The tier resetToProfile forgets. A browser-level setting has to land
        here like any other, or "back to my profile" cannot give it back — and
        keeping a private copy of the same fact would silently outvote the reset.
        """
        target = scope or self._persist_scope
        if not (target and settings):
            return
        try:
            a11y_service("recordScopedSettings", target, settings,
                         {"scopeLabel": "said in the controller"})
        except Exception as e:
            _log(f"not persisted: {e}")

    def applySettings(self, changes, scope=None):
        if IFRAME_HOST:
            return self._apply_iframe(changes, scope)
        self._ensure()
        if not isinstance(changes, dict) or not changes:
            return {"error": "no settings given"}

        # Chrome's Live Caption belongs to this platform, not to the toolkit —
        # the web surface has no adapter for it, so it would come back rejected.
        # Answered here instead, and copied rather than popped in place so the
        # caller's dict is left as they passed it.
        changes = dict(changes)
        asked = changes.pop("liveCaptions", None)
        browser = None
        if asked is not None:
            browser = a11y_live_captions(bool(asked))
            self._record({"liveCaptions": bool(asked)}, scope)
            if not changes:
                return {"applied": {"liveCaptions": bool(asked)}, "previous": {},
                        "rejected": [], "browser": browser}

        before = self._eval("return globalThis.__BH_A11Y.activeSettings()") or {}
        previous = {k: before.get(k) for k in changes}

        result = self._eval("return globalThis.__BH_A11Y.apply(%s)" % json.dumps(changes))
        applied = {}
        for row in result.get("applied", []):
            for k in row.get("from", []):
                if k in changes:
                    applied[k] = changes[k]
        # An adapter switched off is a change too, and the only evidence of it:
        # there is no "applied" row for something that stopped.
        for key in result.get("disabled", []):
            if key in changes:
                applied[key] = changes[key]
        rejected = [s["setting"] for s in result.get("skipped", [])]
        rejected += [e["adapter"] for e in result.get("errors", [])]

        # Whatever the page could not do, ask the browser about. Chrome has
        # accessibility of its own, and for some of these it is the only thing
        # that can help at all: autoDescribe comes back needs-ai from a toolkit
        # holding no model, while Chrome will describe the images with one.
        #
        # Only what the page could not do. These settings are browser-wide and
        # persist, so reaching for one where a page adapter already did the job
        # would change more of someone's browser than they asked for.
        chrome = {}
        # Anything Chrome knows about that the page did not actually apply —
        # not merely what it rejected. An off value never reaches the reject
        # list at all: the dispatcher looks for an adapter to stop, finds none
        # for a browser-level setting, and moves on silently. Keying off
        # `applied` catches turning one off as well as turning it on.
        fallback = {k: v for k, v in changes.items()
                    if k in _CHROME_CONTROLS and k not in applied}
        if fallback:
            try:
                for key, r in a11y_chrome_apply(**fallback).items():
                    if key == "unsupported":
                        continue
                    if r.get("state") in ("on", "off") and (r["state"] == "on") == bool(changes[key]):
                        applied[key] = changes[key]
                        if key in rejected:
                            rejected.remove(key)
                    chrome[key] = r
            except Exception as e:
                _log(f"chrome settings unreachable: {e}")

        # Turning a setting off produces no "applied" rows — there is no adapter
        # to report — so the browser has to be followed before this early
        # return, or "switch captions off" leaves Chrome still captioning.
        if asked is None:
            try:
                after = self._eval("return globalThis.__BH_A11Y.activeSettings()") or {}
                # They named a caption setting, so this is an instruction about
                # captions, not something inferred from a profile.
                browser = _follow_captions(
                    after, explicit=any(k in changes for k in _WANTS_CAPTIONS))
            except Exception as e:
                browser = {"live_captions": "failed", "detail": str(e)}

        if not applied:
            out = {"error": "nothing applied", "rejected": rejected}
            if browser:
                out["browser"] = browser
            if chrome:
                out["chrome"] = chrome
            return out

        self._undo.append({k: previous[k] for k in applied})

        # A person stating a preference through the Controller is an explicit
        # choice, which is the one provenance the profile's strongest tier is for.
        self._record(applied, scope)

        out = {"applied": applied, "previous": previous, "rejected": rejected}
        if browser:
            out["browser"] = browser
        if chrome:
            out["chrome"] = chrome
        return out

    def undoLast(self):
        if not self._undo:
            return {"error": "nothing to undo"}
        previous = self._undo.pop()
        self._ensure()
        self._eval("return globalThis.__BH_A11Y.revert(%s)" % json.dumps(previous))
        out = {"reverted": previous, "remainingUndos": len(self._undo)}
        # Undo has to reach the browser as well: undoing "captions off" put the
        # page adapter back while Chrome stayed silent, which is a worse state
        # than either of the two it was meant to move between.
        try:
            after = self._eval("return globalThis.__BH_A11Y.activeSettings()") or {}
            out["browser"] = _follow_captions(
                after, explicit=any(k in previous for k in _WANTS_CAPTIONS))
        except Exception as e:
            out["browser"] = {"live_captions": "failed", "detail": str(e)}
        return out

    def resetUndo(self):
        self._undo.clear()
        return {"ok": True}

    def getContent(self, mode="outline", chunk=0):
        self._ensure()
        mode = "text" if mode == "text" else "outline"
        return self._eval("return globalThis.__BH_A11Y.content(%s, %d)"
                          % (json.dumps(mode), int(chunk or 0)))

    def syncProfileToSession(self):
        """Resolve this person's settings and hand them to every viewer.

        The tab path applies the profile to a document. Here there is no single
        document that matters: each viewer holds its own copy of the page, so
        the profile becomes part of the session and every viewer applies it.
        """
        prefs = a11y_service("effectivePreferences", STATE_URL_FALLBACK) or {}
        settings = dict((prefs.get("settings") or {}))
        try:
            model = a11y_service("getAbilityModel")
        except Exception:
            model = None
        if model:
            resolved = self._in_frame(
                "return globalThis.__BH_A11Y.resolveWeb(%s, %s)"
                % (json.dumps(prefs), json.dumps(model))) or {}
            settings = resolved.get("settings") or settings
        if settings:
            self._iframe_post({"settings": settings})
        return {"settings": settings}

    def _iframe_post(self, body):
        req = urllib.request.Request(
            IFRAME_HOST + "/state", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}") or {}

    def _iframe_viewer(self):
        """A tab on this machine showing the host page, used only for reading.

        Every viewer renders the same URL with the same settings, so the content
        is the same wherever it is read. Writes still go through the host, or
        they would land on this copy alone — which is the one nobody is looking
        at.
        """
        tabs = list_tabs(include_chrome=False)
        port = urllib.parse.urlparse(IFRAME_HOST).port
        # The same server is "localhost", "127.0.0.1" and, through a tunnel,
        # something else again — so the port narrows the search and the page
        # itself settles it. Matching the configured URL as a string found
        # nothing while the viewer sat open under the other name.
        likely = [t for t in tabs if port and f":{port}" in (t.get("url") or "")]
        for t in (likely or tabs):
            tid = t.get("targetId")
            if not tid:
                continue
            try:
                if js("!!window.__BH_IFRAME_VIEWER", target_id=tid):
                    return tid
            except Exception:
                continue  # a tab that will not answer is not the viewer
        return None

    def _iframe_call(self, method, *args):
        """Call the bridge inside the local viewer's frame."""
        tid = self._iframe_viewer()
        if not tid:
            return {"error": "no viewer open on this machine — open the "
                              f"Framed page ({IFRAME_HOST}/) in a tab here"}
        return js("""(() => new Promise((res) => {
            const f = document.getElementById('frame');
            if (!f || !f.contentWindow) return res({error: 'no frame'});
            const id = 'r' + Math.random().toString(36).slice(2);
            const t = setTimeout(() => res({error: 'the frame did not answer'}), 6000);
            addEventListener('message', function h(e) {
              if (!e.data || e.data.kind !== 'bh-iframe-res' || e.data.id !== id) return;
              removeEventListener('message', h); clearTimeout(t);
              res(e.data.error ? {error: e.data.error} : e.data.result);
            });
            f.contentWindow.postMessage({kind: 'bh-iframe-req', id,
              method: %s, args: %s}, '*');
        }))()""" % (json.dumps(method), json.dumps(list(args))), target_id=tid)

    def _apply_iframe(self, changes, scope=None):
        """Broadcast settings to every viewer, rather than adapting a document here.

        Applying them locally is what the tab path does, and in this mode that
        document is on the wrong machine — the adapters would run where nobody
        is reading. The host holds them and each viewer applies them to its own
        frame.
        """
        if not isinstance(changes, dict) or not changes:
            return {"error": "no settings given"}
        try:
            state = self._iframe_post({"settings": changes})
        except Exception as e:
            return {"error": f"could not reach the iframe host at {IFRAME_HOST} — {e}"}
        _log(f"iframe host settings {json.dumps(changes)} (rev {state.get('rev')})")
        # Stated through the Controller, so it is a preference like any other.
        self._record(changes, scope)
        return {"applied": changes, "previous": {}, "rejected": [],
                "session": {"rev": state.get("rev"), "viewers": "all"}}

    def _navigate_iframe(self, url):
        """Point every viewer's frame at a page, by asking the host to move.

        Not a tab navigation: the person reading this is on another machine,
        watching their own copy of the host page through the tunnel. The only
        thing both copies share is the server.
        """
        try:
            rev = self._iframe_post({"url": url}).get("rev")
            _log(f"iframe host -> {url} (rev {rev})")
            return {"ok": True, "detail": f"opening {url}"}
        except Exception as e:
            return {"ok": False,
                    "detail": f"could not reach the iframe host at {IFRAME_HOST} — {e}"}

    def _answer_about_page(self, kind):
        """Answer a question about the page from the page.

        Reading is the one thing this mode is unambiguously good at, and the
        answer is the page's own structure rather than a model's account of it.
        """
        try:
            if kind == "links":
                names = self._eval("return globalThis.__BH_A11Y.targets(25)") or []
                if not names:
                    return {"ok": True, "detail": "I cannot find anything to activate here."}
                return {"ok": True,
                        "detail": f"{len(names)} things you can activate: "
                                  + ", ".join(names[:15])
                                  + ("…" if len(names) > 15 else "")}
            r = self.getContent("text" if kind == "text" else "outline") or {}
            if r.get("error"):
                return {"ok": True, "detail": "There is no readable content on this page."}
            title = (r.get("title") or "this page").strip()
            if kind == "text":
                body = (r.get("text") or "").strip()
                more = ""
                if (r.get("totalChunks") or 1) > 1:
                    more = f" (part 1 of {r['totalChunks']}; say 'read more' to go on)"
                return {"ok": True, "detail": f"{title}. {body}{more}"}
            heads = r.get("outline") or []
            if not heads:
                return {"ok": True, "detail": f"{title}. It has no headings to move between."}
            return {"ok": True,
                    "detail": f"{title}. {len(heads)} sections: " + ", ".join(heads[:20])
                              + ("…" if len(heads) > 20 else "")}
        except Exception as e:
            return {"ok": False, "detail": f"I could not read the page — {e}"}

    def performAction(self, actionId, target=None, text=None, meta=None):
        # Answered before _ensure(): silencing the room must not wait on the
        # toolkit bundle being injected into a heavy page, nor fail with it.
        if actionId == "muteAudio":
            # Fired when voice input starts, so the microphone does not
            # transcribe whatever is playing. The chat can only reach its own
            # tab; this receiver owns the browser, so it silences the rest.
            #
            # Pause rather than mute: a muted video still advances, so the person
            # dictates over a clip that has moved on by the time they look back.
            # speechSynthesis is cancelled too — the loudest thing in the room is
            # often the Controller reading a result aloud.
            paused = reached = 0
            for t in list_tabs(include_chrome=False)[:MUTE_TAB_LIMIT]:
                tid = t.get("targetId")
                if not tid:
                    continue
                try:
                    n = int(js(_MUTE_JS, target_id=tid) or 0)
                    if n < 0:
                        continue  # the control surface, skipped inside the sweep
                    reached += 1
                    paused += n
                except Exception:
                    continue  # a tab that will not answer is not worth the wait
            _log(f"muteAudio: paused {paused} across {reached} tabs")
            return {"ok": True, "detail": f"paused {paused} in {reached} tabs"}

        self._ensure()
        if actionId == "scroll":
            where = (target or "down").lower()
            self._eval("window.scrollTo({top: %s, behavior: 'instant'}); return 1" % {
                "top": "0", "bottom": "document.body.scrollHeight",
                "up": "window.scrollY - window.innerHeight*0.8",
            }.get(where, "window.scrollY + window.innerHeight*0.8"))
            return {"ok": True, "detail": f"scrolled {where}"}
        if actionId == "activate":
            return self._eval("return globalThis.__BH_A11Y.activate(%s)" % json.dumps(target or ""))
        if actionId == "navigate":
            url = (target or text or "").strip()
            if not url:
                return {"ok": False, "detail": "no address given"}
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            if IFRAME_HOST:
                return self._navigate_iframe(url)
            self._eval("location.assign(%s); return 1" % json.dumps(url))
            return {"ok": True, "detail": f"opening {url}"}

        if actionId == "search":
            q = (target or text or "").strip()
            if not q:
                return {"ok": False, "detail": "no search terms given"}
            # "search for apples on google" arrives as the whole phrase, because
            # the grammar captures everything after "search for". Honour a named
            # engine rather than searching for its name.
            default = _FRAME_ENGINE if IFRAME_HOST else "google"
            engine, m = default, re.search(r"\s+(?:on|in|with|using)\s+(\w+)\s*$", q, re.I)
            asked_for = None
            if m and m.group(1).lower() in ENGINES:
                engine = asked_for = m.group(1).lower()
            swapped = None
            if IFRAME_HOST and engine in _FRAME_HOSTILE:
                swapped, engine = engine, _FRAME_ENGINE
                q = q[:m.start()].strip()
            url = ENGINES[engine] + urllib.parse.quote_plus(q)
            if IFRAME_HOST:
                # Not location.assign: _eval now runs inside the frame, so that
                # would steer this viewer's frame straight at the site, around
                # the proxy — arriving unadapted, framing headers back in force,
                # and every other viewer left behind.
                r = self._navigate_iframe(url)
                if not r.get("ok"):
                    return r
                said = f"searching {engine} for {q}"
                if swapped and asked_for:
                    said += f" — {swapped} will not display in a frame"
                return {**r, "detail": said}
            self._eval("location.assign(%s); return 1" % json.dumps(url))
            return {"ok": True, "detail": f"searching {engine} for {q}"}


        if actionId == "stop":
            if not _TASK["running"]:
                return {"ok": True, "detail": "nothing running"}
            _TASK["cancelled"].set()
            token = self._agent_token()
            killed = 0
            try:
                req = urllib.request.Request(
                    AGENT_CANCEL_URL, data=b"{}", method="POST",
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    killed = (json.loads(r.read()) or {}).get("killed", 0)
            except Exception as e:
                _log(f"cancel failed: {e}")
                return {"ok": False, "detail": f"could not stop the agent: {e}"}
            _log(f"stop requested; killed {killed}")
            return {"ok": True, "detail": "stopping"}

        if actionId == "task":
            utterance = text or target or ""
            # Only while something is running: with nothing to halt, "stop" is
            # more likely a real instruction ("stop autoplay") and belongs to the
            # agent.
            if _TASK["running"] and _STOP_RE.match(utterance):
                _log(f"heard {utterance.strip()!r} as a stop, not a new task")
                return self.performAction("stop")
            hit = browser_setting_request(utterance)
            if hit:
                name, want = hit
                r = a11y_chrome_apply(**{name: want}).get(name, {})
                state, label = r.get("state"), _CHROME_CONTROLS[name]
                _log(f"answered {utterance.strip()!r} directly: {name} -> {state}")
                if state in ("on", "off") and (state == "on") == want:
                    self._record({name: want})
                    return {"ok": True,
                            "detail": f"{label} is {'on' if want else 'off'}"}
                # Say what is actually true. The person may have no way to look.
                return {"ok": False,
                        "detail": r.get("detail")
                                  or f"could not change {label}; it is {state}"}

            # meta.returnToController defaults true (PROTOCOL.md); the person can
            # turn it off in the Controller when they would rather stay on the
            # page the task acted on.
            self._return_to_controller = (meta or {}).get("returnToController", True)
            return self._task(utterance)

        if IFRAME_HOST and actionId in ("activate", "scroll", "back", "forward"):
            try:
                self._iframe_post({"action": {"id": actionId, "target": target}})
            except Exception as e:
                return {"ok": False, "detail": f"could not reach the iframe host — {e}"}
            return {"ok": True, "detail": f"{actionId} {target or ''}".strip()}

        if actionId in ("back", "forward"):
            self._eval(f"history.{actionId}(); return 1")
            return {"ok": True, "detail": f"went {actionId}"}
        return {"ok": False, "detail": f"unsupported action: {actionId}"}


    def _say(self, text):
        """Deliver a late result to the person, if the transport can carry it.

        Focus goes back to the Controller first, so the announcement lands where
        the person is looking rather than in a tab they have navigated away from.
        """
        if self._return_to_controller:
            if self._controller_tab is None:
                self._controller_tab = self._find_controller_tab()
            if self._controller_tab:
                try:
                    cdp("Target.activateTarget", targetId=self._controller_tab)
                    _log("returned focus to the controller tab")
                except Exception as e:
                    _log(f"could not return focus: {e}")
        notify = _TASK["notify"] or self._notify
        if notify:
            try:
                notify(str(text))
            except Exception as e:
                _log(f"could not deliver: {e}")


    def _task(self, utterance):
        """Hand an arbitrary instruction to the agent, and return at once.

        A real task takes far longer than the Controller's 10s timeout, so the
        person gets an immediate acknowledgement and the work continues in the
        background. Holding the socket open would guarantee a timeout and tell
        them it failed while it was still working.
        """
        utterance = (utterance or "").strip()
        if not utterance:
            return {"ok": False, "detail": "nothing to do"}
        token = self._agent_token()
        if not token:
            return {"ok": False, "detail": "no agent configured"}

        # Name the tab explicitly. "active" means whatever has focus, and the
        # Controller runs in a tab of its own — so an agent following focus reads
        # the Controller instead of the page the person is on, and answers
        # confidently about the wrong document.
        where = ""
        if IFRAME_HOST:
            # The page is inside a frame served from localhost, and the person
            # is reading their own copy of it somewhere else. Reading the frame
            # here is fine — every viewer renders the same page — but a
            # navigation has to go through the host or it moves this screen
            # alone, around the proxy, and theirs stays where it was.
            viewer = self._iframe_viewer()
            where = (
                f"WORKING PAGE: the page under test is inside an iframe on "
                f"{IFRAME_HOST}/ "
                + (f"(browser-harness targetId {viewer}; call switch_tab('{viewer}') "
                   f"first). " if viewer else ". ")
                + f"Read it with: js(\"document.getElementById('frame')"
                  f".contentDocument.body.innerText\") — the frame is same-origin "
                f"with its holder, so its document is reachable that way. "
                f"To open a different page, do NOT navigate and do NOT call "
                f"new_tab: post the address to the host instead, which moves "
                f"every viewer including the person's — "
                f"curl -s -X POST {IFRAME_HOST}/state "
                f"-H 'Content-Type: application/json' "
                f"-d '{{\"url\":\"https://example.com\"}}' — then re-read the "
                f"frame. Never navigate the holder page itself. "
                # The agent reaches for Google unprompted, and Google is one of
                # the pages that blanks itself once it sees it is framed: a real
                # title over an empty body, which reads as the tool being broken.
                f"To search, use Bing — https://www.bing.com/search?q=... — and "
                f"NOT Google or DuckDuckGo, which return an empty page inside a "
                f"frame. ")
        elif self._target:
            try:
                url = self._eval("return location.href")
                where = (
                    f"WORKING TAB: browser-harness targetId {self._target} "
                    f"(currently {url}). Call switch_tab('{self._target}') first "
                    f"and do ALL work in that tab. "
                    # The skill tells agents "first navigation is new_tab(url)".
                    # Here that is wrong and has to be overridden explicitly: the
                    # tab already exists and is positioned beside the person's
                    # Controller, so a new one lands in the wrong window and they
                    # lose sight of what is happening.
                    f"Navigate with goto_url(...) — do NOT call new_tab(). The "
                    f"skill's \"first navigation is new_tab\" rule does not apply: "
                    f"this tab already exists and sits beside the person's "
                    f"Controller, and a new tab opens in the wrong window where "
                    f"they cannot see it. "
                    f"Never act on a tab running the accessibility Controller — "
                    f"that is the control surface they are typing into, and "
                    f"navigating it away ends the session. "
                    f"Only if switch_tab fails because that tab is gone, open one "
                    f"with new_tab(). ")
            except Exception:
                pass

        _TASK["cancelled"].clear()
        _TASK["running"] = True

        def run():
            body = json.dumps({
                "prompt": where + utterance,
                # Deliberately NO tab_policy. The service's "active" policy says
                # to operate on "the page they are viewing right now" — and after
                # returnToController that is the Controller itself, so the agent
                # was being told to act on the control surface. The WORKING TAB
                # line above is the only tab instruction, and it is explicit.
            }).encode()
            req = urllib.request.Request(
                AGENT_URL, data=body, method="POST",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"})
            try:
                text = None
                with urllib.request.urlopen(req, timeout=AGENT_TIMEOUT) as r:
                    # NDJSON: one gemini event per line. The answer is whichever
                    # object last carried a "response"; the rest is progress.
                    for raw in r:
                        if _TASK["cancelled"].is_set():
                            break
                        line = raw.decode("utf-8", "replace").strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(ev, dict):
                            continue
                        # stream-json events: {type:init}, {type:message, role,
                        # content, delta?}, {type:tool_use}, {type:tool_result},
                        # {type:result, status}.
                        #
                        # The answer is the assistant's LAST run of messages. It
                        # narrates before each tool call ("I will use
                        # browser-harness to..."), so accumulating everything
                        # returned the whole transcript of its working instead of
                        # the result. Resetting at each tool call keeps only what
                        # it said after the final one. The user role is skipped:
                        # it echoes our own prompt back.
                        kind = ev.get("type")
                        if kind in ("tool_use", "tool_result"):
                            text = None
                        elif kind == "message" and ev.get("role") == "assistant":
                            chunk = ev.get("content") or ""
                            text = (text or "") + chunk if ev.get("delta") else chunk
                        elif kind == "result" and ev.get("status") not in (None, "success"):
                            text = text or f"the agent stopped: {ev.get('status')}"
                if _TASK["cancelled"].is_set():
                    _log("task stopped")
                    self._say("Stopped.")
                    return
                text = (text or "").strip() or "the task finished"
                _log(f"task done: {json.dumps(text)[:160]}")
                self._say(text)
            except Exception as e:
                if _TASK["cancelled"].is_set():
                    _log("task stopped")
                    self._say("Stopped.")
                else:
                    _log(f"task failed: {e}")
                    self._say(f"That didn't work: {e}")
            finally:
                _TASK["running"] = False

        threading.Thread(target=run, daemon=True).start()
        _log(f"task started: {utterance!r}")
        return {"ok": True, "detail": f"working on: {utterance}"}


METHODS = ("describeCapabilities", "getContext", "applySettings", "undoLast",
           "resetUndo", "getContent", "performAction")


async def _handle(ws, receiver):
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        # Ignore any message whose kind we don't recognise, per the protocol.
        if not isinstance(msg, dict) or msg.get("kind") != REQ:
            continue

        method, args = msg.get("method"), msg.get("args") or []
        reply = {"kind": RES, "id": msg.get("id")}
        if method not in METHODS:
            reply["result"] = {"error": f"unknown method: {method}"}
        else:
            try:
                fn = getattr(receiver, method)
                reply["result"] = await asyncio.wait_for(
                    asyncio.to_thread(fn, *args), timeout=CALL_TIMEOUT)
            except asyncio.TimeoutError:
                # Never drop a request: a timeout is an answer the Controller
                # can show, a silence is one it can only wait out.
                reply["error"] = f"{method} timed out after {CALL_TIMEOUT}s"
            except Exception as e:
                reply["error"] = (
                    "lost the connection to Chrome and could not recover — "
                    "say it again, or check for an \u201cAllow remote "
                    "debugging\u201d prompt in the browser"
                    if _lost_browser(str(e)) else str(e))
        _log(f"{method}({json.dumps(args)[:60]}) -> "
             f"{json.dumps(reply.get('result', reply.get('error')))[:90]}")
        await ws.send(json.dumps(reply))


async def _serve(host, port, persist_scope, sync_on_connect, target):
    import websockets

    async def handler(ws):
        _log(f"controller connected from {ws.remote_address}")
        receiver = Receiver(persist_scope=persist_scope, target=target)

        # A task finishes on a worker thread, long after the request that began
        # it has been answered. Hand the receiver a way back onto the socket, via
        # the loop, so the person hears the outcome instead of only the promise.
        loop = asyncio.get_running_loop()

        def notify(text):
            async def send():
                try:
                    await ws.send(json.dumps({"kind": NOTE, "text": text}))
                    _log(f"note sent: {text[:80]!r}")
                except Exception as e:
                    hold_note(text)
                    _log(f"note held for the next connection ({e})")
            asyncio.run_coroutine_threadsafe(send(), loop)

        receiver._notify = notify
        # Later connections take over delivery, so a task that outlives a
        # refresh still reports to whoever is actually listening.
        _TASK["notify"] = notify

        await flush_notes(ws)
        # Never before _handle. This runs on the connection's own coroutine, so
        # anything slow here — the profile service, a frame mid-load — happens
        # while the chat's requests sit unread, and the chat simply hangs. In
        # iframe mode there is nothing to do anyway: the session was adapted at
        # startup, and re-pushing it on every reconnect only churns the viewers.
        async def _sync_later():
            """Bring the driven tab in line with the profile, out of band."""
            try:
                await asyncio.to_thread(a11y_target, target)
                r = await asyncio.to_thread(a11y_sync)
                # The browser-level part is logged too: it is the only place a
                # failure to follow the profile into Chrome's own settings would
                # otherwise be visible.
                _log(f"profile from {SERVICE_URL}: "
                     f"{list(r.get('settings') or {}) or 'none recorded'}"
                     f" | browser: {json.dumps(r.get('browser'))}")
            except Exception as e:
                _log(f"profile unavailable: {e}")

        # Not awaited, and not run at all in iframe mode. Awaiting it would
        # hold the connection's coroutine before _handle starts reading, so a
        # slow profile service or a frame mid-load leaves the chat hanging with
        # its requests unread. In iframe mode there is nothing to do anyway: the
        # session was adapted at startup, and re-pushing it on every reconnect
        # only churns every viewer.
        if sync_on_connect and not IFRAME_HOST:
            asyncio.create_task(_sync_later())
        try:
            await _handle(ws, receiver)
        finally:
            _log("controller disconnected")

    async with websockets.serve(handler, host, port, max_size=8 * 1024 * 1024):
        _log(f"listening on ws://{host}:{port}  (profile service: {SERVICE_URL})")
        _log("point the Controller at it: connectRemoteReceiver('ws://%s:%d')" % (host, port))

        if IFRAME_HOST and sync_on_connect:
            # Adapt the session before anyone joins. The chat is opened on
            # another machine here, so waiting for it would mean the first thing
            # the person meets is an unadapted page — and they may be the least
            # able to work around it.
            try:
                r = await asyncio.to_thread(
                    Receiver(persist_scope=persist_scope, target=target)
                    .syncProfileToSession)
                _log(f"session ready: "
                     f"{list(r.get('settings') or {}) or 'nothing recorded'}")
            except Exception as e:
                _log(f"could not prepare the session ({e}); "
                     f"the first chat to connect will do it")

        await asyncio.Future()


def main(argv):
    host, port = "127.0.0.1", 9333
    # Recording defaults ON, at the broadest scope. It was None, which meant the
    # "said in the controller" write below never ran for anything — a spoken
    # preference lasted until the next sync and then vanished, with no record in
    # the profile for resetToProfile to give back. --persist narrows it.
    persist_scope, sync, target = "general", True, None
    it = iter(argv)
    for a in it:
        if a == "--port":
            port = int(next(it, port))
        elif a == "--host":
            host = next(it, host)
        elif a == "--persist":
            # e.g. --persist general | category:reference | origin:example.com
            persist_scope = next(it, None)
        elif a == "--no-persist":
            persist_scope = None
        elif a == "--no-sync":
            sync = False
        elif a == "--target":
            target = next(it, None)
        else:
            print(f"control: unknown argument {a}", flush=True)
            return 2
    if target is None:
        # Pin to whatever is current at startup — the page the person is on,
        # before the Controller opens a tab of its own.
        try:
            tab = current_tab()
            target = tab["targetId"]
            if is_controller_tab(target):
                # Starting the receiver while the Controller has focus must not
                # make the Controller the thing we drive.
                target = _target_id_of(new_tab("about:blank"))
                _log("current tab is the Controller; opened a fresh tab to drive")
            else:
                _log(f"driving tab: {tab.get('title', '')[:50]!r}")
        except Exception as e:
            _log(f"no tab to pin ({e}); will follow the current tab")
    try:
        asyncio.run(_serve(host, port, persist_scope, sync, target))
    except KeyboardInterrupt:
        pass
    return 0
