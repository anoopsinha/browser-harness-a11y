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
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import (SERVICE_URL, _build_id, _bundle_source, _guarded, _js,
               a11y_service, a11y_sync)
from ..helpers import cdp, current_tab, js

# The agent that executes anything the Controller's grammar could not resolve.
# browser-harness supplies capability and deliberately holds no model, so the
# deciding is done by an external agent — here the Gemini CLI service in the
# sibling browser-harness checkout, which drives this same browser through the
# harness skill. Unset AGENT_URL to leave `task` undeclared.
AGENT_URL = os.environ.get("BH_AGENT_URL", "http://127.0.0.1:8787/run")
AGENT_TOKEN_FILE = os.environ.get(
    "BH_AGENT_TOKEN_FILE",
    str(Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/projects"
                      "/browser-harness/extension-service/.token"))
# The agent may browse for minutes. The Controller gives up at 10s, so a task is
# acknowledged immediately and left running rather than held open.
AGENT_TIMEOUT = float(os.environ.get("BH_AGENT_TIMEOUT", "600"))

REQ = "aa-control-req"
RES = "aa-control-res"

# The Controller times out a request after 10s and shows the person an error, so
# anything slower than this should fail loudly rather than hang the UI.
CALL_TIMEOUT = 9.0


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

    def _eval(self, expression, tries=3):
        """Evaluate in the driven tab, waiting out a renderer mid-relayout."""
        for attempt in range(tries):
            try:
                if self._target:
                    return js(expression, target_id=self._target)
                return _js(expression)
            except RuntimeError as e:
                if "timed out" in str(e) and attempt < tries - 1:
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
        return base + (["task"] if self._agent_token() else [])

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

    def performAction(self, actionId, target=None, text=None):
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
            url = "https://duckduckgo.com/?q=" + urllib.parse.quote_plus(q)
            self._eval("location.assign(%s); return 1" % json.dumps(url))
            return {"ok": True, "detail": f"searching for {q}"}

        if actionId == "task":
            return self._task(text or target or "")

        if actionId in ("back", "forward"):
            self._eval(f"history.{actionId}(); return 1")
            return {"ok": True, "detail": f"went {actionId}"}
        return {"ok": False, "detail": f"unsupported action: {actionId}"}


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
                where = (f"The working tab is browser-harness targetId {self._target} "
                         f"(currently {url}). Call switch_tab('{self._target}') before "
                         f"anything else, and act only on that tab — never on the "
                         f"controller UI at :4000 or any other tab. ")
            except Exception:
                pass

        def run():
            body = json.dumps({
                "prompt": where + utterance,
                # Act on the page the person is already reading, do not open tabs.
                "tab_policy": "active",
            }).encode()
            req = urllib.request.Request(
                AGENT_URL, data=body, method="POST",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=AGENT_TIMEOUT) as r:
                    out = json.loads(r.read())
                _log(f"task done: {json.dumps(out.get('result'))[:160]}")
            except Exception as e:
                _log(f"task failed: {e}")

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
                reply["error"] = str(e)
        _log(f"{method}({json.dumps(args)[:60]}) -> "
             f"{json.dumps(reply.get('result', reply.get('error')))[:90]}")
        await ws.send(json.dumps(reply))


async def _serve(host, port, persist_scope, sync_on_connect, target):
    import websockets

    async def handler(ws):
        _log(f"controller connected from {ws.remote_address}")
        receiver = Receiver(persist_scope=persist_scope, target=target)
        if sync_on_connect:
            try:
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
            _log(f"driving tab: {tab.get('title', '')[:50]!r}")
        except Exception as e:
            _log(f"no tab to pin ({e}); will follow the current tab")
    try:
        asyncio.run(_serve(host, port, persist_scope, sync, target))
    except KeyboardInterrupt:
        pass
    return 0
