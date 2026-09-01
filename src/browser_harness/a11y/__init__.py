"""Ability-adaptive browsing: the AI for Accessibility Toolkit, driven over CDP.

Two things live here. `a11y_attach` puts the toolkit's adapter catalog into the
page and `a11y_profile` / `a11y_apply` / `a11y_sync` render a person's needs into
it. `a11y_snapshot` / `a11y_audit` read the page back the way a screen reader
would, so the agent plans on what the person can actually reach rather than on
pixels alone.

The bundle is generated — run `python3 scripts/build_a11y.py` first.
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..helpers import cdp, current_tab, js, page_info
from ..paths import runtime_dir

BUNDLE = Path(__file__).with_name("bundle.js")

# Requests are one JSON line; _ipc raised the daemon's stream limit to cover a
# bundle this size, but a daemon from an unpatched install still caps at 64 KiB.
# Injection falls back to chunk-and-eval there rather than dying on a broken pipe.
_CHUNK = 20_000

SERVICE_URL = os.environ.get("AI4A11Y_URL", "http://localhost:8080")

# AX properties worth carrying into a snapshot line. The rest are noise for a
# reader deciding what a person can do on this page.
_AX_FLAGS = ("level", "checked", "selected", "expanded", "disabled", "required",
             "pressed", "focused", "invalid", "readonly")

# Roles that carry no information on their own; their children are re-parented.
_AX_SKIP = {"none", "generic", "GenericContainer"}

# Layout artefacts. InlineTextBox splits a run of text at line-wrap boundaries,
# so it repeats the words above it a fragment at a time — always noise, even
# when named. Dropped outright rather than gated on having a name.
_AX_NOISE = {"InlineTextBox", "LineBreak"}


def _bundle_source():
    if not BUNDLE.exists():
        raise RuntimeError(
            "a11y bundle not built — run: python3 scripts/build_a11y.py"
        )
    return BUNDLE.read_text(encoding="utf-8")


def _evaluate(source, session_id=None):
    """Runtime.evaluate the source, chunking if the daemon's IPC limit is low."""
    try:
        return cdp("Runtime.evaluate", session_id=session_id, expression=source)
    except (BrokenPipeError, ConnectionResetError):
        cdp("Runtime.evaluate", session_id=session_id, expression="globalThis.__bh_load='';")
        for i in range(0, len(source), _CHUNK):
            part = json.dumps(source[i:i + _CHUNK])
            cdp("Runtime.evaluate", session_id=session_id,
                expression=f"globalThis.__bh_load += {part};")
        return cdp("Runtime.evaluate", session_id=session_id,
                   expression="(0,eval)(globalThis.__bh_load); delete globalThis.__bh_load;")


# The JS call that re-applies this person's settings on a fresh document, or
# None while nothing is set. Registering only the catalog would put the tools on
# every page but leave each one unadapted — the settings have to travel too.
_sticky_call = None

# A new-document script outlives the process that registered it: it belongs to
# the daemon's session, and every CLI invocation is a fresh Python process. Left
# untracked, each run adds another copy of the bundle to every future navigation
# until the page crawls. The identifiers are therefore kept on disk, per target,
# so a later run can retract what an earlier one left behind.
_SCRIPTS_FILE = runtime_dir() / "a11y-scripts.json"

# Re-applying the profile on every new document is the behaviour we want: a
# profile that lapses when the person follows a link is not a profile.
#
# It replays the settings that were last applied, not a fresh per-site sync, so
# a11y_sync() should be called again when the site category changes — the
# service scopes some preferences to a category and those will not follow a
# navigation off it.
_sticky_enabled = True

# The tab the settings belong to. A long-lived receiver drives one tab while the
# daemon's "current tab" wanders — so registering the sticky script against
# current_tab() put it on whatever happened to be in front, and the tab the
# person was actually using got nothing. Set by a host that pins a target.
_driven_target = None


def a11y_target(target_id=None):
    """Pin which tab sticky settings are registered against."""
    global _driven_target
    _driven_target = target_id
    return _driven_target


