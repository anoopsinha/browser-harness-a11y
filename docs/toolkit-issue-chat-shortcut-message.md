# Toolkit: a framed chat cannot be given its keyboard shortcuts

Direction: **receiver → toolkit**. Found while embedding the chat beside the
page under test, which is the shape the toolkit's own framed-page mode assumes.

## The problem

`onboarding/chat.js` binds Ctrl-Space on its own `document`:

```js
document.addEventListener('keydown', (e) => {
  if (e.ctrlKey && ... && (e.code === 'Space' || e.key === ' ')) {
    if (SR && voiceInOn && recog) { e.preventDefault(); toggleMic(); }
  }
});
```

That only fires while focus is inside the chat. Embed the chat — as
`scripts/iframe-host/split.html` does, chat on the left and the page under test
on the right — and a press landing anywhere else never reaches it. The keystroke
cannot be forwarded either: a cross-origin frame will not accept a synthetic
event, and `contentWindow.focus()` from the embedder is ignored silently.

The best an embedder can do is focus the frame on the press, so the *next* one
works. That is what this repo does now, and it is a poor answer for this
shortcut in particular: the person reaching for voice is often the person for
whom typing is hard, and "press it twice" is exactly the friction they were
avoiding.

## Suggested shape

A message the chat accepts from its embedder, doing what the shortcut does:

```js
frame.contentWindow.postMessage({ kind: 'aa-chat-command', name: 'voice' }, '*');
```

`voice` to toggle, or `voice-start` / `voice-stop` if an embedder wants to hold
a key. Worth routing anything else already on a shortcut through the same door
rather than adding one message per key later.

## Two things to settle first

**Does it work without a user gesture?** The message arrives with no user
activation, and speech recognition may refuse to start on that basis — in which
case this fixes nothing and the answer is the focus dance after all. Worth
checking before building it: the fix is cheap, and it either works or it does
not.

**Who may send it.** Any page can frame the chat — it sends no framing headers —
so "start the microphone" should not be a message from any embedder at all.
`event.source === parent` narrows it to the embedder, which is the only party
that can grant the frame a microphone in the first place, so it holds that power
already. An origin allowlist would be firmer if the chat gains a way to be
configured with one.

## Acceptance

With the chat framed and focus in the neighbouring panel, one Ctrl-Space starts
dictation — no second press, and no click into the chat first.
