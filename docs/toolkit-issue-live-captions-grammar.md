# Toolkit: "live captions" and "captions" should not mean the same thing

Direction: **receiver → toolkit**. The receiver half is done and verified; this
is the Controller-side change needed to reach it.

## What the person expects

- **"turn off live captions"** → switch off *Chrome's Live Caption* only, and
  leave a video's own subtitles running.
- **"turn off captions"** → switch off *both* the media's caption track and
  Chrome's Live Caption.

## What happens now

Both phrases produce the same request, so "turn off live captions" also switches
off the video's own subtitles:

```
applySettings([{"showCaptions": false}])
```

`controller/grammar.js:61` matches the `live` prefix and then discards it:

```js
{ re: /\b(no|stop|hide|turn off|switch off|disable|remove|drop) (the )?(live |closed )?(captions?|subtitles?|cc)\b|.../,
  build: (_m, u) => adapt(u, { changes: { showCaptions: false }, say: 'Turning captions off' }) },
```

`controller/test/controller.test.mjs:38-39` asserts this, so it is deliberate —
it predates there being anything else "live captions" could mean.

## Why the receiver cannot fix it alone

Both utterances arrive as the same payload. There is nothing left to
distinguish them by the time they reach the receiver.

## The receiver is ready

`liveCaptions` is a declared setting key on this platform — Chrome's Live
Caption captions any audio on-device, which no page-level adapter can do — and
is advertised in `describeCapabilities().settingKeys`. Measured against the live
receiver, reading Chrome's own toggle in `chrome://settings/accessibility` to
confirm rather than trusting the return value:

| sent                      | video's own captions | Chrome Live Caption |
| ------------------------- | -------------------- | ------------------- |
| `{"liveCaptions": false}` | still on             | **off**             |
| `{"showCaptions": false}` | **off**              | **off**             |

Both rows are already the desired behaviour. Send `liveCaptions` and it works.

## What to change

**1. Declare the key** in `toolkit/registry/tools.js` `settingsMeta`, beside
`showCaptions` (~L641):

```js
liveCaptions: { type: 'boolean', description: "Browser-generated captions for any audio (Chrome Live Caption)" },
```

This is required, not cosmetic. `controller/router.js:129` calls `validate(k, v)`,
which returns `null` for any key absent from `settingsMeta` (L24-25), so the
change would be dropped with *"that setting is not valid here"* even though the
receiver lists `liveCaptions` in `settingKeys` and would accept it.

**2. Add grammar rules** in `controller/grammar.js`, **before** the two generic
caption rules at L61-62 — the generic ones would otherwise match first and
swallow `live`:

```js
// Chrome's Live Caption is a browser feature that captions any audio, distinct
// from a media file's own caption track. Must precede the generic caption rules.
{ re: /\b(no|stop|hide|turn off|switch off|disable|remove|drop) (the )?live (captions?|cc)\b|\blive captions? off\b/,
  build: (_m, u) => adapt(u, { changes: { liveCaptions: false }, say: 'Turning live captions off' }) },
{ re: /\b(show|turn on|switch on|enable|start|give me|put on|with) (the )?live (captions?|cc)\b|\blive captions? on\b|^live captions?$/,
  build: (_m, u) => adapt(u, { changes: { liveCaptions: true }, say: 'Turning live captions on' }) },
```

**3. Drop `live ` from the generic rules'** `(live |closed )?` group, leaving
`(closed )?` — "closed captions" still means the media's own track.

**4. Update the tests** that assert the old behaviour
(`controller/test/controller.test.mjs:38-39`): `"stop live captions"` and
`"no live captions"` should now yield `liveCaptions === false`, with new cases
keeping `"captions off"` and `"stop closed captions"` on `showCaptions`.

## Note on "turn off captions"

No change needed for this one. The receiver already takes Chrome's Live Caption
down alongside the media track whenever a caption setting is named explicitly,
so `{"showCaptions": false}` switches off both — the second row of the table.

## Acceptance

In the chat, with a captioned video playing and both kinds of captions on:

- "turn off live captions" → Chrome's caption bubble disappears; the video's own
  subtitles keep showing.
- "turn off captions" → both stop.
- "turn on live captions" → the bubble comes back, subtitles untouched.
