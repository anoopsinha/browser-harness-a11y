# Settings commands now route to the agent and lose the adaptation

Against `rearch-experiment` @ **83bba63**. Supersedes the earlier partial-match
write-up: `rawToTask` has shipped and fixed that symptom. This is what it cost.

## What changed

`rawToTask` is now unconditional when the Controller drives a URL — no local
grammar, every utterance goes to the receiver as a `task`. That correctly fixes
compound instructions: *"open google and search for apples"* now reaches the
agent whole, instead of being partially executed as a search for "apples".

Verified end to end against `browser-harness-a11y`:

```
"open google and search for apples"
  → performAction("task", null, "open google and search for apples")
  → note: "I have searched for \"apples\" on Google in the specified tab."
  → tab:  google.com/search?q=apples          ✅
```

## The problem

The settings vocabulary goes the same way, and the adaptation is lost.

`"bigger text"` now reaches the agent. It answers *"The text size has been
increased"*, which sounds right. Measured on the page afterwards:

```
adapters live                    : FixLandmarks, KeyboardNavigator,
                                   LiveRegionAnnouncer, PageOutline,
                                   SkipLinks, SpaFocus     ← VisualAssist absent
ai4a11y-visual-assist stylesheet : false
body zoom                        : 1.2                     ← the agent's own CSS
```

The agent set `zoom: 1.2` on the body instead of applying the toolkit adapter.
The outcome looks correct and the mechanism is detached from the person:

- **It does not survive navigation.** No adapter is enabled, so the sticky
  re-apply has nothing to re-apply. Next page load, it is gone.
- **It is not in `activeSettings`.** The next relative request ("bigger still")
  reads a context that does not know text was ever enlarged.
- **`undoLast` cannot revert it.** There is no journal entry — the change never
  went through `applySettings`.
- **It is never recorded to the profile.** A preference the person stated aloud
  does not become theirs; it dies with the tab.
- **It costs ~30–60s and a model call** for something that was instant and free.

This matters more than the latency: these are the *accessibility* commands. A
blind or low-vision person asking for bigger text, dark mode, or reduced motion
is the Controller's core case, and it is now the path that degrades — silently,
because the confirmation sounds like success.

## Suggested fix

Under `rawToTask`, still short-circuit an utterance that maps cleanly onto the
**receiver's own declared `settingKeys`**, and send everything else as a task.

```js
if (rawToTask) {
  const det = parse(utterance);
  // Keep only fully-consuming adapt/undo/query matches — the deterministic
  // vocabulary the receiver declared. Anything partial or unrecognised is a
  // task, so compound instructions stay fixed.
  if (det && consumesWholeUtterance(det, utterance)
          && (det.type === 'adapt' || det.type === 'undo' || det.type === 'query')) {
    return det;
  }
  return taskCommand(utterance);
}
```

`consumesWholeUtterance` is the guard from the original write-up, and it is what
keeps this from reintroducing the bug `rawToTask` fixed:

- **"bigger text", "dark mode", "undo", "read this to me"** consume the whole
  utterance → deterministic, instant, free, and they go through `applySettings`
  so they persist, appear in `activeSettings`, and can be undone.
- **"open google and search for apples"** does not → task, unchanged from today.

A ratio of matched length to utterance length is the crude form; "starts at the
beginning (allowing a leading politeness) and reaches the end" is cleaner. An
unmatched `\b(and|then|,)\b` in the remainder is a reliable tell for a second
clause.

Note the `command` type is deliberately excluded above — `scroll`, `navigate`,
`search`, `activate` are all things an agent can do at least as well, and
routing them through the agent is what makes compound phrasing work. It is only
`adapt` / `undo` / `query` that have no agent equivalent, because they go
through the ControlPort into the adapter catalog and the person's profile.

## Why this cannot be fixed in the consuming app

The obvious workaround on our side is to teach the agent the adapter helpers —
append `a11y_*` documentation to the agent's context so `"bigger text"` calls
`a11y_apply(fontScale=…)` rather than improvising a zoom.

That is worth doing anyway, but it does not solve this:

- Every consuming app would have to do it independently, and get it right.
- It still costs a model call and ~30–60s for a command that has a deterministic
  answer.
- It is probabilistic. The agent might apply the adapter, or might zoom again —
  and when it improvises, the failure is invisible, because the page did get
  bigger.

The receiver already tells the Controller exactly which keys it can apply, in
the registry's shared vocabulary. That declaration is the contract PROTOCOL.md
describes; using it for these four intents is cheaper and more reliable than
asking a model to rediscover it each time.

`toolkit/controller/router.js`, `toolkit/controller/grammar.js` (`parse`)
