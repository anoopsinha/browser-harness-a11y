# Toolkit issue list

Found integrating the toolkit into `browser-harness-a11y`. Verified against
`rearch-experiment` @ **a7b06db** on 27 Aug 2026 — anything already fixed is
listed at the bottom so it doesn't get re-done.

Ordered by what blocks the most.

---

## 1. Router refuses arbitrary commands instead of handing them to the receiver

**Blocks:** a person saying anything the grammar doesn't already know.

`createRouter` tries the grammar, then the LLM lane if one is supplied, then
returns `noMatch`. With no LLM lane wired — which is the demo's default —
*"search for braille music books"* or *"open wikipedia.org"* never becomes a
call. It dies in the Controller and the receiver never hears it.

That matters because the receiving app may be able to do far more than the
grammar can express. `browser-harness-a11y` can drive a full agent (Gemini CLI
over the browser-harness skill), so it can execute an arbitrary instruction —
but has no way to be offered one.

**Suggested:** let a receiver declare an action meaning *"give me anything you
couldn't parse"* (e.g. `task`), and have `resolve()` fall through to it on
`noMatch` with the raw utterance as `text`, rather than refusing. Keeps the
grammar deterministic for the settings vocabulary and routes the rest to
whatever the app can actually do. No model needed in the Controller.

`toolkit/controller/router.js`

## 2. Grammar has no navigation or search rules

Four patterns exist: `scroll`, `go forward`, `go back`, and
`click|press|tap|activate|open|select <target>`. *"open wikipedia.org"* matches
the last one and becomes `activate` — so the receiver hunts for a **link named
"wikipedia.org" on the current page**, finds none, and reports no match. There
is no way to express "go to a URL" or "search for X".

**Suggested:** `open <url|domain>` → a `navigate` action, and `search for <q>` →
a `search` action, both gated on the receiver declaring them. Deterministic and
useful even where #1 lands, since these are common enough to deserve grammar
rather than an agent round-trip.

`toolkit/controller/grammar.js`

## 3. `fixLandmarks` adds only `navigation`, never `banner` or `contentinfo`

The setting key and adapter now exist (thank you — that closed the reachability
gap). But `ensureStructuralLandmarks()` only ever sets `role="navigation"`, and
only on `div[class*="nav" i]` elements that pass a link-density test.

Measured on the Hacker News front page, sticky re-apply disabled so the before
reading is clean:

```
before: hasMain true, hasBanner false, hasContentinfo false, hasNavigation false
after : hasMain true, hasBanner false, hasContentinfo false, hasNavigation true
```

A screen-reader user still cannot jump to the header or the footer, which are
two of the four regions landmark navigation exists for. Sites built on tables or
non-`nav`-classed divs (HN being the canonical case) get nothing for banner or
contentinfo.

**Suggested:** infer `banner` from a top-of-body header-like block and
`contentinfo` from a bottom-of-body one, the same shape as the existing nav
heuristic.

`tools/adapters/fix-landmarks.js`

## 4. Re-onboarding appends duplicate notes instead of upserting

`demo-user` currently holds **six identical** `"I'm blind"` records, one per
onboarding run, each `user-explicit` at confidence 1.

They carry no settings so they don't affect adaptation, but `recall()` and the
retrieval scorer weight repetition — so a fact stated once and re-saved six
times outranks a fact stated once. Any host that re-onboards, or a person who
corrects themselves, silently inflates their own history.

**Suggested:** `onboard()` upserts the free-text note the way
`recordScopedSettings` already upserts per `aspect`, rather than appending.

`onboarding/server.js` → `addNote`

## 5. `scopeLabel` composes without a separator

`recordScopedSettings` builds `You set ${key} to ${value}${where}.` The built-in
fallbacks each begin with a space (` on news sites`), so they read correctly —
but a caller passing `scopeLabel` naturally gets no space:

```
"You set fontScale to 140everywhere."
"You set readingRuler to truereference sites."
```

The parameter is documented as "a human phrase for the record text", which
doesn't suggest it must start with a space. These strings are user-facing —
they're what a person is shown when reviewing their own profile.

