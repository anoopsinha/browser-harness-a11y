# Controller UI issues

Four items, from driving `browser-harness-a11y` through the Controller.
Verified against `rearch-experiment` @ **b01b2c0**.

The receiving end of #2 is already built and pushed on our side, so once the
Controller learns the message, everything below works with no further change
here.

---

## 1. Quick-connect button for a local receiver

The Drive field needs `ws://127.0.0.1:9333` typed in full every time, which is
the first thing anyone does and easy to typo. A one-click button beside the
field that fills the default and connects would remove the whole step.

Suggested: a **Connect to local harness** button that sets the input to
`ws://127.0.0.1:9333` and runs the same handler as **Connect remote**. Keeping
the text field lets a different port or host still be entered by hand.

`toolkit/controller/demo/index.html`

## 2. No channel for a result that arrives after the response

**This is the substantive one.**

`performAction('task', …)` must return immediately — the Controller times out at
10s, and a real browsing task takes 30–120s. So the person is told *"Passing
that to the app"* and then never told what happened. The agent's answer exists
and has nowhere to go: the ControlPort is strictly one response per request,
with no receiver→Controller push.

The receiver already emits this on the same socket when the agent finishes:

```json
{ "kind": "aa-control-note", "text": "The top story is …" }
```

Sent on failure too — silence after "working on it" is the worst outcome for
someone who cannot see the page. It is safe to emit today because §2 of
PROTOCOL.md says to ignore unrecognised kinds, so it is inert until the
Controller learns it.

**Suggested:**
- `transport/remote.js` — in `websocketChannel`/`remoteControl`, surface a
  message whose `kind` is `aa-control-note` to an `onNote(text)` callback
  instead of dropping it (it has no `id`, so the current `waiting.has(msg.id)`
  check discards it).
- `mount` / `web/ui.js` — route `onNote` into the existing `show()`.

Everything else then falls out for free, which is why it is worth doing here
rather than in each receiver: `.aa-feedback` is already
`role="status" aria-live="polite"`, so a screen-reader user hears the result in
their own voice, and `presentation.output.speech` already decides whether to
also speak it.

Worth documenting in PROTOCOL.md as an optional receiver→Controller message, so
other receivers can adopt it.

## 3. The acknowledgement should say what it is running

`router.js` sets `say: 'Passing that to the app'` on the task fallthrough. For a
blind user that is the only signal anything happened, and it does not confirm
what was heard — which matters most for **spoken** input, where mis-recognition
is the common failure and the person has no other way to catch it.

Suggested: `say: \`Ok, running: ${utterance}\`` (or a trimmed form for long
utterances). Reading the utterance back is what lets someone notice that
"search for braille music" was heard as "search for braille moozik" before the
agent spends a minute on it.

`toolkit/controller/router.js`

## 4. Speaking the result — no new TTS path, please

Requested as "speak the result via TTS". Flagging it because the obvious
implementation would undo the issue #7 fix.

`presentation.js` currently computes `speech = !assistiveTech && (has('vision')
|| has('reading'))`, so a person whose profile carries screen-reader needs gets
**no** toolkit TTS — they get the live region, announced by their own screen
reader at their own rate. That is correct and should stay.

So #4 needs no new code beyond #2: route the note into `show()`, and the
existing gate does the right thing for both audiences — spoken for a
speech-output profile, announced via the live region for a screen-reader user.

The distinction is easy to lose because the **demo's** operator buttons set
`supportAreas` with an empty `needs[]`, so clicking *vision* yields
`assistiveTech === false` and the Controller speaks. A real onboarded blind
profile takes the other branch. Both are right; they just look contradictory
when testing.

`toolkit/controller/presentation.js` (no change expected — noting the constraint)
