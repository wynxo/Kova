/* Optional custom CSS (#178), browser half.
 *
 * The stylesheet itself is KOVA_HOME/custom.css, served by the server at
 * /app/custom.css only while the shared `custom_css` preference is on. This
 * module appends the <link> to the end of <head>, after every packaged sheet,
 * so a rule in the user's file wins the cascade and can override a token.
 * Nothing is ever inlined and no other origin is used, so the strict
 * `style-src 'self'` CSP still holds.
 *
 * `?safe=1` (or any non-zero `safe` value) skips the sheet for this page, so a
 * stylesheet that hides the app can be undone from Settings. The command
 * palette offers the same reload.
 */
(() => {
  'use strict';

  const LINK_ID = 'custom-css';
  const URL = '/app/custom.css';

  let enabled = false;
  let loaded = null;

  function request(url, options) {
    if (typeof fetch !== 'function') return null;
    try { return fetch(url, options); } catch (_) { return null; }
  }

  function safeValue() {
    try {
      const value = new URLSearchParams(location.search).get('safe');
      return value !== null && value !== '' && value !== '0';
    } catch (_) {
      return false;
    }
  }

  function drop() {
    const node = document.getElementById && document.getElementById(LINK_ID);
    if (node && typeof node.remove === 'function') node.remove();
    loaded = null;
  }

  function paint() {
    // Safe mode skips loading even when the preference is on: the page has to
    // stay usable so the switch below can turn the sheet off.
    if (!enabled || safeValue()) {
      drop();
      return;
    }
    if (loaded && loaded.parentNode) return;
    const node = document.createElement('link');
    node.setAttribute('id', LINK_ID);
    node.setAttribute('rel', 'stylesheet');
    node.setAttribute('href', URL);
    node.addEventListener?.('error', () => { loaded = null; });
    document.head.appendChild(node);
    loaded = node;
  }

  function adopt(next) {
    enabled = Boolean(next);
    paint();
    announce();
  }

  function announce() {
    try {
      if (typeof document.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
        document.dispatchEvent(new CustomEvent('kova:custom-css', {
          detail: {enabled, safe: safeValue()},
        }));
      }
    } catch (_) {}
  }

  /** Re-read the shared preference; signed-out pages simply keep the current state. */
  function sync() {
    const pending = request('/preferences', {headers: {'Accept': 'application/json'}});
    if (!pending || typeof pending.then !== 'function') return Promise.resolve(enabled);
    return pending
      .then(response => (response && response.ok ? response.json() : null))
      .then(payload => {
        if (!payload) return enabled;
        adopt(payload.custom_css === 'on');
        return enabled;
      })
      .catch(() => enabled);
  }

  /** Turn the sheet on or off, then save the shared preference. */
  function set(next) {
    adopt(Boolean(next));
    const pending = request('/preferences', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({custom_css: enabled ? 'on' : 'off'}),
    });
    if (pending && typeof pending.catch === 'function') pending.catch(() => {});
    return enabled;
  }

  window.KovaCustomCss = {
    enabled: () => enabled,
    loaded: () => Boolean(loaded && loaded.parentNode),
    safe: safeValue,
    set,
    sync,
  };
  sync();
})();
