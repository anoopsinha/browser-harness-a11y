# Keep a deterministic fast path for the settings vocabulary under rawToTask

Measured against toolkit `main` @ **3198ff0**, driving `browser-harness-a11y`.

`rawToTask` is unconditional when the Controller drives a URL, so every
utterance goes to the receiver as a `task`. That correctly fixed compound
instructions — *"open google and search for apples"* now reaches the agent whole
instead of being partially executed as a search for "apples".

The cost is that the settings vocabulary goes the same way. This asks for a
narrowing, not a revert.

## Measured

Same request, same page (a Wikipedia article — a heavy document), through the
control protocol.

Deterministic path, 5 runs, milliseconds:

| call | median | min | max |
|---|---|---|---|
| `getContext` | 18.7 | 9.5 | 36.4 |
| `applySettings {fontScale:130}` | 130.0 | 92.9 | 155.0 |
| `undoLast` | 8.9 | 7.9 | 9.6 |

Agent path — *"make the text bigger"* as a task:

```
acknowledged in    8.9 ms
result after       8.1 s
```

A relative change costs `getContext` + `applySettings` ≈ **150 ms**, against
**8.1 s** through the agent — about **54× slower**, plus a model call per
utterance.

Two honest notes on those numbers:

- **Most of the 130 ms is page work, not protocol.** `VisualAssist` rescales
  text across a large article; `undoLast` at 8.9 ms and `getContext` at 18.7 ms
  show the round trip itself is single-digit-to-teens milliseconds. On a lighter
  page the whole operation is well under 50 ms.
- **8.1 s, not the 30–120 s seen for browsing tasks.** A single settings change
  is a short agent turn. The gap is real but smaller than a multi-step task
  would suggest.

## Correctness is no longer the argument

Earlier the agent could only improvise — it set `body { zoom: 1.2 }`, which died
on the next navigation and never reached the person's profile. That is fixed on
our side: the agent now runs this checkout (it was resolving a stale global
install) and `SKILL.md` documents the helpers, so it calls `a11y_apply` and
reports *"increased the text size to 150%"*.

So this is now purely about **latency, cost, and determinism** — not about the
agent being unable to do it.

That said, determinism still matters for these particular commands. The agent
applying the adapter is likely, not guaranteed; when it improvises instead, the
failure is invisible, because the page did get bigger.

## Suggested change

Under `rawToTask`, short-circuit an utterance that maps cleanly onto the
**receiver's declared `settingKeys`**; send everything else as a task.

```js
if (rawToTask) {
  const det = parse(utterance);
  // Only fully-consuming adapt/undo/query matches — the deterministic
  // vocabulary the receiver declared. Anything partial or unrecognised is a
  // task, so compound instructions stay fixed.
  if (det && consumesWholeUtterance(det, utterance)
          && (det.type === 'adapt' || det.type === 'undo' || det.type === 'query')) {
    return det;
  }
  return taskCommand(utterance);
}
```

`consumesWholeUtterance` is what stops this reintroducing the bug `rawToTask`
fixed:

- **"bigger text", "dark mode", "undo", "read this to me"** consume the whole
  utterance → deterministic. They go through `applySettings`, so they persist,
  appear in `activeSettings`, and can be undone.
- **"open google and search for apples"** does not → task, unchanged from today.

A ratio of matched length to utterance length is the crude form; "starts at the
beginning (allowing a leading politeness) and reaches the end" is cleaner. An
unmatched `\b(and|then|,)\b` in the remainder is a reliable tell for a second
clause.

`command` intents are deliberately excluded — `scroll`, `navigate`, `search`,
`activate` are things the agent does at least as well, and routing them through
it is what makes compound phrasing work. Only `adapt` / `undo` / `query` reach
the adapter catalog and the person's profile.

## Overhead of the guard itself

- `parse()` — a handful of regex tests, microseconds.
- One `getContext` round trip for *relative* changes only (to read the current
  value): **18.7 ms median**.

Against ~8 s saved per settings command. The guard costs noise.

## Why it belongs in the toolkit

The Controller is the only component holding both halves — the utterance *and*
the receiver's declared `settingKeys`. A consuming app doing this itself would
be writing a second grammar in its own language, duplicating these regexes and
drifting from them, and every other host (extension, mobile, XR) would have to
write one too. The registry vocabulary exists so that mapping is defined once.

If the classification is ever wrong, the failure mode is a settings command
reaching the agent — which is exactly what happens today, so the downside is
bounded.

`controller/router.js`, `controller/grammar.js` (`parse`)
