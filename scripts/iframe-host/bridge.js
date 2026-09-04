/* Control for a page that is inside an iframe rather than behind CDP.
 *
 * The receiver drives a browser from outside, over the DevTools protocol. That
 * is unavailable here: with a screen reader on a hosted VM, the page under test
 * renders in that machine's browser, which nothing on this side can attach to.
 * So the same jobs are done from inside the page, and the surrounding chat
 * speaks to it by postMessage.
 *
 * Deliberately the same shape as the ControlPort methods, so the chat can treat
 * an iframe and a remote receiver as two implementations of one thing.
 */
(() => {
  if (window.__BH_BRIDGE) return;
  const A = () => globalThis.__BH_A11Y;

  const reply = (id, result, error) => {
    // The parent is the host page on the same origin; "*" is fine here and
    // avoids the frame having to know which port it was served from.
    parent.postMessage({ kind: 'bh-iframe-res', id, result, error }, '*');
  };

  const METHODS = {
    describeCapabilities() {
      const a = A();
      return {
        platform: 'browser-harness-iframe',
        settingKeys: a ? a.supportedKeys() : [],
        actions: ['activate', 'scroll', 'back', 'forward', 'navigate'],
        canReadContent: true,
        targets: a ? a.targets(40) : [],
      };
    },
    getContext() {
      const a = A();
      return {
        focus: document.title || null,
        url: location.href,
        activeSettings: a ? a.activeSettings() : {},
        capabilities: METHODS.describeCapabilities(),
      };
    },
    applySettings(changes) {
      const a = A();
      if (!a) return { error: 'adapters not loaded' };
      const before = a.activeSettings();
      const previous = {};
      for (const k of Object.keys(changes || {})) previous[k] = before[k] ?? null;
      const r = a.apply(changes || {});
      const applied = {};
      for (const row of r.applied || []) for (const k of row.from || []) {
        if (k in changes) applied[k] = changes[k];
      }
      for (const k of r.disabled || []) if (k in changes) applied[k] = changes[k];
      if (!Object.keys(applied).length) {
        return { error: 'nothing applied', rejected: (r.skipped || []).map((s) => s.setting) };
      }
      undoStack.push(previous);
      return { applied, previous, rejected: (r.skipped || []).map((s) => s.setting) };
    },
    applyProfile(settings) {
      const a = A();
      return a ? a.apply(settings || {}) : { error: 'adapters not loaded' };
    },
    undoLast() {
      if (!undoStack.length) return { error: 'nothing to undo' };
      const previous = undoStack.pop();
      A() && A().revert(previous);
      return { reverted: previous, remainingUndos: undoStack.length };
    },
    getContent(mode, chunk) {
      const a = A();
      return a ? a.content(mode || 'outline', chunk || 0) : { error: 'adapters not loaded' };
    },
    performAction(actionId, target) {
      const a = A();
      if (actionId === 'activate') return a ? a.activate(target || '') : { ok: false };
      if (actionId === 'scroll') {
        const where = (target || 'down').toLowerCase();
        const to = { top: 0, bottom: document.body.scrollHeight,
                     up: scrollY - innerHeight * 0.8 }[where] ?? scrollY + innerHeight * 0.8;
        scrollTo({ top: to, behavior: 'instant' });
        return { ok: true, detail: `scrolled ${where}` };
      }
      if (actionId === 'back') { history.back(); return { ok: true, detail: 'went back' }; }
      if (actionId === 'forward') { history.forward(); return { ok: true, detail: 'went forward' }; }
      if (actionId === 'navigate') {
        // Straight to the proxy, or the new page arrives unadapted and unframed.
        parent.postMessage({ kind: 'bh-iframe-navigate', url: target }, '*');
        return { ok: true, detail: `opening ${target}` };
      }
      return { ok: false, detail: `unsupported action: ${actionId}` };
    },
  };

  const undoStack = [];

  addEventListener('message', (ev) => {
    const m = ev.data;
    if (!m || m.kind !== 'bh-iframe-req') return;
    const fn = METHODS[m.method];
    if (!fn) return reply(m.id, undefined, `unknown method: ${m.method}`);
    try {
      reply(m.id, fn(...(m.args || [])));
    } catch (e) {
      reply(m.id, undefined, String(e));
    }
  });

  // Search results are wrapped in a tracking redirector, and a redirector
  // cannot be followed on our side: Bing's computes the destination in script
  // and calls location.replace, which no code in the page can intercept. What
  // it can do is read the destination out of the link before it is followed —
  // it is sitting in the URL's own parameter.
  function unwrapRedirect(href) {
    let u;
    try { u = new URL(href); } catch (e) { return href; }
    for (const key of ['u', 'url', 'redirect', 'target', 'dest', 'q']) {
      const v = u.searchParams.get(key);
      if (!v) continue;
      if (/^https?:\/\//i.test(v)) return v;             // plain, as Google uses
      // Bing prefixes a base64url payload with two characters.
      const payload = /^[a-z0-9]{2}[A-Za-z0-9_-]{8,}$/.test(v) ? v.slice(2) : v;
      try {
        const text = atob(payload.replace(/-/g, '+').replace(/_/g, '/')
                                 .padEnd(payload.length + (4 - payload.length % 4) % 4, '='));
        if (/^https?:\/\//i.test(text)) return text;
        if (/^\//.test(text)) return new URL(text, u.origin).href;  // often a path
      } catch (e) { /* not base64; the link is what it says it is */ }
    }
    return href;
  }

  // Every link navigation goes through the host, whatever the page intended.
  //
  // Sites open results in new tabs — Bing marks 29 of 48 links target="_blank" —
  // and a new tab escapes the frame entirely: no proxy, no adapters, and nothing
  // at all happens on the screen of the person this is being driven for. A
  // plain same-frame link is wrong here too, for the second reason: it moves
  // this viewer only, straight to the site, around the proxy.
  addEventListener('click', (e) => {
    if (e.defaultPrevented) return;
    const a = e.target && e.target.closest && e.target.closest('a[href]');
    if (!a) return;
    const href = a.href || '';
    if (!/^https?:/i.test(href)) return;          // mailto:, tel:, javascript:
    // A fragment on this same page is movement within the document, not a
    // navigation — routing it would reload the page and lose their place.
    const here = location.href.split('#')[0];
    if (href.split('#')[0] === here && href.includes('#')) return;
    e.preventDefault();
    parent.postMessage({ kind: 'bh-iframe-navigate', url: unwrapRedirect(href) }, '*');
  }, true);

  // Some pages skip the anchor and call this directly.
  const openedBy = window.open;
  window.open = function (url) {
    if (url && /^https?:/i.test(String(url))) {
      parent.postMessage({ kind: 'bh-iframe-navigate', url: String(url) }, '*');
      return null;
    }
    return openedBy.apply(window, arguments);
  };

  window.__BH_BRIDGE = true;
  // Announced, not polled: the host cannot know when a cross-document load has
  // finished running our scripts, and guessing produces a race on every page.
  parent.postMessage({ kind: 'bh-iframe-ready', url: location.href,
                       title: document.title }, '*');
})();
