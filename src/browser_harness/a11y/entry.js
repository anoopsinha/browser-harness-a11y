// esbuild entry → src/browser_harness/a11y/bundle.js (a classic IIFE injected
// into a page over CDP). Built by scripts/build_a11y.py, which copies this file
// into the toolkit checkout so the relative imports below resolve there.
//
// The toolkit ships the catalog and the settings vocabulary but not a dispatcher
// from one to the other — the Chrome extension owned that. APPLY below is that
// dispatcher: one row per setting key in the registry's settingsMeta, naming the
// adapter it drives and the options it passes.
import * as adapters from './tools/adapters/index.js';
import * as auditors from './tools/auditors/index.js';
import * as profiles from './tools/profiles/settings.js';
import * as aria from './tools/validators/aria-parse.js';
import { settingsMeta } from './toolkit/registry/tools.js';
import { deriveWebSettings } from './toolkit/platforms/chrome/web-surface.js';
import { coerceSetting } from './toolkit/core/units.js';

const A = adapters;

// Settings whose adapters call into utils/ai.js for something an LLM has to
// answer. Reported as needs-ai rather than enabled, because an adapter that
// reports itself on and then does nothing is worse than one that says it cannot
// run — a blind user pressing Alt+D would get silence with no explanation.
// Verified against what each adapter actually imports, not against its category.
const NEEDS_AI = new Set([
  'autoDescribe', 'autoVideoDescribe', 'autoSimplify', 'autoSummarize',
  'autoFixLabels', 'autoCaptions', 'fixContrast',
  'describeOnDemand', 'exploreChart',
]);

// Repairs applied by handler in response to an auditor finding, not by an
// enable() toggle. wcag-fixes needs no LLM — it fixes lang attributes, duplicate
// ids, heading order, tabindex and ARIA validity, which is exactly the structural
// repair a screen reader depends on. It needs the audit→fix path, not a11y_apply.
const NEEDS_AUDIT = new Set(['autoWcagFix']);

// VisualAssist is one adapter driven by eight settings, so it is collected
// rather than dispatched per key.
const VISUAL = ['fontScale', 'lineHeight', 'letterSpacing', 'largeCursor',
                'enhanceFocus', 'dyslexiaFont', 'readingGuide', 'contrastMode'];

// setting key → [adapter, options-from-settings]. `on` means the setting is a
// plain boolean toggle and the adapter takes no options.
const on = (adapter) => [adapter, () => undefined];

const APPLY = {
  darkMode: on(A.DarkMode),
  motionReducer: on(A.MotionReducer),
  dismissOverlays: on(A.DismissOverlays),
  bigTargets: on(A.BigTargets),
  readerMode: on(A.ReaderMode),
  highlightLinks: on(A.LinkHighlighter),
  pageOutline: on(A.PageOutline),
  bionicReading: on(A.BionicReading),
  unpinSticky: on(A.UnpinSticky),
  muteSounds: on(A.MuteSounds),
  defineWords: on(A.DefineWords),
  stopAutoAdvance: on(A.StopAutoAdvance),
  reduceBrightness: on(A.ReduceBrightness),
  soundVisualizer: on(A.SoundVisualizer),
  announceUpdates: on(A.LiveRegionAnnouncer),
  magnifier: on(A.Magnifier),
  flashGuard: on(A.FlashGuard),
  reflowColumn: on(A.ReflowColumn),
  focusLocator: on(A.FocusLocator),
  persistentHover: on(A.PersistentHover),
  readingRuler: on(A.ReadingRuler),
  confirmActions: on(A.ConfirmActions),
  rememberSpot: on(A.ReadingSpot),
  expandAbbreviations: on(A.AbbreviationExpand),
  languageTag: on(A.LanguageTag),
  spaFocus: on(A.SpaFocus),
  skipLinks: on(A.SkipLinks),
  mathAccessible: on(A.MathA11y),
  keyboardNav: on(A.KeyboardNavigator),
  voiceCommands: on(A.VoiceCommands),

  // Settings that carry a value rather than a flag.
  colorFilter: [A.ColorBlindMode, (s) => s.colorFilter],
  translatePage: [A.TranslatePage, (s) => ({ target: s.translateTo || 'en' })],
  focusMode: [A.FocusMode, (s) => ({
    hideDistractions: s.hideDistractions !== false,
    showProgress: s.showProgress !== false,
  })],
};

