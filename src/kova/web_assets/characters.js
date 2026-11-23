/* Each bot keeps its own silhouette and expression across the app. */
(() => {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';

  function rounded(points, r) {
    const n = points.length, f = v => v.toFixed(2);
    const at = (a, b, d) => { const dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy); return [a[0] + dx / len * d, a[1] + dy / len * d]; };
    let d = '';
    points.forEach((p, i) => {
      const a = at(p, points[(i - 1 + n) % n], r), b = at(p, points[(i + 1) % n], r);
      d += `${i ? 'L' : 'M'}${f(a[0])} ${f(a[1])}Q${f(p[0])} ${f(p[1])} ${f(b[0])} ${f(b[1])}`;
    });
    return `<path d="${d}Z"/>`;
  }
  const SHAPES = {
    round: {body: '<circle cx="32" cy="32" r="26"/>', eyeY: 30},
    cloud: {body: '<path d="M17 51a13 13 0 0 1-2-26 17 17 0 0 1 32-2 14 14 0 0 1 0 28Z"/>', eyeY: 36},
    triangle: {body: rounded([[32, 4], [62, 56], [2, 56]], 13), eyeY: 39},
    diamond: {body: rounded([[32, 3], [61, 32], [32, 61], [3, 32]], 12), eyeY: 31},
    squircle: {body: '<rect x="7" y="7" width="50" height="50" rx="15"/>', eyeY: 29},
  };
  const NAMES = Object.keys(SHAPES);
  const LOOKS = [[0, 0], [2, 1], [-2, 1], [2, 0], [-2, 0], [0, -1]];
  const BODY = 'var(--chat-colour, var(--status-info))';

  function hash(text) {
    let h = 2166136261;
    for (const char of String(text || '')) h = Math.imul(h ^ char.codePointAt(0), 16777619);
    return h >>> 0;
  }

  /** The look a bot gets from its id: shape, eye direction and lid. */
  function traits(id) {
    const h = hash(id);
    return {shape: NAMES[h % NAMES.length], look: LOOKS[(h >>> 4) % LOOKS.length],
            lid: ((h >>> 9) & 7) === 0};
  }

  function face(id) {
    const {shape, look: [x, y], lid} = traits(id);
    const {body, eyeY} = SHAPES[shape];
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 64 64');
    svg.setAttribute('class', 'character');
    svg.setAttribute('aria-hidden', 'true');
    // Each face blinks on its own beat, so a list of bots never blinks in unison.
    const beat = hash(`${id}:beat`);
    svg.style.setProperty('--blink-delay', `${-(beat % 6000)}ms`);
    svg.style.setProperty('--blink-every', `${5200 + (beat >>> 13) % 2600}ms`);
    const eye = left => `<rect x="${left}" y="${eyeY + y - 5}" width="5" height="${lid ? 5 : 10}" rx="2.5"/>`;
    svg.innerHTML = `
      <g style="fill:${BODY}">${body}</g>
      <g class="character-eyes" style="fill:var(--bg-base)">${eye(22 + x)}${eye(37 + x)}</g>`;
    return svg;
  }

  globalThis.KovaCharacters = {face, traits};
})();
