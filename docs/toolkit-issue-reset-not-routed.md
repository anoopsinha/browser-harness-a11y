# Toolkit: `resetToProfile` is not routed by the service, so it silently forgets nothing

Direction: **receiver → toolkit**. Follow-up to the reset work in `47687ce`,
which is otherwise exactly right — the receiver side is done and its shadow store
is gone.

## Symptom

Against the running stack, "reset to my profile" reports success and changes
nothing:

```
POST http://127.0.0.1:4000/api/reset-to-profile  {"uid":"demo-user"}
  -> {"ok":true}
```

No `forgotten`, no `scopes`, no `restored` — and the record is still there. The
profile still resolves `liveCaptions: false` afterwards, so the person is told
their settings were reset and they were not.

## Cause

`librarian.resetToProfile()` exists and works, but the HTTP service does not
expose it. `server/src/routes.js` lists 45 librarian routes;
`resetToProfile` is not among them:

```
grep -c "kind: 'librarian'" server/src/routes.js   ->  45
grep -n  "resetToProfile"    server/src/routes.js  ->  (nothing)
```

Calling it directly confirms it:

```
a11y_service("resetToProfile", {})  ->  HTTP 404 Not Found
```

So in `ONBOARD_MODE=remote`, `resetToProfileFor()` proxies to the service, the
call fails, `r.body?.result || {}` yields `{}`, and the route answers
`{ok: true}` spread with nothing. The failure is invisible.

In `ONBOARD_MODE=local` the method runs — but against onboarding's own data dir,
which is a different store from the service any receiver talks to, so it still
does not reset what the person is actually using.

## Fix

Add the route:

```js
{ route: 'resetToProfile', target: 'resetToProfile', kind: 'librarian' },
```

Worth pairing with: `resetToProfileFor` should not report `ok: true` when the
remote call returned no result. A reset that forgot nothing is either an error or
`forgotten: []`, and the two are worth telling apart — the person is being told
their settings went back to normal.

## Note on the two stores

Unrelated to the routing but found alongside it: `ONBOARD_MODE` defaults to
`local`, so out of the box the chat keeps its own copy of every profile under
`onboarding/onboarding-data` while a receiver reads the service on :8080. Same
person, two files. A preference the receiver recorded was invisible to the chat,
and the chat's reset cleared records the receiver never read.

`scripts/a11y` in browser-harness-a11y now starts onboarding with
`ONBOARD_MODE=remote` and `TOOLKIT_URL=http://127.0.0.1:8080` so both sides read
one store. A default of `remote` when `TOOLKIT_URL` is reachable, or a louder
warning about the split, would save the next person the same hunt.

## Acceptance

"turn off live captions", then "back to my profile" → the response names what was
forgotten, and the next profile sync turns Live Caption back on because the
profile asks for it.
