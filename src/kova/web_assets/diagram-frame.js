// Mermaid runs in an opaque, script-only sandbox. Only the finished SVG is
// sent back, where it is displayed as an image, not inserted as a document.
(() => {
  'use strict';
  let queue = Promise.resolve();
  window.addEventListener('message', event => {
    if (event.source !== parent) return;
    const {id, source, palette} = event.data || {};
    if (!Number.isSafeInteger(id) || typeof source !== 'string' || !palette) return;
    queue = queue.then(() => render(id, source, palette, event.origin));
  });

  async function render(id, source, palette, origin) {
    let svg = '';
    try {
      const {darkMode, background, raised, text, border} = palette;
      mermaid.initialize({
        startOnLoad: false, securityLevel: 'strict', suppressErrorRendering: true,
        htmlLabels: false, maxTextSize: 12000, maxEdges: 100, theme: 'base',
        fontFamily: 'system-ui',
        themeVariables: {darkMode, background, mainBkg: raised, primaryColor: raised,
          primaryTextColor: text, primaryBorderColor: border, lineColor: border,
          textColor: text, edgeLabelBackground: background},
      });
      ({svg} = await mermaid.render(`diagram-${id}`, source));
    } catch (_) { /* Invalid/unsupported diagrams keep their original code. */ }
    parent.postMessage({id, svg}, origin);
  }
  parent.postMessage({ready: true}, '*');
})();
