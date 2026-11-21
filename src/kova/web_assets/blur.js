/* Compare frame pacing before and after dropping the actual panel blur.
   A slow result lasts only for this page, not for future sessions. */
(() => {
  'use strict';

  const root = document.documentElement;
  const attribute = 'data-blur';
  const samples = 12;
  let started = false;

  function hasLiveBlur() {
    const main = document.querySelector('main');
    if (!root.hasAttribute('data-background') || !main) return false;
    const style = getComputedStyle(main);
    const filter = style.backdropFilter || style.webkitBackdropFilter;
    return Boolean(filter && filter !== 'none');
  }

  function sampleFrames() {
    return new Promise(resolve => {
      const gaps = [];
      let previous;
      function frame(now) {
        if (document.hidden) {
          resolve(null);
          return;
        }
        if (previous !== undefined) gaps.push(now - previous);
        previous = now;
        if (gaps.length < samples) requestAnimationFrame(frame);
        else {
          gaps.sort((a, b) => a - b);
          resolve((gaps[5] + gaps[6]) / 2);
        }
      }
      requestAnimationFrame(frame);
    });
  }

  async function measure() {
    if (started || document.hidden || !hasLiveBlur()) return;
    started = true;

    const blurred = await sampleFrames();
    // Don't switch surfaces at all when the current cadence is healthy.
    if (blurred === null || blurred < 30 || !hasLiveBlur()) return;

    root.setAttribute(attribute, 'slow');
    const solid = await sampleFrames();
    // A slow frame rate alone isn't enough: keeping the blur off must help.
    if (solid === null || blurred < solid * 1.5) root.removeAttribute(attribute);
  }

  function schedule() {
    requestAnimationFrame(() => { void measure(); });
  }

  document.addEventListener('kova:theme', schedule);
  document.addEventListener('visibilitychange', schedule);
  schedule();
})();
