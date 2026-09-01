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

from . import (SERVICE_URL, _build_id, _bundle_source, _guarded, _js,
               a11y_service, a11y_sync, a11y_target)
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

# Typed "stop" arrives as a task, because the Controller sends everything
# unparsed straight through. Spawning a second agent to reason about the word
# while the first keeps driving the browser is the opposite of what was asked.
#
# Deliberately narrow: the WHOLE utterance must be a stop word. "stop the video"
# and "stop autoplay" are real instructions for the agent, and only an utterance
# that is nothing but the intent to halt is treated as one.
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


def _target_id_of(tab):
    """new_tab() returns a target id; other helpers hand back a dict."""
    return tab.get("targetId") if isinstance(tab, dict) else tab


def is_controller_tab(tid):
    """True when this tab is running the Controller widget.

    Checked by looking for the mounted widget, not by URL: the Controller is
    served from both localhost:4000 and 127.0.0.1:4000, and a host may mount it
    on any page. Driving it would have the agent act on the control surface
    instead of the content — and navigate away the very page the person is
    typing into.
    """
    try:
        return bool(js(
            # Two shapes over the same core: /controller mounts the widget, while
            # /chat builds its own window with createController and never calls
            # mountController — so there is no .aa-controller on it, and looking
            # only for that would let the agent navigate away the very chat the
            # person is typing into.
            "return !!(document.querySelector('.aa-controller')"
            " || (document.getElementById('composer-input')"
            "     && document.getElementById('transcript')))",
            target_id=tid))
    except Exception:
        return False  # a tab that will not answer is not a control surface


def _lost_browser(msg):
    """The daemon's CDP link to Chrome went away, as opposed to a page error."""
    return ("no close frame" in msg or "ConnectionClosed" in msg
            or "not connected" in msg or "connection is closed" in msg.lower())


