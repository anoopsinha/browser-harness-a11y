# A hearing profile gets no captions on a video that already has them

Traced against toolkit `main` while running a Deaf/HoH profile against YouTube.

## What happens

Captions stay off. The chain dead-ends:

```
hearing support area  →  need { dimension: 'captions', value: true }
                      →  setting  autoCaptions: true
                      →  adapter  generate-captions
                      →  imports transcribeVideo / transcribeAudio from utils/ai.js
                      →  no LLM caller wired  →  reported needs-ai, never applied
```

The receiver reports `needs-ai` rather than pretending, so nothing is silently
broken — but a Deaf/HoH profile ends up with `enhanceFocus` and
`soundVisualizer` and **nothing at all for captions**.

## The part worth fixing regardless of the LLM

Even with a model wired, `generate-captions` would be the wrong tool here. It
*transcribes* media that has no captions. A YouTube video almost always **already
has** a caption track — it needs switching on, not transcribing. That is
`video.textTracks[i].mode = 'showing'`, or a click on the player's CC button. No
LLM, no latency, no cost.

Nothing in the catalog does it. Checked every adapter for `textTracks`,
`showing`, `ytp-subtitles` and caption-button handling:

- `generate-captions` — AI transcription.
- `auto-transcriber` — also AI (`getYouTubeTranscript`), and **not in the
  registry**, so no setting key reaches it at all.
- `autoCaptions` is the only caption-shaped key in `settingsMeta`, and it points
  at the transcription path.

So the common case — captions exist, turn them on — has no route, while the rare
case has an expensive one.

## Suggested: a `showCaptions` adapter

Turn on what is already there.

```js
// Native <video> with text tracks: pick the person's language, else the first.
for (const v of document.querySelectorAll('video')) {
  const tracks = [...v.textTracks].filter(t => t.kind === 'captions' || t.kind === 'subtitles');
  if (!tracks.length) continue;
  const want = tracks.find(t => (t.language || '').startsWith(lang)) || tracks[0];
  want.mode = 'showing';
}
```

Players that own their captions (YouTube among them) do not always expose a
usable `textTracks` entry, so a per-player fallback is needed — for YouTube,
pressing `.ytp-subtitles-button` when `aria-pressed="false"`. Worth treating that
as the first entry in a small table of player selectors rather than a YouTube
special case, since Vimeo and the big news players each have their own.

Two behaviours that matter for this audience:

- **Re-apply on new media.** SPA navigation and autoplay playlists swap the video
  element without a page load; a one-shot pass turns captions on for the first
  video and nothing after. The shared sweep in `utils/observe.js` already exists
  for this.
- **Do not fight the person.** If they turn captions off, leave them off — a
  sweep that re-enables on every mutation is worse than not running.

## Suggested: split the setting key

`autoCaptions` currently means two different things. They deserve separate keys
so a profile can ask for the cheap one without the expensive one:

| key | meaning | needs AI |
|---|---|---|
| `showCaptions` | turn on captions the media already has | no |
| `autoCaptions` | generate captions for media that has none | yes |

The `hearing` area should derive **both** — `showCaptions` as the baseline that
always works, `autoCaptions` as the enhancement that activates when a model is
available. Today it derives only the second, which is why the profile does
nothing on the sites where captions are one toggle away.

`captions` as a *need* dimension can stay as it is; it is the rendering into
settings that needs the split.

## Also worth a look

`auto-transcriber` exists, is AI-powered, and is unreachable from any setting
key — the same reachability gap that `fix-landmarks` and `read-aloud` had.
Either wire it to `autoCaptions` or drop it, but leaving it in the catalog
unreachable makes the catalog look larger than it is.

`tools/adapters/` (new adapter), `toolkit/registry/tools.js` (`settingsMeta`),
`onboarding/server.js` (`DEFAULT_NEEDS_BY_AREA.hearing`),
`toolkit/platforms/chrome/web-surface.js` (`WEB_DERIVATION`)
