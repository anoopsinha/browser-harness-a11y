# Toolkit: onboarding cannot say "none", so a profile can be created but never undone

Direction: **receiver → toolkit**. Raised from running the stack day to day
against a profile onboarded as a test persona.

## What is wanted

An explicit **none** for areas of need, which clears `supportAreas` and `needs`
rather than leaving the last answer in place.

## Why it matters

A profile asserts itself on every sync — that is the design, and it is right: a
person's needs do not reset when they reopen a browser. The corollary is that a
profile which no longer describes anyone keeps adapting a browser anyway, and
there is currently no way to say so.

Concretely: `demo-user` is onboarded as *"i'm deaf"*, so every sync reasserts
captions, Live Caption, dark mode, 140% text and high contrast. That is correct
for a hearing-profile test session and wrong for the same profile used while
developing — and the only escape today is recording a `user-explicit` override
per setting, which `resetToProfile` then clears.

It matters beyond a test fixture. Needs change. Someone who onboarded during an
injury, or picked the wrong area, has no way back.

## Three things block it

**1. The form refuses an empty answer** — `onboarding/index.html:196`

```js
if (!payload.freeText && supportAreas.length === 0) {
  return setStatus($('add-status'), 'Enter a need or pick at least one area.', 'err');
}
```

So "nothing" cannot be submitted at all.

**2. There is no affordance for it.** Areas are checkboxes built from
`cfg.supportAreas` (`index.html:177-180`). Unchecking everything is the only way
to express none, which reads as an unfinished form rather than an answer — and
is refused anyway by the guard above.

**3. The API would not clear it even if asked** — `onboarding/server.js`, in
`onboard()`:

```js
if (areas.length) await remoteLibrarian(token, 'setProfileField', ['supportAreas', areas]);
// Always write (even []) so a re-onboard clears stale needs — e.g. a profile
// corrected from low-vision to blind must drop the old magnification needs.
await remoteLibrarian(token, 'setProfileField', ['fields.needs', needs]);
...
if (text) { await remoteLibrarian(token, 'setProfileField', ['freeText', text]); ... }
```

`needs` is written unconditionally and the comment says exactly why. Its two
siblings are not. So an empty submission would clear `needs` while leaving
`supportAreas: ["hearing"]` and `freeText: "i'm deaf"` behind — a profile that
disagrees with itself, and one where anything re-deriving from the areas brings
the needs straight back.

`deriveDefaultNeeds` itself is already correct: it loops over `areas`, so an
empty list yields no needs, and free text alone adds none.

## The wider shape it belongs to

The form only ever adds. It is titled "Add a profile", it opens empty, and it
never shows what the profile currently says — so the only way to change one
answer is to retype all of them, and an empty box means "no opinion" rather than
"nothing". That is why "none" has nowhere to live: a form that cannot show the
current answer cannot express changing it to none either.

So the same change should make it an editor:

- **Rename the section to "Profile."** It already updates as well as creates —
  both buttons say "Update profile" — and the heading is the last thing still
  calling it an add.
- **Fill it from the current profile.** `freeText` into the "What do you need"
  box, `supportAreas` into the checkboxes, the vision kind into its radio, and
  the uid into the id field so submitting updates that profile rather than
  minting another. `loadCurrent()` already fetches exactly this and only uses it
  for the banner.
- **`/api/profile` needs to return `visionKind`.** `profileConfig()` returns
  `supportAreas` and `freeText` only, so the blind / low-vision answer — by its
  own comment "the single most consequential fact about how the page should
  adapt" — cannot be shown back to the person who gave it, and silently resets
  on the next update.
- **Then add "None of these"** to the areas group, exclusive with the rest:
  choosing it clears the others, choosing any other clears it. With the fields
  filled, unchecking everything is now genuinely ambiguous — it could be a
  half-finished edit — so none has to be a positive answer, not an absence.

## Suggested fix

- **A "None of these" option** in the areas group, which clears the other
  checkboxes when chosen and is cleared by choosing any of them. An explicit
  answer rather than an absence.
- **Allow the empty submission** when none is chosen — the guard should refuse
  an *unanswered* form, not an answer of none.
- **Write `supportAreas` and `freeText` unconditionally**, as `needs` already
  is, so a re-onboard clears what it no longer says. The rationale in that
  comment applies unchanged to both.

Worth deciding at the same time: whether choosing none should also drop
`user-explicit` settings the person recorded by hand, or leave them. Leaving
them is probably right — those were their own decisions, not derived from the
persona — but it should be a decision rather than a side effect.

## Acceptance

Open onboarding against an existing profile: the heading reads "Profile", the
need box carries its free text, its areas are checked, and its vision kind is
selected. Choose "None of these" — the other boxes clear. Submit, and read the
profile back: `supportAreas: []`, `needs: []`, `freeText: ""`, with the next
sync asserting nothing.

Then re-check an area and submit again: the profile comes back, which is what
tells you none cleared rather than broke it.