// Settings consumed as options by another key's adapter, so asking for them
// alone is a no-op rather than an error.
const ABSORBED = new Set(['hideDistractions', 'showProgress', 'translateTo', ...VISUAL]);

function isOff(v) {
  return v === false || v === undefined || v === null || v === '' || v === 'none';
}

/** Apply a settings object. Returns what happened, per key. */
function apply(settings = {}) {
  const applied = [];
  const skipped = [];
  const errors = [];

  // VisualAssist first: several keys, one adapter, and it reads unscaled sizes.
  const visual = {};
  for (const k of VISUAL) if (settings[k] !== undefined) visual[k] = settings[k];
  if (Object.keys(visual).length) {
    try {
      A.VisualAssist.enable(visual);
      applied.push({ adapter: 'visual-assist', from: Object.keys(visual) });
    } catch (e) {
      errors.push({ adapter: 'visual-assist', error: String(e) });
    }
  }

  for (const [key, value] of Object.entries(settings)) {
    if (key === 'enabled' || ABSORBED.has(key)) continue;
    if (isOff(value)) continue;
    if (NEEDS_AI.has(key)) { skipped.push({ setting: key, reason: 'needs-ai' }); continue; }
    if (NEEDS_AUDIT.has(key)) { skipped.push({ setting: key, reason: 'needs-audit-pipeline' }); continue; }

    const row = APPLY[key];
    if (!row) { skipped.push({ setting: key, reason: 'no-adapter' }); continue; }

    const [adapter, opts] = row;
    if (!adapter || typeof adapter.enable !== 'function') {
      skipped.push({ setting: key, reason: 'adapter-missing' });
      continue;
    }
    try {
      const o = opts(settings);
      o === undefined ? adapter.enable() : adapter.enable(o);
      applied.push({ adapter: key, from: [key] });
    } catch (e) {
      errors.push({ adapter: key, error: String(e) });
    }
  }
  return { applied, skipped, errors };
}

/** Apply one of the twelve presets by id. */
function applyProfile(id) {
  const p = profiles.profiles[id];
  if (!p) return { error: `unknown-profile: ${id}`, known: Object.keys(profiles.profiles) };
  return { profile: id, name: p.name, ...apply(p.tools) };
}

/** Turn every adapter that exposes disable() back off. */
function disableAll() {
  const off = [];
  for (const [name, a] of Object.entries(A)) {
    if (a && typeof a === 'object' && typeof a.disable === 'function' && a.enabled) {
      try { a.disable(); off.push(name); } catch { /* an adapter that will not stop is not fatal */ }
    }
  }
  if (A.VisualAssist?.enabled) { try { A.VisualAssist.disable(); off.push('VisualAssist'); } catch {} }
  return off;
}

/** Which adapters currently report themselves enabled. */
function status() {
  const live = [];
  for (const [name, a] of Object.entries(A)) {
    if (a && typeof a === 'object' && a.enabled === true) live.push(name);
  }
  return live;
}

/** A finding as something that survives the CDP boundary.
 *  Auditors return either bare elements or a record wrapping one (poor-contrast
 *  carries the measured ratio alongside it), so both shapes are handled. */
