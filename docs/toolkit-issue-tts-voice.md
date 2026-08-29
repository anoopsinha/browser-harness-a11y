# The Controller speaks with whatever voice the OS defaults to

Measured in the automation Chrome on macOS, against toolkit `main`.

`web/ui.js` speaks like this:

```js
speechSynthesis.speak(new SpeechSynthesisUtterance(text))
```

No `voice` is set, so the platform default is used. On this machine that is
**Samantha** — one of macOS's *compact* voices, and noticeably poor.

## What is actually available

```
total voices                : 199
default                     : Samantha            (local, compact)
premium / enhanced / Siri   : none installed
network (Google)            : Google US English, Google UK English Female/Male
local en-US                 : 28  — mostly the novelty set
                              (Bad News, Bells, Boing, Bubbles, Cellos, Jester …)
```

So the default is close to the worst reasonable choice: the compact voice sits in
a list whose other local entries are largely novelty voices, while a
better-sounding network voice goes unused.

## Suggested fix

Choose a voice rather than inheriting one. Preference order, first match wins:

```js
function bestVoice(lang = 'en') {
  const vs = speechSynthesis.getVoices().filter(v => v.lang && v.lang.startsWith(lang));
  const pick = (rx) => vs.find(v => rx.test(v.name));
  return pick(/\((Premium|Enhanced)\)/i)   // macOS high quality, local, offline
      || pick(/^Google /)                   // clearly better than a compact voice
      || vs.find(v => v.default)
      || vs[0] || null;
}
```

Then `u.voice = bestVoice()` before speaking, and re-read on `voiceschanged` —
`getVoices()` is empty on first call in Chrome and populates asynchronously,
which is its own source of "it used the wrong voice once".

Local-before-network is deliberate: a network voice adds a round trip before the
first word and stops working offline, which is a poor property for an
accessibility control surface. It is only preferred over a *compact* voice.

Worth exposing the choice too — a voice list beside the existing "Speak results
aloud" toggle, persisted the same way. Voice is a strong personal preference,
and the people most affected are the least able to work around a bad default.

## The bigger win is not code

**No Enhanced or Premium voices are installed on this machine.** Installing them
improves every app, not just this one, and makes the code change above pick a
local high-quality voice automatically:

> System Settings → Accessibility → Spoken Content → System Voice → Manage
> Voices → install e.g. *Ava (Premium)* or *Zoe (Premium)*

Without that, the best available is a network voice. With it, the best available
is local, instant, and offline.

## Two things noticed while measuring

- **`speechSynthesis` is gated behind a user gesture.** A probe with no prior
  interaction never fires `onstart` — for either a local or a network voice. In
  normal use the person has typed or clicked, so it works; but any speech the
  Controller tries *before* first interaction is silently dropped. Worth knowing
  if a greeting or a startup announcement is ever added.
- **This path should not fire at all for a screen-reader user.** With the
  speak-results toggle now defaulting ON, someone running VoiceOver gets the
  Controller's voice over their own until they find the toggle. Voice quality and
  that default are worth deciding together.

`controller/web/ui.js`
