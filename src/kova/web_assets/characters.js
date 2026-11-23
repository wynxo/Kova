/* Bot characters: a glossy shaped face for a bot that has no image or icon.

   The shape (round, cloud, triangle, diamond, squircle), the eye direction and
   the sleepy lid come from the bot's id, so a bot keeps its face everywhere.
   Its colour is the bot's avatar colour key, which resolves to a theme token
   through chat-appearance.css (--chat-colour), so themes recolour it.
   shell.css floats and blinks it only while <html data-motion="full">. */
(() => {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  let seq = 0;

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
    round: {body: '<circle cx="32" cy="33" r="25"/>', eyeY: 33, shine: [21, 19], spread: 9},
    cloud: {body: '<circle cx="19.5" cy="38" r="11.5"/><circle cx="32" cy="28.5" r="14.5"/><circle cx="45.5" cy="36" r="12.5"/><rect x="8" y="36" width="48" height="19" rx="9.5"/>', eyeY: 40, shine: [26, 22], spread: 9},
    triangle: {body: rounded([[32, 7], [59, 55], [5, 55]], 12), eyeY: 41, shine: [26, 25], spread: 8.4},
    diamond: {body: rounded([[32, 5], [59, 32], [32, 59], [5, 32]], 11), eyeY: 33, shine: [23, 20], spread: 8.6},
    squircle: {body: '<path d="M32 8c18 0 24 6 24 24s-6 24-24 24S8 50 8 32 14 8 32 8Z"/>', eyeY: 33, shine: [20, 18], spread: 9},
  };
  const NAMES = Object.keys(SHAPES);
  /* The colour comes from the bot's theme token; the light and the lid are
     that colour mixed toward white, so every theme keeps the same gloss. */
  const BASE = 'var(--chat-colour, var(--status-info))';
  const LIGHT = `color-mix(in srgb, ${BASE} 38%, #fff)`;
  const MID = `color-mix(in srgb, ${BASE} 70%, #fff)`;
  const LID = `color-mix(in srgb, ${BASE} 80%, #fff)`;
  const LOOKS = [[0, 0], [1, 1], [-1, 1], [1, 0], [-1, 0], [0, -1], [1, -1]];

  function hash(text) {
    let h = 2166136261;
    for (const char of String(text || '')) h = Math.imul(h ^ char.codePointAt(0), 16777619);
    return h >>> 0;
  }

  /** The look a bot gets from its id: shape, eye direction and lid. */
  function traits(id) {
    const h = hash(id);
    return {shape: NAMES[h % NAMES.length], look: LOOKS[(h >>> 4) % LOOKS.length], lid: ((h >>> 9) & 7) === 0, blush: ((h >>> 12) & 3) === 0};
  }

  /** An <svg> face for the bot; the caller sizes it with CSS. */
  function face(id) {
    const t = traits(id), s = SHAPES[t.shape], u = `kc${++seq}`;
    const [ox, oy] = t.look, y = s.eyeY, xs = [32 - s.spread, 32 + s.spread];
    const eyes = xs.map(x => `<ellipse cx="${x}" cy="${y}" rx="6.6" ry="8" fill="#fff"/>`
      + `<ellipse cx="${x + 1 + ox * 1.2}" cy="${y + 1.5 + oy * 1.2}" rx="4.4" ry="5" fill="#14141c"/>`
      + `<circle cx="${x + 2.6 + ox * 1.2}" cy="${y - 1.1 + oy * 1.2}" r="1.9" fill="#fff"/>`
      + `<circle cx="${x - 0.6 + ox}" cy="${y + 3.9 + oy}" r="0.9" fill="#fff" fill-opacity="0.9"/>`
      + (t.lid ? `<path style="fill:${LID}" d="M${x - 7.2} ${y - 1.2}Q${x} ${y - 11.5} ${x + 7.2} ${y - 1.2}Z"/>` : '')).join('');
    const blush = t.blush ? xs.map((x, i) => `<ellipse cx="${x + (i ? 4.8 : -4.8)}" cy="${y + 7.6}" rx="4.4" ry="2.5" fill="#ff5c93" fill-opacity="0.45" filter="url(#${u}f)"/>`).join('') : '';
    const [hx, hy] = s.shine;
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 64 64');
    svg.setAttribute('class', 'character');
    svg.setAttribute('aria-hidden', 'true');
    svg.innerHTML = `<defs>
<radialGradient id="${u}b" gradientUnits="userSpaceOnUse" cx="23" cy="17" r="52"><stop offset="0" style="stop-color:${LIGHT}"/><stop offset="0.5" style="stop-color:${MID}"/><stop offset="1" style="stop-color:${BASE}"/></radialGradient>
<linearGradient id="${u}s" gradientUnits="userSpaceOnUse" x1="0" y1="28" x2="0" y2="60"><stop offset="0" stop-color="#000" stop-opacity="0"/><stop offset="1" stop-color="#000" stop-opacity="0.32"/></linearGradient>
<filter id="${u}f" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="1.5"/></filter>
<filter id="${u}g" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="2.2"/></filter>
<clipPath id="${u}c">${s.body}</clipPath></defs>
<ellipse cx="32" cy="61" rx="17" ry="2.6" fill="#000" fill-opacity="0.38" filter="url(#${u}g)"/>
<g class="character-body"><g fill="url(#${u}b)">${s.body}</g>
<g clip-path="url(#${u}c)"><rect width="64" height="64" fill="url(#${u}s)"/>
<ellipse cx="${hx}" cy="${hy}" rx="9" ry="5" fill="#fff" fill-opacity="0.8" filter="url(#${u}f)" transform="rotate(-30 ${hx} ${hy})"/></g>
<circle cx="${hx + 7.5}" cy="${hy - 1.5}" r="1.4" fill="#fff" fill-opacity="0.85"/>${blush}
<g class="character-eyes">${eyes}</g></g>`;
    svg.style.setProperty('--character-delay', `${-((hash(id) % 40) / 10)}s`);
    return svg;
  }

  globalThis.KovaCharacters = {face, traits};
})();