function describeEl(el) {
  if (el && el.nodeType !== 1 && typeof el === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(el)) {
      out[k] = (v && v.nodeType === 1) ? describeEl(v)
             : (typeof v === 'number') ? Math.round(v * 100) / 100
             : v;
    }
    return out;
  }
  if (!el || el.nodeType !== 1) return String(el);
  const bits = [el.tagName.toLowerCase()];
  if (el.id) bits.push(`#${el.id}`);
  const cls = (el.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean).slice(0, 2);
  if (cls.length) bits.push('.' + cls.join('.'));
  const out = { at: bits.join('') };
  const src = el.getAttribute('src') || el.getAttribute('href');
  if (src) out.src = src.slice(0, 120);
  const text = (el.textContent || '').trim().replace(/\s+/g, ' ');
  if (text) out.text = text.slice(0, 80);
  return out;
}

// runAxeAnalysis needs axe-core on the page and we do not bundle it; it is named
// in the result as unavailable rather than dropped, so a caller is never told a
// check passed when it never ran.
const NOT_BUNDLED = { runAxeAnalysis: 'needs axe-core on the page' };

/** Run the zero-argument auditors over the live page. */
function audit({ samples = 5 } = {}) {
  const findings = {};
  const unavailable = {};
  for (const [name, fn] of Object.entries(auditors)) {
    if (typeof fn !== 'function' || fn.length > 0) continue;
    if (!/^(find|page|audit)/.test(name)) continue;
    if (NOT_BUNDLED[name]) { unavailable[name] = NOT_BUNDLED[name]; continue; }
    try {
      const r = fn();
      if (Array.isArray(r)) {
        if (r.length) findings[name] = { count: r.length, samples: r.slice(0, samples).map(describeEl) };
      } else if (r === true) {
        findings[name] = true;
      } else if (r && typeof r === 'object') {
        findings[name] = r;
      }
    } catch (e) {
      unavailable[name] = String(e);
    }
  }
  return { findings, unavailable, url: location.href };
}

/** Setting keys this receiver can actually apply.
 *  The control protocol requires settingKeys to be registry settingsMeta keys —
 *  that shared vocabulary is the contract — so this is the intersection of the
 *  dispatcher above with the registry, computed rather than hand-listed so the
 *  two cannot drift. Keys needing an LLM or the audit pipeline are excluded:
 *  the Controller must only offer what will actually happen.
 */
function supportedKeys() {
  const mine = new Set([...Object.keys(APPLY), ...VISUAL]);
  return Object.keys(settingsMeta)
    .filter((k) => mine.has(k) && !NEEDS_AI.has(k) && !NEEDS_AUDIT.has(k));
}

/** Current non-default settings, as the control protocol's activeSettings. */
function activeSettings() {
  const out = {};
  const va = A.VisualAssist;
  if (va?.enabled) {
    for (const k of VISUAL) {
      const v = va.settings?.[k];
      if (v !== undefined && v !== false && v !== 'none' && !(k === 'fontScale' && v === 1)) out[k] = v;
    }
  }
  for (const [key, row] of Object.entries(APPLY)) {
    const ad = row[0];
    if (ad && ad.enabled === true) out[key] = true;
  }
  return out;
}

/** Accessible names of things a person could ask to activate. */
function targets(limit = 40) {
  const sel = 'a[href], button, [role="button"], [role="link"], input[type="submit"], summary';
  const seen = new Set();
  for (const el of document.querySelectorAll(sel)) {
    const name = (el.getAttribute('aria-label') || el.textContent || el.value || '')
      .trim().replace(/\s+/g, ' ');
    if (name && name.length <= 60) seen.add(name);
    if (seen.size >= limit) break;
  }
  return [...seen];
}

/** Click the element whose accessible name best matches `label`. */
function activate(label) {
  const want = String(label || '').trim().toLowerCase();
  if (!want) return { ok: false, detail: 'no target given' };
  const sel = 'a[href], button, [role="button"], [role="link"], input[type="submit"], summary';
  const els = [...document.querySelectorAll(sel)];
  const nameOf = (el) => (el.getAttribute('aria-label') || el.textContent || el.value || '')
    .trim().replace(/\s+/g, ' ');
  let hit = els.find((el) => nameOf(el).toLowerCase() === want)
         || els.find((el) => nameOf(el).toLowerCase().includes(want));
  if (!hit) return { ok: false, detail: `no target matching "${label}"` };
  const name = nameOf(hit);
  hit.scrollIntoView({ block: 'center' });
  hit.click();
  return { ok: true, detail: `activated ${name}` };
}

