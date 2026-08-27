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
import time

from . import (SERVICE_URL, _build_id, _bundle_source, _guarded, _js,
               a11y_service, a11y_sync)
from ..helpers import cdp, current_tab, js

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

    def _capabilities(self):
        self._ensure()
        return {
            "platform": "browser-harness",
            "settingKeys": self._eval("return globalThis.__BH_A11Y.supportedKeys()"),
            "actions": ["scroll", "activate", "back", "forward"],
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
        if actionId in ("back", "forward"):
            self._eval(f"history.{actionId}(); return 1")
            return {"ok": True, "detail": f"went {actionId}"}
        return {"ok": False, "detail": f"unsupported action: {actionId}"}


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
