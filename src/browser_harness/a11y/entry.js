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

const A = adapters;

// Settings whose adapters need an LLM. They are not wired here: the harness
// applies them through a11y_fix(), which has a caller. Listed so that asking
// for one reports "needs-ai" rather than silently doing nothing.
const NEEDS_AI = new Set([
  'autoDescribe', 'autoVideoDescribe', 'autoSimplify', 'autoSummarize',
  'autoWcagFix', 'autoFixLabels', 'autoCaptions', 'fixContrast',
]);

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
  describeOnDemand: on(A.DescribeOnDemand),
  reflowColumn: on(A.ReflowColumn),
  focusLocator: on(A.FocusLocator),
  persistentHover: on(A.PersistentHover),
  readingRuler: on(A.ReadingRuler),
  confirmActions: on(A.ConfirmActions),
  rememberSpot: on(A.ReadingSpot),
  expandAbbreviations: on(A.AbbreviationExpand),
  languageTag: on(A.LanguageTag),
  exploreChart: on(A.ExploreAChart),
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

globalThis.__BH_A11Y = {
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