def a11y_sticky(enabled=True):
    """Re-apply the current settings automatically on every new page.

    On by default. Turn it off to adapt each page explicitly.
    """
    global _sticky_enabled
    _sticky_enabled = bool(enabled)
    if not enabled:
        _stick(None)
    return {"sticky": _sticky_enabled}


def _script_registry():
    try:
        return json.loads(_SCRIPTS_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _persist():
    """(Re)register the new-document script: catalog, plus the settings if any."""
    cdp("Page.enable")
    target = _driven_target or current_tab()["targetId"]
    registry = _script_registry()
    for stale in registry.get(target, []):
        try:
            cdp("Page.removeScriptToEvaluateOnNewDocument", identifier=stale)
        except Exception:
            pass  # registered by a session that has since gone; nothing to undo

    ident = cdp("Page.addScriptToEvaluateOnNewDocument",
                source=_document_script()).get("identifier")

    registry[target] = [ident] if ident else []
    # Every tab ever touched left an entry; this had grown to 27 for one
    # session. Only the recent ones can still be retracted, and a stale id is
    # harmless to forget.
    if len(registry) > 12:
        registry = dict(list(registry.items())[-12:])
    _SCRIPTS_FILE.write_text(json.dumps(registry))
    return ident


def _build_id(source):
    """Identity of this bundle build, so a page can tell stale from current."""
    return hashlib.sha1(source.encode()).hexdigest()[:12]


def _guarded(source):
    """Skip the bundle only when the page already has THIS build of it.

    Guarding on existence alone means a page that loaded an older build never
    upgrades — the new functions are simply missing, and the failure surfaces far
    away as "x is not a function".
    """
    build = json.dumps(_build_id(source))
    return (f"if (globalThis.__BH_A11Y_BUILD !== {build}) {{\n"
            + source
            + f"\nglobalThis.__BH_A11Y_BUILD = {build};\n}}")


def _document_script():
    """The whole payload, deferred until the page has finished building itself.

    A new-document script runs before anything is parsed — document.body is still
    null, so the toolkit's shared MutationObserver cannot even attach. DOMContent-
    Loaded is not late enough either: adapters that rewrite text nodes then race
    the site's own initialisation, and on a large Wikipedia article that reliably
    locks the renderer about a second and a half later. Waiting for `load` costs a
    brief flash of unadapted page and buys a profile that does not hang it.
    """
    body = _guarded(_bundle_source())
    if _sticky_call:
        body += f"\ntry{{ {_sticky_call} }}catch(e){{ console.error('[bh-a11y]', e); }}"
    return ("(function(){if(window.top!==window)return;"
            "var go=function(){var t0=performance.now();"
            "globalThis.__BH_A11Y_RUNS=(globalThis.__BH_A11Y_RUNS||0)+1;" + body +
            "globalThis.__BH_A11Y_MS=Math.round(performance.now()-t0);};"
            # A hidden tab barely runs requestIdleCallback, so a profile applied
            # in a background tab would simply never arrive. setTimeout is
            # throttled there but still fires.
            "var soon=function(){setTimeout(go,0);};"
            "if(document.readyState==='complete')soon();"
            "else addEventListener('load',soon,{once:true});"
            "})();")


def a11y_attach(persist=True):
    """Inject the adapter catalog into the current page.

    persist=True also registers it with Page.addScriptToEvaluateOnNewDocument, so
    it survives navigation and lands in frames created afterwards, carrying
    whatever settings are currently set. Iframes that already exist when this is
    called do not get it — attach before navigating.
    """
    source = _bundle_source()
    out = {"bytes": len(source)}
    if persist:
        out["persisted_as"] = _persist()
    r = _evaluate(_guarded(source))
    if r.get("exceptionDetails"):
        raise RuntimeError("a11y bundle failed to load: "
                           + r["exceptionDetails"].get("text", "unknown"))
    out["adapters"] = _js("return Object.keys(globalThis.__BH_A11Y.adapters).length")
    return out


def _js(expression, patience=45.0):
    """js(), but tolerant of a renderer that is mid-relayout.

    Applying a heavy profile to a large document queues a very large layout, and
    while it runs the renderer answers nothing — Runtime.evaluate, page_info and
    screenshots all time out together. The work does finish; the harness's 5s CDP
    timeout is just shorter than the pause. Retry rather than report a failure
    that is really a wait.
    """
    deadline = time.monotonic() + patience
    last = None
    while True:
        try:
            return js(expression)
        except RuntimeError as e:
            if "timed out" not in str(e) or time.monotonic() >= deadline:
                raise
            last = e
            time.sleep(0.5)
    raise last  # unreachable; kept so the intent is explicit


def _settle(patience=45.0):
    """Return once the renderer answers again.

    Scaling text on a large document queues a relayout of the whole page, and
    while it runs the renderer answers no CDP at all — the next screenshot or
    page_info fails, several helpers away from the call that caused it. Absorbing
    the wait here makes the contract honest: an a11y helper returns when the page
    is both adapted and usable.
    """
    _js("return 1", patience=patience)


def _need_attached():
    if _js("return globalThis.__BH_A11Y_BUILD || null") != _build_id(_bundle_source()):
        a11y_attach()


def a11y_profiles():
    """The built-in ability presets, id → description."""
    _need_attached()
    return _js("const p = globalThis.__BH_A11Y.profiles;"
              "return Object.fromEntries(Object.entries(p).map(([k,v]) => [k, v.description]))")


def _stick(call):
    """Remember a settings call so later pages get it too, when sticky is on."""
    global _sticky_call
    _sticky_call = call if _sticky_enabled else None
    _persist()


def a11y_profile(name):
    """Apply one preset by id — see a11y_profiles(). Reports what it skipped.

    Sticks: pages opened afterwards get the same preset, because a profile that
    lapses on navigation is not a profile.

    Known bad: `lowVision` includes reflowColumn, whose universal-selector CSS
    (`html.ai4a11y-reflow *`) forces a global style recalc that blocks the
    renderer for tens of seconds on a large document. The other eleven presets
    apply in under a third of a second.
    """
    _need_attached()
    call = f"globalThis.__BH_A11Y.applyProfile({json.dumps(name)})"
    result = _js(f"return {call}")
    if not result.get("error"):
        _stick(call)
    _settle()
    return result


def a11y_apply(**settings):
    """Apply settings directly, e.g. a11y_apply(fontScale=150, darkMode=True).

    Sticks across navigation, the same as a11y_profile.
    """
    _need_attached()
    call = f"globalThis.__BH_A11Y.apply({json.dumps(settings)})"
    result = _js(f"return {call}")
    _stick(call)
    _settle()
    return result


def a11y_off():
    """Turn every enabled adapter off and stop re-applying on new pages."""
    global _sticky_call
    _need_attached()
    stopped = _js("return globalThis.__BH_A11Y.disableAll()")
    _sticky_call = None
    _persist()  # keep the catalog available, drop the settings
    _settle()   # undoing a scale relayouts the page just as applying it did
    return stopped


def a11y_status():
    """Which adapters currently report themselves enabled."""
    _need_attached()
    return _js("return globalThis.__BH_A11Y.status()")


def a11y_service(method, *args, timeout=15.0):
    """Call one Librarian method on the toolkit service. Needs AI4A11Y_TOKEN."""
    token = os.environ.get("AI4A11Y_TOKEN")
    if not token:
        raise RuntimeError(
            "AI4A11Y_TOKEN is not set — mint one with POST /admin/tokens and put "
            "it in .env (see the a11y setup notes)"
        )
    req = urllib.request.Request(
        f"{SERVICE_URL}/v1/librarian/{method}",
        data=json.dumps({"args": list(args)}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"toolkit service {method}: HTTP {e.code} {e.reason}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"toolkit service unreachable at {SERVICE_URL}: {e.reason}") from None
    # The service returns application errors as data, not as transport failures.
    if not body.get("ok"):
        raise RuntimeError(f"toolkit service {method}: {body.get('error')}")
    return body.get("result")


def a11y_sync(url=None):
    """Fetch this person's effective settings for the page and apply them.

    Two things come out of the profile and both matter. `effectivePreferences`
    is the authoritative merge of what they have explicitly set or been learned
    to prefer, scoped by origin and site category. `getAbilityModel` is what
    they said about themselves at onboarding, rendered for the web. The merge
    wins wherever it has an opinion; the ability baseline only fills keys it
    left unset — the same composition the toolkit's own resolveWebPreferences
    does, run here because that function needs an in-process librarian.

    Reading only the merge (as this did before) meant a person's onboarding
    never reached the page — a profile that said "I'm blind" applied nothing
    until they had also changed a setting by hand.
    """
    url = url or page_info()["url"]
    _need_attached()
    prefs = a11y_service("effectivePreferences", url) or {}
    try:
        model = a11y_service("getAbilityModel")
    except Exception as e:
        model = None
        _log_once(f"ability model unavailable ({e}); using recorded settings only")

    resolved = _js("return globalThis.__BH_A11Y.resolveWeb(%s, %s)"
                   % (json.dumps(prefs), json.dumps(model)))
    settings = resolved.get("settings") or {}
    out = {
        "url": url,
        "settings": settings,
        "provenance": resolved.get("provenance"),
        # Ability needs the web surface cannot render. Reported rather than
        # dropped: a need nobody can meet is a finding, not a silence.
        "unmet": resolved.get("unmet") or [],
    }
    if not settings:
        out["note"] = "no preferences or needs recorded for this person yet"
        return out
    out.update(a11y_apply(**settings))  # a11y_apply sticks it for later pages
    return out


_logged = set()


def _log_once(msg):
    if msg not in _logged:
        _logged.add(msg)
        print(f"[a11y] {msg}")


def _ax_flags(node):
    out = []
    for p in node.get("properties") or []:
        name = p.get("name")
        if name not in _AX_FLAGS:
            continue
        v = (p.get("value") or {}).get("value")
        if v is False or v == "false" or v is None:
            continue
        out.append(name if v is True or v == "true" else f"{name}={v}")
    return out


def a11y_snapshot(max_lines=600):
    """The accessibility tree as indented text — what a screen reader exposes.

    Rendered in the shape the toolkit's aria-parse.js reads, so findings can run
    against it unchanged. This is the view an agent should plan on: a control
    that is not here is one the person cannot reach, however visible it looks.
    """
    cdp("Accessibility.enable")
    nodes = cdp("Accessibility.getFullAXTree").get("nodes", [])
    by_id = {n["nodeId"]: n for n in nodes}
    roots = [n for n in nodes if not n.get("parentId")]

    lines = []
    # Iterative: a real page's tree runs to tens of thousands of nodes, deep
    # enough to blow the recursion limit.
    stack = [(r["nodeId"], 0, "") for r in reversed(roots)]
    truncated = False
    while stack:
        node_id, depth, parent_name = stack.pop()
        node = by_id.get(node_id)
        if node is None:
            continue
        role = (node.get("role") or {}).get("value") or ""
        name = " ".join(((node.get("name") or {}).get("value") or "").split())
        # An ignored node is not exposed to assistive tech, but its children
        # still are — it is a pass-through wrapper, not a pruned subtree. Same
        # for a structural role carrying no name, a layout artefact, and the
        # StaticText that merely restates the name of the control above it.
        passthrough = (
            node.get("ignored")
            or role in _AX_NOISE
            or (role in _AX_SKIP and not name)
            or (role == "StaticText" and name == parent_name)
        )
        if not passthrough:
            if len(lines) >= max_lines:
                truncated = True
                break
            flags = "".join(f" [{f}]" for f in _ax_flags(node))
            label = f' "{name}"' if name else ""
            lines.append(f"{'  ' * depth}- {role}{label}{flags}")
        child_depth = depth if passthrough else depth + 1
        child_parent = parent_name if passthrough else name
        for c in reversed(node.get("childIds") or []):
            stack.append((c, child_depth, child_parent))

    text = "\n".join(lines)
    if truncated:
        text += f"\n… truncated at {max_lines} lines of {len(nodes)} AX nodes — raise max_lines"
    return text


CONTROLLER_URL = os.environ.get("AA_CONTROLLER_URL", "http://127.0.0.1:4000/chat")


def a11y_layout(controller_url=None, controller_side="left", split=1/3,
                adopt_current=True):
    """Put the Controller and the page it drives side by side, and return both.

    Chrome's own Split View is a browser-UI feature with no CDP surface, so this
    tiles two windows instead — same result, and scriptable. The Controller gets
    its own window so it can never be the tab an agent navigates away.

    `split` is the fraction of the screen the control surface gets; the page
    being driven takes the rest. A third is comfortable for a chat column and
    still leaves the page readable, which matters when the adaptations being
    applied are about legibility. It is also safely above Chrome's ~500px
    minimum window width on ordinary displays, so the requested split is what
    you actually get.

    Returns {"controller": targetId, "driven": targetId} — pass the driven id to
    `browser-harness control --target`.
    """
    controller_url = controller_url or CONTROLLER_URL

    # The page being driven. With a throwaway profile the current tab is one we
    # made, so reusing it is right. Against someone's own Chrome it is whatever
    # they were reading — adopting it would hand the agent their work to navigate
    # away, and tiling would move the window they had arranged. adopt_current=
    # False opens a fresh tab in its own window and leaves everything else alone.
    if adopt_current:
        driven = current_tab()["targetId"]
        # A tab that will not answer the probe gets a fresh one rather than
        # failing the whole layout — "cannot tell" and "is the Controller" want
        # the same answer, since driving the Controller ends the session.
        try:
            looks_like_controller = js(
                "return !!document.querySelector('.aa-controller')", target_id=driven) is True
        except Exception:
            looks_like_controller = True
        if looks_like_controller:
            driven = cdp("Target.createTarget", url="about:blank")["targetId"]
    else:
        driven = cdp("Target.createTarget", url="about:blank",
                     newWindow=True)["targetId"]

    # The Controller, in a window of its own.
    controller = cdp("Target.createTarget", url=controller_url,
                     newWindow=True)["targetId"]

    screen = js("return {w: screen.availWidth, h: screen.availHeight,"
                " l: screen.availLeft || 0, t: screen.availTop || 0}",
                target_id=driven)
    def place(tid, bounds):
        wid = cdp("Browser.getWindowForTarget", targetId=tid)["windowId"]
        # A maximised window ignores bounds; normalise first.
        cdp("Browser.setWindowBounds", windowId=wid, bounds={"windowState": "normal"})
        cdp("Browser.setWindowBounds", windowId=wid, bounds=bounds)
        return cdp("Browser.getWindowForTarget", targetId=tid)["bounds"]

    # Place the control surface first and read back what Chrome actually gave.
    # Chrome enforces a ~500px minimum window width, so a narrower request comes
    # back wider — and positioning the other window at the *requested* edge left
    # the two overlapping, with the chat covering part of the page it drives.
    want = int(screen["w"] * split)
    on_left = controller_side == "left"
    got = place(controller, {
        "left": screen["l"] if on_left else screen["l"] + screen["w"] - want,
        "top": screen["t"], "width": want, "height": screen["h"],
        "windowState": "normal"})
    used = got.get("width") or want

    place(driven, {
        "left": screen["l"] + used if on_left else screen["l"],
        "top": screen["t"], "width": screen["w"] - used, "height": screen["h"],
        "windowState": "normal"})

    return {"controller": controller, "driven": driven, "screen": screen,
            "controller_side": controller_side,
            "requested_width": want, "actual_width": used,
            # Chrome would not go narrower; the caller can say so rather than
            # leaving someone to wonder why 25% looks like 26%.
            "clamped": used != want}


def a11y_audit():
    """Run the toolkit's auditors over the live page and return their findings."""
    _need_attached()
    return _js("return globalThis.__BH_A11Y.audit()")


__all__ = [
    "a11y_attach", "a11y_profiles", "a11y_profile", "a11y_apply", "a11y_off",
    "a11y_status", "a11y_service", "a11y_sync", "a11y_snapshot", "a11y_audit",
    "a11y_sticky", "a11y_layout", "a11y_target",
]
