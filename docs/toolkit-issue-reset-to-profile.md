# Toolkit: "reset to my profile" belongs here, and a receiver-side shadow store is blocking it

Direction: **receiver → toolkit**. Raised as a design question, not a bug report:
the receiver currently holds state that this project models better, and that
state would silently defeat a reset added upstream.

## The gap

There is no way to say "forget what I've changed, go back to my profile".
`undoLast()` is LIFO and per-connection; `resetUndo()` clears the journal without
restoring anything. Neither answers "start again from who I am".

That matters more than a convenience: the whole point of the ability model is
that a profile describes the person. Once a session has drifted through a dozen
spoken adjustments, there is no way back to it.

## Why the receiver should not own it

Browser-harness now remembers browser-level settings the person asked for **by
name** — `liveCaptions`, `autoDescribe` and the other Chrome-owned ones — and
re-asserts them on every profile sync. It had to: "turn off live captions" was
undone by the next sync, which re-derived the setting from a hearing profile that
still says captions, so the person was told it was off while it was on.

But that record is a **private copy of provenance the toolkit already models**.
`user-explicit` is exactly this tier, and `recordScopedSettings` is exactly this
write. Two stores of the same fact, one of them invisible to the Controller,
unscoped by origin, and subject to no decay policy.

The consequence is concrete: **a reset added upstream would appear not to work.**
It would clear the toolkit's `user-explicit` records, `effectivePreferences`
would revert, and the receiver would then re-assert its own remembered choice on
the very next sync. The person asks to go back to their profile and captions stay
off, with nothing in the toolkit to explain why.

## What would fix it properly

**1. Let the profile name the setting.** `tools/profiles/settings.json` `deaf`
asks for `showCaptions` and `autoCaptions` but not `liveCaptions`:

```json
{"showCaptions": true, "autoCaptions": true, "enhanceFocus": true, ...}
```

So the receiver *infers* Live Caption from the caption keys. That inference is
the reason an explicit "off" conflicts with the profile at all — the profile
never said Live Caption, so nothing upstream can record that it is not wanted.
`liveCaptions` is in `settingsMeta` now; adding it to the preset (and to the
ability-model derivation for hearing) makes the profile the single source.

**2. Add a reset to the protocol.** Something like:

```
resetToProfile(scope?) → { restored: {...}, forgotten: [...] }
```

Dropping `user-explicit` records at that scope and recomputing
`effectivePreferences`, so the next sync restores profile behaviour. A grammar
phrase to reach it — "reset my settings", "back to my profile", "start over".

**3. Then the receiver deletes its shadow store**, records explicit
browser-setting choices through `recordScopedSettings` like every other setting,
and reads `liveCaptions` from the resolved settings rather than inferring it.
One store, one provenance model, and a reset that actually resets.

## Until then

The receiver keeps its local record, because without it a live bug returns. It
lives in the runtime dir, so it survives reconnects and restarts but not a
reboot. If a reset lands upstream before step 3, expect exactly the failure
described above.

## Acceptance

"turn off live captions", then "back to my profile" → captions return, because
the profile asks for them and nothing local is still overriding it.
