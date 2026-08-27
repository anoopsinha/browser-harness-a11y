# Onboarding derives low-vision needs for a blind user

**Repo:** AI-for-Accessibility-Toolkit (`rearch-experiment`)
**Found:** 26 Aug 2026, integrating the toolkit into `browser-harness-a11y`
**Severity:** the adaptation a blind user receives is not merely incomplete — it is
the wrong modality entirely.

## What happens

A person onboards, selects support area **vision**, and types **"I'm blind"**.
The stored profile is faithful:

```json
{ "supportAreas": ["vision"], "freeText": "I'm blind" }
```

`getAbilityModel()` then returns:

```json
"needs": [
  { "dimension": "textSize",      "value": 1.5,            "source": "onboarding-derived" },
  { "dimension": "contrast",      "value": "yellow-black", "source": "onboarding-derived" },
  { "dimension": "readAloudRate", "value": 1,              "source": "onboarding-derived" }
]
```

Rendered through `deriveWebSettings`, that is:

```json
{ "fontScale": 150, "contrastMode": "yellow-black", "speechRate": 1 }
```

**150% yellow-on-black text, for someone who cannot see the screen.**

Nothing in the derived needs concerns structure, landmarks, labels, image
descriptions, live regions or SPA focus — the things a screen-reader user
actually depends on. Two of the three needs are inert for this person; the third
(`readAloudRate`) is owned by their own screen reader.

## Cause

`onboarding/server.js:99`

```js
const DEFAULT_NEEDS_BY_AREA = {
  vision: [{ dimension: 'textSize', value: 1.5 },
           { dimension: 'contrast', value: 'yellow-black' },
           { dimension: 'readAloudRate', value: 1.0 }],
  …
};
```

`vision` maps to exactly one answer, and that answer is magnification. The
disambiguating information — the sentence "I'm blind" — is never consulted:

```js
function deriveDefaultNeeds(areas) { … }        // takes areas only
const needs = deriveDefaultNeeds(areas);         // freeText is in scope, unused
```

So **"I'm blind" and "I need bigger text" produce byte-identical profiles.**

This matters more than a normal default being wrong, because the two populations
need *opposite* adaptations. The catalog already encodes the difference: the
`blind` preset is structure-only and changes nothing visual, while `lowVision`
is 150% text, magnifier and reflow. A profile that cannot distinguish them
cannot pick between them.

## Suggested fixes

1. **Split the area.** `vision` cannot be one row. Whether it becomes two
   support areas, or one area plus a follow-up question, the onboarding has to
   capture which it is — it is the single most consequential fact about this
   person's browsing.

2. **Read the free text.** `deriveDefaultNeeds` already has `freeText` in scope.
   `interpretNeedsPrompt` is the principled route (turn the sentence into needs);
   even a keyword check would beat shipping magnification to a blind user.
   Whatever infers it should land as a **proposal** rather than a fact, per
   "suggest, never apply" — the person confirms.

3. **Add dimensions for screen-reader needs.** There is currently no dimension
   that can express "describe images", "repair landmarks", or "announce dynamic
   updates", so even a correct inference has no vocabulary to write into.
   `needs[]` can only describe how a page should *look*.

## Related gaps found alongside

- **`fix-landmarks` is unreachable.** `tools/adapters/fix-landmarks.js` and
  `tools/auditors/missing-landmarks.js` both exist, but no key in
  `settingsMeta` maps to them, so no profile can request landmark repair. On
  Hacker News' front page the auditor reports **no main, no banner, no
  contentinfo, no navigation** — a page a screen-reader user navigates by brute
  force, with the fix sitting in the catalog unreachable.

- **`read-aloud` is unreachable** for the same reason — no setting key. (Its
  absence from the `blind` preset is correct on the merits: a blind user's
  screen reader owns the voice, and a second one talking over it is harmful. The
  bug is that low-vision, dyslexic and cognitive users cannot reach it either.)

- **`reflowColumn` blocks the renderer.** `tools/adapters/reflow-column.js:33`
  applies `float: none !important; column-count: 1 !important` through a
  universal selector, forcing a global style recalc that locks the main thread
  for tens of seconds on a large article — at load or on a settled page. It is
  in the `lowVision` preset, so the one preset that hangs is the one built for
  people who need reflow. Eleven of the twelve presets apply in under 0.3s.

## How this was found

`browser-harness-a11y` embeds the catalog and renders a person's profile into
whatever page an agent drives. Running the twelve presets against real pages,
and then running demo-user's actual onboarded profile, made each of these
visible as a concrete outcome rather than a code-reading. Happy to share the
harness-side reproduction.
