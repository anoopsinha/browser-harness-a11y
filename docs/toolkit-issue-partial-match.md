# Grammar matches part of an utterance and silently executes it

Against `rearch-experiment` @ **e2b7b25**.

An alternative to "when a remote receiver is connected, bypass the grammar and
send everything as a task". That change fixes the symptom below, but it also
routes the settings vocabulary to an agent — see **What bypassing costs** at the
end. This is the narrower fix.

## The bug

The grammar's rules are unanchored, so a rule can match a *fragment* of a longer
utterance and win. The rest of the sentence is discarded without a word to the
person.

Reproduced against the live grammar, with a receiver declaring
`['scroll','activate','back','forward','navigate','search','task']`:

```
"open google and search for apples"     -> search,   target "apples"
"go to news.ycombinator.com and tell
 me the title of the top story"         -> navigate, target "news.ycombinator.com"
"find me some apples"                   -> null  (correctly falls through to task)
```

Both of the first two are single instructions with two clauses. In each case the
grammar executed one clause and dropped the other. The person is told
*"Searching for apples"* / *"Opening news.ycombinator.com"* — which is true, and
is also not what they asked for.

This is worse than a plain no-match, because a no-match is visible: the
Controller says it didn't understand and the person rephrases. A partial match
looks like success, so the person believes the whole instruction was taken.

For a blind user it is worse still: the visible page changed in a way that
half-satisfies the request, and the only signal is a confirmation that sounds
correct.

## Suggested fix

**Only accept a grammar match when it accounts for substantially all of the
utterance.** Otherwise fall through to `task` (or to no-match where no `task`
action is declared).

Roughly, in `parse()`:

```js
const m = rule.re.exec(utterance);
if (!m) continue;
// A rule that consumed only part of the utterance has probably caught a clause
// of a longer instruction. Leaving the remainder unexecuted is worse than not
// matching, because the confirmation sounds like success.
const consumed = m[0].length / utterance.trim().length;
if (consumed < THRESHOLD) continue;   // → task
```

Notes on shaping it:

- **A ratio is the crude version.** A cleaner rule is "the match must start at
  the beginning (allowing a leading politeness) and reach the end". "please
  scroll down" should still match; "scroll down and read me the third result"
  should not.
- **Conjunctions are the reliable tell.** An unmatched `\b(and|then|,)\b` in the
  remainder is strong evidence of a second clause.
- **Keep short commands exact.** "bigger text", "dark mode", "undo",
  "scroll down" consume the whole utterance and must stay on the deterministic
  path — they are the ones that need to be instant and free.

## Why this shape rather than bypassing the grammar

The bug is not that a grammar exists; it is that a rule can win on a fragment.
Fixing the eagerness keeps two properties that a blanket bypass loses:

**1. The settings vocabulary stays instant, free, and deterministic.**

| utterance | with the guard | with a blanket bypass |
|---|---|---|
| "bigger text" | `applySettings {fontScale:110}` — immediate | task → agent, 30–120s, a model call |
| "dark mode" | `applySettings {darkMode:true}` | task → agent |
| "undo" | `undoLast` | task → agent; `undoLast` unreachable |
| "read this to me" | `getContent` | task → agent |
| "open google and search for apples" | **task** ✅ | task ✅ |

**2. The receiver's declared `settingKeys` keep meaning something.**
PROTOCOL.md calls that shared vocabulary "the contract"; a bypass makes it
decorative.

## What bypassing costs, concretely

Checked in `browser-harness-a11y`: the agent's `GEMINI.md` is 138 lines of the
browser-harness skill and contains **zero** mentions of `a11y_*`. The agent has
never heard of the adapter catalog, `a11y_apply`, or the person's ability
profile.

So under a blanket bypass, "dark mode" reaches an agent that cannot perform it.
It would drive the browser instead — toggling a site's own theme, or nothing —
while the toolkit adaptation, which is the Controller's reason to exist, is
never applied.

If the bypass is preferred anyway, two things need to follow or the
accessibility commands are simply lost:

1. The consuming app must teach its agent the adaptation helpers (for us, adding
   `a11y_*` documentation to `GEMINI.md`).
2. Something must keep a fast path for the receiver's declared `settingKeys`, so
   the commands a blind user needs most do not each cost a minute and a model
   call.

`toolkit/controller/grammar.js` (`parse`), `toolkit/controller/router.js`