/** Readable content: headings for an outline, innerText for the full read. */
function content(mode = 'outline', chunk = 0, chunkChars = 4000) {
  const title = (document.title || '').replace(/^\uD83D\uDC34\s*/, '');
  if (mode === 'outline') {
    const hs = [...document.querySelectorAll('h1,h2,h3,[role="heading"]')]
      .map((h) => h.textContent.trim().replace(/\s+/g, ' '))
      .filter(Boolean).slice(0, 60);
    if (!hs.length) return { error: 'no readable content' };
    return { source: 'untrusted-content', title, outline: hs };
  }
  const main = document.querySelector('main, [role="main"], article') || document.body;
  const text = (main.innerText || '').replace(/\n{3,}/g, '\n\n').trim();
  if (!text) return { error: 'no readable content' };
  const totalChunks = Math.max(1, Math.ceil(text.length / chunkChars));
  const i = Math.min(Math.max(0, chunk | 0), totalChunks - 1);
  return { source: 'untrusted-content', title,
           text: text.slice(i * chunkChars, (i + 1) * chunkChars),
           chunk: i, totalChunks };
}

/** Restore a {key: previousValue} map. A null/false previous means the setting
 *  was not in effect, so the adapter is switched off rather than re-applied —
 *  without this, undo could only ever add. */
function revert(previous = {}) {
  const back = {};
  const off = [];
  for (const [k, v] of Object.entries(previous)) {
    if (v === null || v === undefined || v === false) off.push(k); else back[k] = v;
  }
  for (const k of off) {
    if (VISUAL.includes(k)) continue;           // handled by the VisualAssist pass below
    const ad = APPLY[k]?.[0];
    if (ad && typeof ad.disable === 'function') { try { ad.disable(); } catch {} }
  }
  // Any VISUAL key going back to "unset" means re-running VisualAssist without it.
  if (off.some((k) => VISUAL.includes(k))) {
    const keep = {};
    for (const k of VISUAL) if (back[k] !== undefined) keep[k] = back[k];
    try { A.VisualAssist.disable(); } catch {}
    if (Object.keys(keep).length) A.VisualAssist.enable(keep);
  }
  if (Object.keys(back).length) apply(back);
  return { reverted: previous };
}

/** Compose the authoritative merge with the ability baseline underneath.
 *
 *  This mirrors the toolkit's own resolveWebPreferences, which takes an
 *  in-process librarian we do not have over HTTP: the merge is used verbatim and
 *  never altered, derived values only FILL keys it did not set, and each is
 *  coerced to the registry's range. Without this the harness saw only
 *  effectivePreferences, so what a person said at onboarding never reached the
 *  page — only settings they had explicitly changed later did.
 *
 *  `unmet` is the honest part: ability needs the web surface cannot render.
 */
function resolveWeb(prefs = {}, model = null) {
  const { settings: derived, unmet } = deriveWebSettings(model || {});
  const settings = { ...(prefs.settings || {}) };
  const provenance = { ...(prefs.provenance || {}) };
  for (const [k, v] of Object.entries(derived)) {
    if (!(k in settings)) {
      settings[k] = coerceSetting(k, v, settingsMeta);
      provenance[k] = 'derived:ability';
    }
  }
  return { settings, provenance, unmet, satisfied: unmet.length === 0 };
}

globalThis.__BH_A11Y = {
  supportedKeys,
  resolveWeb,
  revert,
  activeSettings,
  targets,
  activate,
  content,
  version: 1,
  adapters: A,
  auditors,
  aria,
  profiles: profiles.profiles,
  defaults: profiles.defaults,
  apply,
  applyProfile,
  disableAll,
  status,
  audit,
};
