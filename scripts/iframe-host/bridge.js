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

  window.__BH_BRIDGE = true;
  // Announced, not polled: the host cannot know when a cross-document load has
  // finished running our scripts, and guessing produces a race on every page.
  parent.postMessage({ kind: 'bh-iframe-ready', url: location.href,
                       title: document.title }, '*');
})();
