# Toolkit: the caption grammar misses "captioning", so the phrase falls to the agent

Direction: **receiver → toolkit**. There is a floor for this on the receiver
side now, but the grammar is where it should be caught.

## What happened

In the chat:

```
> turn off live captioning
Ok, running: turn off live captioning.
Live captioning turned off.
```

Live Caption was still on. Two things went wrong; the first is yours.

## 1. The grammar did not match

`controller/grammar.js` matches `captions?` — "caption" or "captions", but not
**"captioning"**. So "turn off live captioning" matched no rule, and the router
sent the whole utterance to the agent lane instead:

```
[control] task started: 'turn off live captioning'
```

Suggested change, in both the live-caption rules and the two generic caption
rules — `captions?` → `caption(s|ing)?`:

```js
/\b(no|stop|hide|turn off|switch off|disable|remove|drop) (the )?live (caption(s|ing)?|cc)\b|\blive caption(s|ing)? off\b/
```

Worth a scan for the same shape elsewhere: `subtitles?` will not match
"subtitling", and any other rule ending `s?` has the same gap for its -ing form.

## 2. The agent lane reported success without acting

Once it reached the agent, the answer came back:

```
[control] task done: "Live captioning turned off."
```

Nothing had been turned off. A model asked to turn something off will tend to
report that it did, and the person on the other end may have no way to look —
which is exactly the population this is for.

Not your bug, but it shapes the priority: **anything the grammar misses is
answered by something that can claim success it did not achieve.** Widening the
regex is cheap; the failure mode behind it is not.

## What the receiver now does

Utterances that plainly name a browser-level setting and a direction —
`liveCaptions`, `autoDescribe`, `caretBrowsing`, `hideProfanity`,
`liveTranslate` — are answered by the receiver before the agent sees them, and
the result is read back from Chrome before any claim is made:

```
"turn off live captioning" -> {"ok": true, "detail": "Live Caption is off"}   (Chrome: off)
```

A question ("what do live captions do") is not treated as an instruction and
still goes to the agent.

This is a floor, not a fix: it only covers settings this platform owns and can
verify. Everything the grammar misses that lands in the agent lane is still
answered by a model that may report success it did not achieve.

## Acceptance

"turn off live captioning" in the chat is resolved by the grammar to
`{ liveCaptions: false }`, not sent to the agent.
