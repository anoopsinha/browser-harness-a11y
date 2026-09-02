# Toolkit: adapters that inject body children can break a page's layout

Direction: **receiver → toolkit**. Found by the adapters breaking one of our own
pages, which makes it likely they do it to other people's.

## What happened

The iframe host page laid its bar and frame out with:

```css
body { display: grid; grid-template-rows: auto 1fr; }
```

With the adapters applied, the bar rendered **878px tall** instead of 28px, and
the frame collapsed to 150px. The page had no CSS bug:

```
bodyRows: "0px 878px 150px"
children: [DIV#ai4a11y-skip-links:0, HEADER:878, IFRAME:150,
           SCRIPT:0, DIV#ai4a11y-announcer:1]
```

`skipLinks` injects its block as the **first child of body** and the announcer
appends another. Positional rows then shift by one: the injected div takes the
`auto` row and the real header takes the `1fr`.

## Why it matters beyond us

Our page was easy to fix — it now uses a column flex that does not care about
child order. A site being adapted for a person cannot be fixed, and this is the
kind of breakage that gets reported as "the accessibility tool broke the page".

Anything positional is exposed: `grid-template-rows` or `grid-template-areas` on
body, `body > *:first-child`, `:nth-child()` rules, and flex layouts that assume
a child count. Injecting a first child changes all of them.

Skip links do have to come first in **tab order** — that is the point of them —
but tab order is not the same as being the first element in the box layout.

## Options

1. **Take it out of flow.** `position: fixed` on the injected block, so it
   occupies no layout slot. It stays first in the DOM, so tab order is
   preserved, and it stops shifting sibling positions in grid and flex.
2. **Host it in a shadow root**, attached to a wrapper that is itself fixed.
   Also isolates the injected styles from the page's.
3. **Append rather than prepend**, and move focus to it programmatically. Keeps
   layout intact but gives up the natural tab order, so option 1 looks better.

The announcer has the same shape and is easier: it is visually hidden already,
so making it `position: fixed` costs nothing.

## Worth checking alongside

Whether any other adapter adds an element to `body` directly. The same reasoning
applies to each.

## Acceptance

A page laying out `body` with `grid-template-rows: auto 1fr` renders the same
with the adapters applied as without them.
