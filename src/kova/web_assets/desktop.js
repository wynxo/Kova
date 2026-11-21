/* Desktop-only chrome setup, before the body paints.

   The Electron shell loads /app with ?titlebar=themed only when
   [desktop] title_bar = "themed". The title bar itself is ordinary markup in
   index.html, hidden until this attribute is set, so the browser UI and kova web
   never grow a second window control strip. */
(() => {
  'use strict';
  const themed = new URLSearchParams(location.search).get('titlebar') === 'themed';
  if (themed && /Electron\//.test(navigator.userAgent)) {
    document.documentElement.dataset.titlebar = 'themed';
  }
})();
