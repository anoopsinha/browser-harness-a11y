# Toolkit: an optional iframe target for the chat, alongside the remote receiver

Direction: **receiver → toolkit**. The localhost half is built and working in
browser-harness-a11y (`scripts/iframe-host/`); this is the chat-side half.

## Why

Screen-reader testing is moving to **Assistiv Labs** — real NVDA and JAWS on
hosted Windows, which can see the tester's machine only through a localhost
tunnel. The screen reader reads whatever is in *its* browser, so the page under
test has to render there. CDP cannot reach that browser, so the receiver cannot
drive it.

An iframe can. The chat and the page under test then sit in one accessibility
tree, which is what makes a screen reader able to move between them.

## What exists already

`scripts/iframe-host/server.py` serves any URL from localhost with framing
headers dropped, `<base href>` set, and two scripts injected: the toolkit's
adapter bundle, and `bridge.js`.

`bridge.js` answers the ControlPort method names over postMessage:

```js
frame.contentWindow.postMessage(
  { kind: 'bh-iframe-req', id: '1', method: 'applySettings',
    args: [{ fontScale: 150 }] }, '*');

// { kind: 'bh-iframe-res', id: '1', result: { applied, previous, rejected } }
```

`describeCapabilities`, `getContext`, `applySettings`, `applyProfile`,
`undoLast`, `getContent`, `performAction`. The frame posts `bh-iframe-ready`
once its scripts have run.

Verified in a browser: github.com — which sends `X-Frame-Options: deny` —
renders with adapters loaded, `applySettings` takes paragraph text from 14px to
21px, and `getContent` returns a real outline.

## What to add

**1. An iframe ControlPort.** `controller/control-port.js` defines the
interface, and `mock-receiver.js` is already an in-process implementation of it.
An `iframe-receiver.js` in the same shape — wrapping the postMessage calls above
— would let the router drive a frame with no changes to the router, the grammar
or the chat's own logic.

**2. A panel in the chat.** An optional iframe with a URL field. Off by default;
the remote receiver stays the normal path.

**3. Choosing between them.** The chat currently offers
`connectRemoteReceiver(ws)`. It needs a second way in — "drive the page in this
window" — and the two are mutually exclusive.

**4. Apply the profile to the chat as well.** In this mode the tester is reading
the chat and the page in one document tree. Visual settings that reach only the
iframe leave the chat unadapted beside it.

## Two things this mode cannot do

Worth encoding in `describeCapabilities` rather than discovering at runtime:

- **No `task`.** The agent drives a browser over CDP; there is no CDP here.
  Spoken commands that fall through to the agent have nowhere to go, so the
  grammar's unmatched-utterance path needs an answer other than silence.
- **No browser-level settings.** `liveCaptions`, `autoDescribe` and the rest are
  Chrome preferences reached through `chrome://settings`, which a page cannot
  touch. In iframe mode those keys should be reported unsupported, not applied
  and silently ignored.

## Acceptance

With the iframe host running and both ports tunnelled, a tester on an Assistiv
Labs Windows VM opens the chat, says "make the text bigger", and NVDA reads the
adapted page in the same tab — with no software installed on their own machine.