def _log(msg):
    print(f"[control] {msg}", flush=True)


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
        # Raised by stop(); the worker checks it so a cancelled task reports
        # being stopped rather than going quiet or announcing a stale result.
        self._cancelled = threading.Event()
        self._task_running = False

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
        # Nothing left to drive: open a tab rather than fail every later call.
        try:
            self._target = _target_id_of(new_tab("about:blank"))
            _log("driven tab closed; opened a fresh one")
            return True
        except Exception as e:
            _log(f"driven tab closed and could not open another: {e}")
            self._target = None
            return False

    def _eval(self, expression, tries=4):
        """Evaluate in the driven tab, waiting out a renderer mid-relayout."""
        reacquired = False
        for attempt in range(tries):
            try:
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
        base = ["scroll", "activate", "back", "forward", "navigate", "search"]
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
            "platform": "browser-harness",
            "settingKeys": self._eval("return globalThis.__BH_A11Y.supportedKeys()"),
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

    def applySettings(self, changes, scope=None):
        self._ensure()
        if not isinstance(changes, dict) or not changes:
            return {"error": "no settings given"}

        before = self._eval("return globalThis.__BH_A11Y.activeSettings()") or {}
        previous = {k: before.get(k) for k in changes}

        result = self._eval("return globalThis.__BH_A11Y.apply(%s)" % json.dumps(changes))
        applied = {}
        for row in result.get("applied", []):
            for k in row.get("from", []):
                if k in changes:
                    applied[k] = changes[k]
        rejected = [s["setting"] for s in result.get("skipped", [])]
        rejected += [e["adapter"] for e in result.get("errors", [])]

        if not applied:
            return {"error": "nothing applied", "rejected": rejected}

        self._undo.append({k: previous[k] for k in applied})

        # A person stating a preference through the Controller is an explicit
        # choice, which is the one provenance the profile's strongest tier is for.
        target_scope = scope or self._persist_scope
        if target_scope:
            try:
                a11y_service("recordScopedSettings", target_scope, applied,
                             {"scopeLabel": "said in the controller"})
            except Exception as e:
                _log(f"not persisted: {e}")

        return {"applied": applied, "previous": previous, "rejected": rejected}

    def undoLast(self):
        if not self._undo:
            return {"error": "nothing to undo"}
        previous = self._undo.pop()
        self._ensure()
        self._eval("return globalThis.__BH_A11Y.revert(%s)" % json.dumps(previous))
        return {"reverted": previous, "remainingUndos": len(self._undo)}

    def resetUndo(self):
        self._undo.clear()
        return {"ok": True}

    def getContent(self, mode="outline", chunk=0):
        self._ensure()
        mode = "text" if mode == "text" else "outline"
        return self._eval("return globalThis.__BH_A11Y.content(%s, %d)"
                          % (json.dumps(mode), int(chunk or 0)))

    def performAction(self, actionId, target=None, text=None, meta=None):
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
            self._eval("location.assign(%s); return 1" % json.dumps(url))
            return {"ok": True, "detail": f"opening {url}"}

        if actionId == "search":
            q = (target or text or "").strip()
            if not q:
                return {"ok": False, "detail": "no search terms given"}
            # "search for apples on google" arrives as the whole phrase, because
            # the grammar captures everything after "search for". Honour a named
            # engine rather than searching for its name.
            engine, m = "google", re.search(r"\s+(?:on|in|with|using)\s+(\w+)\s*$", q, re.I)
            if m and m.group(1).lower() in ENGINES:
                engine = m.group(1).lower()
                q = q[:m.start()].strip()
            url = ENGINES[engine] + urllib.parse.quote_plus(q)
            self._eval("location.assign(%s); return 1" % json.dumps(url))
            return {"ok": True, "detail": f"searching {engine} for {q}"}

        if actionId == "stop":
            if not self._task_running:
                return {"ok": True, "detail": "nothing running"}
            self._cancelled.set()
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
            if self._task_running and _STOP_RE.match(utterance):
                _log(f"heard {utterance.strip()!r} as a stop, not a new task")
                return self.performAction("stop")
            # meta.returnToController defaults true (PROTOCOL.md); the person can
            # turn it off in the Controller when they would rather stay on the
            # page the task acted on.
            self._return_to_controller = (meta or {}).get("returnToController", True)
            return self._task(utterance)

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
        if self._notify:
            try:
                self._notify(str(text))
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
        if self._target:
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

        self._cancelled.clear()
        self._task_running = True

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
                        if self._cancelled.is_set():
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
                        # content, delta?}, {type:result, status}. The answer is
                        # the assistant's message content — the user role echoes
                        # the prompt back, so taking any message would report our
                        # own instructions as the result. Deltas accumulate.
                        if ev.get("type") == "message" and ev.get("role") == "assistant":
                            chunk = ev.get("content") or ""
                            text = (text or "") + chunk if ev.get("delta") else chunk
                        elif ev.get("type") == "result" and ev.get("status") not in (None, "success"):
                            text = text or f"the agent stopped: {ev.get('status')}"
                if self._cancelled.is_set():
                    _log("task stopped")
                    self._say("Stopped.")
                    return
                text = text or "the task finished"
                _log(f"task done: {json.dumps(text)[:160]}")
                self._say(text)
            except Exception as e:
                if self._cancelled.is_set():
                    _log("task stopped")
                    self._say("Stopped.")
                else:
                    _log(f"task failed: {e}")
                    self._say(f"That didn't work: {e}")
            finally:
                self._task_running = False

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
                    _log(f"note undeliverable ({e}) — controller likely gone")
            asyncio.run_coroutine_threadsafe(send(), loop)

        receiver._notify = notify
        if sync_on_connect:
            try:
                await asyncio.to_thread(a11y_target, target)
                r = await asyncio.to_thread(a11y_sync)
                _log(f"profile from {SERVICE_URL}: {list(r.get('settings') or {}) or 'none recorded'}")
            except Exception as e:
                _log(f"profile unavailable: {e}")
        try:
            await _handle(ws, receiver)
        finally:
            _log("controller disconnected")

    async with websockets.serve(handler, host, port, max_size=8 * 1024 * 1024):
        _log(f"listening on ws://{host}:{port}  (profile service: {SERVICE_URL})")
        _log("point the Controller at it: connectRemoteReceiver('ws://%s:%d')" % (host, port))
        await asyncio.Future()


def main(argv):
    host, port = "127.0.0.1", 9333
    persist_scope, sync, target = None, True, None
    it = iter(argv)
    for a in it:
        if a == "--port":
            port = int(next(it, port))
        elif a == "--host":
            host = next(it, host)
        elif a == "--persist":
            # e.g. --persist general | category:reference | origin:example.com
            persist_scope = next(it, None)
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
