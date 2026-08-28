# Controller UI issues

Four items, from driving `browser-harness-a11y` through the Controller.
Verified against `rearch-experiment` @ **b01b2c0**.

> **Update (28 Aug 2026) — all four done** (`cb75b41`).
> - **#2** `remoteControl().onNote(cb)` surfaces a `{kind:"aa-control-note", text}`
>   push (no id) instead of dropping it; `web/ui.js` routes it into the
>   `role=status aria-live` region (and `speak()`, already gated). Documented in
>   PROTOCOL.md. Your receiver already emits it, so it works end to end now.
> - **#4** falls out of #2 — no new TTS path; the `presentation.output.speech`
>   gate keeps a screen-reader operator on the live region (issue #7 intact).
> - **#3** the task ack now reads the utterance back: `Ok, running: <utterance>`
>   (trimmed past 80 chars).
> - **#1** a **Connect to local harness** button fills `ws://127.0.0.1:9333` and
>   connects; the field still takes a custom host/port.

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

---

# Follow-ups (28 Aug 2026)

Two items that came out of #4. Both start from the same fact: **the toolkit
cannot detect a screen reader.** `assistiveTech` is inferred from the profile's
needs, not observed — so the current gate is a guess, and it is wrong in both
directions.

## 5. Make "speak results" the person's choice, not an inference

`presentation.js` decides TTS from `assistiveTech`, which is really "does this
person's profile carry screen-reader needs". That is a proxy for "is a screen
reader running right now", and the two come apart:

- **Blind profile, no screen reader on this surface** (a kiosk, a shared
  machine, TTS-without-AT, a phone with VoiceOver off) → they get **silence**.
  That is worse than doubled speech: nothing tells them the agent finished.
- **Sighted low-vision profile, screen reader running anyway** → two voices.

Neither is recoverable by the person, because nothing is exposed.

**Suggested:** a **Speak results** toggle in the Controller widget.
- Default **off** when `assistiveTech` is inferred, **on** otherwise — i.e. the
  current behaviour becomes the default rather than the rule.
- Persist per operator (it is a stable preference, and a good candidate for a
  `speakResults` setting on the profile so it follows them across surfaces).
- Keep it reachable by keyboard and labelled, since the people most affected are
  the ones who cannot see it.

Why a toggle rather than better detection: there is no reliable screen-reader
detection API, and every heuristic for it has historically been wrong often
enough to be an accessibility problem in its own right. Asking is cheaper and
honest.

**Why not just always speak** (the tempting simplification) — with a screen
reader running, a second `speechSynthesis` voice:
- **does not stop when they silence speech.** Ctrl (VoiceOver/NVDA/JAWS) stops
  the screen reader; it has no relationship to page TTS, which keeps talking
  after they told it to stop. This is the decisive one — it is a lost control,
  not noise.
- ignores their rate. Screen-reader users routinely run 300–500+ WPM; a default
  `SpeechSynthesisUtterance` is roughly a third of that, so the second voice is
  still going long after the first finished the same sentence.
- arrives out of sync — the live region is `polite` and queues; TTS fires
  immediately, so they hear it twice with a gap.
- ignores the voice, punctuation verbosity, and language switching they
  configured once.

`toolkit/controller/presentation.js`, `toolkit/controller/web/ui.js`

## 6. Split the politeness: acknowledgements assertive, results polite

`.aa-feedback` is a single `role="status" aria-live="polite"` region, and
everything goes through it. `polite` means the announcement **queues behind
whatever the person is currently reading**.

That is right for a long result — do not interrupt someone mid-sentence to read
them a headline. It is wrong for the acknowledgement: *"Ok, running: search for
braille music"* is a confirmation that an agent has just started acting on their
browser, and it is the only chance to catch a mis-recognition (*"braille
moozik"*) before it spends a minute on the wrong thing. Queued behind a
paragraph, it arrives too late to be worth saying.

**Suggested:** two regions rather than one.
- Acknowledgements and errors → `role="alert"` / `aria-live="assertive"`.
- Task results and content reads → the existing `polite` region.

Keep both visible in the same place; only the live-region semantics differ.

Worth pairing with a "stop"/"cancel" affordance while a task is running — an
assertive announcement that something has started is only useful if there is
something to do about it. (That is also where the verifier's `hold` would
attach.)

`toolkit/controller/web/ui.js`

---

# Catalog

## 7. `page-outline`'s panel is unreadable on any dark-themed site

**Measured contrast: 1.16:1.** WCAG AA needs 4.5:1. The panel is effectively
blank — white text on a white card. Seen on DuckDuckGo dark mode; it will happen
on every dark-themed site.

`page-outline.js:35` styles the panel container inline:

```js
nav.style.cssText = '… background: #fff; color: #111; …';
```

`color: #111` is right for the container, but it only reaches text that
**inherits**. The panel's contents are `<a>` elements, and the host page's own
`a { color: … }` rule beats inheritance — so on a dark site the links keep the
page's near-white link colour and land on the panel's white background:

```
panel background : rgb(255, 255, 255)
panel color      : rgb(17, 17, 17)     ← set, but not what the links use
link colour      : rgb(238, 238, 238)  ← from DuckDuckGo's stylesheet
contrast         : 1.16 : 1
```

This is worth treating as high severity rather than cosmetic: `pageOutline` is
derived for a **blind** profile (`pageStructure`), so the people most likely to
have it switched on are the least likely to notice it is broken — and a
low-vision user, who would notice, gets a panel they cannot read.

**Suggested:** set the colour on the elements that render text, not on an
ancestor, and use a scoped stylesheet with `!important` so the host page cannot
win:

```css
#ai4a11y-page-outline a,
#ai4a11y-page-outline a:visited,
#ai4a11y-page-outline a:hover { color: #0b3d91 !important; }
```

`skip-links` already does the right thing — it sets `color` **and**
`background` on the anchor itself, and measures 21:1 on the same page. The rule
that distinguishes them is worth stating in the adapter guidance: **injected UI
must set its own colours on the elements that paint them, defensively against
the host stylesheet.** Anything relying on inheritance is one dark-mode site away
from disappearing.

**Cheap regression test:** the toolkit's own `findLowContrastText` auditor
already catches this class — during this integration it flagged
`.ai4a11y-skip-link` and `.ai4a11y-bionic` on Wikipedia. Running the auditors
over the adapters' *own* injected UI, on a light page and a dark page, would
catch any future overlay that fails its own standard.

`tools/adapters/page-outline.js`
