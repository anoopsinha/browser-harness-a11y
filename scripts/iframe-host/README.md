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

With the rest of the stack, which starts the host, points the driven window at
it, and puts the receiver in iframe mode:

```bash
AA_IFRAME=1 ./scripts/a11y up
```

That opens the Framed page here and applies the profile to it before anyone
connects, so the session is already adapted when the tester joins. It does not
open a chat here: the chat belongs on the machine running the screen reader, and
a second one on the same receiver would push its own settings and take replies
meant for the first. A chat left open here from an earlier run is closed for the
same reason.

Or on its own, against a receiver you start yourself:

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

## Both panels in one page

`/split` puts the chat and the page under test side by side in a single
document: the chat on the left at a third, the page on the right. One URL for
the tester to open, and — because both are frames of the same document rather
than two windows — a screen reader moves between them without leaving the page.

```
http://localhost:8124/split
http://localhost:8124/split?chat=http://localhost:4000/chat   # if the chat is elsewhere
```

The chat's address is a parameter rather than a constant, since through a tunnel
it may not share a host with this page. Both ports have to be reachable from
wherever the page is opened.

The chat frame carries `allow="microphone *; autoplay *"`. Without it a
cross-origin frame gets no microphone at all, so dictation and speech
recognition never start — and nothing says why, which is the worst way for a
voice feature to fail.

It also takes focus when the page loads, and Ctrl-Space out in the page panel
hands focus back to it. The chat binds its shortcuts on its own document, so
they only reach it when focus is inside that frame; the keystroke itself cannot
be forwarded, since a cross-origin frame will not accept a synthetic event, so
that press is spent moving focus and the next one works.

The divider is a real `separator`: focusable, with arrow keys to move it, Home
and End for the limits, and Shift for larger steps — someone working by keyboard
has as much reason to resize it as someone with a mouse. The position is
remembered per browser. Below 700px the split becomes horizontal.

Back, forward and reload sit beside the address on the Framed page, and they
move the *session* rather than a frame. A frame's own history belongs to one
viewer, and the person reading is on another machine — pressing back on either
screen has to move both.

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
Controller can tell which surface it is driving.

In that mode the receiver never touches a tab. Everything it evaluates runs
inside the Framed page's iframe, which matters because the tab it was pinned to
is usually something else entirely — the hosting service's own page, in a
session driven from Assistiv Labs. It finds the Framed page by a marker the page
sets rather than by URL, since the same server is `localhost`, `127.0.0.1`, and
something else again through a tunnel. In that mode the receiver
broadcasts writes — settings, navigation, activate and scroll — through the host,
and answers reads from a viewer open on this machine, since every viewer renders
the same page with the same settings.

### Open-ended tasks

They work, and answer the way they do on a tab: an acknowledgement first, the
model's summary as a note when it finishes.

What changes is where the agent is told to look. The page is inside the frame,
which is same-origin with the page holding it, so the agent reads it through
`document.getElementById('frame').contentDocument`. Navigation is the part it
must not do itself — a `goto_url` here moves this screen alone, around the
proxy, while the person's copy stays where it was — so it is told to post the
address to the host, which moves every viewer at once.

### Search

Bing, not Google, while the page is a frame. Stripping `X-Frame-Options` gets a
document delivered; it does not stop a page blanking itself once it sees it is
framed. Measured in a browser:

| engine | in a frame |
| --- | --- |
| Bing | renders normally |
| Google | title, empty body |
| DuckDuckGo | title, empty body |

Asking for one of the others by name gets Bing and is told why, rather than an
empty results page and no explanation. On a tab nothing changes: Google stays
the default.

The agent is told the same thing, and needs to be: the Controller's grammar does
not match every phrasing, so "search for oranges" arrives as an open-ended task
rather than a search — and left to itself the agent reaches for Google.

### What this mode will not do

`liveCaptions`, `autoDescribe` and the other Chrome-level settings are out of
reach: they are preferences of a browser on this machine, and the person is
reading in theirs.

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
