# Iframe host

Serves any page from localhost, adapted and drivable, so a screen reader on a
hosted Windows VM can read it.

Built for **Assistiv Labs**, which runs real NVDA and JAWS on real Windows and
reaches this machine only through a localhost tunnel. That constraint decides
the design: the screen reader reads whatever is in *its* browser, so the page
under test has to render there — which rules out CDP, because the harness drives
a Chrome on this machine that the VM cannot see.

So control moves into the page. This proxy fetches a URL, serves it from
localhost, and injects the toolkit's adapters plus a small bridge. The chat and
the page under test end up in one accessibility tree, and the adaptations are
applied by code running inside the page rather than over a wire.

## Running it

```bash
python3 scripts/build_a11y.py            # adapters, if not already built
python3 scripts/iframe-host/server.py    # http://127.0.0.1:8124/
```

Expose **8124** through the Assistiv Labs tunnel alongside the chat's **4000**.
Both are needed: the chat is one origin, the proxied page another.

`/` is a bare frame with a URL bar, for checking that a page loads and its
adapters came up before a screen reader is involved. It has no controls of its
own — settings are the chat's job. The chat embeds `/go?url=…` directly in its
own iframe and talks to the bridge; it does not need this page.

## What it does to a page

| step | why |
| --- | --- |
| Drops `X-Frame-Options` and `frame-ancestors` | the reason most pages cannot be framed at all |
| Injects `<base href>` | so images, CSS and scripts still load from the real origin |
| Injects the adapter bundle | the toolkit runs inside the page, no CDP needed |
| Injects `bridge.js` | lets the surrounding page drive it by postMessage |

Only the top-level HTML is proxied. Everything else loads from where it always
did, which keeps the page behaving like itself.

The injected `<script src>` is an absolute URL, deliberately: a relative one is
resolved against the `<base>` that was just set, so the page loads perfectly and
quietly fetches its adapters from the proxied site, where they do not exist.

## The current page is server state

Two browsers render the page under test: the operator's, and the one on the
hosted VM reached through the tunnel. They are separate documents, so navigating
a tab on this machine moves nothing on the tester's screen.

So the current URL lives here instead, and every viewer follows it:

```bash
curl localhost:8124/state
# {"url": "https://en.wikipedia.org/wiki/Apple", "rev": 3}

curl -X POST localhost:8124/state -H 'Content-Type: application/json' \
     -d '{"url":"https://example.com/"}'
```

`GET /events` is a server-sent stream that pushes each change, which is what the
host page listens to. Verified with two viewers open at once: a POST from a third
party moved both.

Settings travel the same way, and for the same reason — an adapter applied to a
document on this machine reaches nobody. Each viewer applies the session's
settings to its own frame, including after a navigation, so bigger text stays
bigger on the next page.

```bash
curl -X POST localhost:8124/state -H 'Content-Type: application/json' \
     -d '{"settings":{"fontScale":150}}'
```

Point the receiver at it so the chat drives the frame rather than a tab:

```bash
BH_IFRAME_HOST=http://127.0.0.1:8124 ./browser-harness control --port 9333
```

`describeCapabilities` then reports `platform: browser-harness-iframe`, so the
Controller can tell which surface it is driving. In that mode the receiver
broadcasts writes — settings, navigation, activate and scroll — through the host,
and answers reads from a viewer open on this machine, since every viewer renders
the same page with the same settings.

## Driving it

`bridge.js` answers the same method names as the ControlPort receiver, so a
caller can treat an iframe and a remote receiver as two implementations of one
thing:

```js
frame.contentWindow.postMessage(
  { kind: 'bh-iframe-req', id: '1', method: 'applySettings',
    args: [{ fontScale: 150 }] }, '*');

// { kind: 'bh-iframe-res', id: '1', result: { applied: {...}, previous: {...} } }
```

`describeCapabilities`, `getContext`, `applySettings`, `applyProfile`,
`undoLast`, `getContent`, `performAction` (`activate`, `scroll`, `back`,
`forward`, `navigate`). The frame announces `bh-iframe-ready` when its scripts
have run — there is no reliable way for the host to know that otherwise, and
polling for it races on every page load.

It is postMessage only, never `contentDocument`, so it keeps working when the
chat is served from a different port.

## What breaks

Measured against real sites, not assumed:

- **Signed-in pages.** A proxied request carries none of the browser's cookies,
  so you get the logged-out view. Most accessibility problems live behind a
  login, and this does not reach them.
- **Frame-busting.** Pages that check `window.top !== window.self` and escape.
- **Absolute-path fetches.** `<base>` fixes markup URLs; it does not affect
  `fetch('/api/...')`, which still resolves against the proxy and 404s.
- **Single-page navigation.** Following a link inside the frame leaves the proxy
  and hits the real origin, unadapted, where framing headers apply again. Route
  navigations back through `/go` — `performAction('navigate')` does.

Treat it as a way to exercise adapters against a curated corpus, not as a
general browser.

## Security

The proxied page's JavaScript runs in this proxy's origin. Keep it on its own
port — `8124`, not the chat's `4000` — so a fetched page cannot read the chat's
storage, which holds the person's profile. Do not sign in to anything through
it, and do not put anything else on this port.