**Suggested:** normalise in `recordScopedSettings` (prepend a space when the
caller's label lacks one) rather than making every caller remember.

`toolkit/core/librarian.js` ~line 407

## 6. `user-explicit` is reachable by any token holder, with no record of who wrote it

`recordScopedSettings` writes at `source: 'user-explicit'`, confidence 1 — the
strongest tier, which by design gets final say in `getEffectivePreferences` and
is what the decay/proposal machinery deliberately will not touch. The record
text reads *"You set …"*.

Nothing distinguishes a person toggling a control from a script POSTing with a
bearer token. While integrating, an agent (me) wrote seven such records into a
profile; they were indistinguishable from real ones, and they outranked the
person's own onboarding until deleted.

This is not hypothetical for the roadmap: the moment the verifier, an insight
loop, or a Controller writes preferences, that is the door they go through.

**Suggested:** a `writer` field (`person` | `agent` | `import`) alongside
`source`. Keeps the strength semantics intact while letting review surfaces and
the proposal budget tell the two apart.

`toolkit/core/librarian.js`

## 7. `ActuationPort.readPage` presumes the host speaks

The port's method is named "read the page aloud". For a screen-reader user that
is the failure mode: a second voice over VoiceOver, at the app's rate, ignoring
their verbosity and punctuation settings. `announce()` already gets this right
by writing to an ARIA live region — the message arrives in the person's own
voice, at their speed, queued behind what they are already reading.

A port method named `readPage` invites every host to reach for
`speechSynthesis`, which is wrong for exactly the users it is meant to serve.

**Suggested:** rename to something like `deliver(text, {interrupt})` — "put this
in front of the person" — so the host chooses live region (web), UIAccessibility
(iOS), spatial audio (XR), or actual TTS (kiosk with no AT).

`toolkit/ports/actuation.js`

## 8. `voice-commands` vocabulary is spatial, so it cannot help a non-visual user

Nine fixed phrases: `scroll down/up`, `page down/up`, `go to top/bottom`,
`go back/forward`, `click`. Every one is a *movement*; `click` acts on whatever
happens to be focused. There is no way to say **what** you want, only where to
move — and the recognised text is shown in a visual feedback element.

It is reachable only from the `motor` preset, which is consistent with what it
does. Flagging it because "voice commands" reads like voice access for blind
users and isn't.

**Suggested:** either scope the name/description to motor use explicitly, or
grow it toward semantic commands ("read me the third result") — which is really
the `voice-mode.md` architecture, and belongs in a host with a real microphone
rather than in a page adapter.

`tools/adapters/voice-commands.js`

## 9. `blind` preset carries two adapters that do little for a blind user

- **`page-outline`** builds an on-page headings navigator. NVDA/JAWS have H-key
  heading navigation and VoiceOver has the rotor, so this largely duplicates
  something the person already has, in a worse form.
- **`keyboard-nav`** contributes enhanced focus indicators and a tab-sequence
  overlay (both visual), and skip links that duplicate the `skip-links` adapter.
  Its `Alt+1/2/H/F` shortcuts are worth testing against a real screen reader —
  NVDA and JAWS bind heavily in that space, and a collision would make things
  worse rather than better.

**Suggested:** verify against a real screen reader before keeping them in
`blind`; both are strong choices for *sighted keyboard* and *low-vision* users.

`tools/profiles/settings.json`

---

## Already fixed — listed so nobody redoes them

- **Blind vs low-vision derivation** (a7b06db, 330762c). A blind profile now
  derives `describeImages, labelControls, repairLandmarks, announceUpdates,
  spaAnnounce, skipLinks, pageStructure, keyboardAccess` at `floor` strength,
  rendering to the catalog's `blind` preset. Verified end to end through the
  harness: six screen-reader adapters apply, `autoDescribe`/`autoFixLabels`
  correctly report `needs-ai`.
- **`reflowColumn` renderer hang.** The universal-selector rule is now a finite
  element list with a comment explaining the style-sharing cost. Was locking the
  main thread for tens of seconds on large articles.
- **`fixLandmarks` and `readAloud`/`speechRate` reachability.** `settingsMeta`
  keys now exist for all three.
